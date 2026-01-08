import argparse
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import pytz
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from feedgen.feed import FeedGenerator

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://stevejobsarchive.com"
LISTING_URL = f"{BASE_URL}/stories"
DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b"
)


def in_ci() -> bool:
    return os.environ.get("CI", "").lower() == "true"


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory() -> Path:
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def build_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Referer": BASE_URL,
        }
    )
    return session


def setup_selenium_driver():
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    driver_path = os.environ.get("CHROMEDRIVER_PATH")
    browser_path = os.environ.get("CHROME_BINARY")
    return uc.Chrome(
        options=options,
        driver_executable_path=driver_path,
        browser_executable_path=browser_path,
        user_multi_procs=True,
    )


def fetch_page_requests(url: str, session: requests.Session | None = None) -> str:
    sess = session or build_requests_session()
    logger.info(f"Fetching page (requests): {url}")
    resp = sess.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text


def fetch_page_selenium(url: str) -> str:
    driver = None
    try:
        logger.info(f"Fetching page (selenium): {url}")
        driver = setup_selenium_driver()
        driver.get(url)

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "script#__NEXT_DATA__"))
            )
        except Exception:
            logger.warning("Could not confirm Next.js payload, continuing...")

        return driver.page_source
    finally:
        if driver:
            driver.quit()


def fetch_page(url: str, session: requests.Session | None = None) -> str:
    try:
        return fetch_page_requests(url, session=session)
    except Exception as e:
        if in_ci():
            raise
        logger.warning(f"Requests fetch failed ({e}); falling back to Selenium...")
        return fetch_page_selenium(url)


def _extract_next_data(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return None
    payload = script.string or script.text
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse __NEXT_DATA__: {e}")
        return None


def _normalize_story_link(slug: str) -> str | None:
    if not slug:
        return None
    slug = slug.strip()
    if not slug:
        return None
    if slug.startswith("http://") or slug.startswith("https://"):
        return slug
    return urljoin(BASE_URL + "/", slug.lstrip("/"))


def _rich_text_to_text(node) -> str:
    parts: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            if value.get("nodeType") == "text":
                text = value.get("value") or ""
                if text.strip():
                    parts.append(text)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    text = " ".join(parts)
    return re.sub(r"\s+", " ", text).strip()


def _find_first_hyperlink(node) -> str | None:
    if isinstance(node, dict):
        if node.get("nodeType") == "hyperlink":
            uri = node.get("data", {}).get("uri")
            if uri:
                return uri
        for child in node.values():
            found = _find_first_hyperlink(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_first_hyperlink(child)
            if found:
                return found
    return None


def parse_listing(html: str) -> list[dict]:
    data = _extract_next_data(html)
    if not data:
        logger.warning("No Next.js data found on listing page.")
        return []

    page_data = data.get("props", {}).get("pageProps", {}).get("pageData", {})
    modules = page_data.get("modulesCollection", {}).get("items", [])
    items: list[dict] = []

    for module in modules:
        typename = module.get("__typename")
        if typename == "ModuleMediaSplitMedia":
            title = (module.get("title") or "").strip()
            link = module.get("imageLinkUrl") or _find_first_hyperlink(module.get("links"))
            description = (module.get("subtitle") or "").strip()
            if not description:
                description = _rich_text_to_text(module.get("body", {}))
            link = _normalize_story_link(link) if link else None
            if not title or not link:
                continue
            items.append(
                {
                    "title": title,
                    "link": link,
                    "description": description,
                    "category": "Stories",
                }
            )
            continue
        if typename != "ModuleMediaGrid":
            continue
        grid_items = module.get("gridItemsCollection", {}).get("items", [])
        for entry in grid_items:
            title = (entry.get("title") or "").strip()
            slug = entry.get("slug") or entry.get("url")
            description = (entry.get("description") or entry.get("subtitle") or "").strip()
            link = _normalize_story_link(slug)
            if not title or not link:
                continue
            items.append(
                {
                    "title": title,
                    "link": link,
                    "description": description,
                    "category": "Stories",
                }
            )

    seen = set()
    deduped: list[dict] = []
    for item in items:
        if item["link"] in seen:
            continue
        seen.add(item["link"])
        deduped.append(item)

    return deduped


def _absolutize_srcset(value: str, base_url: str) -> str:
    parts = []
    for part in value.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        bits = chunk.split()
        url = bits[0]
        if not url.startswith(("http://", "https://", "data:")):
            url = urljoin(base_url, url)
        bits[0] = url
        parts.append(" ".join(bits))
    return ", ".join(parts)


def _absolutize_url(value: str, base_url: str) -> str:
    if not value:
        return value
    if value.startswith(("http://", "https://", "mailto:", "#", "data:")):
        return value
    return urljoin(base_url, value)


def _clean_article_html(container, base_url: str) -> None:
    if container is None:
        return

    for tag in container.select(
        "script, style, noscript, svg, form, iframe, input, button, select, textarea, "
        "nav, header, footer, aside"
    ):
        tag.decompose()

    for tag in container.find_all(True):
        if tag.name == "a" and tag.get("href"):
            tag["href"] = _absolutize_url(tag["href"], base_url)
        if tag.name == "img" and tag.get("src"):
            tag["src"] = _absolutize_url(tag["src"], base_url)
        if tag.name in {"img", "source"} and tag.get("srcset"):
            tag["srcset"] = _absolutize_srcset(tag["srcset"], base_url)
        if tag.name in {"video", "audio"}:
            if tag.get("poster"):
                tag["poster"] = _absolutize_url(tag["poster"], base_url)
            if tag.get("src"):
                tag["src"] = _absolutize_url(tag["src"], base_url)

    for tag in list(container.find_all(["div", "span"])):
        if tag.find(["p", "img", "figure", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4"]):
            continue
        if not tag.get_text(strip=True):
            tag.decompose()


def _extract_summary(container) -> str:
    if container is None:
        return ""
    for tag in container.find_all(["p", "li"]):
        text = tag.get_text(" ", strip=True)
        if text:
            return text
    for tag in container.find_all(["h2", "h3", "h4"]):
        text = tag.get_text(" ", strip=True)
        if text:
            return text
    return ""


def _extract_pub_date(container) -> datetime | None:
    if container is None:
        return None
    for tag in container.find_all(["p", "span", "div", "time"]):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 50:
            continue
        match = DATE_RE.search(text)
        if not match:
            continue
        try:
            dt = dateparser.parse(match.group(0))
        except (TypeError, ValueError):
            continue
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.UTC)
        return dt
    return None


def extract_article_content(html: str, page_url: str) -> tuple[str, str, datetime | None]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(id="main") or soup.find("main") or soup.find("article")
    if container is None:
        logger.warning("Could not locate article container; using full body.")
        container = soup.body or soup

    _clean_article_html(container, page_url)
    summary = _extract_summary(container)
    pub_date = _extract_pub_date(container)
    return str(container), summary, pub_date


def generate_rss_feed(articles: list[dict], feed_name: str) -> FeedGenerator:
    fg = FeedGenerator()
    fg.title("Steve Jobs Archive Stories")
    fg.link(href=LISTING_URL)
    fg.description("Selections of video and writing drawn from moments in Steve's life.")
    fg.language("en")
    fg.author({"name": "Steve Jobs Archive"})

    for article in reversed(articles):
        fe = fg.add_entry()
        fe.title(article["title"])
        fe.link(href=article["link"])
        fe.description(article.get("description") or article["title"])
        if article.get("content_html"):
            fe.content(article["content_html"])
        if article.get("date"):
            fe.published(article["date"])
        fe.category(term=article.get("category", "Stories"))
        fe.id(article["link"])

    logger.info("RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator: FeedGenerator, feed_name: str) -> Path:
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"RSS feed saved to {output_file}")
    return output_file


def _sort_articles(articles: list[dict]) -> list[dict]:
    with_dates = [article for article in articles if article.get("date")]
    without_dates = [article for article in articles if not article.get("date")]
    with_dates.sort(key=lambda x: x["date"], reverse=True)
    return with_dates + without_dates


def main(feed_name: str = "steve_jobs_archive_stories") -> bool:
    try:
        session = build_requests_session()
        listing_html = fetch_page(LISTING_URL, session=session)
        articles = parse_listing(listing_html)
        if not articles:
            logger.warning("No stories parsed from listing page.")

        for article in articles:
            article_html = fetch_page(article["link"], session=session)
            if not article_html:
                logger.warning(f"Could not fetch article page: {article['link']}")
                continue
            content_html, summary, pub_date = extract_article_content(article_html, article["link"])
            article["content_html"] = content_html
            if summary:
                article["description"] = summary
            if pub_date:
                article["date"] = pub_date

        sorted_articles = _sort_articles(articles)
        feed = generate_rss_feed(sorted_articles, feed_name)
        save_rss_feed(feed, feed_name)
        logger.info(f"Successfully generated RSS feed with {len(sorted_articles)} items")
        return True
    except Exception as e:
        logger.error(f"Failed to generate Steve Jobs Archive Stories RSS: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Steve Jobs Archive Stories RSS feed.")
    parser.add_argument(
        "--feed-name",
        default="steve_jobs_archive_stories",
        help="Output feed name (feed_<name>.xml)",
    )
    args = parser.parse_args()
    main(feed_name=args.feed_name)
