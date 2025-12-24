import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

HN_RSS_URL = "https://news.ycombinator.com/rss"
HN_BASE_URL = "https://news.ycombinator.com/"

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def _parse_pub_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def setup_selenium_driver():
    """Set up Selenium WebDriver with undetected-chromedriver."""
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    return uc.Chrome(options=options)


def fetch_rss_content(url: str = HN_RSS_URL) -> str:
    """Fetch Hacker News RSS content."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.text


def parse_rss_items(xml_content: str) -> list[dict]:
    """Parse Hacker News RSS XML and return items."""
    root = ET.fromstring(xml_content)
    channel = root.find("channel")
    if channel is None:
        return []

    items = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue

        description = (item.findtext("description") or "").strip() or title
        date = _parse_pub_date((item.findtext("pubDate") or "").strip())
        guid = (item.findtext("guid") or "").strip() or link

        items.append(
            {
                "title": title,
                "link": link,
                "description": description,
                "date": date,
                "category": "Hacker News",
                "guid": guid,
            }
        )

    logger.info(f"Parsed {len(items)} items from Hacker News RSS")
    return items


def fetch_article_page(url: str) -> str | None:
    """Fetch HTML for a single article page."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        content_type = (resp.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            logger.info(f"Skipping non-HTML content for {url} ({content_type})")
            return None
        return resp.text
    except Exception as e:
        logger.warning(f"Requests fetch failed for {url} ({e}); falling back to Selenium")
        try:
            driver = setup_selenium_driver()
            driver.get(url)
            html = driver.page_source
            driver.quit()
            return html
        except Exception as e2:
            logger.warning(f"Selenium fetch failed for {url}: {e2}")
            return None


def _clean_article_html(container, base_url: str) -> str:
    """Clean article container and keep feed-friendly tags."""
    if container is None:
        return ""

    for tag in container.select("script, style, noscript, svg, form, iframe, nav, header, footer, aside"):
        tag.decompose()

    noise_pattern = re.compile(
        r"(author|byline|avatar|profile|subscribe|newsletter|share|social|comment|footer|header|nav|breadcrumb|related|promo|advert|ads|banner)",
        re.I,
    )
    noise_text_pattern = re.compile(
        r"(written by|posted by|subscribe|newsletter|share|related|sponsored|advertis|promo|sign up|follow|log in|login|comments?)",
        re.I,
    )

    def is_low_value_block(tag) -> bool:
        text = tag.get_text(" ", strip=True)
        if not text:
            return True
        words = text.split()
        link_text = " ".join(a.get_text(" ", strip=True) for a in tag.find_all("a"))
        link_density = (len(link_text) / len(text)) if text else 0
        if len(words) <= 6 and noise_text_pattern.search(text):
            return True
        if len(words) <= 12 and link_density > 0.6 and noise_text_pattern.search(text):
            return True
        return False
    for tag in list(container.find_all(True)):
        if tag is None or tag.attrs is None:
            continue
        tag_id = tag.get("id") or ""
        tag_class = " ".join(tag.get("class") or [])
        if noise_pattern.search(tag_id) or noise_pattern.search(tag_class):
            tag.decompose()

    for text_node in list(container.find_all(string=True)):
        text = text_node.strip()
        if not text:
            continue
        if len(text) <= 120 and noise_text_pattern.search(text):
            lowered = text.lower()
            if lowered.startswith(("written by", "posted by", "author:", "byline:")):
                text_node.extract()
            elif re.fullmatch(r".*\\bcomments?\\b.*", text, flags=re.I) and len(text.split()) <= 6:
                text_node.extract()
            elif re.fullmatch(r".*\\b(share|subscribe|newsletter|follow|log in|login)\\b.*", text, flags=re.I):
                text_node.extract()

    for img in list(container.find_all("img")):
        if img is None or img.attrs is None:
            continue
        alt_text = img.get("alt") or ""
        img_class = " ".join(img.get("class") or [])
        img_src = img.get("src") or ""
        if re.search(r"(avatar|profile|author|logo|icon|category)", alt_text, re.I) or re.search(
            r"(avatar|profile|gravatar|author|logo|icon|category)", img_class + " " + img_src, re.I
        ):
            img.decompose()

    allowed = {"p", "a", "img"}
    for tag in list(container.find_all(True)):
        if tag is None or tag.attrs is None:
            continue
        if is_low_value_block(tag):
            tag.decompose()
            continue
        if tag.name not in allowed:
            tag.unwrap()
            continue
        attrs = {}
        if tag.name == "a" and tag.get("href"):
            attrs["href"] = tag["href"]
        elif tag.name == "img" and tag.get("src"):
            attrs["src"] = tag["src"]
            if tag.get("alt"):
                attrs["alt"] = tag["alt"]
        tag.attrs = attrs

    for a in container.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(("http://", "https://", "mailto:", "#")):
            a["href"] = urljoin(base_url, href)

    for img in container.find_all("img", src=True):
        src = img["src"]
        if not src.startswith(("http://", "https://", "data:")):
            img["src"] = urljoin(base_url, src)

    return str(container)


def extract_article_content(html: str, page_url: str) -> tuple[str, str]:
    """Extract main article content HTML and a plain-text summary."""
    soup = BeautifulSoup(html, "html.parser")
    container = (
        soup.find("article")
        or soup.find("main")
        or soup.find("div", class_=re.compile(r"(content|article|post|story|entry|main)", re.I))
        or soup.body
    )
    content_html = _clean_article_html(container, base_url=page_url)

    summary = ""
    if container:
        first_p = container.find("p")
        if first_p:
            summary = first_p.get_text(" ", strip=True)
    if not summary:
        summary = soup.title.get_text(strip=True) if soup.title else ""

    return content_html, summary


def generate_rss_feed(articles: list[dict], feed_name: str = "hackernews") -> FeedGenerator:
    """Generate RSS feed for Hacker News with full content."""
    fg = FeedGenerator()
    fg.title("Hacker News Full Content Feed")
    fg.description("Hacker News front-page links with full article content.")
    fg.link(href=HN_BASE_URL)
    fg.language("en")

    for article in reversed(articles):
        fe = fg.add_entry()
        fe.title(article["title"])
        fe.link(href=article["link"])
        fe.description(article.get("description") or article["title"])

        if article.get("content_html"):
            fe.content(article["content_html"])

        if article.get("date"):
            fe.published(article["date"])

        fe.category(term=article.get("category") or "Hacker News")
        fe.id(article.get("guid") or article["link"])

    logger.info("Hacker News RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator: FeedGenerator, feed_name: str = "hackernews") -> Path:
    """Save RSS feed to file."""
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"Hacker News RSS feed saved to {output_file}")
    return output_file


def get_existing_entries_from_feed(feed_path: Path) -> list[dict]:
    """Parse the existing RSS feed and return entries for reuse."""
    entries = []
    if not feed_path.exists():
        return entries

    try:
        tree = ET.parse(feed_path)
        root = tree.getroot()
        ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
        for item in root.findall("./channel/item"):
            link_elem = item.find("link")
            title_elem = item.find("title")
            desc_elem = item.find("description")
            content_elem = item.find("content:encoded", ns)
            date_elem = item.find("pubDate")
            category_elem = item.find("category")
            guid_elem = item.find("guid")

            link = link_elem.text.strip() if link_elem is not None and link_elem.text else None
            if not link:
                continue

            date = None
            if date_elem is not None and date_elem.text:
                date = _parse_pub_date(date_elem.text.strip())

            entries.append(
                {
                    "title": title_elem.text.strip() if title_elem is not None and title_elem.text else link,
                    "link": link,
                    "date": date,
                    "category": category_elem.text.strip() if category_elem is not None and category_elem.text else "Hacker News",
                    "description": desc_elem.text if desc_elem is not None and desc_elem.text else "",
                    "content_html": content_elem.text if content_elem is not None and content_elem.text else "",
                    "guid": guid_elem.text.strip() if guid_elem is not None and guid_elem.text else link,
                }
            )
    except Exception as e:
        logger.warning(f"Failed to parse existing feed entries: {str(e)}")

    return entries


def main(feed_name: str = "hackernews", force: bool = False) -> bool:
    """Main function to generate Hacker News RSS feed."""
    try:
        feed_path = ensure_feeds_directory() / f"feed_{feed_name}.xml"
        existing_entries = get_existing_entries_from_feed(feed_path)
        existing_links = {entry["link"] for entry in existing_entries}

        rss_xml = fetch_rss_content()
        articles = parse_rss_items(rss_xml)
        if not articles:
            logger.warning("No items parsed from Hacker News RSS")
            return False

        if force:
            new_articles = articles
        else:
            new_articles = [article for article in articles if article["link"] not in existing_links]
        skipped_count = len(articles) - len(new_articles)
        if skipped_count:
            logger.info(f"Skipping {skipped_count} existing links already in feed.")

        for article in new_articles:
            article_html = fetch_article_page(article["link"])
            if not article_html:
                continue
            content_html, summary = extract_article_content(article_html, article["link"])
            if content_html:
                article["content_html"] = content_html
            if summary:
                article["description"] = summary

        combined_articles = new_articles + existing_entries
        seen_links = set()
        deduped_articles = []
        for article in combined_articles:
            link = article.get("link")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            deduped_articles.append(article)

        min_dt = datetime.min.replace(tzinfo=timezone.utc)
        deduped_articles.sort(key=lambda item: item.get("date") or min_dt, reverse=True)

        feed = generate_rss_feed(deduped_articles, feed_name)
        save_rss_feed(feed, feed_name)
        logger.info(f"Successfully generated Hacker News feed with {len(deduped_articles)} items")
        return True
    except Exception as e:
        logger.exception(f"Failed to generate Hacker News feed: {e}")
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate Hacker News RSS feed.")
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    args = parser.parse_args()
    main(force=args.force)
