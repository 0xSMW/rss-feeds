import argparse
import requests
import time
import undetected_chromedriver as uc
import re
from bs4 import BeautifulSoup
from datetime import datetime
from email.utils import parsedate_to_datetime
import pytz
from feedgen.feed import FeedGenerator
import logging
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


BASE_URL = "https://mistral.ai"
NEWS_URL = f"{BASE_URL}/news"


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def fetch_news_content_requests(url: str = NEWS_URL) -> str:
    """Fetch HTML for Mistral AI news page using requests (no JS)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }
    logger.info(f"Fetching Mistral AI news page (requests): {url}")
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def setup_selenium_driver():
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    return uc.Chrome(options=options)


def fetch_news_content_selenium(url: str = NEWS_URL) -> str:
    """Fetch fully rendered HTML for Mistral AI news page using Selenium."""
    driver = None
    try:
        driver = setup_selenium_driver()
        logger.info(f"Fetching Mistral AI news page (selenium): {url}")
        driver.get(url)
        time.sleep(5)

        # Scroll to load more items, if lazy-loaded
        last_height = driver.execute_script("return document.body.scrollHeight")
        for _ in range(10):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        html = driver.page_source
        return html
    finally:
        if driver:
            driver.quit()


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
    """Clean article container and keep only <p>, <a>, and <img> tags."""
    if container is None:
        return ""

    related_markers = (
        "related articles",
        "related posts",
        "more articles",
        "you might also like",
    )
    share_domains = (
        "linkedin.com/sharing",
        "facebook.com/sharer",
        "twitter.com/intent",
        "x.com/intent",
        "reddit.com/submit",
    )

    for el in container.find_all(["h2", "h3", "h4", "p", "div", "section", "aside"]):
        text = el.get_text(" ", strip=True).lower()
        if any(marker in text for marker in related_markers):
            parent = el.find_parent(["section", "aside", "div"]) or el
            parent.decompose()

    for tag in list(container.find_all(True)):
        if tag.parent is None:
            continue
        if tag.name == "a":
            href = tag.get("href", "")
            href_l = href.lower()
            if any(domain in href_l for domain in share_domains):
                tag.decompose()
                continue
            if not href:
                tag.decompose()
                continue
            if not href.startswith(("http://", "https://", "mailto:", "#")):
                tag["href"] = urljoin(base_url, href)
            if not tag.get_text(strip=True) and not tag.find("img"):
                tag.decompose()
                continue
            tag.attrs = {"href": tag["href"]}
            continue
        if tag.name == "img":
            if "src" in tag.attrs:
                src = tag["src"]
                if not src.startswith(("http://", "https://", "data:")):
                    tag["src"] = urljoin(base_url, src)
            else:
                tag.decompose()
                continue
            tag.attrs = {"src": tag["src"]}
            continue

        if tag.name == "p":
            if tag.find("img"):
                for img in tag.find_all("img"):
                    tag.insert_after(img)
            tag.attrs = {}
            continue

        tag.unwrap()

    parts: list[str] = []
    for tag in container.find_all(["p", "a", "img"], recursive=True):
        if tag.name == "p" and not tag.get_text(strip=True):
            continue
        parts.append(str(tag))

    return "\n".join(parts)


def extract_article_content(html: str, page_url: str) -> tuple[str, str]:
    """Extract main article content HTML and a plain-text summary."""
    soup = BeautifulSoup(html, "html.parser")
    container = (
        soup.select_one("article")
        or soup.select_one("main article")
        or soup.select_one("main")
        or soup.select_one("[class*='content']")
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


def _parse_date(text: str) -> datetime:
    """Best-effort parse of a date string; default to now UTC on failure."""
    if not text:
        return datetime.now(pytz.UTC)

    text = text.strip()
    # Try ISO first
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.UTC)
        return dt
    except Exception:
        pass

    # Common blog formats
    fmts = [
        "%B %d, %Y",  # January 02, 2025
        "%b %d, %Y",  # Jan 02, 2025
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=pytz.UTC)
        except Exception:
            continue
    logger.warning(f"Unrecognized date format: {text!r}; defaulting to now")
    return datetime.now(pytz.UTC)


def _find_date_text(root) -> str | None:
    """Search within an element subtree for a human-readable date like 'July 3, 2025'."""
    # Regex for month name and day, year (supports short and long month names)
    month_pattern = (
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
        r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    )
    date_re = re.compile(rf"\b{month_pattern}\s+\d{{1,2}},\s+\d{{4}}\b", re.IGNORECASE)

    # Prioritize likely containers, then fall back to any text node in common tags
    for sel in ["time", "div.text-sm span", "div.text-sm time", "span", "p", "div"]:
        for el in root.select(sel):
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            m = date_re.search(text)
            if m:
                return m.group(0)
    return None


def parse_news_html(html_content: str, default_category: str | None = None):
    """Parse the Mistral AI news listing to extract articles.

    Strategy: collect anchors that link to individual news posts (contain '/news/').
    Extract title from heading tags within the anchor; fall back to aria-label or text.
    Extract date from <time> elements (datetime attr or text) within the anchor or parent.
    """
    soup = BeautifulSoup(html_content, "html.parser")

    articles = []
    seen = set()

    anchors = soup.select("a[href]")
    for a in anchors:
        href = a.get("href", "").strip()
        if not href:
            continue

        # Normalize absolute link
        link = urljoin(BASE_URL, href)

        # Select only individual posts under /news/... (not the listing /news itself)
        # Accept both absolute and relative matches due to urljoin above
        if "/news/" not in link:
            continue
        if link.rstrip("/") == NEWS_URL.rstrip("/"):
            continue
        if link in seen:
            continue

        # Title: look for h2/h3 within the anchor first
        title_elem = a.find(["h2", "h3"]) or a.select_one("[class*='title']")
        title = (title_elem.get_text(strip=True) if title_elem else None)
        if not title:
            # Fallbacks: aria-label, then anchor text
            title = a.get("aria-label") or a.get_text(" ", strip=True)
        title = (title or "").strip()
        if not title:
            # Skip if we still don't have a reasonable title
            continue

        # Date: prefer <time> inside anchor, then other elements with date text, then parent containers
        date_dt = None
        time_el = a.find("time")
        if not time_el and a.parent:
            # Sometimes the <time> is a sibling of the anchor
            time_el = a.parent.find("time")
        if time_el:
            dt_attr = (time_el.get("datetime") or "").strip()
            text_val = time_el.get_text(strip=True)
            date_dt = _parse_date(dt_attr or text_val)
        if date_dt is None:
            # Look for a readable date string like 'July 3, 2025' inside the anchor
            date_text = _find_date_text(a)
            if not date_text and a.parent:
                date_text = _find_date_text(a.parent)
            if date_text:
                date_dt = _parse_date(date_text)
        if not date_dt:
            date_dt = datetime.now(pytz.UTC)

        # Category: default to provided category, else try to read a nearby badge, else 'News'
        category = default_category or "News"
        badge = a.select_one(".badge, .tag, .label, [class*='category']")
        if badge and badge.get_text(strip=True):
            category = badge.get_text(strip=True)

        description = title

        articles.append({
            "title": title,
            "link": link,
            "date": date_dt,
            "category": category,
            "description": description,
        })
        seen.add(link)

    logger.info(f"Parsed {len(articles)} Mistral news articles")
    return articles


def collect_articles_from_categories(categories: list[str]) -> list[dict]:
    """Fetch and parse multiple category listing pages, deduplicate by link."""
    urls = [f"{NEWS_URL}?category={c}" for c in categories]

    html_pages: dict[str, str] = {}
    # Try selenium once to reuse the same driver across pages
    try:
        driver = setup_selenium_driver()
        logger.info("Fetching category pages with selenium (single session)")
        for url in urls:
            driver.get(url)
            time.sleep(4)
            # Scroll a bit in case of lazy loading
            last_height = driver.execute_script("return document.body.scrollHeight")
            for _ in range(5):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.5)
                new_height = driver.execute_script("return document.body.scrollHeight")
                if new_height == last_height:
                    break
                last_height = new_height
            html_pages[url] = driver.page_source
    except Exception as e:
        logger.warning(f"Selenium multi-fetch failed ({e}); falling back to requests per page")
        html_pages = {}
        for url in urls:
            html_pages[url] = fetch_news_content_requests(url)
    finally:
        try:
            driver.quit()  # type: ignore[name-defined]
        except Exception:
            pass

    # Parse and dedupe
    by_link: dict[str, dict] = {}
    for url, html in html_pages.items():
        # Extract category key from query param
        if "?category=" in url:
            default_cat = url.split("?category=", 1)[1].split("&", 1)[0].strip()
            default_cat = default_cat.capitalize() if default_cat else None
        else:
            default_cat = None

        articles = parse_news_html(html, default_category=default_cat)
        for a in articles:
            link = a.get("link")
            if not link:
                continue
            if link not in by_link:
                by_link[link] = a
            else:
                # Optionally upgrade category if existing is 'News' and we have a specific one
                if by_link[link].get("category") in (None, "News") and a.get("category") not in (None, "News"):
                    by_link[link]["category"] = a.get("category")

    combined = list(by_link.values())
    logger.info(f"Combined {len(combined)} unique articles from categories: {', '.join(categories)}")
    return combined


def generate_rss_feed(articles, feed_name: str = "mistral_news"):
    """Generate RSS feed from parsed articles."""
    fg = FeedGenerator()
    fg.title("Mistral AI News")
    fg.description("Latest news and updates from Mistral AI")
    fg.link(href=NEWS_URL)
    fg.language("en")

    # Optional metadata
    fg.author({"name": "Mistral AI"})
    fg.link(href=NEWS_URL, rel="alternate")

    for article in articles:
        fe = fg.add_entry()
        fe.title(article["title"])
        fe.link(href=article["link"])
        fe.description(article["description"])
        if article.get("content_html"):
            fe.content(article["content_html"])
        fe.published(article["date"])
        fe.category(term=article["category"])
        fe.id(article["link"])

    logger.info("RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator, feed_name: str = "mistral_news") -> Path:
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"RSS feed saved to {output_file}")
    return output_file


def get_existing_entries_from_feed(feed_path: Path):
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

            link = link_elem.text.strip() if link_elem is not None and link_elem.text else None
            if not link:
                continue

            date = None
            if date_elem is not None and date_elem.text:
                try:
                    date = parsedate_to_datetime(date_elem.text.strip())
                except Exception:
                    date = None

            entries.append(
                {
                    "title": title_elem.text.strip() if title_elem is not None and title_elem.text else link,
                    "link": link,
                    "date": date or datetime.now(pytz.UTC),
                    "category": category_elem.text.strip() if category_elem is not None and category_elem.text else "News",
                    "description": desc_elem.text if desc_elem is not None and desc_elem.text else "",
                    "content_html": content_elem.text if content_elem is not None and content_elem.text else "",
                }
            )
    except Exception as e:
        logger.warning(f"Failed to parse existing feed entries: {str(e)}")
    return entries


def main(feed_name: str = "mistral_news", force: bool = False) -> bool:
    try:
        feeds_dir = ensure_feeds_directory()
        feed_path = feeds_dir / f"feed_{feed_name}.xml"

        existing_entries = []
        existing_links = set()
        if not force:
            existing_entries = get_existing_entries_from_feed(feed_path)
            existing_links = {entry["link"] for entry in existing_entries}

        # Pull from specific category routes and combine
        categories = ["product", "solutions", "research", "company"]
        articles = collect_articles_from_categories(categories)
        if not articles:
            logger.warning("No Mistral news articles parsed. Selectors may need updating.")
        new_articles = [article for article in articles if article["link"] not in existing_links]
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
            if article["link"] in seen_links:
                continue
            seen_links.add(article["link"])
            deduped_articles.append(article)

        feed = generate_rss_feed(deduped_articles, feed_name)
        save_rss_feed(feed, feed_name)
        return True
    except Exception as e:
        logger.error(f"Failed to generate Mistral AI news RSS: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Mistral AI News RSS feed.")
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    args = parser.parse_args()
    main(force=args.force)
