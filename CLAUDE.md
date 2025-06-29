# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an RSS feed generator that creates RSS feeds for blogs that don't provide them. The project uses Python scripts to scrape blog websites and convert them to RSS XML feeds, with automated updates via GitHub Actions.

## Development Commands

### Environment Setup
```bash
# Create virtual environment
make env_create

# Activate virtual environment (run the output of this command)
$(make env_source)

# Install dependencies
make pip_install

# Clean environment and generated files
make clean
```

### Feed Generation
```bash
# Generate all RSS feeds
make generate_all_feeds

# Generate specific feeds
make generate_anthropic_news_feed
make generate_anthropic_engineering_feed  
make generate_anthropic_research_feed
make generate_openai_research_feed
make generate_ollama_feed
make generate_paulgraham_feed
```

### Code Quality
```bash
# Format Python code
make py_format

# Freeze pip requirements
make pip_freeze
```

### Testing
```bash
# Test the feed generation workflow locally (requires 'act' tool)
make test_feed_workflow

# Run test feed generator
make test_feed_generate
```

## Architecture

### Core Components

- **Feed Generators** (`feed_generators/`): Individual Python scripts that scrape specific blogs and generate RSS feeds
  - Each script follows the pattern: fetch HTML → parse content → generate RSS XML
  - Common utilities: BeautifulSoup for HTML parsing, feedgen for RSS generation, requests for HTTP
  - Output location: `feeds/feed_*.xml`

- **Orchestration** (`run_all_feeds.py`): Main script that executes all feed generators automatically

- **Automation** (`.github/workflows/run_feeds.yml`): GitHub Action that runs hourly to update all feeds

### Feed Generator Pattern

Each feed generator script follows this structure:
1. Fetch HTML content from target blog using requests
2. Parse HTML with BeautifulSoup to extract article data
3. Use feedgen library to create RSS XML
4. Save to `feeds/` directory with pattern `feed_*.xml`

### Dependencies

Key Python packages:
- `beautifulsoup4` & `bs4`: HTML parsing
- `feedgen`: RSS feed generation
- `requests`: HTTP requests
- `selenium` & `undetected-chromedriver`: Browser automation for dynamic content
- `python-dateutil` & `pytz`: Date/time handling

## Adding New Feeds

1. Create new Python script in `feed_generators/` following existing patterns
2. Add corresponding make target to Makefile
3. Script will be automatically included in `run_all_feeds.py` execution
4. GitHub Actions will run the new script hourly

## File Structure

- `feed_generators/`: Python scripts for individual blog scrapers
- `feeds/`: Generated RSS XML files and caches
- `requirements.txt`: Python dependencies
- `Makefile`: Development commands and shortcuts
- `.github/workflows/`: Automated feed generation and testing