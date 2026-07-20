import React, { useEffect, useState } from 'react'
import useAppStore from '../store/appStore'
import { api } from '../api/client'
import { getNextScheduledDateLocal } from '../utils/dates'
import Badge from '../components/ui/Badge'
import Toggle from '../components/ui/Toggle'

export default function SchedulesView() {
  const {
    scheduleList,
    setScheduleList,
    schedulePrefill,
    setSchedulePrefill
  } = useAppStore()

  // Form states
  const [sourceMode, setSourceMode] = useState('search') // 'search' | 'direct' | 'config'
  const [keyword, setKeyword] = useState('')
  const [directUrls, setDirectUrls] = useState('')
  const [disableKeyword, setDisableKeyword] = useState(false)
  const [directKeywords, setDirectKeywords] = useState('')
  const [frequency, setFrequency] = useState('daily') // 'daily' | 'weekly' | 'monthly'
  const [timeStr, setTimeStr] = useState('09:00')
  const [weekday, setWeekday] = useState('1') // Monday
  const [day, setDay] = useState('1')
  const [engine, setEngine] = useState('fast')
  const [caseSensitive, setCaseSensitive] = useState(false)
  const [exactMatch, setExactMatch] = useState(false)
  const [ignoreRobots, setIgnoreRobots] = useState(false)

  // Advanced search settings
  const [matchType, setMatchType] = useState('phrase') // 'phrase' | 'boolean'
  const [languageFilter, setLanguageFilter] = useState('')
  const [domainsInclude, setDomainsInclude] = useState('')
  const [domainsExclude, setDomainsExclude] = useState('')
  const [torEnabled, setTorEnabled] = useState(false)
  const [torProxyUrl, setTorProxyUrl] = useState(null)

  // Tor check states
  const [torLoading, setTorLoading] = useState(false)
  const [torError, setTorError] = useState('')

  const [loading, setLoading] = useState(false)

  // Tor pre-flight check handler
  const handleTorToggle = async () => {
    if (torEnabled) {
      setTorEnabled(false)
      setTorProxyUrl(null)
      setTorError('')
      return
    }

    setTorLoading(true)
    setTorError('')
    try {
      const res = await api.getTorStatus()
      if (res.reachable) {
        setTorEnabled(true)
        setTorProxyUrl(res.proxy_url || 'socks5h://127.0.0.1:9050')
      } else {
        setTorError(res.error_message || 'Tor service is not reachable on localhost port 9050.')
        setTorEnabled(false)
        setTorProxyUrl(null)
      }
    } catch (err) {
      setTorError('Failed to query Tor status endpoint. Make sure the backend is active.')
      setTorEnabled(false)
      setTorProxyUrl(null)
    } finally {
      setTorLoading(false)
    }
  }

  // Load schedules list
  const loadSchedules = async () => {
    setLoading(true)
    try {
      const data = await api.getSchedules()
      setScheduleList(data)
    } catch (err) {
      console.error('Failed to load schedules:', err)
    } finally {
      setLoading(false)
    }
  }

  // Handle Mount & Prefill checks
  useEffect(() => {
    loadSchedules()
  }, [])

  useEffect(() => {
    if (schedulePrefill) {
      setSourceMode(schedulePrefill.source_type || 'search')
      if (schedulePrefill.source_type === 'search') {
        setKeyword(schedulePrefill.keyword || '')
      } else if (schedulePrefill.source_type === 'config') {
        setDirectKeywords(schedulePrefill.keyword === '__config__' ? '' : (schedulePrefill.keyword || ''))
        setDirectUrls('')
        setDisableKeyword(false)
      } else {
        setDirectUrls(schedulePrefill.direct_urls || '')
        setDirectKeywords(schedulePrefill.keyword || '')
        setDisableKeyword(!!schedulePrefill.disable_keyword)
      }
      setEngine(schedulePrefill.engine || 'fast')
      setCaseSensitive(!!schedulePrefill.case_sensitive)
      setExactMatch(!!schedulePrefill.exact_match)
      setIgnoreRobots(!!schedulePrefill.ignore_robots)

      // Prefill advanced settings
      setMatchType(schedulePrefill.match_type || 'phrase')
      
      if (schedulePrefill.domains_filter) {
        const incList = schedulePrefill.domains_filter.include || []
        const excList = schedulePrefill.domains_filter.exclude || []
        setDomainsInclude(incList.join(', '))
        setDomainsExclude(excList.join(', '))
      } else {
        setDomainsInclude('')
        setDomainsExclude('')
      }

      if (schedulePrefill.languages_filter) {
        setLanguageFilter(schedulePrefill.languages_filter.join(', '))
      } else {
        setLanguageFilter('')
      }

      if (schedulePrefill.proxy_url) {
        setTorEnabled(true)
        setTorProxyUrl(schedulePrefill.proxy_url)
      } else {
        setTorEnabled(false)
        setTorProxyUrl(null)
      }

      // Consume prefill
      setSchedulePrefill(null)
    }
  }, [schedulePrefill])

  const handleDelete = async (id) => {
    if (!confirm('Are you sure you want to delete this search automation schedule?')) return
    try {
      await api.deleteSchedule(id)
      loadSchedules()
    } catch (err) {
      alert(`Could not delete schedule: ${err.message}`)
    }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()

    try {
      let targetKeyword = ''
      if (sourceMode === 'search') {
        targetKeyword = keyword.trim()
      } else if (sourceMode === 'direct') {
        targetKeyword = disableKeyword ? '' : directKeywords.trim()
      } else if (sourceMode === 'config') {
        targetKeyword = directKeywords.trim() || '__config__'
      }

      // Calculate next run date
      const nextRunDate = getNextScheduledDateLocal(frequency, timeStr, weekday, day)
      const nextRunUTCStr = nextRunDate.toISOString()

      // Construct filters
      let domainsFilter = null
      const inc = domainsInclude.trim()
      const exc = domainsExclude.trim()
      if (inc || exc) {
        domainsFilter = {
          include: inc ? inc.split(',').map(d => d.trim().toLowerCase()) : [],
          exclude: exc ? exc.split(',').map(d => d.trim().toLowerCase()) : []
        }
      }

      let languagesFilter = null
      const langs = languageFilter.trim()
      if (langs) {
        languagesFilter = langs.split(',').map(l => l.trim().toLowerCase())
      }

      const payload = {
        keyword: targetKeyword,
        frequency: frequency,
        engine: engine,
        next_run: nextRunUTCStr,
        config: {
          keyword: targetKeyword,
          match_type: matchType,
          case_sensitive: caseSensitive,
          exact_match: exactMatch,
          ignore_robots: ignoreRobots,
          engine: engine,
          source_type: sourceMode,
          direct_urls: sourceMode === 'direct' ? directUrls.trim() : null,
          schedule_time_hour: nextRunDate.getUTCHours(),
          schedule_time_minute: nextRunDate.getUTCMinutes(),
          schedule_time_weekday: nextRunDate.getUTCDay(),
          schedule_time_day: nextRunDate.getUTCDate(),
          domains_filter: domainsFilter,
          languages_filter: languagesFilter,
          proxy_url: torEnabled ? torProxyUrl : null
        }
      }

      await api.postSchedule(payload)
      alert('Search schedule configured successfully!')
      
      // Reset form
      setKeyword('')
      setDirectUrls('')
      setDirectKeywords('')
      setDisableKeyword(false)
      setTimeStr('09:00')
      setWeekday('1')
      setDay('1')
      setCaseSensitive(false)
      setExactMatch(false)
      setIgnoreRobots(false)
      setMatchType('phrase')
      setLanguageFilter('')
      setDomainsInclude('')
      setDomainsExclude('')
      setTorEnabled(false)
      setTorProxyUrl(null)

      loadSchedules()
    } catch (err) {
      alert(`Error saving schedule: ${err.message}`)
    }
  }

  const formatKeyword = (keyword) => {
    if (!keyword) return '(No Keyword)'
    const list = keyword.split(',').map(s => s.trim()).filter(Boolean)
    if (list.length <= 2) return keyword
    return `${list.slice(0, 2).join(', ')} (+${list.length - 2} more)`
  }

  return (
    <section className="view-section active">
      <div className="dashboard-split">
        {/* Schedules Table */}
        <div className="content-card flex-2">
          <div className="card-header">
            <h2>Configured Keyword Schedules</h2>
          </div>
          <div className="card-body p-0 scroll-table-container">
            <table className="data-table" id="schedules-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Keyword / Phrase</th>
                  <th>Engine</th>
                  <th>Frequency</th>
                  <th>Last Scanned</th>
                  <th>Next Scheduled</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody id="schedules-tbody">
                {scheduleList.length === 0 ? (
                  <tr>
                    <td colSpan="8" className="text-center py-5 text-muted">
                      No scheduled keyword scans configured.
                    </td>
                  </tr>
                ) : (
                  scheduleList.map(item => {
                    const lastRun = item.last_run ? new Date(item.last_run).toLocaleString() : 'Never'
                    const nextRun = new Date(item.next_run).toLocaleString()
                    return (
                      <tr key={item.id}>
                        <td>{item.id}</td>
                        <td title={item.keyword} style={{ cursor: item.keyword && item.keyword.includes(',') ? 'help' : 'default' }}>
                          <strong>{formatKeyword(item.keyword)}</strong>
                        </td>
                        <td>
                          {item.engine === 'fast'
                            ? 'Fast HTTP'
                            : item.engine === 'lightpanda'
                            ? 'Lightpanda JS'
                            : 'Headless Chrome'}
                        </td>
                        <td>
                          <span className="badge badge-pending">
                            {item.frequency.toUpperCase()}
                          </span>
                        </td>
                        <td className="text-muted text-xs">{lastRun}</td>
                        <td className="text-muted text-xs font-medium color-cyan">{nextRun}</td>
                        <td>
                          <Badge status={item.active ? 'active' : 'inactive'} />
                        </td>
                        <td>
                          <button
                            className="table-act-btn delete-btn"
                            onClick={() => handleDelete(item.id)}
                            title="Delete Schedule"
                          >
                            <i className="fa-solid fa-trash-can"></i>
                          </button>
                        </td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* Add Schedule Form */}
        <div className="content-card flex-1">
          <div className="card-header border-bottom">
            <h2>Schedule Keyword Scan</h2>
          </div>
          <div className="card-body">
            <form className="flex-col gap-3" onSubmit={handleSubmit}>
              <div className="form-group">
                <label htmlFor="sched-source-type" className="form-label">Source Mode</label>
                <select
                  id="sched-source-type"
                  className="form-control"
                  value={sourceMode}
                  onChange={(e) => setSourceMode(e.target.value)}
                >
                  <option value="search">Web Search Scrape</option>
                  <option value="direct">Direct URLs / Sitemaps</option>
                  <option value="config">Config File (urls.json + keywords.json)</option>
                </select>
              </div>

              {sourceMode === 'search' && (
                <div className="form-group" id="sched-group-keyword">
                  <label htmlFor="sched-keyword" className="form-label">Keyword / Phrase</label>
                  <div className="input-with-icon">
                    <i className="fa-solid fa-magnifying-glass"></i>
                    <input
                      type="text"
                      id="sched-keyword"
                      className="form-control"
                      placeholder="e.g. Python programming OR FastAPI"
                      value={keyword}
                      onChange={(e) => setKeyword(e.target.value)}
                      required
                    />
                  </div>
                </div>
              )}

              {sourceMode === 'direct' && (
                <>
                  <div className="form-group" id="sched-group-direct-urls">
                    <label htmlFor="sched-direct-urls" className="form-label">Target URLs or Sitemap URLs</label>
                    <textarea
                      id="sched-direct-urls"
                      rows={4}
                      placeholder="https://example.com/article1&#10;https://myblog.com/sitemap.xml"
                      className="form-control text-monospace"
                      value={directUrls}
                      onChange={(e) => setDirectUrls(e.target.value)}
                      required
                    ></textarea>
                  </div>

                  <div className="form-group" id="sched-group-disable-keyword">
                    <Toggle
                      id="sched-chk-disable-keyword"
                      checked={disableKeyword}
                      onChange={setDisableKeyword}
                      label="Disable keyword filtering (Scrape all pages)"
                    />
                  </div>

                  {!disableKeyword && (
                    <div className="form-group" id="sched-group-direct-keywords">
                      <label htmlFor="sched-direct-keywords" className="form-label">Keywords to Search</label>
                      <div className="input-with-icon">
                        <i className="fa-solid fa-tags"></i>
                        <input
                          type="text"
                          id="sched-direct-keywords"
                          placeholder="e.g. Python, automation (comma-separated)"
                          className="form-control"
                          value={directKeywords}
                          onChange={(e) => setDirectKeywords(e.target.value)}
                          required
                        />
                      </div>
                    </div>
                  )}
                </>
              )}

              {sourceMode === 'config' && (
                <>
                  <div className="alert alert-info text-xs p-2 rounded mb-2" style={{ backgroundColor: 'var(--color-bg-light)', border: '1px solid var(--color-border)', color: 'var(--color-text-muted)' }}>
                    <i className="fa-solid fa-circle-info" style={{ marginRight: '6px' }}></i>
                    At trigger time, the scheduler will automatically load all URLs from <code>config/urls.json</code> and all keywords from <code>config/keywords.json</code>.
                  </div>

                  <div className="form-group" id="sched-group-direct-keywords">
                    <label htmlFor="sched-direct-keywords" className="form-label">Override Keywords (Optional)</label>
                    <div className="input-with-icon">
                      <i className="fa-solid fa-tags"></i>
                      <input
                        type="text"
                        id="sched-direct-keywords"
                        placeholder="Leave blank to use config/keywords.json"
                        className="form-control"
                        value={directKeywords}
                        onChange={(e) => setDirectKeywords(e.target.value)}
                      />
                    </div>
                  </div>
                </>
              )}

              <div className="form-group">
                <label htmlFor="sched-frequency" className="form-label">Frequency</label>
                <select
                  id="sched-frequency"
                  className="form-control"
                  value={frequency}
                  onChange={(e) => setFrequency(e.target.value)}
                >
                  <option value="daily">Daily Cron Task</option>
                  <option value="weekly">Weekly Cron Task</option>
                  <option value="monthly">Monthly Cron Task</option>
                </select>
              </div>

              {/* Execution Time */}
              <div className="form-group" id="sched-group-time">
                <label htmlFor="sched-time" className="form-label">Execution Time (Local Time)</label>
                <div className="input-with-icon">
                  <i className="fa-regular fa-clock"></i>
                  <input
                    type="time"
                    id="sched-time"
                    className="form-control"
                    value={timeStr}
                    onChange={(e) => setTimeStr(e.target.value)}
                    required
                  />
                </div>
              </div>

              {/* Day of Week (weekly only) */}
              {frequency === 'weekly' && (
                <div className="form-group" id="sched-group-weekday">
                  <label htmlFor="sched-weekday" className="form-label">Day of Week</label>
                  <select
                    id="sched-weekday"
                    className="form-control"
                    value={weekday}
                    onChange={(e) => setWeekday(e.target.value)}
                    required
                  >
                    <option value="1">Monday</option>
                    <option value="2">Tuesday</option>
                    <option value="3">Wednesday</option>
                    <option value="4">Thursday</option>
                    <option value="5">Friday</option>
                    <option value="6">Saturday</option>
                    <option value="0">Sunday</option>
                  </select>
                </div>
              )}

              {/* Day of Month (monthly only) */}
              {frequency === 'monthly' && (
                <div className="form-group" id="sched-group-day">
                  <label htmlFor="sched-day" className="form-label">Day of Month</label>
                  <input
                    type="number"
                    id="sched-day"
                    className="form-control"
                    min="1"
                    max="31"
                    value={day}
                    onChange={(e) => setDay(e.target.value)}
                    required
                  />
                </div>
              )}

              <div className="form-group">
                <label htmlFor="sched-engine" className="form-label">Crawl Engine</label>
                <select
                  id="sched-engine"
                  className="form-control"
                  value={engine}
                  onChange={(e) => setEngine(e.target.value)}
                >
                  <option value="fast">Fast HTTP Engine</option>
                  <option value="lightpanda">Lightpanda JS Engine</option>
                  <option value="dynamic">Dynamic Headless Engine</option>
                </select>
              </div>

              <div className="form-group">
                <label className="form-label">Config Options</label>
                <div className="toggle-group">
                  <Toggle
                    id="sched-case-sensitive"
                    checked={caseSensitive}
                    onChange={setCaseSensitive}
                    label="Case Sensitive"
                  />
                  <Toggle
                    id="sched-exact-match"
                    checked={exactMatch}
                    onChange={setExactMatch}
                    label="Exact Match"
                  />
                  <Toggle
                    id="sched-ignore-robots"
                    checked={ignoreRobots}
                    onChange={setIgnoreRobots}
                    label="Ignore robots.txt"
                  />
                </div>
              </div>

              {/* Match Evaluator Type */}
              <div className="form-group">
                <label className="form-label">Match Evaluator Type</label>
                <div className="radio-group" style={{ display: 'flex', gap: '16px', marginTop: '4px' }}>
                  <label className="radio-control" style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.8rem', cursor: 'pointer' }}>
                    <input
                      type="radio"
                      name="sched-match-type"
                      value="phrase"
                      checked={matchType === 'phrase'}
                      onChange={() => setMatchType('phrase')}
                    />
                    <span>Phrase Match</span>
                  </label>
                  <label className="radio-control" style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.8rem', cursor: 'pointer' }}>
                    <input
                      type="radio"
                      name="sched-match-type"
                      value="boolean"
                      checked={matchType === 'boolean'}
                      onChange={() => setMatchType('boolean')}
                    />
                    <span>Boolean Expression</span>
                  </label>
                </div>
              </div>

              {/* Language Filtering */}
              <div className="form-group">
                <label htmlFor="sched-languages-filter" className="form-label">Language Filtering (ISO codes)</label>
                <input
                  type="text"
                  id="sched-languages-filter"
                  placeholder="e.g. en, es, fr"
                  className="form-control"
                  value={languageFilter}
                  onChange={(e) => setLanguageFilter(e.target.value)}
                />
              </div>

              {/* Domains Filter */}
              <div className="form-group">
                <label htmlFor="sched-domains-include" className="form-label">Include Specific Domains</label>
                <input
                  type="text"
                  id="sched-domains-include"
                  placeholder="e.g. wikipedia.org"
                  className="form-control"
                  value={domainsInclude}
                  onChange={(e) => setDomainsInclude(e.target.value)}
                />
              </div>
              <div className="form-group">
                <label htmlFor="sched-domains-exclude" className="form-label">Exclude Specific Domains</label>
                <input
                  type="text"
                  id="sched-domains-exclude"
                  placeholder="e.g. badsite.com"
                  className="form-control"
                  value={domainsExclude}
                  onChange={(e) => setDomainsExclude(e.target.value)}
                />
              </div>

              {/* Tor Toggle */}
              <div className="form-group" style={{ borderTop: '1px solid rgba(255,255,255,0.07)', paddingTop: '10px' }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
                  <label className="toggle-label" style={{ cursor: torLoading ? 'not-allowed' : 'pointer' }}>
                    <input
                      type="checkbox"
                      checked={torEnabled}
                      onChange={handleTorToggle}
                      disabled={torLoading}
                      style={{ display: 'none' }}
                    />
                    <span className="toggle-pill" style={{
                      display: 'inline-block', width: '32px', height: '18px', borderRadius: '18px',
                      background: torEnabled ? 'var(--accent-cyan,#00e5ff)' : 'rgba(255,255,255,0.1)',
                      position: 'relative', transition: 'background 0.2s', verticalAlign: 'middle'
                    }}>
                      <span style={{
                        position: 'absolute', width: '12px', height: '12px', borderRadius: '50%',
                        background: '#fff', top: '2px', left: '2px', transition: 'transform 0.2s',
                        transform: torEnabled ? 'translateX(16px)' : 'none'
                      }}></span>
                    </span>
                  </label>
                  <div style={{ flex: 1 }}>
                    <span style={{ fontSize: '0.8rem', fontWeight: 500, color: 'var(--text-primary)' }}>Route through Tor</span>
                    {torLoading && <i className="fa-solid fa-circle-notch fa-spin text-xs" style={{ marginLeft: '6px' }}></i>}
                    <div style={{ fontSize: '0.68rem', color: 'var(--text-muted)', lineHeight: '1.4', marginTop: '2px' }}>
                      Routes scheduling triggers through Tor proxy (127.0.0.1:9050).
                    </div>
                    {torError && (
                      <div style={{ marginTop: '4px', fontSize: '0.68rem', color: '#ef4444' }}>
                        {torError}
                      </div>
                    )}
                  </div>
                </div>
              </div>

              <div className="form-actions pt-2 border-top">
                <button type="submit" className="btn btn-primary w-full">
                  <i className="fa-solid fa-calendar-plus"></i> Save Schedule
                </button>
              </div>
            </form>
          </div>
        </div>
      </div>
    </section>
  )
}
