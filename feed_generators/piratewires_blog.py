import argparse
import logging
import time
import os
import re
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


def in_ci() -> bool:
    return os.environ.get("CI", "").lower() == "true"


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
        if in_ci():
            raise
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


def _decode_payload_text(value: str) -> str:
    if not value:
        return value
    try:
        return value.encode("utf-8").decode("unicode_escape")
    except Exception:
        return value


def parse_listing_payload(html: str, category: str) -> list[dict]:
    items: list[dict] = []
    seen_links = set()
    needle = "\\\"canonical_url\\\":\\\"https://piratewires.substack.com/p/"
    title_key = "\\\"title\\\":\\\""
    subtitle_key = "\\\"subtitle\\\":\\\""
    date_key = "\\\"post_date\\\":\\\""

    idx = 0
    while True:
        idx = html.find(needle, idx)
        if idx == -1:
            break

        slug_start = idx + len(needle)
        slug_end = html.find("\\\"", slug_start)
        if slug_end == -1:
            break

        window_start = max(idx - 2000, 0)
        window = html[window_start:idx]

        title_pos = window.rfind(title_key)
        subtitle_pos = window.rfind(subtitle_key)
        date_pos = window.rfind(date_key)
        if title_pos == -1 or subtitle_pos == -1 or date_pos == -1:
            idx = slug_end + 1
            continue

        title_start = title_pos + len(title_key)
        subtitle_start = subtitle_pos + len(subtitle_key)
        date_start = date_pos + len(date_key)

        title_end = window.find("\\\"", title_start)
        subtitle_end = window.find("\\\"", subtitle_start)
        date_end = window.find("\\\"", date_start)
        if title_end == -1 or subtitle_end == -1 or date_end == -1:
            idx = slug_end + 1
            continue

        title = _decode_payload_text(window[title_start:title_end])
        subtitle = _decode_payload_text(window[subtitle_start:subtitle_end])
        post_date = _decode_payload_text(window[date_start:date_end])
        slug = html[slug_start:slug_end]
        link = f"{BASE_URL}/p/{slug}"
        if link in seen_links:
            idx = slug_end + 1
            continue
        seen_links.add(link)
        items.append(
            {
                "title": title,
                "link": link,
                "date": _parse_date(post_date),
                "category": category,
                "description": subtitle,
            }
        )
        idx = slug_end + 1

    return items


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

        total = len(urls)
        start_time = time.perf_counter()
        for idx, url in enumerate(urls, start=1):
            try:
                logger.info(f"Selenium fetch {idx}/{total}: {url}")
                driver.get(url)
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "section[class*='article_postBody']"))
                )
                try:
                    WebDriverWait(driver, 20).until(
                        lambda d: d.execute_script(
                            "const el=document.querySelector(\"section[class*='article_postBody']\");"
                            "return el ? el.innerText.length : 0;"
                        )
                        > 1500
                    )
                except Exception:
                    pass
                page_source = driver.page_source
                results[url] = page_source
                elapsed = time.perf_counter() - start_time
                logger.info(
                    f"Selenium fetched {idx}/{total} in {elapsed:.1f}s (html {len(page_source)} chars)"
                )
            except Exception as e:
                logger.warning(f"Selenium fetch failed for {url}: {e}")
    finally:
        if driver:
            driver.quit()
    return results


def _clean_article_html(container, base_url: str) -> str:
    from bs4.element import Tag

    if container is None or not isinstance(container, Tag):
        return ""

    footer_markers = (
        "enjoying this story",
        "sign up for free",
        "already have an account",
        "sign in",
    )
    for text_node in list(container.find_all(string=True)):
        text = text_node.strip().lower()
        if not text:
            continue
        if any(marker in text for marker in footer_markers):
            parent = getattr(text_node, "parent", None)
            if parent is None:
                continue
            wrapper = parent.find_parent(["section", "div", "aside"]) or parent
            wrapper.decompose()

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
    needs_selenium = False

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
        paragraphs = [
            p.get_text(" ", strip=True)
            for p in content_container.find_all("p")
            if p.get_text(strip=True)
        ]
        if len(paragraphs) < 6 or len(" ".join(paragraphs)) < 1200:
            needs_selenium = True

        content_html = _clean_article_html(content_container, base_url=page_url)
        result["content_html"] = content_html

        if "description" not in result:
            first_p = content_container.find("p")
            if first_p:
                result["description"] = first_p.get_text(" ", strip=True)

    if needs_selenium:
        result["needs_selenium"] = True

    return result


def collect_listing_articles(session: requests.Session | None = None) -> list[dict]:
    articles: list[dict] = []
    seen_links = set()

    for category, url in CATEGORY_PAGES.items():
        html = fetch_page_requests(url, session=session)
        payload_items = parse_listing_payload(html, category)
        links = parse_listing_links(html)

        if not payload_items and not links and not in_ci():
            logger.warning(f"No links found via requests for {url}; retrying with Selenium...")
            html = fetch_page_selenium(url)
            payload_items = parse_listing_payload(html, category)
            links = parse_listing_links(html)

        for item in payload_items:
            link = item["link"]
            if link in seen_links:
                continue
            seen_links.add(link)
            articles.append(item)

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

        logger.info(
            f"Collected {len(payload_items)} payload items and {len(links)} links from {category}"
        )

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
            needs_refresh = False
            if "piratewires.substack.com/p/" in link:
                slug = link.split("/p/")[-1]
                link = f"{BASE_URL}/p/{slug}"
                needs_refresh = True

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
                    "needs_refresh": needs_refresh,
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
            existing_links = {
                entry["link"] for entry in existing_entries if not entry.get("needs_refresh")
            }

        session = build_requests_session()
        articles = collect_listing_articles(session=session)
        if not articles:
            logger.warning("No Pirate Wires articles parsed. Selectors may need updating.")
            return False

        new_articles = [article for article in articles if article["link"] not in existing_links]
        logger.info(f"Fetching full content for {len(new_articles)} articles...")

        needs_selenium: list[str] = []
        for article in new_articles:
            html = fetch_article_page_requests(article["link"], session=session)
            if not html:
                needs_selenium.append(article["link"])
                continue
            metadata = extract_article_metadata(html, article["link"])
            for key in ("title", "description", "date", "author", "content_html"):
                if metadata.get(key):
                    article[key] = metadata[key]
            if metadata.get("needs_selenium"):
                needs_selenium.append(article["link"])

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
