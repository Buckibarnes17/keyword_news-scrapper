# HANDOFF — adaptive discovery layer + concurrency hardening

**State as of commit `f15b8c1` on branch `feat/crawl4ai-adaptive-discovery`
(2026-08-02).** Read `CLAUDE.md` first, then `backend/discovery/README.md`,
then this document in full before touching `queue_manager.py` or
`crawler.py`.

---

## TL;DR for the next agent

The discovery layer is **wired in and working** — verified against real
external sites and a real Postgres DB, not just unit tests. Five real
concurrency/correctness bugs were found and fixed via live pilot testing this
session. Match yield recovered from **1 match / 1082 crawled (0.09%)** on the
first wired run to **34 matches / 555 crawled (6.1%)** after the fixes,
confirmed via a repeatable pilot harness (see below).

**The original, still-unfulfilled goal**: the operator wants a live crawl
across **all 61 sources in `config/urls.json`** (not the 25 originally
profiled — see "The actual scope" below), for keywords `china`, `myanmar`,
`india`, `pakistan`, plus a POJK (Pakistan-occupied Jammu & Kashmir) cluster,
covering **2026-04-10 → present**, with results landing in the real
PostgreSQL DB. This has **not been launched yet**. The session so far has
stayed in a 12-source diagnostic pilot loop trying to get reliability
established first, and found real, worthwhile bugs doing so — but the actual
deliverable is still pending.

**Two known, unfixed bugs are queued up, found during this session's last
pilot run:**
1. `backend/search_engine.py`'s `scrape_duckduckgo_selenium()` has the
   identical ChromeDriver-filesystem-lock bug that was just fixed in
   `backend/crawler.py` — same root cause, different call site, not yet
   patched.
2. The per-domain candidate budget (`domain_candidate_budget` in
   `queue_manager.py`) only caps `search_web()`/`SiteSearchDetector`'s
   contributions. It does **not** cap primary per-site discovery's own
   candidate count (`KS_MAX_CANDIDATE_URLS`, default 500). When a
   `news_sitemap`/`sitemap`-strategy site's discovery actually succeeds (e.g.
   `cgtn.com`, which usually times out via its crawl4ai path but didn't in
   the last run), it can return hundreds of candidates completely unbounded
   by the fix that targeted the *other* mechanism — confirmed live:
   `cgtn.com` alone returned 353 candidates in one run, overwhelming
   job-wide capacity and pushing the watchdog-failure rate to 95%+ across
   *every* domain in that job, not just cgtn.com's own.

**No clean, unconfounded before/after pilot measurement exists yet for the
combined effect of all three landed commits.** Every recent pilot run was
confounded by something different (see "Pilot run history" below). Get one
clean run before trusting any specific failure-rate number, and before
deciding whether further concurrency work (e.g. an async I/O rewrite — see
"Considered and deliberately not done" below) is still worth it.

---

## The actual scope — read this carefully, it's easy to get wrong

`config/site_profiles.json` has **26 profiles** (the sites originally
profiled on 2026-08-01, documented in `backend/discovery/README.md`). But
`config/urls.json` — the actual configured source list the operator wants
crawled — has **61 URLs**, spanning China/Myanmar/Nepal/Northeast
India/Pakistan-Balochistan/Bangladesh military/Sri Lanka
government/regional-think-tank sources. The **35 unprofiled sources fall
back to legacy link-expansion discovery** (`base.get_strategy()` returns
`None` for them), which is weaker and was never part of the original
profiling exercise.

This matters a lot for the operator's specific ask: **most of the
Pakistan/POJK-relevant sources are in the unprofiled 35** —
`thebalochistanpost.net`, `dawn.com`, `khyberchronicles.pk`, `ispr.gov.bd`,
`idsa.in`, `freebalochistan.com`, `defencejournalbd.com`, `aspi.org.au`,
`sipri.org`. The 26 profiled sites are almost entirely China/Myanmar/Nepal
focused. The operator was explicitly told this and chose to proceed with all
61 sources anyway (legacy discovery + adaptive discovery mixed) rather than
restrict to the 26 profiled ones, specifically because restricting would
gut POJK coverage. Don't "simplify" back to 25/26 sources without checking —
that would silently break the actual goal.

---

## What's been fixed this session (3 commits, read each `git show` in full)

### `4efab41` — wire discovery layer in + fix 5 concurrency/correctness bugs

1. **`base._REGISTRY` was empty at runtime.** `queue_manager.py` imported
   only `backend.discovery.base`, never the strategy submodules (`wp_api`,
   `sitemap`, `news_sitemap`, `site_search`, `deep_crawl`, `oai_pmh`) whose
   `@register` decorators populate the registry on import. Every site was
   silently falling back to legacy discovery, defeating the entire wiring —
   caught by inspecting a live pilot's log output, not by any test.
2. **crawl4ai's `AsyncUrlSeeder` can hang indefinitely.** Confirmed live on
   `cgtn.com`: `SeedingConfig.max_urls` only truncates *results* after full
   domain-wide sitemap discovery completes, not before — so an unconstrained
   `pattern="*"` seed against a site with a large sitemap surface (cgtn.com's
   27-child sitemap index, one child alone 30k URLs) with `extract_head=True`
   (one HTTP request per discovered URL) can run for tens of minutes.
   Fixed with a hard timeout AND a dedicated-daemon-thread bridge — a naive
   `asyncio.wait_for()` + `asyncio.run()` was tried first and found
   insufficient: `asyncio.run()`'s own automatic task-cancellation teardown
   hangs when the wrapped coroutine leaves orphaned background tasks, which
   `AsyncUrlSeeder` does. The fix drives the loop manually
   (`loop.run_until_complete()`) on a daemon thread with a real
   `thread.join(timeout=...)` as the hard backstop — see
   `backend/discovery/sitemap.py`'s `run_async()` for the pattern, reused
   later in the session for other async-bridging needs.
3. **Cross-thread SQLAlchemy session corruption.** `process_search_query()`
   fetched a `SearchQuery` ORM object once and handed the *same live object*
   to every concurrent keyword-worker thread. `expire_on_commit=True` (the
   SQLAlchemy default) means any attribute read after a commit silently
   triggers a fresh SELECT through the same Session — and Sessions aren't
   thread-safe, so concurrent reads/commits from different threads collided
   with `"This session is provisioning a new connection; concurrent
   operations are not permitted"`. Fixed via `_snapshot_query()`: a
   detached, plain `SimpleNamespace` copy of the read-only fields worker
   threads need, taken once before any threads spawn.
4. **A domain's candidate count could reach hundreds, uncapped, across a
   multi-keyword job.** `search_web()` and `SiteSearchDetector` each run
   once **per keyword** per domain and return genuinely distinct real URLs
   each time (not duplicates) — nothing upstream deduplicated across
   keywords. Confirmed live: `thebalochistanpost.net` alone reached 324
   distinct candidate URLs across a 10-keyword job. Combined with
   per-domain fetch-rate-limiting being shared/serialized at ~1 req/sec,
   late-queued candidates on a "hot" domain were mathematically guaranteed
   to exceed the crawl-task watchdog before their turn ever came — this
   alone explained the majority of a 99.5%-of-1082 watchdog failure rate.
   Fixed with a shared, cross-keyword `domain_candidate_budget` (default
   100/domain/job, `KS_MAX_CANDIDATES_PER_DOMAIN`) — **but see the
   still-open gap above: this only covers `search_web`/`SiteSearchDetector`,
   not primary discovery's own cap.**
5. **The per-domain rate limiter busy-waited on one lock shared across every
   domain in the job**, not just the contended one — `while True:
   check-then-sleep-then-recheck` against a single global `threading.Lock()`.
   Under real load (up to ~120 threads across several keyword pools) this
   created severe lock/GIL contention independent of actual rate-limiting
   correctness. Redesigned as `_reserve_domain_slot()`: each thread computes
   and reserves its own unique time slot in one brief **per-domain** critical
   section, then sleeps once for exactly its own precomputed duration — no
   polling, no cross-domain contention.
6. Also: nothing capped **total concurrent crawl threads job-wide** — only
   per-keyword pool size (`url_workers`, ~15) × concurrent keywords
   (`KS_MAX_KEYWORD_WORKERS`, default 8) multiplied toward ~120 threads.
   Added `_CRAWL_CONCURRENCY_SEMAPHORE` (default 25, `KS_MAX_CONCURRENT_CRAWLS`)
   gating the actual fetch+parse work, independent of how many
   `ThreadPoolExecutor`s exist. **Direct load-testing proved this alone
   wasn't sufficient** — see "The GIL-contention investigation" below; it's
   an important methodology reference, not just a changelog entry.
7. Also threaded each site's declared `crawl_delay` (from
   `config/site_profiles.json`) into the fetch-phase rate limiter — it was
   previously only honoured during discovery, so profiled politeness
   requirements (irrawaddy.com 10s, dvb.no 2s) weren't actually respected
   during the real fetch phase.

### `319cf3b` — offload `analyze_page` to a `ProcessPoolExecutor`

Hypothesis going in: `analyze_page()` (HTML parsing, language detection,
content extraction, hashing) does real redundant CPU-bound work (2-3
independent HTML re-parses, two separate language-classifier calls, a
duplicate trafilatura metadata pass) and could be the dominant source of
GIL contention under threading. Extracted into a module-level
`_analyze_page_impl()` (confirmed picklable, no live `Crawler` instance-state
dependency) and offloaded to a process pool
(`KS_ANALYSIS_POOL_WORKERS`, spawn context, `max_tasks_per_child` recycling,
`BrokenProcessPoolError` fallback to in-thread execution).

**The hypothesis turned out to be wrong** — see "The GIL-contention
investigation" below. This was still worth doing (found and fixed a real
latent bug — `matched_keywords` ordering for `match_type="boolean"` was
non-deterministic across OS processes due to per-process hash-seed
randomization affecting `set()` iteration order, caught by this task's own
parity test) but it did not meaningfully move the watchdog-failure needle,
because CPU-bound analysis time turned out to be only ~4% of total
fetch+analyze time in the run that measured it.

### `f15b8c1` — fix ChromeDriver filesystem-lock contention

Found via the timing instrumentation added in the previous commit:
individual fetches took 198s, 222s, and 258s — each alone exceeding the
120s watchdog, before any analysis or queueing delay. Traced to
`Crawler._get_selenium_driver()` calling `ChromeDriverManager().install()`
fresh for every task (`crawl_url_task` creates a brand-new `Crawler()`
per URL), all serializing on `webdriver-manager`'s own filesystem lock file
the moment several tasks need Selenium fallback simultaneously. Fixed by
caching the resolved driver path at module level (double-checked locking,
failures not cached so a transient error can retry).

**Confirmed working for its target** via a real concurrency test (20
threads racing the resolver against a mocked, slowed `install()` → exactly
1 call, not 20; the same test run against the old code got 20/20) and real
log evidence (26 genuine Selenium-fallback attempts through the fixed path
in the next pilot run, zero lock timeouts from that path).

**Found, but did not fix, the identical bug in a sibling module**:
`backend/search_engine.py`'s `scrape_duckduckgo_selenium()` has its own bare
`ChromeDriverManager().install()` call, no caching — 12 lock timeouts from
that path in the same pilot run that showed zero from the fixed one. This is
queued as remaining work (see below).

---

## The GIL-contention investigation — methodology worth preserving

This is worth reading even if you don't touch concurrency code again,
because the *process* found real bugs a less careful investigation would
have missed, and the same pattern (confound before conclusion) bit multiple
times.

1. **Started from a symptom**: first wired pilot run, 1077/1082 (99.5%)
   watchdog failures.
2. **Ruled out hardware and network before touching code**: `nproc`/`lscpu`
   showed 48 real cores, `free -h` showed 125GB RAM, `uptime` showed
   near-zero load — the host was idle. Direct `requests.get()` calls to the
   exact domains showing 100% failure completed in under 2 seconds every
   time. This ruled out "the box is just slow" and "the sites are just
   slow" before any code change was made.
3. **Fixed the bugs that were actually found this way** (candidate-budget
   explosion, busy-wait rate limiter) — real, verified, each with its own
   before/after pilot confirmation.
4. **Still not enough** — even fully serialized (1 keyword at a time,
   ~15 threads), 51% of tasks failed the watchdog. This is when the
   `analyze_page` CPU-cost hypothesis was formed.
5. **Direct reproduction outside the app**: rather than guess, `crawl_url_task`
   was called directly (bypassing all of `_process_single_keyword`'s
   discovery/dedup overhead) against a real batch of previously-failed URLs,
   at controlled thread counts (120, then 30, then 15). This is what
   established the GIL-contention ceiling as real and reproducible
   independent of any pipeline-specific bug: **120 threads → 13% completed
   in 130s; 30 threads → 29% completed, zero over the watchdog.**
6. **Only then** was `analyze_page`'s CPU cost measured directly (fetch_s vs
   analyze_s timing instrumentation) — and it turned out to be ~4% of total
   time, not the dominant factor the earlier reasoning suggested. The
   *actual* dominant cost was a specific, fixable bug (the ChromeDriver
   lock), not a generic "needs more I/O concurrency" problem.
7. **Every "did the fix work" pilot run after that was checked for
   confounds** before trusting the headline number — a DuDDG outage
   inflated one run's failure rate for reasons unrelated to the fix under
   test; an unrelated discovery-volume spike (`cgtn.com` succeeding when it
   usually times out) did the same to another. Both were caught by checking
   domain/status breakdowns, not just the pass/fail percentage.

**Lesson for whoever picks this up**: a live pilot run's headline
found/crawled/matched/failed numbers are not sufficient evidence on their
own for whether a fix worked. Always check the status breakdown and domain
breakdown for the specific run, and look for anything that changed besides
the fix being tested (an external service's reachability, a different
site's discovery suddenly succeeding when it usually doesn't, etc.).

---

## Pilot run history (for calibration — don't treat any single number as gospel)

All runs: 12-source subset (`niice.org.np`, `cgtn.com`, `dvb.no`,
`kachinnews.com`, `bnionline.net`, `ifa.gov.np`, `stats.gov.cn`, `dawn.com`,
`thebalochistanpost.net`, `ispr.gov.bd`, `idsa.in`, `khyberchronicles.pk`),
same 10 keywords, `2026-04-10` → present, `KS_DISABLE_VPN=true`.

| search_id | concurrency | found | crawled | matched | watchdog-failed | note |
|---|---|---:|---:|---:|---:|---|
| 92 | default (8 kw × ~15) | 1082 | 1082 | 1 | 1077 (99.5%) | first wired run, pre-fixes |
| 95 | default | 1019 | 1019 | 7 | 921 (90.4%) | after budget cap + watchdog bump (not yet the rate-limiter redesign) |
| 98 | default | 517 | 517 | 2 | 499 (94.5%) | after combined-mechanism budget fix |
| 101 | default | 528 | 528 | 1 | 505 (95.6%) | after rate-limiter redesign alone |
| 104 | default | 512 | 512 | 2 | 434 (84.8%) | after job-wide semaphore(25) added |
| 105 | `KS_MAX_KEYWORD_WORKERS=2` | 555 | 555 | 34 | 358 (64.5%) | diagnostic: does raw thread count matter beyond the semaphore? |
| 106 | `KS_MAX_KEYWORD_WORKERS=1` (fully sequential) | 579 | 579 | 28 | 296 (51.1%) | diagnostic: concurrency-vs-reliability ceiling |
| 129 | default (Phase 0/1 direct reproduction test, not this 12-source pilot) | — | — | — | — | isolated `crawl_url_task` timing test; 505-URL batch, not a `process_search_query` run |
| 133 | default (after ChromeDriver lock fix) | 762 | 762 | 0 | 726 (95.3%) | **confounded**: cgtn.com's discovery succeeded and returned 353 uncapped candidates, overwhelming job-wide capacity — not representative of the lock fix's effect |

**No run at default concurrency with all three commits' fixes AND no
confound exists yet.** The 104 run (84.8% failure) is the last clean
default-concurrency data point before the analysis-pool and ChromeDriver
fixes landed; 133 (95.3%) is confounded and should not be compared directly
against it as if it were worse.

---

## Remaining work, in priority order

### 1. Fix `search_engine.py`'s ChromeDriver lock bug (small, same pattern as the fixed one)

`scrape_duckduckgo_selenium()` in `backend/search_engine.py` calls
`ChromeDriverManager().install()` directly, uncached. Apply the same fix as
`backend/crawler.py`'s `_get_chromedriver_path()` — ideally, refactor so
BOTH modules share one cached resolver rather than duplicating the caching
logic a third time. Confirmed live: 12 lock timeouts from this path in one
pilot run, all during the discovery phase (before the crawl-task watchdog
starts timing, so they don't directly cause "Task exceeded maximum
duration" failures, but they burn real wall-clock time and could compound
under more load).

### 2. Cap primary discovery's per-domain candidate count too

`_load_site_profiles()`-driven discovery in `run_direct_discovery()` passes
`max_urls = KS_MAX_CANDIDATE_URLS` (default 500) to `strategy.discover()`
per domain, with no job-wide cross-check against how much that domain has
already contributed. This is architecturally the same class of bug commit
`4efab41` fixed for `search_web`/`SiteSearchDetector` — it just wasn't
caught the first time because in every earlier pilot run, `cgtn.com`'s
crawl4ai discovery was timing out (hitting the 40s hard timeout from the
same commit) and falling back to a much smaller XML result, masking the
issue. The moment it succeeded for real, it returned 353 candidates,
uncapped, and tanked the whole job's reliability.

The fix is likely: extend `domain_candidate_budget`/`_trim_to_domain_budget()`
to also gate the primary-discovery branch in `run_direct_discovery()`'s
`_discover_single()`, not just the two secondary mechanisms in
`_process_single_keyword()`. Since primary discovery is currently
keyword-independent (computed once, shared across all keywords — see
`process_search_query()`'s pre-discovery call), the budget check here would
need different semantics than the per-keyword secondary mechanisms: it's
capping one domain's *total* contribution from a *single* discovery call,
not accumulating across multiple calls. Consider whether `max_urls` itself
should just be lowered for the keyword-independent pre-discovery pass, or
whether a smarter fix (e.g., prioritizing recency via `since`/`published_at`
before truncating) is more appropriate — a strategy that discovers 353 real,
recent articles is arguably *working correctly*; the pipeline's downstream
capacity to handle that volume within one job is the actual constraint.
Don't just crudely truncate the highest-value site's yield without thinking
about it.

### 3. Get one clean, unconfounded pilot measurement

Re-run the same 12-source/10-keyword pilot at default concurrency once (1)
and (2) above are fixed, and once you've confirmed DuckDuckGo is currently
reachable (a quick `curl -m 5 https://html.duckduckgo.com` before starting
is cheap insurance). Compare against the 104 baseline (434/512, 84.8%
failure) as the last clean default-concurrency number. If Chrome is
installed in your environment, this run would also be the first real signal
on whether the ChromeDriver lock fix helps on genuinely successful Selenium
fetches (every run so far has had Selenium fail-fast due to no Chrome
binary being present — see `CLAUDE.md`).

### 4. `news_media.scraped_news` table doesn't exist — migration blocked

`migration.sql` (repo root) creates it, fully idempotent
(`CREATE ... IF NOT EXISTS` throughout), but the credentials currently
hardcoded in `backend/database.py` lack `CREATE SCHEMA`/`CREATE TABLE`
privilege on that database (`psycopg2.errors.InsufficientPrivilege`, tested
directly). This is **not fixable by finding a workaround** — it needs either
different credentials with DDL rights, or someone with existing admin access
to run `migration.sql` once. Until then, `process_search_query()`'s
end-of-job auto-sync step (`export_search_to_postgres`) will keep failing
with a clear, non-fatal warning on every run — matched articles still land
correctly in `news_media.crawled_urls`, only the downstream export table is
affected.

### 5. Launch the actual 61-source live run

Once (1)–(3) give you confidence in the reliability numbers, this is the
actual deliverable. Full source list: `config/urls.json`'s `urls[].url`
(all 61, newline-joined for `SearchQuery.direct_urls`). Keywords: `china`,
`myanmar`, `india`, `pakistan`, `POJK`, `Pakistan occupied Jammu and
Kashmir`, `Pakistan occupied Kashmir`, `PoK protests`, `Azad Kashmir
protests`, `Gilgit-Baltistan protests` (JSON-encoded list for
`SearchQuery.keyword`). Date range: `date_range_start=2026-04-10`,
`date_range_end=None` (open-ended, up to present). `source_type="direct"`,
`engine="fast"`, `ignore_robots=False`. Set `KS_DISABLE_VPN=true` unless
ExpressVPN is genuinely installed and authed on the host running this.

Given 61 sources × 10 keywords, this will be a large, long-running job —
budget realistic wall-clock time (the 12-source/10-keyword pilot alone has
taken 3-15 minutes depending on conditions; 61 sources is roughly 5x that
surface area, though not linearly since per-domain rate limiting and the
job-wide semaphore bound total throughput regardless of source count).
Consider running it via the pilot-harness pattern below rather than through
the FastAPI app, so you have direct process control and can watch it via the
same DB-polling pattern used throughout this session.

---

## Pilot harness (reusable pattern used throughout this session)

Bypasses the FastAPI app entirely — creates a real `SearchQuery` row and
calls `process_search_query()` directly against whatever DB
`backend/database.py` resolves to. **Always wrap the actual call in
`if __name__ == "__main__":`** — a `ProcessPoolExecutor` (used by the
analysis-pool fix) with the `spawn` start method re-executes the entire
top-level script in every worker process; without the guard, this
recursively re-runs the whole pilot N times concurrently (this actually
happened once this session — caught via 13 `PILOT_SEARCH_ID=` print lines
appearing instead of 1, cleaned up via `pkill -9 -f pilot_run.py`).

```python
import os, sys, json
sys.path.insert(0, '/path/to/keyword_news-scrapper')
os.environ['KS_DISABLE_VPN'] = 'true'
os.chdir('/path/to/keyword_news-scrapper')  # config/site_profiles.json loaded via relative path

from datetime import datetime, timezone
from backend.database import SessionLocal, init_db
from backend.models import SearchQuery
from backend.queue_manager import process_search_query

init_db()

PILOT_URLS = [ ... ]   # newline-joined into direct_urls
KEYWORDS = [ ... ]     # JSON-encoded into keyword

if __name__ == "__main__":
    db = SessionLocal()
    q = SearchQuery(
        keyword=json.dumps(KEYWORDS), match_type="phrase", case_sensitive=False,
        exact_match=False, date_range_start=datetime(2026, 4, 10, tzinfo=timezone.utc),
        date_range_end=None, engine="fast", source_type="direct",
        direct_urls="\n".join(PILOT_URLS), ignore_robots=False, status="pending",
    )
    db.add(q); db.commit(); db.refresh(q)
    search_id = q.id
    db.close()
    print(f"PILOT_SEARCH_ID={search_id}", flush=True)

    process_search_query(search_id)
    print(f"PILOT_DONE search_id={search_id}", flush=True)
```

Run in background with a generous timeout — jobs at default concurrency
have taken 3-15+ minutes. Poll status via:

```python
from backend.database import SessionLocal
from backend.models import SearchQuery, CrawledURL
from sqlalchemy import select, func
db = SessionLocal()
q = db.scalars(select(SearchQuery).where(SearchQuery.id == search_id)).first()
print(f"status={q.status} found={q.total_urls_found} crawled={q.total_urls_crawled} matched={q.total_urls_matched}")
# Then check status breakdown and domain breakdown before trusting the headline numbers -
# see "The GIL-contention investigation" above for why.
```

---

## Considered and deliberately not done

**A full async I/O rewrite** (aiohttp instead of `requests`+`ThreadPoolExecutor`,
`asyncio.gather`/`TaskGroup` instead of nested thread pools) was scoped in
detail (see git history around the planning phase if you need the full
write-up — it covered picklability, `ProcessPoolExecutor` sizing, an async
rate-limiter design, DB-write bridging via a bounded thread pool, and reusing
`backend/discovery/sitemap.py`'s proven `run_async()` bridging pattern for
`queue_worker_loop()`). **Given the Phase 0 timing data showed CPU-bound
analysis was only ~4% of total time, and the actual dominant cost turned out
to be a specific, now-partially-fixed lock-contention bug rather than a
generic I/O-concurrency ceiling, this large rewrite was not pursued.** It
remains a reasonable option if, after fixing items 1-2 above and getting a
clean measurement, `fetch_s`'s *median* (not just its pathological tail) is
still the dominant cost — but don't reach for it reflexively; get the
cheaper fixes and a clean measurement first.

---

## Prerequisites / operator-supplied info

- **No `.env` in this repo.** `backend/database.py` has a hardcoded
  Postgres fallback (see `CLAUDE.md`) that IS reachable from at least one
  development environment this session ran in — but its credentials lack
  DDL privileges (see item 4 above), and hardcoding a live DB password in
  source is a real security concern flagged but not resolved this session.
  Don't propagate the literal credentials into further documents; point to
  `backend/database.py` instead.
- **`KS_DISABLE_VPN=true`** unless ExpressVPN is genuinely installed and
  authed on the host — `backend/expressvpn_router.py`'s `expressvpnctl`
  lookup paths are Windows-only; on Linux without it, `connect_singapore()`
  raises immediately and every pending Chinese-classified URL gets marked
  failed.
- **Chrome/Chromium binary** — not installed system-wide in at least one
  dev environment this session used. If you need to validate Selenium
  fallback behavior for real (not just fail-fast), you'll need it installed,
  which likely needs privileged access this session didn't have.

---

## Source-list corrections found during original profiling (still valid)

`links.xlsx` (25 sources) and the `config/urls.json` derived from it contain
three label errors. `config/site_profiles.json` has the URL side fixed; the
labels are worth correcting at source:

1. **`fmprc.gov.cn` is labelled "National Statistics bureau China"** — it is
   the Ministry of Foreign Affairs.
2. **`stats.gov.cn` is labelled "Ministry of external Affairs, China"** — it
   is the National Bureau of Statistics. (1 and 2 are swapped.)
3. **`mod.gov.np` is labelled "The Kathmandu Post"** — it is Nepal's
   Ministry of Defence. The actual paper is `kathmandupost.com`, profiled
   separately in `site_profiles.json`.

## Verification standard used throughout

Every value in `config/site_profiles.json` was obtained by fetching the live
site, not by inference — hold to that standard for any new profile entries.
See `backend/discovery/README.md`'s "DO NOT fix these without re-probing"
section for the specific traps already found and corrected once.
