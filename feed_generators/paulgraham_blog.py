import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
from feedgen.feed import FeedGenerator
import logging
from pathlib import Path

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent


def ensure_feeds_directory():
    """Ensure the feeds directory exists."""
    feeds_dir = get_project_root() / "feeds"
    feeds_dir.mkdir(exist_ok=True)
    return feeds_dir


def fetch_html_content(url):
    """Fetch HTML content from the given URL."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        logger.error(f"Error fetching content from {url}: {str(e)}")
        raise


def get_article_description(article_url):
    """Fetch and extract the first paragraph of the article as description."""
    try:
        html_content = fetch_html_content(article_url)
        soup = BeautifulSoup(html_content, "html.parser")

        # Find the first text content after the title
        paragraphs = soup.find_all(["p", "div", "font"])
        for p in paragraphs:
            text = p.get_text().strip()
            if text and len(text) > 50:  # Ensure it's a substantial paragraph
                return text[:500] + "..." if len(text) > 500 else text

        return "No description available"
    except Exception as e:
        logger.error(f"Error fetching description for {article_url}: {str(e)}")
        return "Description unavailable"


def parse_essays_page(html_content, base_url="https://paulgraham.com"):
    """Parse the essays HTML page and extract blog post information."""
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        blog_posts = []

        # Find all essay links
        links = soup.select('font[size="2"] a')

        for link in links:
            # Extract title and link
            title = link.text.strip()
            href = link.get("href")
            if not href:
                continue

            full_url = f"{base_url}/{href}" if not href.startswith("http") else href

            # Get description from the article page
            description = get_article_description(full_url)

            # Since exact dates aren't available on the main page,
            # we'll set the date to None and sort by order of appearance
            blog_posts.append({"title": title, "link": full_url, "description": description})

        logger.info(f"Successfully parsed {len(blog_posts)} blog posts")
        return blog_posts

    except Exception as e:
        logger.error(f"Error parsing HTML content: {str(e)}")
        raise


def generate_rss_feed(blog_posts, feed_name="paulgraham"):
    """Generate RSS feed from blog posts."""
    try:
        fg = FeedGenerator()
        fg.title("Paul Graham Essays")
        fg.description("Essays by Paul Graham")
        fg.link(href="https://paulgraham.com/articles.html")
        fg.language("en")

        # Set feed metadata
        fg.author({"name": "Paul Graham"})
        fg.subtitle("Paul Graham's Essays and Writings")
        fg.link(href="https://paulgraham.com/articles.html", rel="alternate")
        fg.link(href=f"https://paulgraham.com/feed_{feed_name}.xml", rel="self")

        # Add entries
        for post in blog_posts:
            fe = fg.add_entry()
            fe.title(post["title"])
            fe.description(post["description"])
            fe.link(href=post["link"])
            # Since we don't have actual dates, we'll use current date
            fe.published(datetime.now(pytz.UTC))
            fe.id(post["link"])

        logger.info("Successfully generated RSS feed")
        return fg

    except Exception as e:
        logger.error(f"Error generating RSS feed: {str(e)}")
        raise


def save_rss_feed(feed_generator, feed_name="paulgraham"):
    """Save the RSS feed to a file in the feeds directory."""
    try:
        feeds_dir = ensure_feeds_directory()
        output_filename = feeds_dir / f"feed_{feed_name}.xml"
        feed_generator.rss_file(str(output_filename), pretty=True)
        logger.info(f"Successfully saved RSS feed to {output_filename}")
        return output_filename

    except Exception as e:
        logger.error(f"Error saving RSS feed: {str(e)}")
        raise


def main(blog_url="https://paulgraham.com/articles.html", feed_name="paulgraham"):
    """Main function to generate RSS feed from blog URL."""
    try:
        # Fetch blog content
        html_content = fetch_html_content(blog_url)

        # Parse blog posts
        blog_posts = parse_essays_page(html_content)

        # Generate RSS feed
        feed = generate_rss_feed(blog_posts, feed_name)

        # Save feed to file
        output_file = save_rss_feed(feed, feed_name)

        return True

    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {str(e)}")
        return False


if __name__ == "__main__":
    main()
