# 📰 Keyword News Scraper & Content Analyzer

A premium, modern, glassmorphism-styled web application built with **FastAPI**, **SQLAlchemy (SQLite / PostgreSQL)**, and **Vanilla JS/CSS** that dynamically crawls websites for target keyword matches. It supports high-speed HTTP retrieval, Selenium headless browser crawling, article text expansion, multi-page pagination searching, and robust file exports (Excel, CSV, JSON, Parquet).

---

## ✨ Key Features

- **🌐 Asynchronous Scraper Engine**: Powered by a concurrent `ThreadPoolExecutor` worker queue.
- **⚡ Dual Crawling Engines**:
  - **Fast HTTP Engine (`requests`)**: Fast, lightweight scraping with standard browser headers and rate-limiting enforcement.
  - **Dynamic JS Engine (`selenium`)**: Spawns Headless Chrome to load dynamic web pages, bypass basic bot checks, progressively scroll to trigger lazy loading, and auto-click article expand buttons ("Read More", "Show More", etc.).
- **🔒 Tor Anonymous Routing & SOCKS5 Proxy Routing**:
  - Automatically routes traffic through a SOCKS5 Tor proxy (`127.0.0.1:9050`) when the "Route through Tor" toggle is enabled in Advanced Settings.
  - Features DNS leak prevention (using SOCKS5h protocols for HTTP requests and isolated DNS rules for Headless Chrome).
  - Handles IP rotation on request failure or on demand.
  - Automatically downloads and configures the Tor Expert Bundle on Windows via `backend/tor_setup.py` when using `run.bat`.
- **🛡️ Near-Duplicate Content Deduplication (SimHash)**:
  - Integrates a 64-bit Charikar SimHash algorithm utilizing Blake2b hashing.
  - Prevents indexing of near-duplicate content by computing text fingerprints and checking Hamming distances (automatically skips pages with a Hamming distance $\le 3$).
- **🔍 Native Site-Search Form Detection & Interception**:
  - Automatically identifies search fields and forms (`<form>`, `<input type="search">`) on landing pages.
  - Intercepts default crawling to perform native site search queries using search parameters, deep scanning only related matching articles.
- **🗺️ XML Sitemap & RSS/Atom Feed Parsing**:
  - Automatically discovers XML sitemaps and RSS/Atom feeds (e.g. `/sitemap.xml`, `/rss`, `/feed/`) for a domain to instantly expand target lists with high-relevance URLs.
- **🎯 Intelligent URL Classification & Prioritization**:
  - Pre-filters candidate URLs in O(1) based on path patterns, avoiding duplicate pagination result index lists, static assets, and non-content urls to speed up scraper crawling cycles.
- **🔍 Advanced Matching Logic**:
  - **Boolean Search**: Supports logical expressions like `AND`, `OR`, `NOT`, and parenthesis grouping (e.g. `python AND (fastapi OR ruby)`) via a custom recursive descent parser.
  - **Multi-Keyword Lists**: Search for multiple comma- or newline-separated keywords. Matches are flagged in the UI as beautiful tags.
  - **Keyword-Free Archiving**: Toggle keyword filtering off to archive full pages directly.
- **📑 Multi-Page pagination**: Standalone CLI scraper searches sequentially through consecutive next-page links if keywords aren't found on the landing page.
- **🗃️ PostgreSQL Sync & Heuristics Classification**:
  - Automatically synchronizes matched scraping records to a PostgreSQL target database in the background after finishing, or manually via the "Export to PostgreSQL DB" button in the dashboard UI.
  - Features custom heuristics to classify article records automatically by **source type** (News, Journal, Think Tank, Government, Research Institute), **content type** (Article, Report, Policy Brief, Journal, Event, Book, Podcast, Video), **subject theme** (Maritime, Defence, Security, Politics, Economy, General), **country/region coverage** (with custom regex patterns), and **language**.
- **🔥 Firecrawl-Compatible Extraction Normalizer**:
  - An isolated extraction normalization layer that converts raw HTML documents into clean Markdown and structured JSON elements matching the Firecrawl response specifications.
  - Extracts page titles, descriptions, headings, paragraphs, lists, tables, nested code blocks, quotes, and lists of resolved links, image URLs (with dimension/alt metadata), and videos (HTML5 or embedded YouTube/Vimeo/Loom/etc.).
  - Features a self-healing content retention validation block that falls back to minimal cleaning if content is aggressively stripped.
  - Exposes `/api/scrape` for on-demand live scraping and `/api/results/crawled/{url_id}/firecrawl` for retrieving database records in Firecrawl schema.
- **📊 Interactive Dashboard**:
  - Live processing progress indicators and real-time log monitors.
  - Highlighting matching terms inside page titles, metadata descriptions, URLs, and article body snippets.
  - Interactive popup modal to read full scraped content, lead author info, and lead images.
- **⏱️ Scheduled Automation**: Periodic background schedules (daily, weekly, monthly) using a thread-safe cron manager.
- **📥 Advanced Data Exporters**: One-click streaming downloads for **Excel (.xlsx)**, **CSV**, **JSON**, and **Parquet** formats.

---

## 🛠️ Tech Stack

- **Backend**: Python 3.9+, FastAPI, SQLAlchemy ORM, Uvicorn, Slowapi, Beautiful Soup 4, Requests, Selenium, Psycopg2.
- **Database**: PostgreSQL (unified database for search queries, schedules, crawler progress, users, and synchronized production archives).
- **Frontend**: React (Vite, Zustand, Tailwind CSS / Custom CSS modules).

---

## 📂 Project Directory Structure

```
├── backend/
│   ├── main.py                 # FastAPI server app, lifespans, API routes, and rate limits
│   ├── auth.py                 # JWT Token creation, verification, and user details dependencies
│   ├── database.py             # PostgreSQL engine connection pool setup & automatic db startup checks
│   ├── models.py               # SQLAlchemy models (User, SearchQuery, CrawledURL, SearchSchedule, etc.)
│   ├── schemas.py              # Pydantic serialization definitions
│   ├── queue_manager.py        # Threaded worker loops, domain-rate limits, and scrape tasks
│   ├── crawler.py              # Page fetching, BS4 cleaning, language/date detection, and parser
│   ├── exporter.py             # Spreadsheet stream output generator (Excel/CSV/JSON/Parquet)
│   ├── scheduler.py            # Recurring cron scheduler loops for active automations
│   ├── firecrawl_converter.py  # Firecrawl content parser, DOM scoring, and Markdown extraction layer
│   ├── postgres_integration.py # PostgreSQL sync engine and heuristic classifier
│   ├── tor_router.py           # Tor SOCKS5 routing utilities & DNS leak prevention
│   ├── tor_setup.py            # Auto-downloader & extractor for Tor Expert Bundle on Windows
│   ├── simhash_dedup.py        # SimHash content deduplication & near-duplicate filters
│   ├── site_search_detector.py # Auto-detects search forms on target domains to query keywords directly
│   ├── sitemap_discovery.py    # Discovers XML sitemaps & RSS/Atom feeds to find deep links
│   ├── url_classifier.py       # Filters non-content urls (static, pagination) in O(1)
│   └── test_site_search.py     # Unit tests verifying site search detection and form routing
├── frontend/                   # React Single-Page Application (Vite project)
│   ├── src/                    # Source files (components, api layers, state stores, views, etc.)
│   ├── index.html              # HTML entry point for the React app
│   ├── package.json            # Frontend dependency manifest and run scripts
│   └── vite.config.js          # Vite config (routes '/api' to backend and outputs builds to static/)
├── config/                     # Configuration directory
│   ├── keywords.json           # Stores system keywords & classification groups
│   └── urls.json               # Stores predefined domains/sitemaps for custom index scans
├── static/                     # compiled frontend assets served directly by FastAPI server
│   ├── index.html              # Build entry point index
│   └── assets/                 # Compiled CSS and JS bundles from the frontend Vite build
├── requirements.txt            # Python package requirements checklist (includes psycopg2-binary, passlib, jose)
├── run.bat                     # Automated Windows setup & launcher script (launches pre-built backend+frontend)
├── test_crawler.py             # Comprehensive diagnostic suite for database, search, and normalizer logic
└── selenium_scraper.py         # Standalone CLI dynamic sequential pagination scraper with PostgreSQL classifier
```

---

## 🚀 Installation & Setup

### Option A: One-Click Launcher (Windows)
Double-click the **`run.bat`** file in the root workspace. This script will automatically:
1. Verify if Python is installed.
2. Initialize a Python virtual environment (`.venv`).
3. Upgrade `pip` and install all required modules listed in `requirements.txt`.
4. Run pre-flight check to configure the Tor daemon (starts Tor proxy automatically).
5. Open the web interface at `http://127.0.0.1:8000` (which serves the pre-built React application).
6. Start the backend Uvicorn development server.

### Option B: Manual Setup & Development Workflow (All OS)

#### 1. Backend Setup
1. **Navigate** into the project root directory.
2. **Create and activate a virtual environment**:
   - **Windows**:
     ```bash
     python -m venv .venv
     .venv\Scripts\activate
     ```
   - **macOS/Linux**:
     ```bash
     python -m venv .venv
     source .venv/bin/activate
     ```
3. **Install python packages**:
   ```bash
   pip install -r requirements.txt
   ```
4. **Configure Environment Variables**:
   Create or edit the `.env` file in the root directory (based on `.env.example`) and configure your PostgreSQL database and a secure JWT secret:
   ```bash
   DATABASE_URL=postgresql://username:password@localhost:5432/keyword_scraper
   JWT_SECRET=your_secure_random_jwt_secret_key
   ```
5. **Launch the FastAPI Server**:
   ```bash
   python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
   ```

#### 2. Frontend Setup
1. **Navigate** into the `frontend/` directory:
   ```bash
   cd frontend
   ```
2. **Install node dependencies**:
   ```bash
   npm install
   ```
3. **Run in Development Mode**:
   ```bash
   npm run dev
   ```
   *This starts the Vite dev server at `http://localhost:5173/` which automatically proxies all `/api` requests to the backend FastAPI server.*
4. **Build for Production**:
   If you make updates to the frontend code and want to compile them to be served directly by the backend:
   ```bash
   npm run build
   ```
   *This compiles assets directly into the `/static` folder in the project root.*

---

## 🧪 Running Diagnostic Tests

Execute the automated test suite to verify database migrations, page parsers, boolean queries, multi-keyword extraction, exporter modules, classification heuristics, and the Firecrawl normalization layer:

```bash
python test_crawler.py
```

---

## 🖥️ Running the Standalone CLI Scraper

For standalone command-line operations (which check sequential next-page paginations dynamically):

1. Open `selenium_scraper.py` and modify the `test_urls` and `target_keywords` list at the bottom:
   ```python
   test_urls = ["https://realpython.com/tutorials/"]
   target_keywords = ["Python", "automation"]
   ```
2. Run the script:
   ```bash
   python selenium_scraper.py
   ```
3. The script will output a structured JSON containing the extracted text content, lead images, and video resource links from matching landing/paginated pages, conforming to the 19-field classification schema.

---

## 🛠️ Search URL Inspector CLI Utility

A comprehensive terminal administration tool (`inspect_search_urls.py`) is provided to inspect queries, check crawler queues, reset url states, and export database files.

Run the utility:
```bash
python inspect_search_urls.py --list
```

### Options:
- `--list`: Display list of recent search queries and their statuses.
- `--search-id <id>`: Inspect a specific search query detailed URL crawl list (all pending, crawling, matched, skipped, and failed links).
- `--status <status>`: Filter inspected URLs by status (`pending`, `crawling`, `matched`, `skipped`, `failed`).
- `--limit <num>`: Limit output list results (default: 10).
- `--export <path>`: Export filtered URL inspection list to a CSV file.
- `--reset-failed`: Reset all failed URLs of a specific search query ID back to `pending` status so the crawler retries them.
- `--reset-skipped`: Reset all skipped URLs of a specific search query ID back to `pending`.
- `--interactive` / `-i`: Run the utility in interactive step-by-step console mode.

---

## 🔒 Rate Limiting & Configurations

- **Rate Limiting**: To prevent API abuse, endpoints are rate-limited via `slowapi`. The `/api/results/{search_id}` endpoint is set to a maximum of **300 requests/minute** to accommodate real-time front-end polling.
- **Database Configuration**: The application leverages SQLite for storing search configurations, schedules, and local crawled URLs.
- **PostgreSQL Configuration**: The production synchronizer connects to PostgreSQL using the connection string configured via the `DATABASE_URL` or `POSTGRES_URL` environment variable. By default, it falls back to:
  `postgresql://postgres:postgres@localhost:5432/keyword_scraper`
  On backend server startup, the system will automatically check if the target database exists on the database server and auto-create it if missing.
