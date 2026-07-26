import argparse
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import pytz
import requests
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

BASE_URL = "https://alignment.openai.com"
BLOG_URL = BASE_URL
FEED_NAME = "openai_alignment"

# Site-specific chrome inside div.content: the "back to index" link and the
# date/authors meta line (the feed carries the date in pubDate instead).
STRIP_SELECTORS = ("a.back", "div.meta")


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def fetch_page(url: str) -> str:
    """Fetch HTML content."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }
    logger.info(f"Fetching page: {url}")
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def fetch_article_page(url: str) -> str | None:
    """Fetch HTML for a single article page."""
    try:
        logger.debug(f"Fetching article page: {url}")
        return fetch_page(url)
    except Exception as e:
        logger.warning(f"Failed to fetch article page {url}: {e}")
        return None


def _prepare_container(container):
    """Site-specific fixups before the shared cleaner runs.

    Callout boxes (`div.tldr` "In Brief" summaries, `div.correspondence`
    contact lines) hold bare text with inline links; if the divs were simply
    unwrapped, those links would become standalone top-level anchors and get
    dropped as duplicates/CTAs. Convert them to blockquotes so they survive
    as one unit.
    """
    for div in container.find_all("div", class_=["tldr", "correspondence"]):
        div.name = "blockquote"
    return container


def extract_article_metadata(html: str, page_url: str) -> dict:
    """Extract article metadata from page: title, date, description, content."""
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    # Extract title from <h1>
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else None
    if title:
        result["title"] = title

    # Extract date from <div class="meta"> - format: "Dec 18, 2025 · Authors"
    meta_div = soup.select_one("div.meta")
    if meta_div:
        meta_text = meta_div.get_text(strip=True)
        # Extract date part (before the ·)
        date_part = meta_text.split("·")[0].strip()
        dt = _parse_date(date_part)
        if dt:
            result["date"] = dt

    # Find main content in <div class="content">
    content_container = soup.select_one("div.content")

    if content_container is not None:
        content_container = _prepare_container(content_container)
        result["content_html"] = clean_article_html(
            content_container, page_url, title=title, strip_selectors=STRIP_SELECTORS
        )

    description = extract_summary(soup, content_container, title=title)
    if description:
        result["description"] = description

    return result


def _parse_date(text: str) -> datetime | None:
    """Parse date string like 'Dec 18, 2025'."""
    if not text:
        return None
    text = text.strip()
    try:
        # Try dateutil parser first
        dt = dateparser.parse(text)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=pytz.UTC)
            return dt
    except Exception:
        pass

    # Try manual parsing for "Dec 18, 2025" format
    match = re.match(r"(\w+)\s+(\d+),\s+(\d{4})", text)
    if match:
        month_str, day_str, year_str = match.groups()
        try:
            dt = datetime.strptime(f"{month_str} {day_str} {year_str}", "%b %d %Y")
            return dt.replace(tzinfo=pytz.UTC)
        except ValueError:
            pass

    return None


def parse_blog_html(html_content: str) -> list[dict]:
    """Parse the Alignment Research Blog listing page."""
    soup = BeautifulSoup(html_content, "html.parser")
    articles = []
    seen = set()

    # Find all post links
    post_links = soup.select("a.post-link[href]")

    for link in post_links:
        try:
            href = link.get("href", "").strip()
            if not href:
                continue

            # Normalize to absolute link
            article_url = urljoin(BASE_URL + "/", href)
            if article_url in seen:
                continue
            seen.add(article_url)

            # Extract title
            title_elem = link.select_one("div.post-title")
            if not title_elem:
                continue
            title = title_elem.get_text(strip=True)
            if not title:
                continue

            # Extract subtitle/description
            subtitle_elem = link.select_one("div.post-subtitle")
            description = subtitle_elem.get_text(strip=True) if subtitle_elem else title

            # Extract date - format "Dec 18" (year added below if missing)
            date_elem = link.select_one("div.date")
            date_dt = None
            if date_elem:
                date_text = date_elem.get_text(strip=True)
                # Add current year if not present
                if date_text and "," not in date_text:
                    date_text = f"{date_text}, {datetime.now().year}"
                date_dt = _parse_date(date_text)
                # A listing date without a year that lands in the future must
                # belong to the previous year (e.g. "Dec 18" seen in July).
                if date_dt and date_dt > datetime.now(pytz.UTC) + timedelta(days=1):
                    date_dt = date_dt.replace(year=date_dt.year - 1)

            articles.append({
                "title": title,
                "link": article_url,
                "date": date_dt,
                "category": "Alignment Research",
                "description": description,
            })
        except Exception as e:
            logger.warning(f"Skipping an article due to parsing error: {e}")
            continue

    # Sort by date (newest first)
    articles.sort(key=lambda a: a["date"] or datetime.min.replace(tzinfo=pytz.UTC), reverse=True)

    logger.info(f"Parsed {len(articles)} articles from listing page")
    return articles


def generate_rss_feed(articles, feed_name: str = FEED_NAME):
    """Generate RSS feed from parsed articles."""
    fg = build_feed(
        title="OpenAI Alignment Research Blog",
        description="Informal updates from the OpenAI Alignment and Safety Systems teams",
        site_url=BASE_URL,
        feed_url=feed_self_url(f"feed_{feed_name}.xml"),
        items=articles,
        author={"name": "OpenAI Alignment and Safety Systems"},
    )
    logger.info("RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator, feed_name: str = FEED_NAME) -> Path:
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    save_feed(feed_generator, output_file)
    return output_file


def main(feed_name: str = FEED_NAME, force: bool = False) -> bool:
    try:
        feed_path = ensure_feeds_directory() / f"feed_{feed_name}.xml"
        if force:
            logger.info("Force mode: refetching content for all articles")
            existing_items, cache = [], {}
        else:
            existing_items, cache = load_cached_entries(feed_path)
        existing_by_link = {item["link"]: item for item in existing_items}

        html_content = fetch_page(BLOG_URL)
        articles = parse_blog_html(html_content)
        if not articles:
            logger.warning("No articles parsed. Selectors may need updating.")

        # Keep previously seen items that fell off the listing page.
        listing_links = {article["link"] for article in articles}
        articles.extend(
            item for link, item in existing_by_link.items() if link not in listing_links
        )

        # Fetch full content for articles we have not captured yet.
        to_fetch = [
            article
            for article in articles
            if not (cache.get(article["link"]) or {}).get("content_html")
        ]
        for article in articles:
            cached = cache.get(article["link"])
            if cached and cached.get("content_html"):
                article["content_html"] = cached["content_html"]
                if cached.get("description"):
                    article["description"] = cached["description"]

        logger.info(f"Fetching full content for {len(to_fetch)} of {len(articles)} articles...")
        for article in to_fetch:
            article_html = fetch_article_page(article["link"])
            if article_html:
                metadata = extract_article_metadata(article_html, article["link"])

                # Update article with extracted metadata
                if metadata.get("title"):
                    article["title"] = metadata["title"]
                if metadata.get("date"):
                    article["date"] = metadata["date"]
                if metadata.get("content_html"):
                    article["content_html"] = metadata["content_html"]
                if metadata.get("description"):
                    article["description"] = metadata["description"]
            else:
                logger.warning(f"Could not fetch content for {article['link']}")

        feed = generate_rss_feed(articles, feed_name)
        save_rss_feed(feed, feed_name)
        logger.info(f"Successfully generated RSS feed with {len(articles)} articles")
        return True
    except Exception as e:
        logger.error(f"Failed to generate OpenAI Alignment RSS: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate OpenAI Alignment Research Blog RSS feed.")
    parser.add_argument("--feed-name", default=FEED_NAME, help=f"Feed name suffix (default: {FEED_NAME})")
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    args = parser.parse_args()
    raise SystemExit(0 if main(feed_name=args.feed_name, force=args.force) else 1)
