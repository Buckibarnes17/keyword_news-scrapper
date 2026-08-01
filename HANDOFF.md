# HANDOFF — adaptive discovery layer

**State as of commit on branch `feat/crawl4ai-adaptive-discovery` (2026-08-01).**
Read `CLAUDE.md` first, then `backend/discovery/README.md`.

---

## TL;DR for the next agent

The discovery layer is **built, tested, and not connected to anything.** Your job
is (1) install and verify crawl4ai, (2) prove yield on a few sites, (3) wire it
into `queue_manager`, in that order. Do not skip to (3) — the yield measurement is
what tells you whether the layer works before you touch the running pipeline.

---

## Why this work exists — the measured baseline

From `news_media.crawled_urls` (PostgreSQL, schema `news_media`), 2,997 rows
across 31 search jobs and 57 domains:

| status | rows | % |
|---|---:|---:|
| failed | 1,150 | 38.4 |
| skipped | 1,134 | 37.8 |
| pending (never crawled) | 593 | 19.8 |
| **matched** | **120** | **4.0** |

Of the 120 matched: 98 had ≥500 chars of content, 64 had ≥1,500, and ~21 were
navigation/listing pages misclassified as articles (mostly `ifa.gov.np` — titles
like "Events — Seminars", staff bios). One was a leftover `httpbin.org/headers`
test row. **Defensible genuine-article count: ~90, i.e. ~3% yield.**

Where the other 96% went:

| cause | rows | note |
|---|---:|---|
| 45s watchdog timeout | 788 | 26% of the whole table — largest single loss |
| never crawled | 593 | jobs aborted before reaching them |
| VPN routing failure | 346 | 229 "Singapore geolocation check failed" + 117 connection errno |
| date filter | 278 | mostly the implicit 90-day default |
| crawl interrupted | 250 | |
| duplicate content | 220 | SimHash/MD5 dedup working correctly |
| HTTP 404/403/401 | 17 | genuinely dead links |

Only **6 of 57 domains** ever produced a match (chinadaily 50, ifa.gov.np 36,
understandingwar.org 30, + 4 stragglers). **25 of 31 jobs produced zero.** Several
configured sources were never crawled at all: bbc.com (79 pending), dvb.no (72),
rfa.org (45), assamtribune.com (43), dawn.com (20).

Data quality of what did land was actually fine — 0 of 1,000 rows with `raw_html`
had empty `full_content`, dates were real parsed dates (not `now()` fallbacks),
language detection sane. The problem was never extraction. It was discovery.

### The VPN finding

`chinadaily.com.cn` responds in **0.5s directly, with no VPN**, from a normal
host. So do globaltimes (0.31s), cgtn (1.26s) and fmprc (0.64s). The 346
"VPN routing failure" rows were largely **misdiagnosed configuration bugs** — a
wrong seed URL on fmprc and a wrong host filter on cgtn. Only `stats.gov.cn`
genuinely struggles, and that is transport (IPv6 blackhole + WAF + firewalled
port 80), not geo-blocking.

**Implication:** the global VPN phase in `process_search_query` — which takes a
lock and serialises every job into a Chinese phase and a normal phase — is
probably unnecessary for 4 of the 5 Chinese sources. Verify reachability from the
*production* host before removing it, since that host may sit on a different
network.

---

## Remaining work, in order

### 1. Install and verify crawl4ai  ← START HERE

```bash
pip install -r requirements.txt
playwright install chromium
python -c "from crawl4ai import AsyncUrlSeeder, SeedingConfig; print('ok')"
```

Then confirm the parts that have **never run**:

- `AsyncUrlSeeder(...).urls(domain, SeedingConfig(...))` — note it takes a **bare
  domain**, not a URL.
- Whether `result.get("relevance_score")` is actually populated with
  `scoring_method="bm25"`, or whether upstream bug
  [#1306](https://github.com/unclecode/crawl4ai/issues/1306) still bites. Code
  already handles absence; you are confirming which path executes.
- The filter/scorer import paths in `deep_crawl.py`, marked UNVERIFIED — docs give
  class names but the module layout was not confirmable.
- `asyncio.run()` bridging inside the existing ThreadPoolExecutor pipeline.

Re-run `python -m pytest backend/discovery/ -q` after install. Tests should still
pass; if `CRAWL4AI_AVAILABLE` flips to `True` and tests fail, the crawl4ai paths
have a real bug — that is the expected place to find one.

### 2. Prove yield before wiring anything

Dry-run `discover()` per site and compare against the 3% baseline. Suggested
order — one low-risk, one that tests a specific fix, one hostile:

| site | why | expectation |
|---|---|---|
| `niice.org.np` | WP API, verified `X-WP-Total: 1154`, low risk | should return ~1,154 |
| `cgtn.com` | tests the `*.cgtn.com` host fix that explains 140 wasted attempts | should return real article URLs on `newsaf`/`newseu`, none on `www` |
| `stats.gov.cn` | hostile transport; validates the force-IPv4/HTTPS/60s/5-retry flags | expect partial success, ~35% |

Record actual counts. If a strategy returns 0 where the profile predicts
thousands, fix the strategy — do **not** relax the profile.

### 3. Re-probe the two unresolved sites

- **`ndri.org.np`** — `enabled: false`, `needs_reprobe: true`. TLS reset from the
  original egress across requests and curl (v4+v6, HTTP/1.1 and /2, forced
  TLS1.2); port 80 answered `301 → https`, so the origin is up and refusing TLS
  from that network. If it works from the production host, profile it properly and
  set `enabled: true`. Its peers (niice, iids) are WordPress, so test
  `/wp-json/wp/v2/posts?per_page=1` first — but **verify, don't assume**.
- **`mmbiztoday.com`** — `confidence: low`. Reported 4,306 posts via WP API; an
  immediate re-probe got HTTP 403 from nginx on every path, most likely a
  rate-limit ban caused by the profiling itself. Dormant since 2022, so low
  stakes. Re-probe gently, from a clean IP, with delay.

### 4. Wire it into the pipeline

This is the step that changes running behaviour. Deliberately left undone.

Target: `queue_manager.run_direct_discovery()` (~line 459) currently does
one-level link expansion for every URL. Replace its body with a profile lookup:

- load `config/site_profiles.json` once
- resolve each seed URL's domain → profile → `base.get_strategy(profile)`
- **`get_strategy()` returns `None`** for disabled or unregistered — fall back to
  the existing link-expansion path rather than dropping the site
- pass the job's keyword through to `discover(keyword=...)` so strategies that
  support server-side search (WP `?search=`, native site search, BM25) use it
- map `DiscoveredURL.published_at` into the existing date filtering so stale URLs
  are dropped **before** `crawl_url_task` fetches them — this is where the 26%
  timeout loss gets recovered
- keep `CrawledURL` writes, SimHash dedup, `KeywordProgress` and the counter
  updates exactly as they are

Preserve: the stop-flag checks (`is_job_stopped`), the watchdog, and per-domain
rate limiting. Consider raising the 45s watchdog once discovery stops feeding it
junk — but measure first.

### 5. Optional follow-ups

- **Reconsider the VPN phase** — see "The VPN finding" above. `KS_DISABLE_VPN=true`
  already exists as an escape hatch.
- **Per-request proxies instead of the global VPN lock.** Routing `.cn` domains
  through a proxy while everything else goes direct, concurrently, would remove
  the job serialisation entirely and stop one proxy failure from stranding 346
  URLs.
- **Cross-job dedup.** SimHash state is per-job and in-memory, so identical
  content crawled under different `search_id`s is not caught — 3 duplicate pairs
  leaked into the matched set.
- **`relevance_score` is useless as a signal today** — it averages 98.5 and is
  almost always exactly 100. Not usable for ranking as currently computed.

---

## Source-list corrections found along the way

`links.xlsx` (25 sources) and the `config/urls.json` derived from it contain three
label errors. `config/site_profiles.json` has the URL side fixed; the labels are
worth correcting at source:

1. **`fmprc.gov.cn` is labelled "National Statistics bureau China"** — it is the
   Ministry of Foreign Affairs.
2. **`stats.gov.cn` is labelled "Ministry of external Affairs, China"** — it is the
   National Bureau of Statistics. (1 and 2 are swapped.)
3. **`mod.gov.np` is labelled "The Kathmandu Post"** — it is Nepal's Ministry of
   Defence. The actual paper is `kathmandupost.com` (~144,900 articles), which is
   profiled in `site_profiles.json` but was **not** in the original source list.

Also: group tags in `config/urls.json` do not follow the spreadsheet's blocks —
the 7 Nepal sources are split across `general` and `northeast_india`, and there is
no `nepal` group. This matters because `url_classifier.load_chinese_sources()`
reads `group == "china"` from that file to decide VPN routing. The China block is
correct, so routing is unaffected today.

---

## Verification standard used here

Every value in `config/site_profiles.json` was obtained by fetching the live site,
not by inference. Where a claim could not be verified it is recorded as
**untested, not absent** (`ndri.org.np`) or downgraded (`mmbiztoday.com`,
`confidence: low`). Two claims made by profiling agents were **disproved** on
re-check and corrected:

- `iids.org.np` "142 portfolio entries" → no `portfolio` post type exists (REST
  404). Verified posts=270, pages=34; total corrected 412 → 304.
- `mmbiztoday.com` working WP API → 403 on re-probe.

Please hold to this standard. The 3% baseline is largely the product of
plausible-looking assumptions that were never checked — a wrong seed URL, a wrong
host filter, and a sitemap that has returned HTTP 200 while being frozen since
2014.
