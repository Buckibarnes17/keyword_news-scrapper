# CLAUDE.md — orientation for agents working in this repo

## What this is

A keyword news scraper: FastAPI + SQLAlchemy + PostgreSQL backend, React (Vite)
frontend, threaded crawl worker. It crawls a configured set of news and
think-tank sources for keyword matches and syncs matched articles to PostgreSQL.

This is a **fork** of `Viswajith24/keyword_news-scrapper` on branch
`feat/crawl4ai-adaptive-discovery`. `upstream` = the original; `origin` =
`Buckibarnes17/keyword_news-scrapper`.

## Read this before changing crawl behaviour

**`backend/discovery/README.md` is required reading.** It documents a per-site
discovery layer built from live probing of all 25 originally-profiled sources
on 2026-08-01, and it contains a **"DO NOT fix these without re-probing"**
section. Several profile values look wrong or over-complicated and are
deliberately correct — e.g. CGTN articles are only reachable on `*.cgtn.com`
subdomains and 404 on `www`; chinadaily's sitemap and RSS both return HTTP 200
while being frozen since 2014/2017. "Cleaning up" those values reintroduces
bugs that measurably cost yield.

**`HANDOFF.md` is required reading before touching `queue_manager.py`.** It
has the full history of what's been fixed, what's still open, and — most
important — the debugging methodology that found five non-obvious concurrency
bugs this session. Re-read it fully before assuming something is broken; it's
very possibly a known, already-diagnosed issue.

## Current state — IMPORTANT

The adaptive discovery layer is **wired in and has been exercised against
real jobs, real external sites, and a real Postgres DB** (unlike the state
described in older revisions of this file). `queue_manager.py`'s
`run_direct_discovery()` dispatches through `backend/discovery/base.get_strategy()`
per-domain, falling back to legacy link-expansion for unprofiled sites or
sites needing a keyword the pre-discovery phase doesn't have yet.

Three commits landed the current state (read each commit message in full —
they're detailed and explain *why*, not just *what*):

```
f15b8c1 Cache ChromeDriver path resolution to fix filesystem-lock contention under concurrency
319cf3b Offload analyze_page CPU-bound work to a ProcessPoolExecutor, add fetch/analyze timing instrumentation
4efab41 Wire adaptive discovery layer into queue_manager and fix production-blocking concurrency bugs
```

**Reliability is real but not fully validated yet.** Every live pilot run this
session was confounded by something different (a DuckDuckGo outage, a
ChromeDriver lock bug, an uncapped discovery-volume spike) — see `HANDOFF.md`
for the full data trail and what's still open. Do not assume the pipeline is
either "broken" or "fixed" without reading that history first.

## Why the layer was built

Measured baseline from production (`news_media.crawled_urls`): **~3% yield**
— 120 matched of 2,997 attempts, of which ~90 were genuine articles. 26% of
all rows died on a 45-second watchdog timeout. Root cause: one static
discovery mechanism applied to 25 sites that need six different ones. Full
analysis in `HANDOFF.md`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Git identity for commits is already configured in this repo (not global) —
check `git config user.name`/`user.email` before assuming you need to set it.

**No `.env` exists in this repo, only `.env.example` — and unlike what older
docs assumed, `backend/database.py`'s fallback for an unset `DATABASE_URL` is
NOT silently-empty SQLite in every environment.** It first tries a hardcoded
Postgres host/port/credentials (see `backend/database.py` lines ~24-28) and
only falls back to SQLite if that's unreachable. **This is a real,
unresolved security concern** — a live database's credentials are hardcoded
in source. Do not "fix" this yourself without asking the operator; multiple
things in this pipeline currently depend on that fallback actually working.
Verify which DB you're actually talking to before trusting any result
(`[Database] Connected to PostgreSQL: <host>:<port>` prints on import if it's
live; silence means SQLite).

**Chrome/Chromium is NOT installed system-wide in at least one environment
this was developed in** (no passwordless sudo available to install it
either). Every Selenium fallback attempt fails fast with `cannot find Chrome
binary`. This is an environment gap, not a code bug — but it means Selenium
fallback paths have never been observed to actually succeed end-to-end this
session. Check for this before assuming a Selenium-dependent fix works.

Run: `python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload`

## Testing

```bash
.venv/bin/python -m pytest backend/discovery/ test_discovery_dedup.py \
  backend/test_queue_manager_discovery_wiring.py backend/test_analysis_pool_parity.py \
  test_crawler.py backend/test_chromedriver_path_cache.py -q     # 105 tests, no live network
```

**Never add live-network tests to this suite.** Real rate limits apply —
aggressive probing during original profiling triggered an IP ban on one
source (mmbiztoday.com), and this session found the running pipeline can
inadvertently hammer search engines (DuckDuckGo/Bing/Yahoo via
`search_engine.py`) hard enough to get temporarily unreachable from repeated
testing.

**Live pilot testing (real sites, real DB) uses a throwaway script pattern,
not the pytest suite** — see `HANDOFF.md`'s "Pilot harness" section for the
exact, reusable script. It creates a real `SearchQuery` row and calls
`process_search_query()` directly, bypassing the FastAPI app entirely. Use it
for any change to `queue_manager.py`/`crawler.py`'s crawl-execution path —
the pytest suite mocks too much of the real pipeline to catch the concurrency
bugs this session found.

## Conventions that matter here

- **`discover()` must never raise.** Strategies catch everything, append to
  `DiscoveryResult.errors`, and return partial results. A raise takes down
  discovery for an entire site.
- **Use `.get()` on all external data** — API JSON, XML, and especially
  crawl4ai return values (see the `relevance_score` bug in the discovery
  README).
- **Optional dependencies go behind availability guards**, following the
  existing `SELENIUM_AVAILABLE` / `LIGHTPANDA_AVAILABLE` pattern in
  `backend/crawler.py`.
- **Politeness comes from the profile, not from code.** Honour
  `crawl_delay()`, `respects_robots()`, `timeout()`, `max_retries()`. Never
  hardcode. `crawl_delay()` is honoured during discovery AND during the
  actual fetch phase (`crawl_url_task`'s rate limiter reads it from
  `config/site_profiles.json` per-domain — see `_domain_crawl_delay()`).
- **robots.txt is respected by default.** Two sites carry an explicit,
  operator-approved per-site override recorded in the profile with a reason.
  Do not generalise it.
- **Any new per-keyword-repeated network call inside `_process_single_keyword`
  must draw from the shared `domain_candidate_budget`** (see
  `backend/queue_manager.py`'s `_trim_to_domain_budget()`), or it will
  reproduce the exact bug this session found: a single domain accumulating
  hundreds of real candidate URLs across a multi-keyword job because nothing
  capped the total. Note the budget currently only covers `search_web()` and
  `SiteSearchDetector` — it does NOT cover primary per-site discovery's own
  `max_urls` cap (`KS_MAX_CANDIDATE_URLS`, default 500). See `HANDOFF.md` for
  why this is a known, still-open gap (a site whose discovery genuinely
  succeeds, like `cgtn.com`, can still return hundreds of candidates
  unbounded by the fix that targeted the *other* mechanism).
- **Any code that creates a per-instance or per-task resource that itself
  acquires a filesystem/process-wide lock (e.g. `ChromeDriverManager().install()`)
  must cache/dedupe that resolution at module level**, not per-instance. Two
  independent instances of this exact bug were found this session
  (`backend/crawler.py`, fixed; `backend/search_engine.py`'s
  `scrape_duckduckgo_selenium()`, NOT fixed — see `HANDOFF.md`).
- **Before concluding a fix worked or didn't work from a live pilot run,
  check for confounds first.** Every pilot run this session was affected by
  something unrelated to the fix being tested (an external service outage,
  a different bug's volume spike). Compare status breakdowns and domain
  breakdowns, not just the headline pass/fail number.
