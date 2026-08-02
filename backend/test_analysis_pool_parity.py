"""
backend/test_analysis_pool_parity.py — correctness test for Phase 1
(offloading Crawler._analyze_page_impl to a ProcessPoolExecutor).

Phase 1's entire premise is that moving analysis off the main thread and into
a worker process changes *speed*, not *results* - _analyze_page_impl has no
dependency on live Crawler instance state (all its helper calls are
@staticmethods), so calling it directly in-process must produce byte-for-byte
identical output to calling it through backend.queue_manager._run_analysis()
(the real production code path, which submits it to the shared analysis
process pool and falls back in-thread only if that pool is broken).

The fixture HTML below deliberately includes an explicit
`article:published_time` meta tag so `discovered_at` is derived from that
value rather than falling back to `datetime.now(timezone.utc)` (analyze_page's
"no date found" default) - that fallback is the one genuinely non-deterministic
path in this function, and using it here would make two calls a few
milliseconds apart compare unequal for a reason that has nothing to do with
correctness. With that value pinned, every field this function returns is a
pure function of its arguments, so we assert full dict equality rather than
a field-by-field allowlist.

Run with: python3 -m pytest backend/test_analysis_pool_parity.py -v
"""
from __future__ import annotations

import backend.queue_manager as qm
from backend.crawler import _analyze_page_impl

FIXTURE_HTML = """
<html lang="en">
<head>
    <title>Sample Keyword News Article</title>
    <meta name="description" content="A short description mentioning keyword_news for testing.">
    <meta property="article:published_time" content="2026-01-15T08:00:00Z">
    <meta name="author" content="Jane Reporter">
    <meta property="og:image" content="https://example.com/lead.jpg">
    <script type="application/ld+json">
    {"@type": "NewsArticle", "author": {"name": "Jane Reporter"}}
    </script>
</head>
<body>
    <article>
        <h1>Sample Keyword News Article</h1>
        <p>This article discusses keyword_news extensively. The term
        keyword_news appears multiple times to exercise density scoring
        and occurrence counting deterministically.</p>
        <p>Unrelated filler paragraph to pad out the body text so the
        extraction pipeline (trafilatura primary, BeautifulSoup fallback)
        has enough content to work with reliably across both call paths.
        More filler text follows here to increase the word count further
        for a stable density calculation regardless of which extractor
        path is taken on a given run.</p>
        <img src="https://example.com/photo1.jpg" alt="photo">
    </article>
</body>
</html>
"""

URL = "https://example.com/news/sample-keyword-news-article"
KEYWORD = "keyword_news"


def _assert_deterministic_and_equal(direct: dict, via_pool: dict) -> None:
    assert direct.keys() == via_pool.keys(), (
        f"key sets differ: only-direct={direct.keys() - via_pool.keys()} "
        f"only-via-pool={via_pool.keys() - direct.keys()}"
    )
    # Full equality, not a field allowlist - the fixture pins the one field
    # (discovered_at) that could otherwise be non-deterministic, so nothing
    # here is expected to legitimately differ between the two call paths.
    assert direct == via_pool, (
        "in-process and process-pool analysis results diverged:\n"
        + "\n".join(
            f"  {k}: direct={direct[k]!r} via_pool={via_pool[k]!r}"
            for k in direct
            if direct[k] != via_pool[k]
        )
    )


def test_analyze_page_impl_identical_direct_vs_process_pool():
    """The core Phase 1 correctness guarantee: same inputs, same outputs,
    whether _analyze_page_impl runs in this thread or in a pooled worker
    process reached via backend.queue_manager._run_analysis()."""
    direct = _analyze_page_impl(
        html_content=FIXTURE_HTML,
        url=URL,
        keyword=KEYWORD,
        match_type="phrase",
        case_sensitive=False,
        exact_match=False,
    )
    via_pool = qm._run_analysis(
        FIXTURE_HTML, URL, KEYWORD, "phrase", False, False
    )

    # Sanity: prove the fixture actually exercises a real match, not two
    # equally-empty results that would trivially compare equal.
    assert direct["matched"] is True
    assert direct["occurrences"] > 0
    assert direct["title"] == "Sample Keyword News Article"
    assert direct["author"] == "Jane Reporter"
    assert direct["discovered_at"].isoformat() == "2026-01-15T00:00:00+00:00"

    _assert_deterministic_and_equal(direct, via_pool)


def test_analyze_page_impl_identical_direct_vs_process_pool_keyword_free():
    """Same parity guarantee for the keyword-free ("__config__" no-keyword
    crawl) code path, which takes a materially different branch inside
    _analyze_page_impl (matched is unconditionally True, snippet/relevance
    are computed differently)."""
    direct = _analyze_page_impl(
        html_content=FIXTURE_HTML,
        url=URL,
        keyword="",
        match_type="phrase",
        case_sensitive=False,
        exact_match=False,
    )
    via_pool = qm._run_analysis(FIXTURE_HTML, URL, "", "phrase", False, False)

    assert direct["matched"] is True  # keyword-free path always matches
    _assert_deterministic_and_equal(direct, via_pool)


def test_analyze_page_impl_identical_direct_vs_process_pool_boolean_match_type():
    """Same parity guarantee for match_type='boolean', which routes through
    Crawler.evaluate_boolean_query (converted to @staticmethod as part of
    this extraction) instead of the simple OR-of-terms logic."""
    direct = _analyze_page_impl(
        html_content=FIXTURE_HTML,
        url=URL,
        keyword='keyword_news AND "Sample Keyword"',
        match_type="boolean",
        case_sensitive=False,
        exact_match=False,
    )
    via_pool = qm._run_analysis(
        FIXTURE_HTML, URL, 'keyword_news AND "Sample Keyword"', "boolean", False, False
    )

    assert direct["matched"] is True
    _assert_deterministic_and_equal(direct, via_pool)
