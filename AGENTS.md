# Repository Guidelines

Always test your code after changes by simply running `python feed_generators/openai_research_blog.py` or your edited script
YOU DO NOT NEED TO SETUP AN ENV if you are CODEX CLI as you are running on a local system already configured

## Project Structure & Module Organization
- `feed_generators/`: Python scrapers that convert blog HTML → RSS. Each script outputs an XML file in `feeds/`.
- `feeds/`: Generated RSS XML files (`feed_*.xml`). Commit outputs.
- `.github/workflows/`: CI to run generators on a schedule and for tests.
- `Makefile`: Common dev tasks (env, formatters, generators).
- `deprecated/`: Old or superseded code kept for reference.

## Build, Test, and Development Commands
ONLY USE THE FOLLOWING IF YOU ARE NOT CODEX CLI:
- Environment
  - `make env_create` — create virtualenv via `uv`.
  - `$(make env_source)` — print shell command to activate; run its output.
  - `make uvx_install` — install dependencies from `requirements.txt`.
- Feed generation
  - `make generate_all_feeds` — run all scripts in `feed_generators/`.
  - Example single feed: `make generate_openai_research_feed`.
- Quality
  - `make py_format` — run `black` and `isort`.
- Testing
  - `make test_feed_generate` — run local test generator.
  - `make test_feed_workflow` — run the CI workflow locally via `act` (optional).

## Coding Style & Naming Conventions
- Python 3.11+. Use 4‑space indentation, UTF‑8, and type hints when practical.
- Format with `black` and sort imports with `isort` before pushing.
- Script names: describe the source, e.g., `openai_research_blog.py`.
- Output files: `feeds/feed_<source>.xml` (e.g., `feed_openai_research.xml`).
- Functions should be small and testable: fetch → parse → build feed → write file.

## Testing Guidelines
- Prefer deterministic parsing (avoid flaky selectors/timing). Use Selenium only when necessary.
- Prefer a requests-first fetch with Selenium fallback; avoid fixed `sleep` calls and use `WebDriverWait` when Selenium is needed.
- Validate a new generator by running it locally and confirming the XML in `feeds/`.
- If using `act`, ensure Docker is available; CI mirrors `run_all_feeds.py` execution.

## Commit & Pull Request Guidelines
- Commits: imperative, concise, and scoped (e.g., `add xai news generator`, `fix openai date parsing`).
- New feed PRs: use the PR template `.github/PULL_REQUEST_TEMPLATE/add_new_feed.md`.
  - Include description, add a Makefile target, and commit the generated `feeds/feed_*.xml`.
  - Title: `[New RSS Feed] <Feed Name>` and apply the `new-feed` label.

## Agent-Specific Notes
- Follow patterns in existing generators; keep site‑specific logic isolated.
- See `CLAUDE.md` for a deeper architecture overview and examples.
- When adding new scrapers, try a lightweight `requests` path first, then fall back to Selenium only if parsing fails or the site blocks requests.
- If Selenium is required, rely on explicit waits for target elements instead of fixed delays to reduce runtime and flakiness.

## Creating a Feed Generator (Step-by-Step Guide)

A proper feed generator must fetch **full article content** from each article page, not just titles/links from the listing page. This makes the feed actually usable in RSS readers.

### 1. Core Structure

Every generator follows this pattern:

```python
def main(feed_name: str = "sitename") -> bool:
    # Step 1: Fetch the blog listing page
    html_content = fetch_blog_content(BLOG_URL)
    
    # Step 2: Parse article metadata (title, link, date, category)
    articles = parse_blog_html(html_content)
    
    # Step 3: Fetch FULL CONTENT for each article
    for article in articles:
        article_html = fetch_article_page(article["link"])
        if article_html:
            content_html, summary = extract_article_content(article_html, article["link"])
            article["content_html"] = content_html
            article["description"] = summary
    
    # Step 4: Generate and save RSS feed
    feed = generate_rss_feed(articles, feed_name)
    save_rss_feed(feed, feed_name)
```

### 2. Fetching Article Content

Always fetch each article's full page and extract content:

```python
def fetch_article_page(url: str) -> str | None:
    """Fetch HTML for a single article page."""
    headers = {"User-Agent": "Mozilla/5.0 ..."}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.text
    except Exception:
        # Fallback to Selenium if blocked
        return fetch_with_selenium(url)
```

### 3. Extracting & Cleaning Content

Extract the main article body, clean it, and convert relative URLs to absolute:

```python
def extract_article_content(html: str, page_url: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    
    # Find article container (try multiple selectors)
    container = (
        soup.select_one("article") or
        soup.select_one("[class*='content']") or
        soup.select_one("main")
    )
    
    # Clean the HTML
    content_html = _clean_article_html(container, base_url=page_url)
    
    # Extract summary (first paragraph)
    summary = container.find("p").get_text(strip=True) if container else ""
    
    return content_html, summary

def _clean_article_html(container, base_url: str) -> str:
    # Remove scripts, styles, nav, ads, etc.
    for tag in container.select("script, style, nav, footer, .ad, .share"):
        tag.decompose()
    
    # Convert relative URLs to absolute (CRITICAL!)
    for img in container.find_all("img", src=True):
        if not img["src"].startswith(("http://", "https://", "data:")):
            img["src"] = urljoin(base_url, img["src"])
    
    for a in container.find_all("a", href=True):
        if not a["href"].startswith(("http://", "https://", "mailto:", "#")):
            a["href"] = urljoin(base_url, a["href"])
    
    return str(container)
```

### 4. Adding Content to Feed

Use `fe.content()` to add full HTML content to each entry:

```python
def generate_rss_feed(articles, feed_name):
    fg = FeedGenerator()
    fg.title("Site Name")
    fg.link(href=BASE_URL)
    fg.description("Site description")
    
    # feedgen prepends entries, so iterate in reverse for newest-first
    for article in reversed(articles):
        fe = fg.add_entry()
        fe.title(article["title"])
        fe.link(href=article["link"])
        fe.description(article["description"])  # Short summary
        
        # Add full content (creates <content:encoded> tag)
        if article.get("content_html"):
            fe.content(article["content_html"])
        
        if article.get("date"):
            fe.published(article["date"])
        fe.category(term=article["category"])
        fe.id(article["link"])
    
    return fg
```

### 5. Key Points

- **Always fetch each article page** — listing pages only have titles/links
- **Clean the HTML** — remove nav, ads, scripts, interactive elements
- **Absolutize URLs** — relative image/link paths break in RSS readers
- **Use `fe.content()`** — this creates the `<content:encoded>` tag with full HTML
- **Handle Selenium fallback** — some sites block requests, use undetected_chromedriver
- **Sort articles newest-first** — and use `reversed()` when adding to feedgen
- **Test locally** — run the script and verify `feeds/feed_*.xml` has `<content:encoded>` tags with actual content
