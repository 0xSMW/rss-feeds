"""Generate an RSS feed for Anthropic's news blog (https://www.anthropic.com/news)."""

import argparse
import logging
import re
from pathlib import Path
from urllib.parse import urljoin

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

BASE_URL = "https://www.anthropic.com"
INDEX_URL = BASE_URL + "/news"
PATH_PREFIX = "/news/"

# Category chips shown on listing cards; never valid item titles.
CATEGORY_LABELS = {
    "announcement",
    "announcements",
    "alignment",
    "case studies",
    "case study",
    "company",
    "developers",
    "economic research",
    "education",
    "event",
    "events",
    "featured",
    "frontier red team",
    "interpretability",
    "news",
    "policy",
    "product",
    "research",
    "societal impacts",
}

CTA_TEXT_RE = re.compile(
    r"^(read more|learn more|see more|see all|view all|back to \S+.*)$", re.IGNORECASE
)
DATE_TEXT_RE = re.compile(r"^[A-Za-z]{3,9}\.?\s+\d{1,2},\s+\d{4}$")
HEADER_DATE_RE = re.compile(r"\b[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4}\b")

# Site-specific CTA buttons the generic pass misses ("Read the paper", ...).
ARTICLE_CTA_PATTERNS = (r"read the \S+(\s+\S+){0,2}",)

# Boilerplate og:description used on pages without a real summary.
GENERIC_SUMMARY_RE = re.compile(
    r"anthropic is an ai safety and research company", re.IGNORECASE
)


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def setup_selenium_driver():
    """Set up Selenium WebDriver with undetected-chromedriver."""
    options = uc.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    return uc.Chrome(options=options)


def fetch_news_content_requests(url=INDEX_URL):
    """Fetch news content from Anthropic's website via requests."""
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


def fetch_news_content_selenium(url=INDEX_URL):
    """Fetch the fully loaded HTML content using Selenium."""
    driver = None
    try:
        logger.info(f"Fetching content from URL: {url}")
        driver = setup_selenium_driver()
        driver.get(url)

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/news/']")))
            logger.info("News articles loaded successfully")
        except Exception:
            logger.warning("Could not confirm articles loaded, proceeding anyway...")

        return driver.page_source
    except Exception as e:
        logger.error(f"Error fetching content: {e}")
        raise
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


def _parse_date(text):
    """Parse a date string into an aware UTC datetime."""
    if not text or not text.strip():
        return None
    try:
        dt = dateparser.parse(text.strip())
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pytz.UTC)
    return dt


def _looks_like_label(text):
    """True when a candidate title is really a category chip, date, or CTA."""
    t = " ".join((text or "").split()).lower()
    return (
        len(t) < 4
        or t in CATEGORY_LABELS
        or bool(CTA_TEXT_RE.match(t))
        or bool(DATE_TEXT_RE.match(t))
    )


def _card_title(link):
    """Real headline of a listing card, never the category chip."""
    for el in link.select("[class*='title'], [class*='Title'], h1, h2, h3, h4"):
        if el.find_parent(class_=re.compile("meta", re.IGNORECASE)) is not None:
            continue
        text = " ".join(el.get_text(" ", strip=True).split())
        if text and not _looks_like_label(text):
            return text
    text = " ".join(link.get_text(" ", strip=True).split())
    if len(text) >= 5 and not _looks_like_label(text):
        return text
    return None


def _card_category(link):
    el = link.select_one("[class*='subject']")
    if el is None:
        meta = link.select_one("[class*='meta']")
        if meta is not None:
            el = meta.find("span")
    if el is not None:
        text = " ".join(el.get_text(" ", strip=True).split())
        if text and len(text) <= 40 and not DATE_TEXT_RE.match(text):
            return text
    return None


def _card_date(link):
    time_el = link.find("time")
    if time_el is None:
        return None
    return _parse_date(time_el.get("datetime") or time_el.get_text(" ", strip=True))


def _card_description(link):
    p = link.select_one("p[class*='body']") or link.find("p")
    if p is not None:
        text = " ".join(p.get_text(" ", strip=True).split())
        if len(text) > 20:
            return text
    return None


def parse_news_html(html_content, base_url=BASE_URL):
    """Parse the news listing HTML and extract article entries."""
    soup = BeautifulSoup(html_content, "html.parser")
    articles = []
    seen = set()

    for link in soup.select(f"a[href*='{PATH_PREFIX}']"):
        if link.find_parent(["footer", "nav", "header"]):
            continue
        href = (link.get("href") or "").strip()
        if href.startswith(("http://", "https://")):
            if not href.startswith(base_url):
                continue
            href = href[len(base_url):]
        if not href.startswith(PATH_PREFIX) or href.rstrip("/") == PATH_PREFIX.rstrip("/"):
            continue

        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue

        title = _card_title(link)
        if not title:
            # CTA buttons ("Read more") pointing at articles; the real card
            # for the same URL will be picked up elsewhere in the listing.
            continue

        seen.add(full_url)
        articles.append(
            {
                "title": title,
                "link": full_url,
                "date": _card_date(link),
                "category": _card_category(link) or "News",
                "description": _card_description(link) or title,
            }
        )

    logger.info(f"Successfully parsed {len(articles)} articles")
    return articles


def extract_article_data(html: str, page_url: str, listing_title: str | None = None) -> dict:
    """Extract title, cleaned content HTML, summary, and date from an article page."""
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.select_one("main h1") or soup.find("h1")
    page_title = " ".join(h1.get_text(" ", strip=True).split()) if h1 else ""
    if not page_title or _looks_like_label(page_title):
        og = soup.find("meta", property="og:title")
        og_title = (og.get("content") or "").strip() if og else ""
        if og_title and not _looks_like_label(og_title):
            page_title = og_title
    title = page_title if page_title and not _looks_like_label(page_title) else (listing_title or page_title)

    # The page uses an outer <article> (eyebrow, h1, date) wrapping an inner
    # <article> that holds the actual body copy.
    outer = soup.select_one("main article") or soup.find("article") or soup.find("main")
    container = (outer.find("article") if outer else None) or outer

    content_html = clean_article_html(
        container, page_url, title=title, extra_cta_patterns=ARTICLE_CTA_PATTERNS
    )
    summary = extract_summary(soup, container, title=title)
    if summary and GENERIC_SUMMARY_RE.search(summary):
        # Site-wide boilerplate; prefer the first real paragraph instead.
        body = BeautifulSoup(content_html, "html.parser")
        for p in body.find_all("p"):
            text = " ".join(p.get_text(" ", strip=True).split())
            if len(text) >= 60:
                summary = text
                break

    date = None
    meta_time = soup.find("meta", attrs={"property": "article:published_time"})
    if meta_time and meta_time.get("content"):
        date = _parse_date(meta_time["content"])
    if date is None and outer is not None:
        match = HEADER_DATE_RE.search(outer.get_text(" ", strip=True)[:400])
        if match:
            date = _parse_date(match.group(0))

    return {"title": title, "content_html": content_html, "description": summary, "date": date}


def main(feed_name="anthropic_news", force: bool = False):
    """Main function to generate RSS feed from Anthropic's news page."""
    try:
        feeds_dir = ensure_feeds_directory()
        feed_path = feeds_dir / f"feed_{feed_name}.xml"

        if force:
            logger.info("Force mode enabled: ignoring cached feed entries")
            existing_items, cache = [], {}
        else:
            existing_items, cache = load_cached_entries(feed_path)
        existing_links = {item["link"] for item in existing_items}

        articles = []
        try:
            html_content = fetch_news_content_requests()
            articles = parse_news_html(html_content)
        except Exception as e:
            logger.warning(f"Requests fetch failed ({e}); falling back to Selenium")

        if not articles:
            html_content = fetch_news_content_selenium()
            articles = parse_news_html(html_content)

        if not articles:
            logger.error("No articles found; leaving the existing feed untouched.")
            return False

        new_articles = [article for article in articles if article["link"] not in existing_links]
        logger.info(f"Found {len(articles)} listing items; {len(new_articles)} new since last feed")

        merged = []
        seen_links = set()
        for article in new_articles + existing_items:
            if article["link"] in seen_links:
                continue
            seen_links.add(article["link"])
            merged.append(article)

        # Fetch full content for anything not already captured (new items, plus
        # cached items whose earlier fetch failed).
        for article in merged:
            if article.get("content_html"):
                continue
            article_html = fetch_article_page(article["link"])
            if not article_html:
                continue
            data = extract_article_data(article_html, article["link"], listing_title=article["title"])
            if data.get("title"):
                article["title"] = data["title"]
            if data.get("content_html"):
                article["content_html"] = data["content_html"]
            if data.get("description"):
                article["description"] = data["description"]
            if article.get("date") is None and data.get("date"):
                article["date"] = data["date"]

        feed = build_feed(
            title="Anthropic News",
            description="Latest news and updates from Anthropic",
            site_url=INDEX_URL,
            feed_url=feed_self_url(f"feed_{feed_name}.xml"),
            items=merged,
            author={"name": "Anthropic"},
        )
        save_feed(feed, feed_path)

        logger.info(f"Successfully generated RSS feed with {len(merged)} articles")
        return True

    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Anthropic News RSS feed.")
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    args = parser.parse_args()
    main(force=args.force)
