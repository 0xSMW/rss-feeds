import argparse
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup, Comment

from _common import (
    build_feed,
    clean_article_html,
    extract_summary,
    feed_self_url,
    load_cached_entries,
    save_feed,
)

HN_RSS_URL = "https://news.ycombinator.com/rss"
HN_BASE_URL = "https://news.ycombinator.com"
HN_API_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Heuristic pre-clean of arbitrary third-party pages, applied before the shared
# normalizer: id/class noise, boilerplate text nodes, avatar/logo images, and
# short link-dense blocks. The shared cleaner then handles structure, media
# resolution, and the output allowlist.
NOISE_ATTR_RE = re.compile(
    r"(author|byline|avatar|profile|subscribe|newsletter|share|social|comment|"
    r"footer|header|nav|breadcrumb|related|promo|advert|ads|banner)",
    re.I,
)
NOISE_TEXT_RE = re.compile(
    r"(written by|posted by|subscribe|newsletter|share|related|sponsored|"
    r"advertis|promo|sign up|follow|log in|login|comments?)",
    re.I,
)
NOISE_IMG_ALT_RE = re.compile(r"(avatar|profile|author|logo|icon|category)", re.I)
NOISE_IMG_SRC_RE = re.compile(r"(avatar|profile|gravatar|author|logo|icon|category)", re.I)

# Trailing recirculation blocks whose wording is not covered by _common's
# related-section markers (e.g. Substack's "Other articles you might like").
RELATED_TAIL_MARKERS = (
    "other articles you might like",
    "other posts you might like",
    "you might also enjoy",
    "recommended for you",
    "more from this author",
)


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

        date = _parse_pub_date((item.findtext("pubDate") or "").strip())
        comments = (item.findtext("comments") or "").strip() or link

        items.append(
            {
                "title": title,
                "link": link,
                # HN's own <description> is just a "Comments" link, not prose;
                # a real summary is filled in from the linked article later.
                "description": title,
                "date": date,
                "category": "Hacker News",
                "comments": comments,
            }
        )

    logger.info(f"Parsed {len(items)} items from Hacker News RSS")
    return items


def fetch_hn_author(comments_url: str) -> str | None:
    """Look up the HN submitter for a story via the Firebase API (best effort)."""
    try:
        item_id = parse_qs(urlparse(comments_url).query).get("id", [None])[0]
        if not item_id or not item_id.isdigit():
            return None
        resp = requests.get(HN_API_ITEM_URL.format(item_id=item_id), timeout=10)
        resp.raise_for_status()
        data = resp.json() or {}
        author = (data.get("by") or "").strip()
        return author or None
    except Exception as e:
        logger.debug(f"HN author lookup failed for {comments_url}: {e}")
        return None


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


def _is_low_value_block(tag) -> bool:
    """Short, link-dense or boilerplate-flavored blocks are site chrome."""
    text = tag.get_text(" ", strip=True)
    if not text:
        return False
    words = text.split()
    link_text = " ".join(a.get_text(" ", strip=True) for a in tag.find_all("a"))
    link_density = (len(link_text) / len(text)) if text else 0
    if len(words) <= 6 and NOISE_TEXT_RE.search(text):
        return True
    if len(words) <= 12 and link_density > 0.6 and NOISE_TEXT_RE.search(text):
        return True
    return False


def preclean_container(container) -> None:
    """Heuristic pre-pass for arbitrary third-party pages (mutates in place).

    Removes elements whose id/class look like site chrome, boilerplate text
    nodes ("written by …", share/subscribe/login/comments labels), avatar and
    logo images, and short link-dense noise blocks. Structural cleaning and
    the output allowlist are handled afterwards by _common.clean_article_html.
    """
    if container is None:
        return

    # HTML comments (framework hydration markers etc.) would otherwise survive
    # into the output.
    for comment in container.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Elements with noisy id/class names.
    for tag in list(container.find_all(True)):
        if tag.decomposed or tag.parent is None:
            continue
        tag_id = tag.get("id") or ""
        tag_class = " ".join(tag.get("class") or [])
        if NOISE_ATTR_RE.search(tag_id) or NOISE_ATTR_RE.search(tag_class):
            tag.decompose()

    # Boilerplate text nodes.
    for text_node in list(container.find_all(string=True)):
        text = text_node.strip()
        if not text:
            continue
        if len(text) <= 120 and NOISE_TEXT_RE.search(text):
            lowered = text.lower()
            if lowered.startswith(("written by", "posted by", "author:", "byline:")):
                text_node.extract()
            elif re.fullmatch(r".*\bcomments?\b.*", text, flags=re.I) and len(text.split()) <= 6:
                text_node.extract()
            elif re.fullmatch(r".*\b(share|subscribe|newsletter|follow|log in|login)\b.*", text, flags=re.I):
                text_node.extract()

    # Avatar/logo/icon images.
    for img in list(container.find_all("img")):
        if img.decomposed or img.parent is None:
            continue
        alt_text = img.get("alt") or ""
        img_class = " ".join(img.get("class") or [])
        img_src = img.get("src") or ""
        if NOISE_IMG_ALT_RE.search(alt_text) or NOISE_IMG_SRC_RE.search(img_class + " " + img_src):
            img.decompose()

    # Short link-dense noise blocks (never ones carrying real media/structure).
    for tag in list(container.find_all(["p", "div", "li", "span", "section"])):
        if tag.decomposed or tag.parent is None:
            continue
        if tag.find(["img", "picture", "iframe", "pre", "table", "figure"]):
            continue
        if _is_low_value_block(tag):
            tag.decompose()

    # Recirculation tails: a marker paragraph/heading and everything after it.
    for el in list(container.find_all(["p", "h2", "h3", "h4", "strong", "em", "div"])):
        if el.decomposed or el.parent is None:
            continue
        text = el.get_text(" ", strip=True).lower().rstrip(":")
        if text in RELATED_TAIL_MARKERS:
            target = el if el.name != "em" or el.parent.name not in ("p", "div") else el.parent
            for sibling in list(target.next_siblings):
                if getattr(sibling, "decompose", None):
                    sibling.decompose()
                else:
                    sibling.extract()
            target.decompose()

    # Empty headings (JS-populated placeholders).
    for heading in list(container.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])):
        if heading.decomposed or heading.parent is None:
            continue
        if not heading.get_text(strip=True) and not heading.find("img"):
            heading.decompose()


def extract_article_content(html: str, page_url: str, title: str | None = None) -> tuple[str, str]:
    """Extract normalized article content HTML and a plain-text summary."""
    soup = BeautifulSoup(html, "html.parser")
    container = (
        soup.find("article")
        or soup.find("main")
        or soup.find("div", class_=re.compile(r"(content|article|post|story|entry|main)", re.I))
        or soup.body
    )
    preclean_container(container)
    content_html = clean_article_html(container, page_url, title=title)
    summary = extract_summary(soup, container, title=title)
    return content_html, summary


def _comments_link_html(comments_url: str) -> str:
    href = comments_url.replace("&", "&amp;").replace('"', "&quot;")
    return f'<p><a href="{href}">Comments on Hacker News</a></p>'


def populate_article_content(article: dict) -> None:
    """Fill content_html/description/author for a story (mutates in place)."""
    comments_url = article.get("comments") or article["link"]
    author = fetch_hn_author(comments_url)
    if author:
        article["author"] = author

    content_html = ""
    summary = ""
    host = urlparse(article["link"]).netloc.lower()
    if host != "news.ycombinator.com":
        page_html = fetch_article_page(article["link"])
        if page_html:
            content_html, summary = extract_article_content(
                page_html, article["link"], title=article["title"]
            )

    comments_html = _comments_link_html(comments_url)
    article["content_html"] = (content_html + "\n" + comments_html) if content_html else comments_html
    if summary and summary.strip():
        article["description"] = summary.strip()


def generate_rss_feed(articles: list[dict], feed_name: str = "hackernews"):
    """Generate RSS feed for Hacker News with full content."""
    fg = build_feed(
        title="Hacker News",
        description="Hacker News front-page links with full article content.",
        site_url=HN_BASE_URL,
        feed_url=feed_self_url(f"feed_{feed_name}.xml"),
        items=articles,
    )
    logger.info("Hacker News RSS feed generated successfully")
    return fg


def main(feed_name: str = "hackernews", force: bool = False) -> bool:
    """Main function to generate Hacker News RSS feed."""
    try:
        feed_path = ensure_feeds_directory() / f"feed_{feed_name}.xml"

        if force:
            logger.info("Force mode enabled: rebuilding feed from scratch")
            existing_items, cache = [], {}
        else:
            existing_items, cache = load_cached_entries(feed_path)
        existing_links = {item["link"] for item in existing_items}

        rss_xml = fetch_rss_content()
        articles = parse_rss_items(rss_xml)
        if not articles:
            logger.warning("No items parsed from Hacker News RSS")
            return False

        new_articles = [a for a in articles if a["link"] not in existing_links]
        skipped_count = len(articles) - len(new_articles)
        if skipped_count:
            logger.info(f"Skipping {skipped_count} existing links already in feed.")

        for article in new_articles:
            cached = cache.get(article["link"])
            if cached and cached.get("content_html"):
                article["content_html"] = cached["content_html"]
                if cached.get("description"):
                    article["description"] = cached["description"]
                continue
            logger.info(f"Fetching article content: {article['link']}")
            populate_article_content(article)

        combined = new_articles + existing_items
        seen_links = set()
        deduped_articles = []
        for article in combined:
            link = article.get("link")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            deduped_articles.append(article)

        feed = generate_rss_feed(deduped_articles, feed_name)
        save_feed(feed, feed_path)
        logger.info(f"Successfully generated Hacker News feed ({len(deduped_articles)} candidate items)")
        return True
    except Exception as e:
        logger.exception(f"Failed to generate Hacker News feed: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Hacker News RSS feed.")
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    args = parser.parse_args()
    main(force=args.force)
