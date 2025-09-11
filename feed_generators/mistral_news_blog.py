import requests
import time
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
from feedgen.feed import FeedGenerator
import logging
from pathlib import Path
from urllib.parse import urljoin

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


def parse_news_html(html_content: str):
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

        # Date: prefer <time> inside anchor, then in parent containers
        date_dt = None
        time_el = a.find("time")
        if not time_el and a.parent:
            # Sometimes the <time> is a sibling of the anchor
            time_el = a.parent.find("time")
        if time_el:
            dt_attr = (time_el.get("datetime") or "").strip()
            text_val = time_el.get_text(strip=True)
            date_dt = _parse_date(dt_attr or text_val)
        if not date_dt:
            date_dt = datetime.now(pytz.UTC)

        # Category: default to News; try to read a nearby badge if available
        category = "News"
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


def main(feed_name: str = "mistral_news") -> bool:
    try:
        # Prefer Selenium to capture dynamically loaded items; fall back to requests
        try:
            html = fetch_news_content_selenium(NEWS_URL)
        except Exception as e:
            logger.warning(f"Selenium fetch failed ({e}); falling back to requests")
            html = fetch_news_content_requests(NEWS_URL)
        articles = parse_news_html(html)
        if not articles:
            logger.warning("No Mistral news articles parsed. Selectors may need updating.")
        feed = generate_rss_feed(articles, feed_name)
        save_rss_feed(feed, feed_name)
        return True
    except Exception as e:
        logger.error(f"Failed to generate Mistral AI news RSS: {e}")
        return False


if __name__ == "__main__":
    main()
