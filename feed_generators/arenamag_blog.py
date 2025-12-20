import requests
import time
import undetected_chromedriver as uc
import re
from bs4 import BeautifulSoup
from datetime import datetime
from dateutil import parser as dateparser
import pytz
from feedgen.feed import FeedGenerator
import logging
from pathlib import Path
from urllib.parse import urljoin

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


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def fetch_page_requests(url: str) -> str:
    """Fetch HTML using requests."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    logger.info(f"Fetching page (requests): {url}")
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

        html = driver.page_source
        return html
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


def _clean_article_html(container, base_url: str) -> str:
    """Clean article container and absolutize links/media."""
    if container is None:
        return ""

    # Remove noisy elements
    for tag in container.select(
        "script, style, noscript, svg, form, iframe, "
        "input, canvas, link, button, select, textarea, nav, header, footer"
    ):
        tag.decompose()

    # Remove elements with noisy class/id patterns
    noisy_patterns = [
        "share", "social", "breadcrumb", "nav", "header", "footer",
        "subscribe", "newsletter", "related", "sidebar", "menu",
        "comment", "ad", "promo", "cta", "signup",
    ]
    noisy_re = re.compile("|".join([re.escape(p) for p in noisy_patterns]), re.IGNORECASE)
    for el in container.find_all(True):
        cid = (el.get("id") or "") + " " + " ".join(el.get("class", []))
        if cid and noisy_re.search(cid):
            if not el.find(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "img", "pre", "blockquote", "table"]):
                el.decompose()

    # Make links and media absolute
    for a in container.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(("http://", "https://", "mailto:", "#")):
            a["href"] = urljoin(base_url, href)
    for img in container.find_all("img", src=True):
        src = img["src"]
        if not src.startswith(("http://", "https://", "data:")):
            img["src"] = urljoin(base_url, src)

    return str(container)


def extract_article_content(html: str, page_url: str) -> tuple[str, str]:
    """Extract main article content HTML and a plain-text summary."""
    soup = BeautifulSoup(html, "html.parser")

    # Try to find the main article content
    candidates = [
        soup.select_one("article"),
        soup.select_one("main article"),
        soup.select_one("[class*='article']"),
        soup.select_one("[class*='content']"),
        soup.select_one("[class*='post']"),
        soup.select_one("[class*='prose']"),
        soup.select_one("main"),
    ]
    container = next((c for c in candidates if c), None)
    if container is None:
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


def parse_category_page(html_content: str, category_name: str) -> list[dict]:
    """Parse an Arena Magazine category page to extract articles."""
    soup = BeautifulSoup(html_content, "html.parser")
    articles = []
    seen = set()

    # Find all article links
    anchors = soup.select("a[href]")
    
    for a in anchors:
        try:
            href = a.get("href", "").strip()
            if not href:
                continue

            # Normalize to absolute link
            link = urljoin(BASE_URL, href)

            # Skip category pages, home page, and non-article links
            if link in seen:
                continue
            if link.rstrip("/") == BASE_URL.rstrip("/"):
                continue
            # Skip known non-article paths
            skip_paths = ["/technology", "/capitalism", "/science", "/civilization", 
                          "/greatness", "/store", "/issues", "/authors", "/masthead",
                          "/subscribe", "/careers", "/sign-in"]
            if any(link.rstrip("/").endswith(p) for p in skip_paths):
                continue
            # Must be an arenamag.com link
            if not link.startswith(BASE_URL):
                continue

            # Extract title from anchor text or nested elements
            title_elem = a.find(["h1", "h2", "h3", "h4", "h5", "h6"])
            title = (title_elem.get_text(strip=True) if title_elem else None)
            if not title:
                title = a.get_text(" ", strip=True)
            
            # Clean up title - remove author suffix like "byMaxwell Meyer"
            title = re.sub(r'\s*by[A-Z].*$', '', title)
            title = title.strip()
            
            if not title or len(title) < 3:
                continue
            # Skip if it looks like a navigation element
            if title.lower() in ["subscribe", "sign in", "store", "issues", "authors", "masthead", "careers"]:
                continue

            # Extract author from anchor text if present
            full_text = a.get_text(" ", strip=True)
            author = None
            author_match = re.search(r'by([A-Z][a-zA-Z\s•·]+?)$', full_text)
            if author_match:
                author = author_match.group(1).strip()
                # Clean up author - replace bullet characters
                author = author.replace("•", " & ").replace("·", " & ")

            # Try to find date - Arena mag articles may have dates in metadata or nearby
            date_dt = None
            # Look for time element
            time_el = a.find("time")
            if not time_el and a.parent:
                time_el = a.parent.find("time")
            if time_el:
                dt_attr = (time_el.get("datetime") or time_el.get_text(strip=True) or "").strip()
                date_dt = _parse_date(dt_attr)

            articles.append({
                "title": title,
                "link": link,
                "date": date_dt,
                "category": category_name,
                "author": author,
                "description": title,
            })
            seen.add(link)
        except Exception as e:
            logger.warning(f"Skipping an item due to parsing error: {e}")
            continue

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
                # Keep the first category found
        except Exception as e:
            logger.error(f"Failed to fetch category {url}: {e}")
            continue
    
    combined = list(by_link.values())
    
    # Sort by date (newest first), articles without dates go last
    def sort_key(a):
        if a.get("date"):
            return (0, a["date"])
        return (1, datetime.min.replace(tzinfo=pytz.UTC))
    
    combined.sort(key=sort_key, reverse=True)
    
    logger.info(f"Collected {len(combined)} unique articles across all categories")
    return combined


def generate_rss_feed(articles, feed_name: str = "arenamag"):
    """Generate RSS feed from parsed articles."""
    fg = FeedGenerator()
    fg.title("Arena Magazine")
    fg.description("Technology, Capitalism, Science, Civilization, and Greatness - Arena Magazine")
    fg.link(href=BASE_URL)
    fg.language("en")

    fg.author({"name": "Arena Magazine"})
    fg.link(href=BASE_URL, rel="alternate")

    # feedgen prepends entries, so iterate in reverse to get newest-first in output
    for article in reversed(articles):
        fe = fg.add_entry()
        fe.title(article["title"])
        fe.link(href=article["link"])
        
        content_html = article.get("content_html")
        summary = article.get("description", article["title"]) or article["title"]
        
        if content_html:
            fe.content(content_html)
        fe.description(summary)
        
        if article.get("date"):
            fe.published(article["date"])
        
        fe.category(term=article["category"])
        
        if article.get("author"):
            fe.author({"name": article["author"]})
        
        fe.id(article["link"])

    logger.info("RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator, feed_name: str = "arenamag") -> Path:
    feeds_dir = ensure_feeds_directory()
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"RSS feed saved to {output_file}")
    return output_file


def main(feed_name: str = "arenamag") -> bool:
    try:
        articles = collect_all_articles()
        if not articles:
            logger.warning("No Arena Magazine articles parsed. Selectors may need updating.")
        
        # Fetch full content for each article
        logger.info(f"Fetching full content for {len(articles)} articles...")
        for article in articles:
            article_html = fetch_article_page(article["link"])
            if article_html:
                content_html, summary = extract_article_content(article_html, article["link"])
                article["content_html"] = content_html
                if summary and summary.strip() and len(summary) > len(article.get("description", "")):
                    article["description"] = summary
            else:
                logger.warning(f"Could not fetch content for {article['link']}")
        
        feed = generate_rss_feed(articles, feed_name)
        save_rss_feed(feed, feed_name)
        logger.info(f"Successfully generated RSS feed with {len(articles)} articles")
        return True
    except Exception as e:
        logger.error(f"Failed to generate Arena Magazine RSS: {e}")
        return False


if __name__ == "__main__":
    main()

