import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
from feedgen.feed import FeedGenerator
import time
import logging
from pathlib import Path

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

def fetch_news_content_selenium(url):
    """Fetch the fully loaded HTML content of a webpage using Selenium."""
    driver = None
    try:
        logger.info(f"Fetching content from URL: {url}")
        driver = setup_selenium_driver()
        driver.get(url)

        # Log wait time
        wait_time = 5
        logger.info(f"Waiting {wait_time} seconds for the page to fully load...")
        time.sleep(wait_time)

        html_content = driver.page_source
        logger.info("Successfully fetched HTML content")
        return html_content

    except Exception as e:
        logger.error(f"Error fetching content: {e}")
        raise
    finally:
        if driver:
            driver.quit()

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

def main():
    """Main function to generate OpenAI Research News RSS feed."""
    url = "https://openai.com/news/research/?limit=500"

    try:
        html_content = fetch_news_content_selenium(url)
        articles = parse_openai_news_html(html_content)
        if not articles:
            logger.warning("No articles were parsed. Check your selectors.")
        feed = generate_rss_feed(articles)
        save_rss_feed(feed)
    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {e}")

if __name__ == "__main__":
    main()
