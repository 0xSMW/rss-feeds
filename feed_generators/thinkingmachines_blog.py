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

BASE_URL = "https://thinkingmachines.ai"
BLOG_URL = f"{BASE_URL}/blog"


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def fetch_blog_content_requests(url: str = BLOG_URL) -> str:
    """Fetch HTML for Thinking Machines blog page using requests (no JS)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }
    logger.info(f"Fetching Thinking Machines blog page (requests): {url}")
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


def fetch_blog_content_selenium(url: str = BLOG_URL) -> str:
    """Fetch fully rendered HTML for Thinking Machines blog page using Selenium."""
    driver = None
    try:
        driver = setup_selenium_driver()
        logger.info(f"Fetching Thinking Machines blog page (selenium): {url}")
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


def fetch_blog_content(url: str = BLOG_URL) -> str:
    """Fetch the HTML content, with Selenium fallback if blocked."""
    try:
        return fetch_blog_content_requests(url)
    except Exception as e:
        logger.warning(f"Requests fetch failed ({e}); falling back to Selenium...")
        return fetch_blog_content_selenium(url)


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
        logger.debug(f"Fetching article page: {url}")
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning(f"Failed to fetch article page {url}: {e}")
        # Try with Selenium as fallback
        try:
            driver = setup_selenium_driver()
            driver.get(url)
            time.sleep(3)
            html = driver.page_source
            driver.quit()
            return html
        except Exception as e2:
            logger.warning(f"Selenium fallback also failed for {url}: {e2}")
            return None


def extract_article_content(html: str, page_url: str, title: str | None = None) -> tuple[str, str]:
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

    # Site-specific: Cloudflare email obfuscation placeholders.
    if container is not None:
        for cf_email in container.select("span.__cf_email__, [data-cfemail]"):
            cf_email.replace_with("[email protected]")
        # Tufte-style margin sidenotes are inlined once unwrapped; make them
        # read as parentheticals instead of gluing onto the preceding word.
        for sidenote in container.select("span.sidenote"):
            sidenote.insert(0, " (")
            sidenote.append(") ")

    content_html = clean_article_html(container, page_url, title=title)
    summary = extract_summary(soup, container, title=title)
    return content_html, summary


def _parse_date(text: str) -> datetime:
    """Parse date string from Thinking Machines blog format (e.g., 'Dec 12', 'Nov 7').
    
    The blog shows dates like "Dec 12", "Nov 7" without a year. We'll assume
    the current year unless the date would be in the future, in which case
    we use the previous year.
    """
    if not text:
        return datetime.now(pytz.UTC)

    text = text.strip()
    
    # Try ISO format first
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.UTC)
        return dt
    except Exception:
        pass

    # Common blog formats with year
    fmts = [
        "%B %d, %Y",  # January 02, 2025
        "%b %d, %Y",  # Jan 02, 2025
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
        "%b %d %Y",  # Dec 12 2024
        "%B %d %Y",  # December 12 2024
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=pytz.UTC)
        except Exception:
            continue

    # Handle short format without year (e.g., "Dec 12", "Nov 7")
    # Try to parse and infer the year
    short_fmts = [
        "%b %d",  # Dec 12
        "%B %d",  # December 12
    ]
    for fmt in short_fmts:
        try:
            dt = datetime.strptime(text, fmt)
            # Assume current year, but if the date would be in the future, use previous year
            now = datetime.now(pytz.UTC)
            dt = dt.replace(year=now.year, tzinfo=pytz.UTC)
            if dt > now:
                dt = dt.replace(year=now.year - 1)
            return dt
        except Exception:
            continue

    logger.warning(f"Unrecognized date format: {text!r}; defaulting to now")
    return datetime.now(pytz.UTC)


def parse_blog_html(html_content: str):
    """Parse the Thinking Machines blog listing to extract articles.
    
    Based on the blog structure, articles appear to be listed with:
    - Date (e.g., "Dec 12")
    - Title
    - Author info (e.g., "Thinking Machines Lab")
    """
    soup = BeautifulSoup(html_content, "html.parser")
    articles = []
    seen = set()

    # Look for article links - try various selectors
    # The blog likely has links to individual posts
    anchors = soup.select("a[href]")
    
    for a in anchors:
        try:
            href = a.get("href", "").strip()
            if not href:
                continue

            # Normalize absolute link
            link = urljoin(BASE_URL, href)

            # Select only individual blog posts (not the listing page itself)
            if "/blog/" not in link or link.rstrip("/") == BLOG_URL.rstrip("/"):
                continue
            if link in seen:
                continue

            # Title: look for heading elements or text within the anchor
            title_elem = a.find(["h1", "h2", "h3", "h4", "h5", "h6"]) or a.select_one("[class*='title'], [class*='heading']")
            title = (title_elem.get_text(strip=True) if title_elem else None)
            if not title:
                # Fallbacks: aria-label, then anchor text
                title = a.get("aria-label") or a.get_text(" ", strip=True)
            title = (title or "").strip()
            if not title or len(title) < 3:
                continue

            # Date: look for date text near the anchor
            # The blog shows dates like "Dec 12" or "Nov 7"
            date_dt = None
            
            # Try <time> element first
            time_el = a.find("time")
            if not time_el and a.parent:
                time_el = a.parent.find("time")
            if time_el:
                dt_attr = (time_el.get("datetime") or "").strip()
                text_val = time_el.get_text(strip=True)
                date_dt = _parse_date(dt_attr or text_val)
            
            # If no time element, look for date-like text patterns
            if date_dt is None:
                # Look in the anchor and its parent for date patterns
                search_elements = [a]
                if a.parent:
                    search_elements.append(a.parent)
                if a.parent and a.parent.parent:
                    search_elements.append(a.parent.parent)
                
                # Regex for month abbreviation + day (e.g., "Dec 12", "Nov 7")
                month_pattern = (
                    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
                    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
                )
                date_re = re.compile(rf"\b{month_pattern}\s+\d{{1,2}}\b", re.IGNORECASE)
                
                for elem in search_elements:
                    text = elem.get_text(" ", strip=True)
                    match = date_re.search(text)
                    if match:
                        date_dt = _parse_date(match.group(0))
                        break
            
            if not date_dt:
                date_dt = datetime.now(pytz.UTC)

            # Category: default to "Research" for this research blog
            category = "Research"

            description = title

            articles.append({
                "title": title,
                "link": link,
                "date": date_dt,
                "category": category,
                "description": description,
            })
            seen.add(link)
        except Exception as e:
            logger.warning(f"Skipping an item due to parsing error: {e}")
            continue

    # Sort by date descending
    articles.sort(key=lambda x: x.get("date") or datetime.min.replace(tzinfo=pytz.UTC), reverse=True)
    
    logger.info(f"Parsed {len(articles)} Thinking Machines blog articles")
    return articles


def generate_rss_feed(articles, feed_name: str = "thinkingmachines"):
    """Generate RSS feed from parsed articles."""
    fg = build_feed(
        title="Thinking Machines Blog",
        description="Shared science and news from the Thinking Machines team",
        site_url=BLOG_URL,
        feed_url=feed_self_url(f"feed_{feed_name}.xml"),
        items=articles,
        author={"name": "Thinking Machines Lab"},
    )
    logger.info("RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator, feed_name: str = "thinkingmachines") -> Path:
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    save_feed(feed_generator, output_file)
    return output_file


def main(feed_name: str = "thinkingmachines", force: bool = False) -> bool:
    try:
        feeds_dir = ensure_feeds_directory()
        feed_path = feeds_dir / f"feed_{feed_name}.xml"

        existing_entries = []
        existing_links = set()
        if not force:
            existing_entries, _cache = load_cached_entries(feed_path)
            existing_links = {entry["link"] for entry in existing_entries}

        html_content = fetch_blog_content(BLOG_URL)
        articles = parse_blog_html(html_content)
        if not articles:
            logger.warning("No Thinking Machines blog articles parsed. Selectors may need updating.")
        
        # Fetch full content for each article
        new_articles = [article for article in articles if article["link"] not in existing_links]
        logger.info(f"Fetching full content for {len(new_articles)} articles...")
        for article in new_articles:
            article_html = fetch_article_page(article["link"])
            if article_html:
                content_html, summary = extract_article_content(
                    article_html, article["link"], title=article.get("title")
                )
                article["content_html"] = content_html
                if summary and summary.strip() and len(summary) > len(article.get("description", "")):
                    article["description"] = summary
            else:
                logger.warning(f"Could not fetch content for {article['link']}")
        
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
        logger.info(f"Successfully generated RSS feed with {len(deduped_articles)} articles")
        return True
    except Exception as e:
        logger.error(f"Failed to generate Thinking Machines blog RSS: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Thinking Machines RSS feed.")
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    args = parser.parse_args()
    main(force=args.force)
