import requests
from bs4 import BeautifulSoup
from datetime import datetime
from dateutil import parser as dateparser
from urllib.parse import urljoin
import pytz
from feedgen.feed import FeedGenerator
import logging
from pathlib import Path
import time
import re
import undetected_chromedriver as uc
import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://x.ai"
NEWS_URL = "https://x.ai/news"


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists and return its path."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def fetch_news_content_requests(url: str = NEWS_URL) -> str:
    """Fetch HTML via requests with robust headers. Raises for status."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "Referer": BASE_URL + "/",
        "Upgrade-Insecure-Requests": "1",
    }
    logger.info(f"Fetching xAI News page via requests: {url}")
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def build_requests_session() -> requests.Session:
    """Create a configured requests.Session for connection reuse."""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Referer": BASE_URL + "/",
        }
    )
    return s


def setup_selenium_driver():
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    return uc.Chrome(options=options)


def fetch_news_content_selenium(url: str = NEWS_URL) -> str:
    """Fetch HTML via undetected-chromedriver (dynamic rendering)."""
    logger.info(f"Fetching xAI News page via Selenium: {url}")
    driver = None
    try:
        driver = setup_selenium_driver()
        driver.get(url)
        # Initial wait
        time.sleep(4)

        # Scroll to load lazy content if any
        last_height = driver.execute_script("return document.body.scrollHeight")
        for _ in range(10):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        html = driver.page_source
        return html
    finally:
        if driver:
            driver.quit()


def fetch_news_content(url: str = NEWS_URL) -> str:
    """Fetch the HTML content, with Selenium fallback if blocked."""
    try:
        return fetch_news_content_requests(url)
    except Exception as e:
        logger.warning(f"Requests fetch failed ({e}); falling back to Selenium...")
        return fetch_news_content_selenium(url)


def fetch_html(url: str) -> str:
    """Fetch HTML for a URL, falling back to Selenium if needed."""
    try:
        return fetch_news_content_requests(url)
    except Exception as e:
        logger.warning(f"Requests fetch failed for {url} ({e}); falling back to Selenium...")
        # Use dynamic rendering as a fallback
        return fetch_news_content_selenium(url)


def fetch_article_html_selenium(url: str) -> str | None:
    """Fetch a single article page via Selenium and return HTML, or None on error."""
    try:
        driver = setup_selenium_driver()
        driver.get(url)
        time.sleep(4)
        html = driver.page_source
        return html
    except Exception as e:
        logger.debug(f"Selenium fetch failed for {url}: {e}")
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _parse_date(text: str):
    """Parse a date string into an aware UTC datetime, with fallbacks."""
    if not text:
        return None
    text = text.strip()
    try:
        dt = dateparser.parse(text)
        if not dt:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.UTC)
        return dt
    except Exception:
        return None


def _find_date_text_near(element) -> str | None:
    """Find a human-readable date near the given element.

    Looks for:
    - <time> elements (datetime attr or text)
    - Elements with classes like 'mono-tag' used by xAI for dates
    - Any text matching 'Month DD, YYYY' within nearby containers
    """
    if element is None:
        return None

    # Regex: Month name + day, year (e.g., August 28, 2025)
    month_pattern = (
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
        r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    )
    date_re = re.compile(rf"\b{month_pattern}\s+\d{{1,2}},\s+\d{{4}}\b", re.IGNORECASE)

    def from_time(e):
        t = e.find("time")
        if t:
            return (t.get("datetime") or t.get_text(" ", strip=True) or "").strip()
        return None

    # 1) Inside the element
    dt_text = from_time(element)
    if dt_text:
        return dt_text

    # 2) Direct siblings or parent
    for candidate in [element.parent, getattr(element, "previous_sibling", None), getattr(element, "next_sibling", None)]:
        if not candidate or not getattr(candidate, "find", None):
            continue
        dt_text = from_time(candidate)
        if dt_text:
            return dt_text

    # 3) Walk up a few ancestors and search within
    parent = element.parent
    for _ in range(5):
        if not parent:
            break
        # Prefer explicit date containers seen on xAI: class contains 'mono-tag'
        for sel in [".mono-tag", "span", "p", "div", "time"]:
            for el in parent.select(sel):
                text = (el.get("datetime") if el.name == "time" else None) or el.get_text(" ", strip=True)
                if not text:
                    continue
                if date_re.search(text):
                    return text
        parent = parent.parent

    # 4) As a very last resort, scan element subtree
    for el in element.select("time, .mono-tag, span, p, div"):
        text = (el.get("datetime") if el.name == "time" else None) or el.get_text(" ", strip=True)
        if text and date_re.search(text):
            return text

    return None


def parse_xai_news_html(html: str):
    """Parse the xAI News HTML and extract article entries.

    Strategy: find anchors that link to individual news posts (href starting with
    "/news/" or the absolute variant). Titles are typically in h2/h3 elements or
    available via aria-label. Prefer date from a <time> tag, else parse nearby text.
    """
    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen = set()

    # Candidate links to posts
    candidate_links = soup.select("a[href^='/news/'], a[href^='https://x.ai/news/']")

    for a in candidate_links:
        try:
            href = a.get("href", "").strip()
            if not href or href.rstrip("/") in ("/news", "https://x.ai/news"):
                continue

            link = urljoin(BASE_URL + "/", href)
            if link in seen:
                continue

            # Title: prefer heading elements within the anchor; fall back to aria-label; else anchor text
            title_elem = (
                a.find(["h1", "h2", "h3", "h4", "h5", "h6"]) or
                a.select_one("[class*='title'], [class*='heading']")
            )
            title = (
                (title_elem.get_text(strip=True) if title_elem else None)
                or a.get("aria-label", "").strip()
                or a.get_text(strip=True)
            )
            if not title:
                continue

            # Date: try <time> first, then nearby text like '.mono-tag' or Month DD, YYYY
            date_obj = None
            time_elem = a.find("time")
            if time_elem:
                dt_attr = (time_elem.get("datetime") or time_elem.get_text(" ", strip=True) or "").strip()
                date_obj = _parse_date(dt_attr)
            if date_obj is None:
                # Look in siblings/ancestors for a near date text
                dt_text = _find_date_text_near(a)
                if dt_text:
                    date_obj = _parse_date(dt_text)
            if date_obj is None:
                logger.warning(f"Date not found for '{title}'; defaulting to now (UTC)")
                date_obj = datetime.now(pytz.UTC)

            # Description: look for a nearby paragraph
            description = title
            # Common layout: <a>...</a><p>summary</p> within same container
            # Try next siblings within the same parent
            for sib in (a.next_sibling, getattr(a, "next_element", None)):
                if getattr(sib, "name", None) == "p":
                    text = sib.get_text(" ", strip=True)
                    if text:
                        description = text
                        break
            if description == title:
                # Try parent container
                parent = a.parent
                if parent:
                    p = parent.find("p")
                    if p and p.get_text(strip=True):
                        description = p.get_text(" ", strip=True)

            articles.append({
                "title": title,
                "link": link,
                "date": date_obj,
                "category": "News",
                "description": description,
            })
            seen.add(link)
        except Exception as e:
            logger.warning(f"Skipping an item due to parsing error: {e}")
            continue

    logger.info(f"Parsed {len(articles)} xAI news items")
    # Sort by date descending to match typical feed ordering
    try:
        articles.sort(key=lambda x: x.get("date") or datetime.min.replace(tzinfo=pytz.UTC), reverse=True)
    except Exception:
        pass
    return articles


def _clean_article_html(container, base_url: str) -> str:
    """Clean article container and absolutize links/media.

    - Removes script/style/noscript and common non-content elements
    - Converts relative href/src to absolute using base_url
    - Preserves headings, paragraphs, lists, images, blockquotes, code, tables
    """
    if container is None:
        return ""

    # Remove noisy elements by tag
    for tag in container.select("script, style, noscript, svg use[xmlns], form, iframe[aria-hidden='true']"):
        tag.decompose()

    # Remove likely-non-content by class/id hints
    noisy_patterns = [
        "share",
        "social",
        "breadcrumb",
        "nav",
        "header",
        "footer",
        "subscribe",
        "newsletter",
        "related",
        "author",
        "meta",
        "byline",
        "tags",
        "comment",
        "toc",
        "table-of-contents",
        "promo",
        "cta",
    ]
    noisy_re = re.compile("|".join([re.escape(p) for p in noisy_patterns]), re.IGNORECASE)
    for el in container.find_all(True):
        cid = (el.get("id") or "") + " " + " ".join(el.get("class", []))
        if cid and noisy_re.search(cid):
            if not el.find([
                "p",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "li",
                "img",
                "pre",
                "blockquote",
                "table",
            ]):
                el.decompose()

    # Make links and media absolute
    for a in container.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("/", "?")):
            a["href"] = urljoin(base_url, href)
    for img in container.find_all("img", src=True):
        src = img["src"]
        if src.startswith(("/", "?")):
            img["src"] = urljoin(base_url, src)
    for source in container.find_all("source"):
        for attr in ["src", "srcset"]:
            val = source.get(attr)
            if val and (val.startswith("/") or val.startswith("?")):
                source[attr] = urljoin(base_url, val)

    return str(container)


def extract_article_content(html: str, page_url: str) -> tuple[str, str]:
    """Extract main article content HTML and a plain-text summary.

    Returns (content_html, summary_text)
    """
    soup = BeautifulSoup(html, "html.parser")

    # Preferred containers — try obvious content regions
    candidates = [
        soup.select_one("main article"),
        soup.select_one("article"),
        soup.select_one("main [class*='content']"),
        soup.select_one("[class*='richtext']"),
        soup.select_one("[class*='rich-text']"),
        soup.select_one("[class*='prose']"),
        soup.select_one("main"),
    ]
    container = next((c for c in candidates if c), None)
    if container is None:
        # Fallback: largest block by total paragraph text length
        paragraphs = soup.find_all("p")
        parent_scores: dict = {}
        for p in paragraphs:
            parent = p.find_parent()
            if parent:
                parent_scores[parent] = parent_scores.get(parent, 0) + len(p.get_text(strip=True))
        container = max(parent_scores, key=parent_scores.get) if parent_scores else soup.body

    content_html = _clean_article_html(container, base_url=page_url)

    # Summary: first sufficiently long paragraph
    summary = ""
    if container:
        for p in container.find_all("p"):
            text = p.get_text(" ", strip=True)
            if text and len(text) > 40:
                summary = text
                break
    if not summary:
        summary = soup.title.get_text(strip=True) if soup.title else ""

    return content_html, summary


def load_existing_feed(feed_path: Path) -> tuple[list[dict], dict]:
    """Load existing feed items and a link->cached mapping from an RSS file if present.

    Returns (existing_items, cache_by_link)
    """
    items: list[dict] = []
    cache: dict[str, dict] = {}
    if not feed_path.exists():
        return items, cache

    try:
        with open(feed_path, "r", encoding="utf-8") as f:
            xml = f.read()
        soup = BeautifulSoup(xml, "xml")
        for item in soup.find_all("item"):
            link_tag = item.find("link")
            if not link_tag or not link_tag.text:
                continue
            link = link_tag.text.strip()

            title = (item.find("title").text if item.find("title") else link)
            desc_tag = item.find("description")
            desc = desc_tag.text if desc_tag else title
            content_tag = item.find("content:encoded") or item.find("encoded")
            content_html = content_tag.text if content_tag else None
            # Parse pubDate if present
            pub = item.find("pubDate")
            date_obj = None
            if pub and pub.text:
                try:
                    # dateutil handles RFC 2822 format
                    date_obj = dateparser.parse(pub.text)
                    if date_obj and date_obj.tzinfo is None:
                        date_obj = date_obj.replace(tzinfo=pytz.UTC)
                except Exception:
                    date_obj = None
            if date_obj is None:
                date_obj = datetime.now(pytz.UTC)

            cat_tag = item.find("category")
            category = cat_tag.text if cat_tag and cat_tag.text else "News"

            article = {
                "title": title,
                "link": link,
                "date": date_obj,
                "category": category,
                "description": desc,
                "content_html": content_html,
            }
            items.append(article)
            cache[link] = {"description": desc, "content_html": content_html}
    except Exception as e:
        logger.warning(f"Failed to load existing feed from {feed_path}: {e}")

    return items, cache


def fetch_article_page(session: requests.Session, url: str) -> str | None:
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.debug(f"Session fetch failed for {url}: {e}")
        return None


def fetch_contents_parallel(articles: list[dict], cached: dict, max_workers: int = 8) -> None:
    """Populate content_html/description for uncached articles in parallel via requests.

    Falls back to sequential Selenium for any that still lack content on failure.
    Mutates the articles list in place.
    """
    # First apply cache
    for a in articles:
        c = cached.get(a["link"])
        if c:
            a["content_html"] = c.get("content_html")
            if c.get("description"):
                a["description"] = c["description"]

    to_fetch = [a for a in articles if not a.get("content_html")]
    if not to_fetch:
        return

    session = build_requests_session()
    max_workers = max(1, int(os.getenv("XAI_FEED_WORKERS", str(max_workers))))
    futures = {}
    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        for a in to_fetch:
            futures[exe.submit(fetch_article_page, session, a["link"])] = a

        for fut in as_completed(futures):
            art = futures[fut]
            try:
                html = fut.result()
                if html:
                    content_html, summary = extract_article_content(html, art["link"])
                    art["content_html"] = content_html
                    if summary and summary.strip():
                        art["description"] = summary
            except Exception as e:
                logger.debug(f"Parallel fetch parse failed for {art['link']}: {e}")

    # Fallback using Selenium IN PARALLEL for any still missing content
    remaining = [a for a in articles if not a.get("content_html")]
    if remaining:
        selenium_workers = max(1, int(os.getenv("XAI_SELENIUM_WORKERS", "2")))
        logger.info(f"Falling back to Selenium for {len(remaining)} items with {selenium_workers} workers")

        def _work(art: dict):
            url = art["link"]
            html = fetch_article_html_selenium(url)
            if not html:
                return (url, None, None)
            content_html, summary = extract_article_content(html, url)
            return (url, content_html, summary)

        results = []
        with ThreadPoolExecutor(max_workers=selenium_workers) as exe:
            fut_map = {exe.submit(_work, a): a for a in remaining}
            for fut in as_completed(fut_map):
                try:
                    results.append(fut.result())
                except Exception as e:
                    # Individual worker failure should not crash the whole run
                    bad = fut_map[fut]
                    logger.warning(f"Selenium worker failed for {bad['link']}: {e}")

        by_link = {a["link"]: a for a in articles}
        for url, content_html, summary in results:
            if not url:
                continue
            art = by_link.get(url)
            if not art:
                continue
            if content_html:
                art["content_html"] = content_html
            if summary and summary.strip():
                art["description"] = summary


def generate_rss_feed(articles, feed_name: str = "xai_news"):
    """Generate RSS feed from parsed articles."""
    fg = FeedGenerator()
    fg.title("xAI News")
    fg.description("Latest news and updates from xAI")
    # Set site link and self-link (order: set self first, then site link as the visible channel link)
    fg.language("en")

    # Metadata (optional but nice to have)
    fg.author({"name": "xAI"})
    fg.link(href=NEWS_URL, rel="alternate")
    fg.link(href=f"{BASE_URL}/news/feed_{feed_name}.xml", rel="self")
    fg.link(href=NEWS_URL)

    for article in articles:
        fe = fg.add_entry()
        fe.title(article["title"])
        fe.link(href=article["link"])
        # Prefer full HTML content via content:encoded, with description as summary
        content_html = article.get("content_html")
        summary = article.get("description", article["title"]) or article["title"]
        if content_html:
            fe.content(content_html)
        fe.description(summary)
        fe.published(article["date"])
        fe.category(term=article.get("category", "News"))
        fe.id(article["link"])

    logger.info("RSS feed generated successfully for xAI News")
    return fg


def save_rss_feed(feed_generator, feed_name: str = "xai_news") -> Path:
    """Save RSS feed to an XML file under feeds/."""
    feeds_dir = ensure_feeds_directory()
    output_path = feeds_dir / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_path), pretty=True)
    logger.info(f"RSS feed saved to {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Generate xAI News RSS feed with full content and incremental updates")
    parser.add_argument("--html-file", dest="html_file", help="Path to local HTML file to parse instead of fetching", default=None)
    parser.add_argument("--feed-name", dest="feed_name", help="Feed name suffix (default: xai_news)", default="xai_news")
    args = parser.parse_args()

    try:
        feeds_dir = ensure_feeds_directory()
        existing_feed_path = feeds_dir / f"feed_{args.feed_name}.xml"

        # Load existing items to avoid re-fetching and to append new ones only
        existing_items, cache = load_existing_feed(existing_feed_path)
        existing_links = {it["link"] for it in existing_items}

        # Fetch or read index HTML
        if args.html_file:
            logger.info(f"Reading HTML from file: {args.html_file}")
            with open(args.html_file, "r", encoding="utf-8") as f:
                html = f.read()
        else:
            html = fetch_news_content(NEWS_URL)

        # Parse index for latest articles
        parsed = parse_xai_news_html(html)
        new_articles = [a for a in parsed if a["link"] not in existing_links]
        logger.info(f"Found {len(parsed)} parsed items; {len(new_articles)} new since last feed")

        # Fetch full content only for new items (parallel requests; Selenium fallback)
        fetch_contents_parallel(new_articles, cached=cache, max_workers=int(os.getenv("XAI_FEED_WORKERS", "8")))

        # Merge: keep existing items first (already sorted in prior feed order), then add new ones at top by date
        try:
            new_articles.sort(key=lambda x: x.get("date") or datetime.min.replace(tzinfo=pytz.UTC), reverse=True)
        except Exception:
            pass
        merged = new_articles + existing_items

        # Optionally limit feed length to a reasonable number (keep all by default)
        feed = generate_rss_feed(merged, feed_name=args.feed_name)
        save_rss_feed(feed, feed_name=args.feed_name)
    except Exception as e:
        logger.error(f"Failed to generate xAI News feed: {e}")


if __name__ == "__main__":
    main()
