# backend/discovery/ — per-site adaptive URL discovery

## Why this exists

The original pipeline used **one** discovery mechanism for every source
(`queue_manager.run_direct_discovery`): fetch the landing page, collect up to 100
same-domain `<a href>`, stop. That is one level deep, keyword-blind, and identical
for all 25 configured sources.

Measured result in production (`news_media.crawled_urls`, 2,997 rows, 31 jobs):

| status | rows | % |
|---|---:|---:|
| failed | 1,150 | 38.4 |
| skipped | 1,134 | 37.8 |
| pending (never crawled) | 593 | 19.8 |
| **matched** | **120** | **4.0** |

Of those 120, ~90 were genuine articles — a **~3% yield**. Only 6 of 57 domains
ever produced a match. 25 of 31 jobs produced zero. The single largest loss was
788 rows (26% of the table) killed by the 45-second watchdog.

Live probing of all 25 sources on **2026-08-01** established why: **six different
discovery mechanisms are required**, and for several sites the obvious route is a
decoy that returns HTTP 200 while serving nothing useful.

## Design

Each site declares its strategy in `config/site_profiles.json`. `base.get_strategy(profile)`
instantiates it from a name registry.

```python
import json
from backend.discovery import base, wp_api, oai_pmh, sitemap, news_sitemap, site_search, deep_crawl

profiles = json.load(open("config/site_profiles.json"))["profiles"]
strategy = base.get_strategy(profiles["niice.org.np"])   # -> WordPressAPIStrategy | None
result = strategy.discover(keyword="maritime security", max_urls=500, since=None)
# result.urls -> List[DiscoveredURL]; result.errors -> List[str]; result.truncated -> bool
```

`get_strategy()` returns `None` when the profile is disabled or its strategy name
is unregistered — callers must handle `None`.

### The key property

`DiscoveredURL` carries `published_at`, `title` and `relevance_score` **populated at
discovery time**, sourced from WordPress REST fields, OAI-PMH `dc:date`,
`<news:publication_date>`, or crawl4ai's `extract_head`. This lets a caller drop
stale and irrelevant URLs **before spending a fetch**. The old pipeline could not
do this, which is why it burned 26% of its budget on timeouts.

### Contract (`base.DiscoveryStrategy`)

`discover()` **MUST NOT raise.** Catch everything, append to `result.errors`,
return partial results. A raise takes down discovery for the whole site. This is
enforced by test and was violated once already (see Fixes, below).

Subclasses must honour, never hardcode: `respects_robots()`, `crawl_delay()`,
`headers()`, `timeout()`, `max_retries()`, `host_allowed()`, `classify_url()`,
`max_urls` (setting `result.truncated`).

## Strategies

| name | sites | notes |
|---|---|---|
| `wp_api` | niice.org.np, iids.org.np, kachinnews.com, mmbiztoday.com, northeastlivetv.com, newslivetv.com | WordPress REST. `X-WP-Total` gives exact counts; `?search=` is server-side. HTTP 400 past the last page is normal end-of-pagination. |
| `oai_pmh` | nepjol.info | OAI-PMH bulk harvest, 61,830 records, 100/page via `resumptionToken`. Incremental via `&from=`. Keyword filtering is **client-side** (OAI has no search). |
| `news_sitemap` | cgtn.com, burmese.voanews.com, bbc.com, rfa.org, kathmandupost.com | Google News `<news:news>` sitemaps; `<news:publication_date>` is the most reliable date available. |
| `sitemap` | dvb.no, pri.gov.np, mod.gov.np | Sitemap index recursion, gzip, `<lastmod>`. |
| `search` | chinadaily.com.cn, globaltimes.cn, irrawaddy.com, elevenmyanmar.com, bnionline.net, ifa.gov.np | Native site search. Templates live in the profile — never hardcode a URL. |
| `deep_crawl` | fmprc.gov.cn, stats.gov.cn, mizzima.com, hornbilltv.com | BFS/BestFirst, used only where nothing structured exists. |

## ⚠️ DO NOT "fix" these without re-probing

Every value below was empirically verified on 2026-08-01. Each looks wrong or
over-complicated and is not. Changing any of them silently reintroduces a bug
that already cost measurable yield.

- **`cgtn.com` → `allowed_hosts: ["*.cgtn.com"]`.** Articles live on
  `newsaf`/`newseu`/`newsus`/`news`.cgtn.com and **never** on `www`. The same path
  returns 200 on `newsaf.cgtn.com` and 404 on `www.cgtn.com`. Do not "normalise"
  article URLs onto `www`. This was the root cause of **0 matches from 140
  attempts**. Compounding trap: CGTN's 404 page is 116 KB with the plausible title
  "CGTN | Breaking News, China News, World News and Video", so both size-based and
  title-based validity checks pass on 404s.
- **`fmprc.gov.cn` seed is `/eng/`, not `/en/`.** `/en/` returns 302 to a
  Chinese-language *maintenance* page. Worse, **every** unknown path on that host
  soft-404s to the same page with HTTP 200 — `/sitemap.xml`, `/rss`, `/robots.txt`
  all "succeed". Without a redirect check a crawler ingests hundreds of copies of
  one maintenance page as articles.
- **`chinadaily.com.cn` uses `search`, not `sitemap` or `feed`.** Its sitemap is
  frozen at 2014 and its RSS at 2017 — **both still return HTTP 200**. They look
  alive and are not.
- **`burmese.voanews.com` must not use its feeds.** All ~60 RSS feeds are stale
  (newest 2025-04-23) while its sitemaps are current. Feed freshness is not a
  proxy for site freshness.
- **`irrawaddy.com` is scoped to `burma.irrawaddy.com` and needs the FULL browser
  header set.** `www` is Cloudflare-blocked (403). `burma.` returns **403 with a
  User-Agent alone but 200 with the complete header set** (`Accept`,
  `Accept-Language`, `Upgrade-Insecure-Requests`, `Sec-Fetch-*`). Do not trim
  `base.BROWSER_HEADERS`. Crawl-delay is 10s.
- **`stats.gov.cn` transport flags are all load-bearing.** Force IPv4 (DNS returns
  AAAA but there is no v6 route → instant `errno 101`), force HTTPS (port 80 is
  firewalled while the site's own internal links are written `http://`), 60s
  timeout and 5 retries (QiAnXin WAF, 7.5s handshakes, ~35% success). Previously
  118/118 failures.
- **`bbc.com`, `rfa.org`, `burmese.voanews.com` are sections, not sites.** BBC's
  news sitemap is site-wide; without the `/burmese` scope prefix discovery
  explodes into all of bbc.com.
- **`ifa.gov.np` listing patterns.** Only `/en/news/<slug>/` and
  `/en/publications/<slug>/` are documents. `/en/events/`, `/en/staff/`,
  `/en/page/`, `/en/gallery/` are not. The old pipeline reported 36 "articles"
  here that were mostly nav and staff-bio pages, several under 60 chars of body
  text. Its homepage search form is also a decoy: `/en/?q=Nepal` and
  `/en/?q=zzzzqqq` return byte-identical homepages.
- **`elevenmyanmar.com`: construct pager URLs, never follow its pager hrefs.** It
  reflects arbitrary query strings back into `href` attributes; a leftover
  log4j-style injection payload was found in them during profiling.

## Operator decisions recorded as data (not code)

- **`northeastlivetv.com`, `newslivetv.com` — `robots.respect: false`.** Both serve
  `User-agent: * / Disallow: /`, allowing only Googlebot/Bingbot/Facebook/Twitter.
  Together they are 85,249 documents and the *only* Northeast India coverage. A
  per-site override was explicitly approved by the operator on 2026-08-01 and is
  logged in the profile's `override_reason` field. **The global default remains
  `respect: true`** — do not generalise this exception. Reversible by editing one
  field.
- **`ndri.org.np` — `enabled: false`, `needs_reprobe: true`.** TLS handshake reset
  from the profiling egress across requests and curl (v4+v6, HTTP/1.1 and /2,
  forced TLS1.2). Port 80 answers `301 → https`, so the origin is up and refusing
  TLS from that network. Recorded as **untested, not absent** — re-probe from the
  production host before assigning a strategy.

## Confidence caveats

- **`mmbiztoday.com` is `confidence: low`.** The profiling run reported a working
  WP API with 4,306 posts; an immediate re-probe returned HTTP 403 from nginx on
  every path including the homepage, with and without full headers. Most likely
  the profiling itself tripped a rate-limit ban. Dormant since 2022, so low
  stakes — but re-verify before trusting the count.
- **`iids.org.np` was corrected 412 → 304.** The profiling agent reported "270
  posts + 142 portfolio entries"; `/wp-json/wp/v2/types` shows **no `portfolio`
  post type** (REST 404) and `pages` returns 34, not 142. Verified: posts=270,
  pages=34.

**Politeness is not optional.** Aggressive probing during profiling caused a real
IP ban (mmbiztoday). Honour `crawl_delay()` between every request — verified
values include dvb.no 2s and irrawaddy.com 10s, against a 1.0s global default.

## crawl4ai status — UNVERIFIED AT RUNTIME

crawl4ai was **not installed** on the machine where this layer was written. Every
strategy imports it behind a guard:

```python
try:
    from crawl4ai import AsyncUrlSeeder, SeedingConfig
    CRAWL4AI_AVAILABLE = True
except ImportError:
    CRAWL4AI_AVAILABLE = False
```

**All 74 tests run with `CRAWL4AI_AVAILABLE == False` and therefore exercise only
the pure requests+lxml fallbacks.** The crawl4ai paths are written against pinned
official-docs APIs but have never executed. Expect to debug them on first run.

Known upstream bug: [unclecode/crawl4ai#1306](https://github.com/unclecode/crawl4ai/issues/1306)
— `url["relevance_score"]` raises `KeyError` even when BM25 scoring is configured
(open, reported against 0.7.0, possibly fixed by 0.9.2 — unconfirmed). Therefore:
always use `.get()` on crawl4ai return values, and if scoring comes back empty for
*all* results, log and fall back to the local keyword scorer rather than returning
an empty list.

Also note `CrawlerRunConfig.check_robots_txt` **defaults to `False`**. The existing
pipeline honours robots.txt, so it must be set explicitly to preserve behaviour.

## Fixes already applied during validation

- `oai_pmh.discover()` caught only `requests.RequestException`, so a non-requests
  error (bad profile value, lxml edge case, date parse) escaped and violated the
  never-raise contract. Broadened to `except Exception`.
- `RobotsAwareMixin` declared `respects_robots`/`headers`/`timeout` as
  `NotImplementedError` stubs commented "provided by DiscoveryStrategy at
  runtime". Because the mixin sits **left** of `DiscoveryStrategy` in the bases, it
  **shadowed** the real implementations — leftmost wins in Python MRO. Removing the
  stubs unblocked 14 tests. Do not re-add them.

## Tests

```bash
python -m pytest backend/discovery/ -q      # 74 tests, no live network
```

All tests mock the HTTP layer. **Never add live-network tests here** — real rate
limits apply and one ban already occurred.

Validation performed beyond the unit tests: 175 injected-failure cases (7
exception types × 25 active sites) confirmed 0 never-raise violations, and 9/9
site-specific traps above were asserted.
