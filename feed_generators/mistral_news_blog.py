import argparse
import requests
import time
import undetected_chromedriver as uc
import re
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
import logging
from pathlib import Path
from urllib.parse import urljoin

from _common import (
    build_feed,
    clean_article_html,
    extract_summary,
    feed_self_url,
    load_cached_entries,
    save_feed,
)

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


BASE_URL = "https://mistral.ai"
NEWS_URL = f"{BASE_URL}/news"

# Site-specific chrome inside <article> on mistral.ai:
# - <mistral-atom-navigation-scroll-progress> renders the sticky "0%" reading
#   progress indicator; unwrapping it would leak "0%" paragraphs.
# - <mistral-atom-button-copy-clipboard>/<mistral-atom-button-tooltip> hold the
#   share button and its "Copy url to clipboard"/"Copied" tooltip text.
# - .text-body-small covers the byline ("By Mistral AI Team") and the header
#   date (also caught by the shared date filter); article prose uses
#   .text-body-large.
# - <mistral-atom-text-vibe> is the animated "Thinking"/"Summary" AI-summary
#   widget; its summary text still becomes the item description via
#   extract_summary (which reads the raw container).
# - .text-display is the hero title rendered as a <p> on newer landing-style
#   posts (e.g. robostral-navigate), which the shared heading dedupe can't see.
STRIP_SELECTORS = (
    "mistral-atom-navigation-scroll-progress",
    "mistral-atom-button-copy-clipboard",
    "mistral-atom-button-tooltip",
    "mistral-atom-text-vibe",
    ".text-body-small",
    ".text-display",
)

EXTRA_CTA_PATTERNS = (
    r"copy url to clipboard",
    r"copied",
    r"le chat",
    r"ai studio",
)


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
        driver = None
        try:
            driver = setup_selenium_driver()
            driver.get(url)
            time.sleep(3)
            return driver.page_source
        except Exception as e2:
            logger.warning(f"Selenium fetch failed for {url}: {e2}")
            return None
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass


def _dedupe_adjacent_images(content_html: str) -> str:
    """Collapse consecutive <img> tags with the same src into one.

    mistral.ai hero blocks render the same image twice (loading placeholder +
    loaded variant), which survives cleaning as two identical adjacent images.
    """
    if not content_html:
        return content_html
    pattern = re.compile(
        r'(<img[^>]*\bsrc="([^"]+)"[^>]*/?>)\s*<img[^>]*\bsrc="\2"[^>]*/?>'
    )
    prev = None
    while prev != content_html:
        prev = content_html
        content_html = pattern.sub(r"\1", content_html)
    return content_html


_PAGE_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)


def _article_page_meta(container) -> tuple[datetime | None, str | None]:
    """Pull published date and category from the article hero.

    mistral.ai article pages have no <time>/meta/ld+json dates; the hero holds
    a 'March 6, 2025' paragraph (.text-body-small) and a category eyebrow
    (.text-eyebrow-small, alongside an 'N min read' eyebrow we skip).
    """
    date = category = None
    if container is None:
        return None, None
    for p in container.select("p.text-body-small"):
        m = _PAGE_DATE_RE.search(p.get_text(" ", strip=True))
        if m:
            date = _parse_date(m.group(0))
            break
    for el in container.select(".text-eyebrow-small"):
        text = el.get_text(" ", strip=True)
        if text and not re.match(r"^\d+\s*min(?:ute)?s?(?:\s+read)?$", text, re.IGNORECASE):
            category = text
            break
    return date, category


def extract_article_content(
    html: str, page_url: str, title: str | None = None
) -> tuple[str, str, datetime | None, str | None]:
    """Extract cleaned article HTML, a plain-text summary, and page date/category."""
    soup = BeautifulSoup(html, "html.parser")
    container = (
        soup.select_one("main article")
        or soup.select_one("article")
        or soup.select_one("main")
        or soup.select_one("[class*='content']")
    )
    page_date, page_category = _article_page_meta(container)
    content_html = clean_article_html(
        container,
        page_url,
        title=title,
        strip_selectors=STRIP_SELECTORS,
        extra_cta_patterns=EXTRA_CTA_PATTERNS,
    )
    content_html = _dedupe_adjacent_images(content_html)

    # mistral.ai serves the same site-wide boilerplate og:description /
    # meta description on every article page; drop those metas so
    # extract_summary falls back to the first real paragraph instead.
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key.endswith("description"):
            meta.decompose()
    summary = extract_summary(soup, container, title=title)
    return content_html, summary, page_date, page_category


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


def _canonical_link(link: str) -> str:
    """Normalize article URLs so with/without trailing slash dedupe together."""
    link = link.split("#", 1)[0]
    if link.endswith("/"):
        link = link.rstrip("/")
    return link


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
        link = _canonical_link(urljoin(BASE_URL, href))

        # Select only individual posts under /news/... (not the listing /news itself)
        # Accept both absolute and relative matches due to urljoin above
        if "/news/" not in link + "/":
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
        # Leave date as None when the listing card has none; the article page
        # hero date fills it in later (fallback: now).

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
    driver = None
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
        if driver:
            try:
                driver.quit()
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
    fg = build_feed(
        title="Mistral AI News",
        description="Latest news and updates from Mistral AI",
        site_url=NEWS_URL,
        feed_url=feed_self_url(f"feed_{feed_name}.xml"),
        items=articles,
        author={"name": "Mistral AI"},
    )
    logger.info("RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator, feed_name: str = "mistral_news") -> Path:
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    save_feed(feed_generator, output_file)
    return output_file


def main(feed_name: str = "mistral_news", force: bool = False) -> bool:
    try:
        feeds_dir = ensure_feeds_directory()
        feed_path = feeds_dir / f"feed_{feed_name}.xml"

        existing_entries = []
        cache = {}
        existing_links = set()
        if force:
            logger.info("Force mode enabled: rebuilding feed from scratch")
        else:
            existing_entries, cache = load_cached_entries(feed_path)
            existing_links = {entry["link"] for entry in existing_entries}

        # Pull from specific category routes and combine
        categories = ["product", "solutions", "research", "company"]
        articles = collect_articles_from_categories(categories)
        if not articles:
            logger.warning("No Mistral news articles parsed. Selectors may need updating.")
        new_articles = [article for article in articles if article["link"] not in existing_links]
        logger.info(f"{len(new_articles)} articles not in existing feed (of {len(articles)} parsed)")

        combined_articles = new_articles + existing_entries
        seen_links = set()
        deduped_articles = []
        for article in combined_articles:
            link = article["link"]
            if link in seen_links:
                continue
            seen_links.add(link)
            deduped_articles.append(article)

        # Only the newest DEFAULT_MAX_ITEMS make the feed (build_feed caps at
        # 50), so sort first and skip fetching content for items beyond that.
        # Undated items are new listing cards without a visible date; treat
        # them as newest so they get fetched (their page supplies the date).
        future = datetime.max.replace(tzinfo=pytz.UTC)
        deduped_articles.sort(key=lambda a: a.get("date") or future, reverse=True)
        deduped_articles = deduped_articles[:50]

        failed = 0
        for article in deduped_articles:
            if article.get("content_html"):
                continue
            cached = cache.get(article["link"])
            if cached and cached.get("content_html"):
                article["content_html"] = cached["content_html"]
                if cached.get("description"):
                    article["description"] = cached["description"]
                continue
            logger.info(f"Fetching article: {article['link']}")
            article_html = fetch_article_page(article["link"])
            if not article_html:
                failed += 1
                continue
            content_html, summary, page_date, page_category = extract_article_content(
                article_html, article["link"], title=article.get("title")
            )
            if content_html:
                article["content_html"] = content_html
            if summary:
                article["description"] = summary
            if page_date:
                article["date"] = page_date
            if page_category:
                article["category"] = page_category
        if failed:
            logger.warning(f"Failed to fetch content for {failed} articles")
        for article in deduped_articles:
            if not article.get("date"):
                article["date"] = datetime.now(pytz.UTC)

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
