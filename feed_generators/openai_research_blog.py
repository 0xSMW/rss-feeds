import argparse
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.utils import parsedate_to_datetime
import pytz
from feedgen.feed import FeedGenerator
import logging
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def setup_selenium_driver():
    """Set up Selenium WebDriver with undetected-chromedriver."""
    options = uc.ChromeOptions()
    options.add_argument("--headless")  # Ensure headless mode is enabled
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
    return uc.Chrome(options=options)

def build_requests_session() -> requests.Session:
    """Build a requests session with headers that mimic a real browser."""
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
            "Referer": "https://openai.com/",
        }
    )
    return session


def fetch_news_content_requests(url, session: requests.Session | None = None):
    """Fetch the HTML content via requests."""
    sess = session or build_requests_session()
    logger.info(f"Fetching content via requests: {url}")
    resp = sess.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text

def fetch_news_content_selenium(url):
    """Fetch the fully loaded HTML content of a webpage using Selenium."""
    driver = None
    try:
        logger.info(f"Fetching content from URL: {url}")
        driver = setup_selenium_driver()
        driver.get(url)

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href^='/index/']")))
        except Exception:
            logger.warning("Could not confirm OpenAI research items loaded, proceeding anyway...")

        html_content = driver.page_source
        logger.info("Successfully fetched HTML content")
        return html_content

    except Exception as e:
        logger.error(f"Error fetching content: {e}")
        raise
    finally:
        if driver:
            driver.quit()

def fetch_article_page_requests(url: str, session: requests.Session | None = None) -> str | None:
    """Fetch HTML for a single article page via requests."""
    sess = session or build_requests_session()
    try:
        resp = sess.get(url, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning(f"Requests fetch failed for {url} ({e})")
        return None


def fetch_articles_selenium(urls: list[str]) -> dict[str, str]:
    """Fetch article pages via a single Selenium session."""
    results: dict[str, str] = {}
    if not urls:
        return results
    driver = None
    try:
        driver = setup_selenium_driver()
        for url in urls:
            try:
                driver.get(url)
                html = driver.page_source
                results[url] = html
            except Exception as e:
                logger.warning(f"Selenium fetch failed for {url}: {e}")
    finally:
        if driver:
            driver.quit()
    return results


def fetch_article_selenium(url: str) -> str | None:
    """Fetch a single article page via Selenium."""
    driver = None
    try:
        driver = setup_selenium_driver()
        driver.get(url)
        return driver.page_source
    except Exception as e:
        logger.warning(f"Selenium fetch failed for {url}: {e}")
        return None
    finally:
        if driver:
            driver.quit()


def fetch_articles_selenium_parallel(urls: list[str], max_workers: int = 5) -> dict[str, str]:
    """Fetch article pages in parallel Selenium sessions."""
    results: dict[str, str] = {}
    if not urls:
        return results
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(fetch_article_selenium, url): url for url in urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                html = future.result()
            except Exception as e:
                logger.warning(f"Selenium fetch failed for {url}: {e}")
                continue
            if html:
                results[url] = html
    return results


def _clean_article_html(container, base_url: str) -> str:
    """Clean article container and keep only <p>, <a>, and <img> tags."""
    if container is None:
        return ""

    related_markers = (
        "related articles",
        "related posts",
        "more articles",
        "you might also like",
    )
    share_domains = (
        "linkedin.com/sharing",
        "facebook.com/sharer",
        "twitter.com/intent",
        "x.com/intent",
        "reddit.com/submit",
    )

    for el in container.find_all(["h2", "h3", "h4", "p", "div", "section", "aside"]):
        text = el.get_text(" ", strip=True).lower()
        if any(marker in text for marker in related_markers):
            parent = el.find_parent(["section", "aside", "div"]) or el
            parent.decompose()

    for tag in list(container.find_all(True)):
        if tag.parent is None:
            continue
        if tag.name == "a":
            href = tag.get("href", "")
            href_l = href.lower()
            if any(domain in href_l for domain in share_domains):
                tag.decompose()
                continue
            if not href:
                tag.decompose()
                continue
            if not href.startswith(("http://", "https://", "mailto:", "#")):
                tag["href"] = urljoin(base_url, href)
            if not tag.get_text(strip=True) and not tag.find("img"):
                tag.decompose()
                continue
            tag.attrs = {"href": tag["href"]}
            continue
        if tag.name == "img":
            if "src" in tag.attrs:
                src = tag["src"]
                if not src.startswith(("http://", "https://", "data:")):
                    tag["src"] = urljoin(base_url, src)
            else:
                tag.decompose()
                continue
            tag.attrs = {"src": tag["src"]}
            continue

        if tag.name == "p":
            if tag.find("img"):
                for img in tag.find_all("img"):
                    tag.insert_after(img)
            tag.attrs = {}
            continue

        tag.unwrap()

    parts: list[str] = []
    for tag in container.find_all(["p", "a", "img"], recursive=True):
        if tag.name == "p" and not tag.get_text(strip=True):
            continue
        parts.append(str(tag))

    return "\n".join(parts)


def extract_article_content(html: str, page_url: str) -> tuple[str, str]:
    """Extract main article content HTML and a plain-text summary."""
    soup = BeautifulSoup(html, "html.parser")
    container = (
        soup.select_one("article")
        or soup.select_one("main article")
        or soup.select_one("main")
        or soup.select_one("[class*='content']")
    )
    content_html = _clean_article_html(container, base_url=page_url)

    summary = ""
    if container:
        first_p = container.find("p")
        if first_p:
            summary = first_p.get_text(" ", strip=True)
    if not summary:
        summary = soup.title.get_text(strip=True) if soup.title else ""

    return content_html, summary

def parse_openai_news_html(html_content):
    """Parse the HTML content from OpenAI's Research News page.

    The page structure (as of 2025-09) renders each card as an <a href="/index/..."> element
    that contains:
      - title in a div with class token 'text-h5'
      - category in the first span inside p.text-meta
      - date in a <time> tag with ISO 'datetime' attribute
    """
    soup = BeautifulSoup(html_content, "html.parser")
    articles = []

    # Find anchors that link to individual posts
    news_items = soup.select("a[href^='/index/']")

    seen_links = set()
    for item in news_items:
        try:
            # Extract link
            href = item.get("href")
            if not href:
                continue
            link = "https://openai.com" + href
            if link in seen_links:
                continue

            # Extract title: robustly match any element whose class contains 'text-h5'
            title_elem = item.select_one("div.text-h5") or item.select_one("div[class*='text-h5']")
            if title_elem and title_elem.text.strip():
                title = title_elem.text.strip()
            else:
                # Fallback: derive from aria-label (format: "Title - Category - Mon d, YYYY")
                aria = item.get("aria-label", "").strip()
                title = aria.split(" - ")[0] if aria else None
            if not title:
                continue

            # Extract category
            cat_elem = item.select_one("p.text-meta span")
            category = (cat_elem.text.strip() if cat_elem and cat_elem.text else "Research")

            # Extract date from <time datetime="...">
            date_obj = None
            time_elem = item.select_one("time")
            if time_elem:
                dt_attr = time_elem.get("datetime", "").strip()
                if dt_attr:
                    try:
                        # Handle values like '2025-08-07T10:00' (no timezone) by assuming UTC
                        date_obj = datetime.fromisoformat(dt_attr)
                        if date_obj.tzinfo is None:
                            date_obj = date_obj.replace(tzinfo=pytz.UTC)
                    except Exception:
                        pass
            if date_obj is None:
                # Fallback to now (UTC) to avoid missing items
                logger.warning(f"Date not found or unparsable for: {title}; defaulting to now")
                date_obj = datetime.now(pytz.UTC)

            articles.append(
                {
                    "title": title,
                    "link": link,
                    "date": date_obj,
                    "category": category,
                    "description": title,
                }
            )
            seen_links.add(link)
        except Exception as e:
            logger.warning(f"Skipping an article due to parsing error: {e}")
            continue

    logger.info(f"Parsed {len(articles)} articles")
    return articles

def generate_rss_feed(articles, feed_name="openai_research"):
    """Generate RSS feed from parsed articles."""
    fg = FeedGenerator()
    fg.title("OpenAI Research News")
    fg.description("Latest research news and updates from OpenAI")
    fg.link(href="https://openai.com/news/research")
    fg.language("en")

    for article in articles:
        fe = fg.add_entry()
        fe.title(article["title"])
        fe.link(href=article["link"])
        fe.description(article["description"])
        if article.get("content_html"):
            fe.content(article["content_html"])
        fe.published(article["date"])
        fe.category(term=article["category"])

    logger.info("RSS feed generated successfully")
    return fg

def save_rss_feed(feed_generator, feed_name="openai_research"):
    """Save RSS feed to an XML file."""
    feeds_dir = Path("feeds")
    feeds_dir.mkdir(exist_ok=True)
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"RSS feed saved to {output_file}")
    return output_file


def get_existing_entries_from_feed(feed_path: Path):
    """Parse the existing RSS feed and return entries for reuse."""
    entries = []
    if not feed_path.exists():
        return entries
    try:
        tree = ET.parse(feed_path)
        root = tree.getroot()
        ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
        for item in root.findall("./channel/item"):
            link_elem = item.find("link")
            title_elem = item.find("title")
            desc_elem = item.find("description")
            content_elem = item.find("content:encoded", ns)
            date_elem = item.find("pubDate")
            category_elem = item.find("category")

            link = link_elem.text.strip() if link_elem is not None and link_elem.text else None
            if not link:
                continue

            date = None
            if date_elem is not None and date_elem.text:
                try:
                    date = parsedate_to_datetime(date_elem.text.strip())
                except Exception:
                    date = None

            entries.append(
                {
                    "title": title_elem.text.strip() if title_elem is not None and title_elem.text else link,
                    "link": link,
                    "date": date or datetime.now(pytz.UTC),
                    "category": category_elem.text.strip() if category_elem is not None and category_elem.text else "Research",
                    "description": desc_elem.text if desc_elem is not None and desc_elem.text else "",
                    "content_html": content_elem.text if content_elem is not None and content_elem.text else "",
                }
            )
    except Exception as e:
        logger.warning(f"Failed to parse existing feed entries: {str(e)}")
    return entries


def main(limit: int = 500, test_first: bool = False, force: bool = False) -> bool:
    """Main function to generate OpenAI Research News RSS feed."""
    url = "https://openai.com/news/research/"
    if limit:
        url = f"{url}?limit={limit}"

    try:
        feeds_dir = Path("feeds")
        feeds_dir.mkdir(exist_ok=True)
        feed_path = feeds_dir / "feed_openai_research.xml"

        existing_entries = []
        existing_links = set()
        if not force and not test_first:
            existing_entries = get_existing_entries_from_feed(feed_path)
            existing_links = {entry["link"] for entry in existing_entries}

        html_content = fetch_news_content_selenium(url)
        articles = parse_openai_news_html(html_content)

        if not articles:
            logger.warning("No articles were parsed. Check your selectors.")
            return False

        if test_first:
            article = articles[0]
            article_html = fetch_article_selenium(article["link"])
            if not article_html:
                logger.error("Failed to fetch first article content.")
                return False

            content_html, summary = extract_article_content(article_html, article["link"])
            print("TITLE:", article["title"])
            print("LINK:", article["link"])
            print("SUMMARY:", summary)
            print("CONTENT_SNIPPET:", content_html[:800])
            return True

        new_articles = [article for article in articles if article["link"] not in existing_links]
        urls = [article["link"] for article in new_articles]
        selenium_html = fetch_articles_selenium_parallel(urls, max_workers=5)
        for article in new_articles:
            html = selenium_html.get(article["link"])
            if not html:
                continue
            content_html, summary = extract_article_content(html, article["link"])
            if content_html:
                article["content_html"] = content_html
            if summary:
                article["description"] = summary

        combined_articles = new_articles + existing_entries
        seen_links = set()
        deduped_articles = []
        for article in combined_articles:
            if article["link"] in seen_links:
                continue
            seen_links.add(article["link"])
            deduped_articles.append(article)

        feed = generate_rss_feed(deduped_articles)
        save_rss_feed(feed)
    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {e}")
        return False
    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate OpenAI Research News RSS feed.")
    parser.add_argument("--limit", type=int, default=500, help="Listing page limit param")
    parser.add_argument(
        "--test-first",
        action="store_true",
        help="Fetch only the first article and print a content snippet.",
    )
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    args = parser.parse_args()
    raise SystemExit(0 if main(limit=args.limit, test_first=args.test_first, force=args.force) else 1)
