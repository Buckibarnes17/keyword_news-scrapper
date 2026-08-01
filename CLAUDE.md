# CLAUDE.md — orientation for agents working in this repo

## What this is

A keyword news scraper: FastAPI + SQLAlchemy + PostgreSQL backend, React (Vite)
frontend, threaded crawl worker. It crawls a configured set of news and
think-tank sources for keyword matches and syncs matched articles to PostgreSQL.

This is a **fork** of `Viswajith24/keyword_news-scrapper` on branch
`feat/crawl4ai-adaptive-discovery`, created to replace the discovery layer.
`upstream` = the original; `origin` = `Buckibarnes17/keyword_news-scrapper`.

## Read this before changing crawl behaviour

**`backend/discovery/README.md` is required reading.** It documents a per-site
discovery layer built from live probing of all 25 sources on 2026-08-01, and it
contains a **"DO NOT fix these without re-probing"** section. Several profile
values look wrong or over-complicated and are deliberately correct — e.g. CGTN
articles are only reachable on `*.cgtn.com` subdomains and 404 on `www`;
chinadaily's sitemap and RSS both return HTTP 200 while being frozen since
2014/2017. "Cleaning up" those values reintroduces bugs that measurably cost
yield.

## Current state — IMPORTANT

The adaptive discovery layer is **complete and tested but NOT WIRED IN.**

- `backend/discovery/` — 6 strategies + contract + 74 passing tests ✅
- `config/site_profiles.json` — 26 verified site profiles ✅
- **Nothing in the pipeline calls it.** `queue_manager.py` still defines and uses
  the old `run_direct_discovery()` (~line 459). There are **zero references to
  `backend/discovery` from `queue_manager.py`, `main.py` or `crawler.py`.**

So the running app behaves exactly as before. The new layer is additive and
inert. See `HANDOFF.md` for the remaining work, in order.

## Why the layer was built

Measured baseline from production (`news_media.crawled_urls`, 2,997 rows):
**~3% yield** — 120 matched of 2,997 attempts, of which ~90 were genuine
articles. 26% of all rows died on a 45-second watchdog timeout; 12% on VPN
routing. Only 6 of 57 domains ever matched; 25 of 31 jobs produced zero.

Root cause: one static discovery mechanism applied to 25 sites that need six
different ones. Full analysis in `HANDOFF.md`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# crawl4ai is in requirements.txt but ALSO needs a browser:
playwright install chromium
```

Configure `.env` from `.env.example` — needs `DATABASE_URL` and `JWT_SECRET`.

Run: `python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload`

## Testing

```bash
python -m pytest backend/discovery/ -q     # 74 tests, discovery layer, no network
python test_crawler.py                     # legacy diagnostic suite
```

**Never add live-network tests.** Real rate limits apply — aggressive probing
during profiling already triggered an IP ban on one source (mmbiztoday.com).

## Conventions that matter here

- **`discover()` must never raise.** Strategies catch everything, append to
  `DiscoveryResult.errors`, and return partial results. A raise takes down
  discovery for an entire site. This contract was violated once already.
- **Use `.get()` on all external data** — API JSON, XML, and especially crawl4ai
  return values (see the `relevance_score` bug in the discovery README).
- **Optional dependencies go behind availability guards**, following the existing
  `SELENIUM_AVAILABLE` / `LIGHTPANDA_AVAILABLE` pattern in `backend/crawler.py`.
  The app must import and run with crawl4ai absent.
- **Politeness comes from the profile, not from code.** Honour `crawl_delay()`,
  `respects_robots()`, `timeout()`, `max_retries()`. Never hardcode.
- **robots.txt is respected by default.** Two sites carry an explicit,
  operator-approved per-site override recorded in the profile with a reason. Do
  not generalise it, and do not add new overrides without asking the operator.

## Known-unverified

crawl4ai code paths have **never executed** — it was not installed on the build
machine, so all 74 tests exercise the pure requests+lxml fallbacks
(`CRAWL4AI_AVAILABLE == False`). Expect to debug the crawl4ai paths on first real
run. Do not present them as working.
