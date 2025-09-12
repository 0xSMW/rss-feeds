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
        fe.description(article["description"])
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
    parser = argparse.ArgumentParser(description="Generate xAI News RSS feed")
    parser.add_argument("--html-file", dest="html_file", help="Path to local HTML file to parse instead of fetching", default=None)
    parser.add_argument("--feed-name", dest="feed_name", help="Feed name suffix (default: xai_news)", default="xai_news")
    args = parser.parse_args()

    try:
        if args.html_file:
            logger.info(f"Reading HTML from file: {args.html_file}")
            with open(args.html_file, "r", encoding="utf-8") as f:
                html = f.read()
        else:
            html = fetch_news_content(NEWS_URL)

        articles = parse_xai_news_html(html)
        if not articles:
            logger.warning("No articles parsed from xAI News. Selectors may need updates.")
        feed = generate_rss_feed(articles, feed_name=args.feed_name)
        save_rss_feed(feed, feed_name=args.feed_name)
    except Exception as e:
        logger.error(f"Failed to generate xAI News feed: {e}")


if __name__ == "__main__":
    main()
