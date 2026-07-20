import React, { useState, useEffect } from 'react'
import useAppStore from '../store/appStore'
import { api } from '../api/client'
import SearchTab from './tabs/SearchTab'
import DirectTab from './tabs/DirectTab'
import ConfigTab from './tabs/ConfigTab'
import Accordion from '../components/ui/Accordion'

export default function NewCrawlView() {
  const {
    activeView,
    setActiveView,
    crawlTab,
    setCrawlTab,
    setActiveSearch,
    setKeywordsConfig,
    setUrlsConfig,
    setConfigSelected,
    configSelected,
    configSelectedKeywords,
    setConfigSelectedKeywords,
    torEnabled,
    torReachable,
    torProxyUrl,
    setTorState,
    setSchedulePrefill,
    newCrawlKeyword,
    setNewCrawlKeyword
  } = useAppStore()

  // Form states
  const [keyword, setKeyword] = useState('')
  const [directUrls, setDirectUrls] = useState('')
  const [disableKeywordFilter, setDisableKeywordFilter] = useState(false)
  const [directKeywords, setDirectKeywords] = useState('')

  // Config tab state
  const [configDisableFilter, setConfigDisableFilter] = useState(false)

  // Advanced settings states
  const [caseSensitive, setCaseSensitive] = useState(false)
  const [exactMatch, setExactMatch] = useState(false)
  const [ignoreRobots, setIgnoreRobots] = useState(false)
  const [matchType, setMatchType] = useState('phrase') // 'phrase' | 'boolean'
  const [engine, setEngine] = useState('fast') // 'fast' | 'lightpanda' | 'dynamic'
  const [languageFilter, setLanguageFilter] = useState('')
  const [domainsInclude, setDomainsInclude] = useState('')
  const [domainsExclude, setDomainsExclude] = useState('')
  const [dateStart, setDateStart] = useState('')
  const [dateEnd, setDateEnd] = useState('')

  // Tor check states
  const [torLoading, setTorLoading] = useState(false)
  const [torError, setTorError] = useState('')

  const [submitting, setSubmitting] = useState(false)

  // Sync newCrawlKeyword from rocket launch
  useEffect(() => {
    if (newCrawlKeyword) {
      setKeyword(newCrawlKeyword)
      setDirectKeywords(newCrawlKeyword)
      setNewCrawlKeyword('')
    }
  }, [newCrawlKeyword])

  // Load config keywords on mount
  useEffect(() => {
    const loadKeywords = async () => {
      try {
        const data = await api.getConfigKeywords()
        setKeywordsConfig(data)
      } catch (err) {
        console.error('Failed to load keywords config:', err)
      }
    }
    loadKeywords()
  }, [])

  // Tor pre-flight check handler
  const handleTorToggle = async () => {
    if (torEnabled) {
      // Toggle off is simple
      setTorState({ torEnabled: false, torReachable: false, torProxyUrl: null })
      setTorError('')
      return
    }

    setTorLoading(true)
    setTorError('')
    try {
      const res = await api.getTorStatus()
      if (res.reachable) {
        setTorState({
          torEnabled: true,
          torReachable: true,
          torProxyUrl: res.proxy_url || 'socks5h://127.0.0.1:9050'
        })
      } else {
        setTorError(res.error_message || 'Tor service is not reachable on localhost port 9050.')
        setTorState({ torEnabled: false, torReachable: false, torProxyUrl: null })
      }
    } catch (err) {
      setTorError('Failed to query Tor status endpoint. Make sure the backend is active.')
      setTorState({ torEnabled: false, torReachable: false, torProxyUrl: null })
    } finally {
      setTorLoading(false)
    }
  }

  // Handle launch crawl
  const handleSubmit = async (e) => {
    e.preventDefault()
    setSubmitting(true)

    try {
      let targetKeyword = ''
      let targetDirectUrls = null

      if (crawlTab === 'search') {
        targetKeyword = keyword.trim()
      } else if (crawlTab === 'direct') {
        targetKeyword = disableKeywordFilter ? '' : directKeywords.trim()
        targetDirectUrls = directUrls.trim()
      } else if (crawlTab === 'config') {
        // Validate URL selection
        if (configSelected.size === 0) {
          alert('No URLs selected from the config file. Select at least one URL to crawl.')
          setSubmitting(false)
          return
        }

        // Validate keyword selection (unless scrape-all is enabled)
        if (!configDisableFilter && configSelectedKeywords.size === 0) {
          alert('No keywords selected. Select at least one keyword, or enable "Scrape all pages" to crawl without keyword filtering.')
          setSubmitting(false)
          return
        }

        // Build keyword string: comma-joined selected keywords
        // The backend splits on comma/newline and runs each keyword in parallel
        // against all selected URLs — all selected keywords vs all selected URLs
        targetKeyword = configDisableFilter
          ? ''
          : Array.from(configSelectedKeywords).join(',')

        targetDirectUrls = Array.from(configSelected).join('\n')
      }

      const payload = {
        keyword: targetKeyword,
        match_type: matchType,
        case_sensitive: caseSensitive,
        exact_match: exactMatch,
        ignore_robots: ignoreRobots,
        engine: engine,
        source_type: crawlTab === 'config' ? 'direct' : crawlTab,
        direct_urls: targetDirectUrls,
        proxy_url: torEnabled ? torProxyUrl : null
      }

      // Add domains filter
      const inc = domainsInclude.trim()
      const exc = domainsExclude.trim()
      if (inc || exc) {
        payload.domains_filter = {
          include: inc ? inc.split(',').map(d => d.trim().toLowerCase()) : [],
          exclude: exc ? exc.split(',').map(d => d.trim().toLowerCase()) : []
        }
      }

      // Add languages
      const langs = languageFilter.trim()
      if (langs) {
        payload.languages_filter = langs.split(',').map(l => l.trim().toLowerCase())
      }

      // Add dates
      if (dateStart) payload.date_range_start = new Date(dateStart).toISOString()
      if (dateEnd) payload.date_range_end = new Date(dateEnd).toISOString()

      const res = await api.postSearch(payload)
      
      // Clear forms
      setKeyword('')
      setDirectUrls('')
      setDirectKeywords('')

      // Transition to results page
      setActiveSearch(res.id, targetKeyword)
      setActiveView('results')
    } catch (err) {
      alert(`Error launching scraper run: ${err.message}`)
    } finally {
      setSubmitting(false)
    }
  }

  // Handle Schedule setup button
  const handleScheduleRedirect = () => {
    let u = ''
    let k = ''
    let d = false

    if (crawlTab === 'search') {
      k = keyword.trim()
    } else if (crawlTab === 'direct') {
      u = directUrls.trim()
      k = directKeywords.trim()
      d = disableKeywordFilter
    } else if (crawlTab === 'config') {
      u = Array.from(configSelected).join('\n')
      k = configDisableFilter ? '' : Array.from(configSelectedKeywords).join(',')
      d = configDisableFilter
    }

    // Resolve domains_filter, languages_filter, proxy_url
    const inc = domainsInclude.trim()
    const exc = domainsExclude.trim()
    const domains_filter = (inc || exc) ? {
      include: inc ? inc.split(',').map(d => d.trim().toLowerCase()) : [],
      exclude: exc ? exc.split(',').map(d => d.trim().toLowerCase()) : []
    } : null

    const langs = languageFilter.trim()
    const languages_filter = langs ? langs.split(',').map(l => l.trim().toLowerCase()) : null

    setSchedulePrefill({
      source_type: crawlTab,
      keyword: k || (crawlTab === 'config' ? '__config__' : ''),
      direct_urls: u,
      disable_keyword: d,
      engine: engine,
      case_sensitive: caseSensitive,
      exact_match: exactMatch,
      ignore_robots: ignoreRobots,
      match_type: matchType,
      domains_filter: domains_filter,
      languages_filter: languages_filter,
      proxy_url: torEnabled ? torProxyUrl : null
    })

    setActiveView('schedules')
  }

  return (
    <section className="view-section active">
      <div className="content-card max-width-card">
        <div className="card-header border-bottom">
          <div className="tab-header">
            <button
              type="button"
              className={`form-tab ${crawlTab === 'search' ? 'active' : ''}`}
              onClick={() => setCrawlTab('search')}
            >
              <i className="fa-solid fa-earth-americas"></i> Web Search Scrape
            </button>
            <button
              type="button"
              className={`form-tab ${crawlTab === 'direct' ? 'active' : ''}`}
              onClick={() => setCrawlTab('direct')}
            >
              <i className="fa-solid fa-file-invoice"></i> Direct URLs / Sitemaps
            </button>
            <button
              type="button"
              className={`form-tab ${crawlTab === 'config' ? 'active' : ''}`}
              onClick={() => setCrawlTab('config')}
            >
              <i className="fa-solid fa-file-code"></i> Config File Scrape
            </button>
          </div>
        </div>

        <div className="card-body">
          <form className="crawl-form" onSubmit={handleSubmit}>
            {/* Render Tab Contents */}
            {crawlTab === 'search' && (
              <SearchTab keyword={keyword} setKeyword={setKeyword} />
            )}

            {crawlTab === 'direct' && (
              <DirectTab
                directUrls={directUrls}
                setDirectUrls={setDirectUrls}
                disableKeywordFilter={disableKeywordFilter}
                setDisableKeywordFilter={setDisableKeywordFilter}
                directKeywords={directKeywords}
                setDirectKeywords={setDirectKeywords}
              />
            )}

            {crawlTab === 'config' && (
              <ConfigTab
                configDisableFilter={configDisableFilter}
                setConfigDisableFilter={setConfigDisableFilter}
              />
            )}

            {/* Advanced Settings Accordion */}
            <Accordion title={<span><i className="fa-solid fa-sliders"></i> Advanced Search Toggles & Filters</span>}>
              <div className="form-grid">
                {/* Column 1 */}
                <div className="flex-col gap-3">
                  <div className="form-group">
                    <label className="form-label">Match Settings</label>
                    <div className="toggle-group">
                      <label className="toggle-control">
                        <input
                          type="checkbox"
                          checked={caseSensitive}
                          onChange={(e) => setCaseSensitive(e.target.checked)}
                        />
                        <span className="toggle-slider"></span>
                        <span className="toggle-label">Case Sensitive</span>
                      </label>
                      <label className="toggle-control">
                        <input
                          type="checkbox"
                          checked={exactMatch}
                          onChange={(e) => setExactMatch(e.target.checked)}
                        />
                        <span className="toggle-slider"></span>
                        <span className="toggle-label">Exact Word Boundaries</span>
                      </label>
                      <label className="toggle-control">
                        <input
                          type="checkbox"
                          checked={ignoreRobots}
                          onChange={(e) => setIgnoreRobots(e.target.checked)}
                        />
                        <span className="toggle-slider"></span>
                        <span className="toggle-label">Ignore robots.txt</span>
                      </label>
                    </div>
                  </div>

                  <div className="form-group">
                    <label className="form-label">Match Evaluator Type</label>
                    <div className="radio-group">
                      <label className="radio-control">
                        <input
                          type="radio"
                          name="match-type"
                          value="phrase"
                          checked={matchType === 'phrase'}
                          onChange={() => setMatchType('phrase')}
                        />
                        <span className="radio-dot"></span>
                        <span>Phrase Match</span>
                      </label>
                      <label className="radio-control">
                        <input
                          type="radio"
                          name="match-type"
                          value="boolean"
                          checked={matchType === 'boolean'}
                          onChange={() => setMatchType('boolean')}
                        />
                        <span className="radio-dot"></span>
                        <span>Boolean Expression</span>
                      </label>
                    </div>
                  </div>
                </div>

                {/* Column 2 */}
                <div className="flex-col gap-3">
                  <div className="form-group">
                    <label htmlFor="select-crawl-engine" class="form-label">Crawl Engine</label>
                    <select
                      id="select-crawl-engine"
                      className="form-control"
                      value={engine}
                      onChange={(e) => setEngine(e.target.value)}
                    >
                      <option value="fast">Fast HTTP (Requests + BeautifulSoup)</option>
                      <option value="lightpanda">Lightpanda JS (Headless browser for machines)</option>
                      <option value="dynamic">Dynamic JS (Selenium Headless Chrome)</option>
                    </select>
                    <small className="form-hint">
                      Fast mode downloads HTML raw text. Lightpanda is a faster headless browser. Dynamic mode boots headless Chrome (heavy).
                    </small>
                  </div>

                  <div className="form-group">
                    <label htmlFor="languages-filter-input" className="form-label">Language Filtering (ISO codes)</label>
                    <input
                      type="text"
                      id="languages-filter-input"
                      placeholder="e.g. en, es, fr"
                      className="form-control"
                      value={languageFilter}
                      onChange={(e) => setLanguageFilter(e.target.value)}
                    />
                    <small className="form-hint">
                      Filter results by comma-separated ISO languages (e.g. 'en' for English).
                    </small>
                  </div>
                </div>
              </div>

              <hr className="form-divider" />

              <div className="form-grid">
                <div className="form-group">
                  <label htmlFor="domains-include" className="form-label">Include Specific Domains</label>
                  <input
                    type="text"
                    id="domains-include"
                    placeholder="e.g. wikipedia.org, github.com"
                    className="form-control"
                    value={domainsInclude}
                    onChange={(e) => setDomainsInclude(e.target.value)}
                  />
                  <small className="form-hint">Comma-separated domains to search exclusively. Leave empty to allow any.</small>
                </div>

                <div className="form-group">
                  <label htmlFor="domains-exclude" className="form-label">Exclude Specific Domains</label>
                  <input
                    type="text"
                    id="domains-exclude"
                    placeholder="e.g. spammyblog.com, badsite.org"
                    className="form-control"
                    value={domainsExclude}
                    onChange={(e) => setDomainsExclude(e.target.value)}
                  />
                  <small className="form-hint">Comma-separated domains to exclude from search results.</small>
                </div>
              </div>

              <hr className="form-divider" />

              <div className="form-grid">
                <div className="form-group">
                  <label htmlFor="date-start" className="form-label">Published / Indexed Date (Start)</label>
                  <input
                    type="date"
                    id="date-start"
                    className="form-control"
                    value={dateStart}
                    onChange={(e) => setDateStart(e.target.value)}
                  />
                </div>
                <div className="form-group">
                  <label htmlFor="date-end" className="form-label">Published / Indexed Date (End)</label>
                  <input
                    type="date"
                    id="date-end"
                    className="form-control"
                    value={dateEnd}
                    onChange={(e) => setDateEnd(e.target.value)}
                  />
                </div>
              </div>

              {/* Anonymous Routing (Tor) */}
              <div style={{ borderTop: '1px solid rgba(255,255,255,0.07)', margin: '14px 0' }}></div>
              <div className="filter-group">
                <div style={{ fontSize: '0.72rem', fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: '10px' }}>
                  Anonymous Routing
                </div>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '12px' }}>
                  <div style={{ flexShrink: 0, marginTop: '2px' }}>
                    <label className="toggle-label" style={{ cursor: torLoading ? 'not-allowed' : 'pointer' }}>
                      <input
                        type="checkbox"
                        checked={torEnabled}
                        onChange={handleTorToggle}
                        disabled={torLoading}
                        style={{ display: 'none' }}
                      />
                      <span className="toggle-pill" style={{
                        display: 'inline-block', width: '36px', height: '20px', borderRadius: '20px',
                        background: torEnabled ? 'var(--accent-cyan)' : 'rgba(255,255,255,0.1)',
                        border: '1px solid rgba(255,255,255,0.15)',
                        position: 'relative', transition: 'background 0.2s', verticalAlign: 'middle'
                      }}>
                        <span style={{
                          position: 'absolute', width: '14px', height: '14px', borderRadius: '50%',
                          background: '#fff', top: '2px', left: '2px', transition: 'transform 0.2s',
                          transform: torEnabled ? 'translateX(18px)' : 'none'
                        }}></span>
                      </span>
                    </label>
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: '0.82rem', fontWeight: 500, color: 'var(--text-primary)', marginBottom: '3px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                      Route through Tor
                      {torLoading && <i className="fa-solid fa-circle-notch fa-spin text-xs"></i>}
                      {torEnabled && torReachable && (
                        <span style={{ fontSize: '0.65rem', padding: '2px 8px', borderRadius: '10px', fontWeight: 500, backgroundColor: 'rgba(16,185,129,0.15)', color: '#34d399' }}>
                          ● Tor reachable
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)', lineHeight: 1.5 }}>
                      Routes all search queries and page scraping through the local Tor SOCKS5 proxy (127.0.0.1:9050). Tor Browser or the
                      <code style={{ fontSize: '0.7rem', background: 'rgba(255,255,255,0.06)', padding: '1px 5px', borderRadius: '4px', marginLeft: '4px' }}>tor</code> system service must already be running before enabling this toggle.
                    </div>
                    {torError && (
                      <div style={{
                        marginTop: '6px', fontSize: '0.72rem', color: '#ef4444',
                        background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)',
                        borderRadius: '6px', padding: '6px 10px', lineHeight: 1.5
                      }}>
                        {torError}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </Accordion>

            {/* Submit Action Buttons */}
            <div className="form-actions border-top">
              <button
                type="submit"
                className="btn btn-primary"
                id="btn-submit-crawl"
                disabled={submitting}
              >
                {submitting ? (
                  <>
                    <i className="fa-solid fa-circle-notch fa-spin"></i> Initializing Crawl...
                  </>
                ) : (
                  <>
                    <i className="fa-solid fa-rocket"></i> Launch Crawl Process
                  </>
                )}
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                id="btn-schedule-setup"
                onClick={handleScheduleRedirect}
              >
                <i className="fa-solid fa-clock"></i> Schedule Recurring
              </button>
            </div>
          </form>
        </div>
      </div>
    </section>
  )
}
