import argparse
import logging
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pytz
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

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

BASE_URL = "https://arenamag.com"
CATEGORY_URLS = [
    f"{BASE_URL}/technology",
    f"{BASE_URL}/capitalism",
    f"{BASE_URL}/science",
    f"{BASE_URL}/civilization",
    f"{BASE_URL}/greatness",
]

# Only URLs shaped like /articles/<slug> are actual articles; everything else
# (homepage, category indexes, /manage, /about, ...) is site chrome.
ARTICLE_PATH_RE = re.compile(r"/articles/[^/]+")

# Framer-specific chrome inside/around the article container.
ARENA_STRIP_SELECTORS = (
    '[data-framer-name="Header"]',
    '[data-framer-name="HeaderImage"]',
    '[data-framer-name="NewsletterCard"]',
    '[data-framer-name="SubscribePaywall"]',
    '[data-framer-name="RequiresSubscription"]',
    '[data-framer-name="MetaItem"]',
    '[data-framer-name="SidebarActions"]',
    '[data-framer-name="ActionGroup"]',
    '[data-framer-name="MenuItem"]',
    '[class*="paywall"]',
)

# Subscribe promo copy that has leaked into article bodies before.
ARENA_CTA_PATTERNS = (
    r"four (?:beautiful )?100[+-]? ?page issues.*",
    r"subscribe to arena.*",
    r"get arena( magazine)?.*",
)
ARENA_PROMO_RE = re.compile(
    r"^(?:subscribe|four (?:beautiful )?100[+-]? ?page issues per year.*|"
    r"get arena magazine.*|subscribe to arena.*)$",
    re.IGNORECASE,
)

# Mojibake markers: 'â'/'Ã'/'Â' followed by a UTF-8 continuation byte decoded
# as Latin-1 (U+0080-U+00BF), or the cp1252 flavor 'â€¦'. Correct English prose
# never contains these bigrams.
_MOJIBAKE_RE = re.compile("[\u00e2\u00c3\u00c2][\u0080-\u00bf]|\u00e2\u20ac")


def _repair_mojibake(text: str) -> str:
    """Reverse a UTF-8-decoded-as-Latin-1/cp1252 mangle. Returns input on failure."""
    for encoding in ("latin-1", "cp1252"):
        try:
            return text.encode(encoding).decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return text


def normalize_text(text: str) -> str:
    """Fix mojibake if (and only if) present, then NFC-normalize.

    Correctly encoded text passes through untouched; the Latin-1 round-trip is
    attempted only when the text actually contains mojibake marker bigrams.
    """
    if not text:
        return text
    if _MOJIBAKE_RE.search(text):
        text = _repair_mojibake(text)
    return unicodedata.normalize("NFC", text)


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _decode_response(resp: requests.Response) -> str:
    """Decode a response as UTF-8.

    arenamag.com serves ``Content-Type: text/html`` without a charset, so
    ``resp.text`` mis-decodes the UTF-8 body as ISO-8859-1 (the source of the
    old feed's mojibake). The site is UTF-8; decode it explicitly.
    """
    return resp.content.decode("utf-8", errors="replace")


def fetch_page_requests(url: str) -> str:
    """Fetch HTML using requests."""
    logger.info(f"Fetching page (requests): {url}")
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
    resp.raise_for_status()
    return _decode_response(resp)


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


def fetch_page_selenium(url: str) -> str:
    """Fetch fully rendered HTML using Selenium."""
    driver = None
    try:
        driver = setup_selenium_driver()
        logger.info(f"Fetching page (selenium): {url}")
        driver.get(url)
        time.sleep(5)

        # Scroll to load lazy content
        last_height = driver.execute_script("return document.body.scrollHeight")
        for _ in range(5):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        return driver.page_source
    finally:
        if driver:
            driver.quit()


def fetch_page(url: str) -> str:
    """Fetch the HTML content, with Selenium fallback if blocked."""
    try:
        return fetch_page_requests(url)
    except Exception as e:
        logger.warning(f"Requests fetch failed ({e}); falling back to Selenium...")
        return fetch_page_selenium(url)


def build_requests_session() -> requests.Session:
    """Create a configured requests.Session for connection reuse."""
    s = requests.Session()
    s.headers.update(REQUEST_HEADERS)
    return s


def fetch_article_page(session: requests.Session, url: str) -> str | None:
    """Fetch HTML for a single article page via requests; None on failure."""
    try:
        logger.debug(f"Fetching article page: {url}")
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        return _decode_response(resp)
    except Exception as e:
        logger.warning(f"Failed to fetch article page {url}: {e}")
        return None


def fetch_article_page_selenium(url: str) -> str | None:
    """Fetch a single article page via Selenium; None on failure."""
    driver = None
    try:
        driver = setup_selenium_driver()
        driver.get(url)
        time.sleep(3)
        return driver.page_source
    except Exception as e:
        logger.warning(f"Selenium fetch failed for {url}: {e}")
        return None
    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass


def _parse_date(text: str) -> datetime | None:
    """Parse date string, return None if unparsable."""
    if not text:
        return None
    text = text.strip()
    try:
        dt = dateparser.parse(text)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=pytz.UTC)
            return dt
    except Exception:
        pass
    return None


def canonical_article_link(href: str) -> str | None:
    """Absolute /articles/<slug> URL (query/fragment stripped), or None."""
    if not href:
        return None
    link = urljoin(BASE_URL + "/", href.strip())
    parsed = urlparse(link)
    if parsed.netloc and parsed.netloc != urlparse(BASE_URL).netloc:
        return None
    path = parsed.path.rstrip("/")
    if not ARTICLE_PATH_RE.fullmatch(path):
        return None
    return f"{BASE_URL}{path}"


def parse_category_page(html_content: str, category_name: str) -> list[dict]:
    """Parse an Arena Magazine category page to extract articles."""
    soup = BeautifulSoup(html_content, "html.parser")
    by_link: dict[str, dict] = {}

    for a in soup.select("a[href]"):
        try:
            # Only actual article permalinks; skips homepage, category pages,
            # /manage, /about, /subscribe, and every other non-article link.
            link = canonical_article_link(a.get("href", ""))
            if not link:
                continue

            # Extract title from anchor text or nested elements
            title_elem = a.find(["h1", "h2", "h3", "h4", "h5", "h6"])
            title = (title_elem.get_text(strip=True) if title_elem else None)
            if not title:
                title = a.get_text(" ", strip=True)

            # Clean up title - remove "NEW" badge and author suffix
            # ("by Maxwell Meyer" / legacy "byMaxwell Meyer")
            title = re.sub(r"\s*\bby\s*[A-Z].*$", "", title)
            title = re.sub(r"\s+NEW$", "", title)
            title = normalize_text(title.strip())

            if (not title or len(title) < 3) and link not in by_link:
                continue

            # Extract author(s) from anchor text if present
            full_text = a.get_text(" ", strip=True)
            author = None
            author_match = re.search(r"\bby\s*([A-Z][a-zA-Z.'\-\s•·]+?)\s*$", full_text)
            if author_match:
                names = re.split(r"\s*[•·]\s*", author_match.group(1).strip())
                author = " & ".join(n.strip() for n in names if n.strip())
                author = normalize_text(re.sub(r"\s+", " ", author))

            # Try to find date - Arena mag articles may have dates in metadata or nearby
            date_dt = None
            time_el = a.find("time")
            if not time_el and a.parent:
                time_el = a.parent.find("time")
            if time_el:
                dt_attr = (time_el.get("datetime") or time_el.get_text(strip=True) or "").strip()
                date_dt = _parse_date(dt_attr)

            existing = by_link.get(link)
            if existing:
                # The same article is linked several times per listing page
                # (image anchor, title anchor, byline anchor); backfill fields
                # the first anchor was missing.
                if not existing.get("author") and author:
                    existing["author"] = author
                if not existing.get("date") and date_dt:
                    existing["date"] = date_dt
            else:
                by_link[link] = {
                    "title": title,
                    "link": link,
                    "date": date_dt,
                    "category": category_name,
                    "author": author,
                    "description": title,
                }
        except Exception as e:
            logger.warning(f"Skipping an item due to parsing error: {e}")
            continue

    articles = list(by_link.values())
    logger.info(f"Parsed {len(articles)} articles from {category_name}")
    return articles


def collect_all_articles() -> list[dict]:
    """Fetch and parse all category pages, deduplicate articles."""
    category_names = {
        f"{BASE_URL}/technology": "Technology",
        f"{BASE_URL}/capitalism": "Capitalism",
        f"{BASE_URL}/science": "Science",
        f"{BASE_URL}/civilization": "Civilization",
        f"{BASE_URL}/greatness": "Greatness",
    }

    by_link: dict[str, dict] = {}

    for url in CATEGORY_URLS:
        try:
            html = fetch_page(url)
            category = category_names.get(url, "Article")
            articles = parse_category_page(html, category)

            for article in articles:
                link = article["link"]
                if link not in by_link:
                    by_link[link] = article
                else:
                    # Keep the first category found; backfill missing fields.
                    for key in ("author", "date"):
                        if not by_link[link].get(key) and article.get(key):
                            by_link[link][key] = article[key]
        except Exception as e:
            logger.error(f"Failed to fetch category {url}: {e}")
            continue

    combined = list(by_link.values())
    logger.info(f"Collected {len(combined)} unique articles across all categories")
    return combined


def _find_content_container(soup: BeautifulSoup):
    """Locate the article body container on a Framer article page."""
    container = soup.select_one('[data-framer-name="FullContent"]')
    if not container:
        container = soup.select_one('[data-framer-name="Content"]')
    if not container:
        candidates = [
            soup.select_one("article"),
            soup.select_one("main"),
            soup.select_one("[class*='content']"),
        ]
        container = next((c for c in candidates if c), None)
    return container


def extract_article_metadata(html: str, page_url: str, title: str | None = None) -> dict:
    """Extract article metadata from page: title, date, description, content."""
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    # Extract clean title from og:title or <title>
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        result["title"] = normalize_text(og_title["content"].strip())
    elif soup.title:
        result["title"] = normalize_text(soup.title.get_text(strip=True))
    title = result.get("title") or title

    # Extract date: article:published_time meta, else the header date paragraph
    # (Arena renders dates like "Nov 10, 2025" in framer-text paragraphs).
    published = soup.find("meta", property="article:published_time")
    if published and published.get("content"):
        result["date"] = _parse_date(published["content"])
    if not result.get("date"):
        date_pattern = re.compile(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+202\d"
        )
        for p in soup.find_all("p"):
            classes = p.get("class", [])
            if classes and "framer-text" in " ".join(classes):
                text = p.get_text(strip=True)
                if date_pattern.match(text):
                    dt = _parse_date(text)
                    if dt:
                        result["date"] = dt
                        break

    # Author(s): article pages link contributors to /authors/<slug>.
    author_names = []
    for a in soup.select("a[href*='/authors/']"):
        name = normalize_text(a.get_text(" ", strip=True))
        if name and name not in author_names:
            author_names.append(name)
    if author_names:
        result["author"] = " & ".join(author_names)

    container = _find_content_container(soup)
    if container:
        content_html = clean_article_html(
            container,
            page_url,
            title=title,
            strip_selectors=ARENA_STRIP_SELECTORS,
            extra_cta_patterns=ARENA_CTA_PATTERNS,
        )
        content_html = _strip_promo_paragraphs(normalize_text(content_html))
        if content_html:
            result["content_html"] = content_html

    # Summary: og:description first (article dek), else first real paragraph.
    # Some articles set og:description to a bare category list; fall back to
    # the first substantial content paragraph in that case.
    summary = extract_summary(soup, container, title=title)
    if summary and _CATEGORY_ONLY_RE.match(summary.strip()):
        summary = _first_paragraph_summary(result.get("content_html")) or summary
    if summary and summary.strip():
        result["description"] = normalize_text(summary.strip())

    return result


# og:description that is nothing but a list of Arena's category names.
_CATEGORY_ONLY_RE = re.compile(
    r"^(?:(?:Technology|Capitalism|Science|Civilization|Greatness)[,\s]*)+\.?$"
)


def _first_paragraph_summary(content_html: str | None, min_length: int = 60) -> str | None:
    """First substantial paragraph of cleaned content, for use as a summary."""
    if not content_html:
        return None
    soup = BeautifulSoup(content_html, "html.parser")
    for p in soup.find_all("p"):
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if len(text) >= min_length:
            return text[:300] + "..." if len(text) > 300 else text
    return None


def _strip_promo_paragraphs(content_html: str) -> str:
    """Drop leftover subscribe-promo paragraphs from cleaned content."""
    if not content_html or "ubscri" not in content_html and "issues per year" not in content_html:
        return content_html
    soup = BeautifulSoup(content_html, "html.parser")
    changed = False
    for p in soup.find_all(["p", "h2", "h3", "h4"]):
        text = p.get_text(" ", strip=True)
        if text and ARENA_PROMO_RE.match(text):
            p.decompose()
            changed = True
    return str(soup) if changed else content_html


def enrich_article(article: dict, html: str) -> None:
    """Update an article dict in place from its article-page HTML."""
    metadata = extract_article_metadata(html, article["link"], title=article.get("title"))
    if metadata.get("title"):
        article["title"] = metadata["title"]
    if metadata.get("date"):
        article["date"] = metadata["date"]
    if metadata.get("content_html"):
        article["content_html"] = metadata["content_html"]
    if metadata.get("description"):
        article["description"] = metadata["description"]
    if metadata.get("author"):
        article["author"] = metadata["author"]


def fetch_contents_parallel(articles: list[dict], cached: dict, max_workers: int = 8) -> None:
    """Populate content_html/description for uncached articles in parallel.

    Applies the existing-feed cache first, fetches the rest via requests in a
    thread pool, then falls back to sequential Selenium for stragglers.
    Mutates the articles list in place.
    """
    for a in articles:
        c = cached.get(a["link"])
        if c and c.get("content_html"):
            a["content_html"] = c["content_html"]
            if c.get("description"):
                a["description"] = c["description"]

    to_fetch = [a for a in articles if not a.get("content_html")]
    if not to_fetch:
        return
    logger.info(f"Fetching full content for {len(to_fetch)} articles...")

    session = build_requests_session()
    max_workers = max(1, int(os.getenv("ARENA_FEED_WORKERS", str(max_workers))))
    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        futures = {
            exe.submit(fetch_article_page, session, a["link"]): a for a in to_fetch
        }
        for fut in as_completed(futures):
            art = futures[fut]
            try:
                html = fut.result()
                if html:
                    enrich_article(art, html)
            except Exception as e:
                logger.warning(f"Failed to extract content for {art['link']}: {e}")

    remaining = [a for a in articles if not a.get("content_html")]
    if remaining:
        logger.info(f"Falling back to Selenium for {len(remaining)} items (sequential)")
        for art in remaining:
            html = fetch_article_page_selenium(art["link"])
            if html:
                try:
                    enrich_article(art, html)
                except Exception as e:
                    logger.warning(f"Failed to extract content for {art['link']}: {e}")


def generate_rss_feed(articles, feed_name: str = "arenamag"):
    """Generate RSS feed from parsed articles."""
    fg = build_feed(
        title="Arena Magazine",
        description="Technology, Capitalism, Science, Civilization, and Greatness - Arena Magazine",
        site_url=BASE_URL,
        feed_url=feed_self_url(f"feed_{feed_name}.xml"),
        items=articles,
        author={"name": "Arena Magazine"},
    )
    logger.info("RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator, feed_name: str = "arenamag") -> Path:
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    save_feed(feed_generator, output_file)
    return output_file


def _load_cached_authors(feed_path: Path) -> dict[str, str]:
    """Map link -> dc:creator from a previously generated feed.

    ``load_cached_entries`` in _common does not round-trip item authors, so
    read them back here to keep authors on cached items across runs.
    """
    authors: dict[str, str] = {}
    if not feed_path.exists():
        return authors
    try:
        soup = BeautifulSoup(feed_path.read_text(encoding="utf-8"), "xml")
        for item in soup.find_all("item"):
            link = item.find("link")
            creator = item.find("creator")
            if link and link.text and creator and creator.text:
                authors[link.text.strip()] = creator.text.strip()
    except Exception as e:
        logger.warning(f"Failed to load cached authors from {feed_path}: {e}")
    return authors


def main(feed_name: str = "arenamag", force: bool = False) -> bool:
    try:
        feeds_dir = ensure_feeds_directory()
        feed_path = feeds_dir / f"feed_{feed_name}.xml"

        if force:
            logger.info("Force mode enabled: rebuilding feed from scratch")
            existing_items, cache = [], {}
        else:
            existing_items, cache = load_cached_entries(feed_path)
            # Drop previously cached junk (homepage, /manage, category pages).
            existing_items = [
                it for it in existing_items if canonical_article_link(it["link"])
            ]
            cached_authors = _load_cached_authors(feed_path)
            for it in existing_items:
                if cached_authors.get(it["link"]):
                    it["author"] = cached_authors[it["link"]]
        existing_links = {it["link"] for it in existing_items}

        articles = collect_all_articles()
        if not articles:
            logger.warning("No Arena Magazine articles parsed. Selectors may need updating.")
        by_link = {a["link"]: a for a in articles}

        # Cached entries lose author/category detail on round-trip; refresh
        # them from the freshly parsed category listings.
        for item in existing_items:
            listed = by_link.get(item["link"])
            if listed:
                if not item.get("author") and listed.get("author"):
                    item["author"] = listed["author"]
                if not item.get("category") and listed.get("category"):
                    item["category"] = listed["category"]

        new_articles = [a for a in articles if a["link"] not in existing_links]
        logger.info(f"Found {len(articles)} listed articles; {len(new_articles)} new since last feed")

        merged = new_articles + existing_items
        fetch_contents_parallel(
            merged, cached=cache, max_workers=int(os.getenv("ARENA_FEED_WORKERS", "8"))
        )

        missing = [a["link"] for a in merged if not a.get("content_html")]
        if missing:
            logger.warning(f"{len(missing)} articles still lack content: {missing}")

        feed = generate_rss_feed(merged, feed_name)
        save_rss_feed(feed, feed_name)
        logger.info(f"Successfully generated RSS feed with {min(len(merged), 50)} articles")
        return True
    except Exception as e:
        logger.error(f"Failed to generate Arena Magazine RSS: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Arena Magazine RSS feed.")
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    parser.add_argument("--feed-name", dest="feed_name", default="arenamag", help="Feed name suffix (default: arenamag)")
    args = parser.parse_args()
    main(feed_name=args.feed_name, force=args.force)
