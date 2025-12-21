import argparse
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pytz
import requests
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from feedgen.feed import FeedGenerator

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://www.piratewires.com"
CATEGORY_PAGES = {
    "Home": BASE_URL,
    "Culture": f"{BASE_URL}/c/culture",
    "Politics": f"{BASE_URL}/c/politics",
    "Technology": f"{BASE_URL}/c/technology",
}


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
    return uc.Chrome(options=options)


def fetch_page_requests(url: str, session: requests.Session | None = None) -> str:
    sess = session or build_requests_session()
    logger.info(f"Fetching page (requests): {url}")
    resp = sess.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text


def _wait_for_article_links(driver, timeout: int = 15) -> None:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/p/']"))
    )


def fetch_page_selenium(url: str) -> str:
    driver = None
    try:
        logger.info(f"Fetching page (selenium): {url}")
        driver = setup_selenium_driver()
        driver.get(url)
        _wait_for_article_links(driver, timeout=20)

        # Scroll to load lazy content without fixed sleeps.
        from selenium.webdriver.support.ui import WebDriverWait

        last_height = driver.execute_script("return document.body.scrollHeight")
        for _ in range(5):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            try:
                WebDriverWait(driver, 5).until(
                    lambda d: d.execute_script("return document.body.scrollHeight") > last_height
                )
                last_height = driver.execute_script("return document.body.scrollHeight")
            except Exception:
                break

        return driver.page_source
    finally:
        if driver:
            driver.quit()


def fetch_page(url: str, session: requests.Session | None = None) -> str:
    try:
        return fetch_page_requests(url, session=session)
    except Exception as e:
        logger.warning(f"Requests fetch failed ({e}); falling back to Selenium...")
        return fetch_page_selenium(url)


def _normalize_article_link(href: str) -> str | None:
    if not href:
        return None
    if href.startswith("/p/"):
        return urljoin(BASE_URL, href)
    if href.startswith("http://") or href.startswith("https://"):
        parsed = urlparse(href)
        if parsed.netloc.endswith("piratewires.com") and parsed.path.startswith("/p/"):
            return href.split("?")[0].split("#")[0]
    return None


def parse_listing_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for a in soup.select("a[href]"):
        link = _normalize_article_link(a.get("href", "").strip())
        if link:
            links.append(link)
    # Deduplicate while preserving order
    seen = set()
    unique_links = []
    for link in links:
        if link in seen:
            continue
        seen.add(link)
        unique_links.append(link)
    return unique_links


def _parse_date(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = dateparser.parse(text.strip())
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.UTC)
        return dt
    except Exception:
        return None


def fetch_article_page_requests(url: str, session: requests.Session | None = None) -> str | None:
    sess = session or build_requests_session()
    try:
        resp = sess.get(url, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning(f"Requests fetch failed for {url}: {e}")
        return None


def fetch_articles_selenium(urls: list[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    if not urls:
        return results
    driver = None
    try:
        driver = setup_selenium_driver()
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        for url in urls:
            try:
                driver.get(url)
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "section[class*='article_postBody']"))
                )
                results[url] = driver.page_source
            except Exception as e:
                logger.warning(f"Selenium fetch failed for {url}: {e}")
    finally:
        if driver:
            driver.quit()
    return results


def _clean_article_html(container, base_url: str) -> str:
    if container is None:
        return ""

    for tag in container.select(
        "script, style, noscript, svg, form, iframe, "
        "input, canvas, link, button, select, textarea, nav, header, footer"
    ):
        tag.decompose()

    for tag in list(container.find_all(True)):
        if tag.parent is None:
            continue
        if tag.name == "a":
            href = tag.get("href", "")
            if not href:
                tag.decompose()
                continue
            if not href.startswith(("http://", "https://", "mailto:", "#")):
                tag["href"] = urljoin(base_url, href)
            tag.attrs = {"href": tag["href"]}
            continue
        if tag.name == "img":
            src = tag.get("src")
            if not src:
                tag.decompose()
                continue
            if not src.startswith(("http://", "https://", "data:")):
                tag["src"] = urljoin(base_url, src)
            tag.attrs = {"src": tag["src"], "alt": tag.get("alt", "")}
            continue
        if tag.name == "p":
            tag.attrs = {}
            continue

        if tag.name in {"h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong", "br", "hr"}:
            tag.attrs = {}
            continue

        tag.unwrap()

    parts: list[str] = []
    for tag in container.find_all(
        ["p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong", "a", "img", "br", "hr"],
        recursive=True,
    ):
        if tag.name == "p" and not tag.get_text(strip=True) and not tag.find("img"):
            continue
        parts.append(str(tag))

    return "\n".join(parts)


def extract_article_metadata(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, str | datetime] = {}

    title_el = soup.find("h1")
    if title_el:
        result["title"] = title_el.get_text(strip=True)

    excerpt_el = soup.select_one("[class*='article_excerpt']")
    if excerpt_el:
        result["description"] = excerpt_el.get_text(" ", strip=True)

    author_el = soup.select_one("[class*='article_bottom'] a")
    if author_el:
        result["author"] = author_el.get_text(strip=True)

    date_el = soup.select_one("[class*='article_bottom'] p")
    if date_el:
        parsed = _parse_date(date_el.get_text(strip=True))
        if parsed:
            result["date"] = parsed

    content_container = soup.select_one("section[class*='article_postBody']")
    if content_container:
        content_html = _clean_article_html(content_container, base_url=page_url)
        result["content_html"] = content_html

        if "description" not in result:
            first_p = content_container.find("p")
            if first_p:
                result["description"] = first_p.get_text(" ", strip=True)

    return result


def collect_listing_articles(session: requests.Session | None = None) -> list[dict]:
    articles: list[dict] = []
    seen_links = set()

    for category, url in CATEGORY_PAGES.items():
        html = fetch_page(url, session=session)
        links = parse_listing_links(html)
        if not links:
            logger.warning(f"No links found via requests for {url}; retrying with Selenium...")
            html = fetch_page_selenium(url)
            links = parse_listing_links(html)

        for link in links:
            if link in seen_links:
                continue
            seen_links.add(link)
            articles.append(
                {
                    "title": link,
                    "link": link,
                    "date": None,
                    "category": category,
                    "description": "",
                }
            )

        logger.info(f"Collected {len(links)} links from {category}")

    return articles


def generate_rss_feed(articles, feed_name: str = "piratewires"):
    fg = FeedGenerator()
    fg.title("Pirate Wires")
    fg.description("Pirate Wires - Culture, Politics, and Technology")
    fg.link(href=BASE_URL)
    fg.language("en")

    for article in reversed(articles):
        fe = fg.add_entry()
        fe.title(article["title"])
        fe.link(href=article["link"])
        fe.description(article.get("description") or article["title"])
        if article.get("content_html"):
            fe.content(article["content_html"])
        if article.get("date"):
            fe.published(article["date"])
        fe.category(term=article.get("category") or "Pirate Wires")
        if article.get("author"):
            fe.author({"name": article["author"]})
        fe.id(article["link"])

    logger.info("RSS feed generated successfully")
    return fg


def save_rss_feed(feed_generator, feed_name: str = "piratewires") -> Path:
    feeds_dir = Path("feeds")
    feeds_dir.mkdir(exist_ok=True)
    output_file = feeds_dir / f"feed_{feed_name}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"RSS feed saved to {output_file}")
    return output_file


def get_existing_entries_from_feed(feed_path: Path):
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
            author_elem = item.find("author")

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
                    "category": category_elem.text.strip() if category_elem is not None and category_elem.text else "Pirate Wires",
                    "author": author_elem.text.strip() if author_elem is not None and author_elem.text else None,
                    "description": desc_elem.text if desc_elem is not None and desc_elem.text else "",
                    "content_html": content_elem.text if content_elem is not None and content_elem.text else "",
                }
            )
    except Exception as e:
        logger.warning(f"Failed to parse existing feed entries: {str(e)}")
    return entries


def main(force: bool = False) -> bool:
    try:
        feeds_dir = Path("feeds")
        feeds_dir.mkdir(exist_ok=True)
        feed_path = feeds_dir / "feed_piratewires.xml"

        existing_entries = []
        existing_links = set()
        if not force:
            existing_entries = get_existing_entries_from_feed(feed_path)
            existing_links = {entry["link"] for entry in existing_entries}

        session = build_requests_session()
        articles = collect_listing_articles(session=session)
        if not articles:
            logger.warning("No Pirate Wires articles parsed. Selectors may need updating.")
            return False

        new_articles = [article for article in articles if article["link"] not in existing_links]
        logger.info(f"Fetching full content for {len(new_articles)} articles...")

        needs_selenium = []
        for article in new_articles:
            html = fetch_article_page_requests(article["link"], session=session)
            if not html:
                needs_selenium.append(article["link"])
                continue

            metadata = extract_article_metadata(html, article["link"])
            if metadata.get("content_html"):
                article["content_html"] = metadata["content_html"]
            else:
                needs_selenium.append(article["link"])

            for key in ("title", "description", "date", "author"):
                if metadata.get(key):
                    article[key] = metadata[key]

        if needs_selenium:
            selenium_html = fetch_articles_selenium(needs_selenium)
            for article in new_articles:
                if article["link"] not in selenium_html:
                    continue
                metadata = extract_article_metadata(selenium_html[article["link"]], article["link"])
                for key in ("title", "description", "date", "author", "content_html"):
                    if metadata.get(key):
                        article[key] = metadata[key]

        combined_articles = new_articles + existing_entries
        seen_links = set()
        deduped_articles = []
        for article in combined_articles:
            if article["link"] in seen_links:
                continue
            seen_links.add(article["link"])
            deduped_articles.append(article)

        def sort_key(a):
            if a.get("date"):
                return (0, a["date"])
            return (1, datetime.min.replace(tzinfo=pytz.UTC))

        deduped_articles.sort(key=sort_key, reverse=True)

        feed = generate_rss_feed(deduped_articles)
        save_rss_feed(feed)
    except Exception as e:
        logger.error(f"Failed to generate Pirate Wires RSS feed: {e}")
        return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Pirate Wires RSS feed.")
    parser.add_argument("--force", action="store_true", help="Refetch all articles and rebuild the feed.")
    args = parser.parse_args()
    raise SystemExit(0 if main(force=args.force) else 1)
