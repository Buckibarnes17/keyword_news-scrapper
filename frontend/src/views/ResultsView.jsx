import React, { useState, useEffect } from 'react'
import useAppStore from '../store/appStore'
import { api } from '../api/client'
import { usePolling } from '../hooks/usePolling'
import Badge from '../components/ui/Badge'
import Dropdown from '../components/ui/Dropdown'
import ProgressBar from '../components/ui/ProgressBar'
import Pagination from '../components/ui/Pagination'
import Modal from '../components/ui/Modal'
import { escapeHtml, escapeRegExp, extractHighlightTerms } from '../utils/highlight'

export default function ResultsView() {
  // Trigger polling hook
  usePolling()

  const {
    activeSearchId,
    activeSearchKeyword,
    searchResults,
    searchMeta,
    setActiveSearch,
    filters,
    setFilters,
    setPage
  } = useAppStore()

  // Local state
  const [exportDropdownOpen, setExportDropdownOpen] = useState(false)
  const [activeSnippet, setActiveSnippet] = useState(null)
  const [submittingAction, setSubmittingAction] = useState(false)

  // Status mapping to classes
  const getBadgeClass = (item) => {
    let badgeClass = `badge-${item.status || 'pending'}`
    if (item.status === 'crawling' || item.status === 'processing') {
      badgeClass = 'badge-crawling'
    }
    if (item.is_duplicate) {
      badgeClass = 'badge-duplicate'
    }
    return badgeClass
  }

  // Highlight helper
  const getHighlightedHtml = (text, query, fallback = '') => {
    if (!text) return fallback
    let clean = escapeHtml(text)
    const terms = extractHighlightTerms(query || activeSearchKeyword || '')
    terms.forEach(term => {
      if (!term || term.length < 2) return
      const regex = new RegExp(`(${escapeRegExp(term)})`, 'gi')
      clean = clean.replace(regex, '<span class="highlight">$1</span>')
    })
    return clean
  }

  // Filter & Sort Results client-side
  let processedItems = [...searchResults]

  // 1. Text Filter (Domain / Title)
  if (filters.searchQuery) {
    const q = filters.searchQuery.toLowerCase()
    processedItems = processedItems.filter(item =>
      (item.title || '').toLowerCase().includes(q) ||
      (item.url || '').toLowerCase().includes(q)
    )
  }

  // 2. Status Filter
  if (filters.status) {
    if (filters.status === 'matched') {
      processedItems = processedItems.filter(item => item.status === 'matched')
    } else if (filters.status === 'skipped') {
      processedItems = processedItems.filter(item => item.status === 'skipped')
    } else if (filters.status === 'failed') {
      processedItems = processedItems.filter(item => item.status === 'failed')
    }
  }

  // 3. Exclude Duplicates
  if (filters.excludeDuplicates) {
    processedItems = processedItems.filter(item => !item.is_duplicate)
  }

  // 4. Sort Items
  processedItems.sort((a, b) => {
    let valA = 0
    let valB = 0

    if (filters.sortBy === 'relevance') {
      valA = a.relevance_score || 0
      valB = b.relevance_score || 0
    } else if (filters.sortBy === 'occurrences') {
      valA = a.occurrences || 0
      valB = b.occurrences || 0
    }

    if (valA === valB) {
      // Tie breaker by Rank/ID
      return a.id - b.id
    }

    return filters.sortDesc ? valB - valA : valA - valB
  })

  // 5. Pagination Offset
  const totalRecords = processedItems.length
  const limit = filters.limit
  const page = filters.page
  const pagedItems = processedItems.slice((page - 1) * limit, page * limit)

  const handleSort = (field) => {
    if (filters.sortBy === field) {
      setFilters({ sortDesc: !filters.sortDesc })
    } else {
      setFilters({ sortBy: field, sortDesc: true })
    }
  }

  // Abort crawl
  const handleAbort = async () => {
    if (!activeSearchId) return
    setSubmittingAction(true)
    try {
      const data = await api.stopSearch(activeSearchId)
      alert(`Scraper job stopped successfully. Status: ${data.status || 'stopped'}`)
    } catch (err) {
      alert(`Failed to stop crawl run: ${err.message}`)
    } finally {
      setSubmittingAction(false)
    }
  }

  // Retry crawl
  const handleRetry = async () => {
    if (!activeSearchId) return
    setSubmittingAction(true)
    try {
      const newQuery = await api.retrySearch(activeSearchId)
      setActiveSearch(newQuery.id, activeSearchKeyword)
    } catch (err) {
      alert(`Could not retry run: ${err.message}`)
    } finally {
      setSubmittingAction(false)
    }
  }

  // Export Results
  const handleExport = async (format) => {
    if (!activeSearchId) return
    if (format === 'postgres') {
      try {
        await api.exportPostgres(activeSearchId)
        alert('Export to PostgreSQL database completed successfully!')
      } catch (err) {
        alert(`Export to PostgreSQL failed: ${err.message}`)
      }
      return
    }

    try {
      const p = {
        format,
        exclude_duplicates: filters.excludeDuplicates ? 'true' : 'false',
        sort_by: filters.sortBy,
        sort_desc: filters.sortDesc ? 'true' : 'false'
      }
      const res = await api.getExport(activeSearchId, p)
      if (!res.ok) throw new Error('Failed to generate export file')
      const blob = await res.blob()
      const downloadUrl = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = downloadUrl
      a.download = `crawl_results_${activeSearchId}.${format === 'xlsx' ? 'xlsx' : format}`
      document.body.appendChild(a)
      a.click()
      a.remove()
      window.URL.revokeObjectURL(downloadUrl)
    } catch (err) {
      alert(`Export failed: ${err.message}`)
    }
  }

  // Parse Media Lists
  let imageLinks = []
  let videoLinks = []
  if (activeSnippet) {
    if (activeSnippet.image_links) {
      try {
        imageLinks = JSON.parse(activeSnippet.image_links)
      } catch (e) {}
    }
    if (activeSnippet.video_links) {
      try {
        videoLinks = JSON.parse(activeSnippet.video_links)
      } catch (e) {}
    }
  }

  // Progress math
  const crawledCount = searchMeta?.total_urls_crawled || 0
  const foundCount = searchMeta?.total_urls_found || 0
  const progressPct = foundCount > 0 ? (crawledCount / foundCount) * 100 : 0
  const status = searchMeta?.status || 'pending'
  const isRunning = ['pending', 'processing'].includes(status)

  const getKeywordDisplayText = () => {
    if (!activeSearchKeyword) return '(No keyword)'
    const list = activeSearchKeyword.split(',').map(s => s.trim()).filter(Boolean)
    if (list.length <= 3) {
      return activeSearchKeyword
    }
    return `${list.slice(0, 3).join(', ')} (+${list.length - 3} more)`
  }

  return (
    <section className="view-section active">
      {/* Progress Banner */}
      <div className="content-card progress-banner" id="progress-monitor-card">
        <div className="progress-header">
          <div className="progress-title-details">
            {isRunning && <span className="pulse-indicator" id="progress-pulse"></span>}
            <h3 id="progress-keyword-text" title={activeSearchKeyword}>
              {searchMeta?.status_message || (status === 'pending' ? 'Initializing crawl: ' : 'Analyzing: ')} 
              <strong style={{ cursor: activeSearchKeyword && activeSearchKeyword.includes(',') ? 'help' : 'default' }}>
                {getKeywordDisplayText()}
              </strong>
            </h3>
            <Badge status={status} />
          </div>

          <div style={{ display: 'flex', gap: '0.50rem', alignItems: 'center', marginLeft: 'auto', marginRight: '1.50rem' }}>
            {isRunning ? (
              <button
                className="btn btn-xs btn-outline btn-abort"
                onClick={handleAbort}
                disabled={submittingAction}
              >
                <i className="fa-solid fa-ban"></i> Abort Crawl
              </button>
            ) : (
              <button
                className="btn btn-xs btn-outline btn-retry"
                onClick={handleRetry}
                disabled={submittingAction}
              >
                <i className="fa-solid fa-redo"></i> Clone & Re-run
              </button>
            )}
          </div>
          <span className="progress-percentage-text" id="progress-percent-label">
            {Math.round(progressPct)}% Complete
          </span>
        </div>

        <ProgressBar value={progressPct} />

        <div className="progress-stats-summary">
          <div className="p-stat">
            <span className="p-stat-lbl">Discovered URLs</span>
            <span className="p-stat-val" id="progress-stat-found">{foundCount}</span>
          </div>
          <div className="p-stat-divider"></div>
          <div className="p-stat">
            <span className="p-stat-lbl">Crawled Pages</span>
            <span className="p-stat-val" id="progress-stat-crawled">{crawledCount}</span>
          </div>
          <div className="p-stat-divider"></div>
          <div className="p-stat">
            <span className="p-stat-lbl">Matches Found</span>
            <span className="p-stat-val" id="progress-stat-matched">{searchMeta?.total_urls_matched || 0}</span>
          </div>
          <div className="p-stat-divider"></div>
          <div className="p-stat">
            <span className="p-stat-lbl">Crawl Engine</span>
            <span className="p-stat-val" id="progress-stat-engine" style={{ textTransform: 'capitalize' }}>
              {searchMeta?.engine || 'Fast'}
            </span>
          </div>
        </div>
      </div>

      {/* Results Toolbar */}
      <div className="results-toolbar">
        <div className="toolbar-filters">
          <div className="search-box">
            <i className="fa-solid fa-magnifying-glass"></i>
            <input
              type="text"
              placeholder="Filter by Domain/Title..."
              value={filters.searchQuery}
              onChange={(e) => setFilters({ searchQuery: e.target.value })}
            />
          </div>

          <select
            className="form-control select-sm"
            style={{ maxWidth: '160px' }}
            value={filters.status}
            onChange={(e) => setFilters({ status: e.target.value })}
          >
            <option value="">All Statuses</option>
            <option value="matched">Matched Results</option>
            <option value="skipped">Skipped (No Match)</option>
            <option value="failed">Failed / Errors</option>
          </select>

          <label className="toggle-control text-xs">
            <input
              type="checkbox"
              checked={filters.excludeDuplicates}
              onChange={(e) => setFilters({ excludeDuplicates: e.target.checked })}
            />
            <span className="toggle-slider"></span>
            <span>Hide Duplicates</span>
          </label>
        </div>

        <div className="toolbar-actions">
          <Dropdown
            trigger={
              <button className="btn btn-outline" id="btn-export-dropdown">
                <i className="fa-solid fa-file-export"></i> Export Results <i className="fa-solid fa-chevron-down"></i>
              </button>
            }
          >
            <button className="export-opt" onClick={() => handleExport('csv')}>
              <i className="fa-solid fa-file-csv color-csv"></i> CSV Spreadsheet
            </button>
            <button className="export-opt" onClick={() => handleExport('xlsx')}>
              <i className="fa-solid fa-file-excel color-excel"></i> Excel Worksheet (.xlsx)
            </button>
            <button className="export-opt" onClick={() => handleExport('json')}>
              <i className="fa-solid fa-file-code color-json"></i> JSON Structure
            </button>
            <button className="export-opt" onClick={() => handleExport('parquet')}>
              <i className="fa-solid fa-cubes color-parquet"></i> Parquet Archive
            </button>
            <button className="export-opt" onClick={() => handleExport('postgres')}>
              <i className="fa-solid fa-database color-amber"></i> Export to PostgreSQL DB
            </button>
          </Dropdown>
        </div>
      </div>

      {/* Crawled Results Data Table */}
      <div className="content-card">
        <div className="card-body p-0 scroll-table-container min-h-300">
          <table className="data-table" id="results-data-table" style={{ minWidth: '900px' }}>
            <thead>
              <tr>
                <th style={{ width: '50px' }}>Rank</th>
                <th>Page details / URL</th>
                <th
                  style={{ width: '120px' }}
                  className="sortable"
                  onClick={() => handleSort('occurrences')}
                >
                  Occurrences {filters.sortBy === 'occurrences' ? (filters.sortDesc ? <i className="fa-solid fa-sort-down"></i> : <i className="fa-solid fa-sort-up"></i>) : <i className="fa-solid fa-sort"></i>}
                </th>
                <th
                  style={{ width: '100px' }}
                  className="sortable"
                  onClick={() => handleSort('relevance')}
                >
                  Relevance {filters.sortBy === 'relevance' ? (filters.sortDesc ? <i className="fa-solid fa-sort-down"></i> : <i className="fa-solid fa-sort-up"></i>) : <i className="fa-solid fa-sort"></i>}
                </th>
                <th style={{ width: '120px' }}>Location Match</th>
                <th style={{ width: '100px' }}>Language</th>
                <th style={{ width: '140px' }}>Status</th>
              </tr>
            </thead>
            <tbody>
              {pagedItems.length === 0 ? (
                <tr>
                  <td colSpan="7" className="text-center py-5 text-muted">
                    {searchResults.length === 0 ? (
                      <>
                        <i className="fa-solid fa-spinner fa-spin fa-2x mb-3 color-cyan"></i>
                        <p>Crawl run active. Awaiting results streams...</p>
                      </>
                    ) : (
                      <p>No results match the selected filters.</p>
                    )}
                  </td>
                </tr>
              ) : (
                pagedItems.map((item, index) => {
                  const rank = (page - 1) * limit + index + 1
                  
                  // Location match tags
                  const hasLocationTags = item.found_in_title || item.found_in_description || item.found_in_body || item.found_in_url
                  
                  // Parsed keyword badges
                  let matchedKeywords = []
                  if (item.matched_keywords) {
                    try {
                      matchedKeywords = JSON.parse(item.matched_keywords)
                    } catch (e) {}
                  }

                  return (
                    <tr key={item.id}>
                      <td className="text-center font-medium color-text-muted">#{rank}</td>
                      <td>
                        <div className="table-url-cell">
                          <span className="table-title" title={item.title || 'Untitled'}>
                            {item.title || 'Untitled'}
                          </span>
                          <a href={item.url} target="_blank" rel="noreferrer" className="table-url-link" title={item.url}>
                            <i className="fa-solid fa-link"></i> {item.url}
                          </a>
                          
                          {/* Matched keywords list */}
                          {matchedKeywords.length > 0 && (
                            <div className="matched-keywords-tags">
                              {matchedKeywords.map((kw, kwIdx) => (
                                <span className="kw-tag" key={kwIdx}>
                                  {kw}
                                </span>
                              ))}
                            </div>
                          )}

                          {/* Snippet Preview */}
                          {item.snippet && (
                            <div
                              className="snippet-preview"
                              onClick={() => setActiveSnippet(item)}
                              title="Click to view full context"
                              dangerouslySetInnerHTML={{
                                __html: getHighlightedHtml(item.snippet, activeSearchKeyword || item.url)
                              }}
                            />
                          )}
                        </div>
                      </td>
                      <td className="font-medium text-center">{item.occurrences || 0}</td>
                      <td className="font-medium text-center color-cyan">{item.relevance_score || 0}/100</td>
                      <td>
                        <div className="location-tags">
                          {item.found_in_title && <span className="loc-tag active">Title</span>}
                          {item.found_in_description && <span className="loc-tag active">Desc</span>}
                          {item.found_in_body && <span className="loc-tag active">Body</span>}
                          {item.found_in_url && <span className="loc-tag active">URL</span>}
                          {!hasLocationTags && item.status === 'matched' && <span className="loc-tag active">Matched</span>}
                          {!hasLocationTags && item.status !== 'failed' && <span className="loc-tag">None</span>}
                        </div>
                      </td>
                      <td className="text-center">
                        {(item.language || 'N/A').toUpperCase()}
                      </td>
                      <td>
                        <Badge status={item.is_duplicate ? 'duplicate' : item.status} />
                      </td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination Footer */}
        <Pagination
          page={page}
          total={totalRecords}
          limit={limit}
          onPageChange={setPage}
        />
      </div>

      {/* Snippet Modal popup wrapper */}
      <Modal
        isOpen={!!activeSnippet}
        onClose={() => setActiveSnippet(null)}
        title="Page Snippet Analysis"
        footer={
          <>
            <button className="btn btn-secondary btn-sm" onClick={() => setActiveSnippet(null)}>
              Close Detail View
            </button>
            <a
              href={activeSnippet?.url || '#'}
              target="_blank"
              rel="noreferrer"
              className="btn btn-primary btn-sm"
            >
              <i className="fa-solid fa-arrow-up-right-from-square"></i> Visit Website
            </a>
          </>
        }
      >
        {activeSnippet && (
          <div>
            <div className="mb-3">
              <label className="text-xs text-muted">Page Title</label>
              <div className="font-medium text-sm" style={{ marginTop: '0.25rem' }}>
                {activeSnippet.title || 'Untitled Snippet'}
              </div>
            </div>
            <div className="mb-3">
              <label className="text-xs text-muted">Page URL</label>
              <div className="modal-url-display">{activeSnippet.url}</div>
            </div>
            
            <div className="form-grid mb-3" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
              <div>
                <label className="text-xs text-muted">Article Author / Source</label>
                <div id="modal-author" className="font-medium text-sm color-cyan" style={{ marginTop: '0.25rem', padding: '0.5rem 0.85rem', backgroundColor: 'rgba(0, 0, 0, 0.2)', border: '1px solid var(--border-glass)', borderRadius: '8px', textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' }}>
                  {activeSnippet.author || 'Unknown'}
                </div>
              </div>
              {activeSnippet.image_url && (
                <div>
                  <label className="text-xs text-muted">Lead Image URL</label>
                  <div style={{ marginTop: '0.25rem', textAlign: 'center', backgroundColor: 'rgba(0, 0, 0, 0.2)', border: '1px solid var(--border-glass)', borderRadius: '8px', overflow: 'hidden', maxHeight: '38px' }}>
                    <a href={activeSnippet.image_url} target="_blank" rel="noreferrer" style={{ display: 'block', fontSize: '0.75rem', padding: '0.5rem 0.85rem', color: 'var(--accent-violet)', textDecoration: 'none', textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap', fontWeight: '500' }}>
                      <i className="fa-regular fa-image"></i> View Lead Image
                    </a>
                  </div>
                </div>
              )}
            </div>

            {activeSnippet.image_url && (
              <div className="mb-3">
                <label className="text-xs text-muted">Lead Article Image</label>
                <div style={{ marginTop: '0.35rem', textAlign: 'center', backgroundColor: 'rgba(0, 0, 0, 0.25)', border: '1px solid var(--border-glass)', borderRadius: '8px', overflow: 'hidden', maxHeight: '200px' }}>
                  <img src={activeSnippet.image_url} alt="Lead Article Image" style={{ maxWidth: '100%', maxHeight: '200px', objectFit: 'contain', display: 'block', margin: '0 auto' }} />
                </div>
              </div>
            )}

            <div className="mb-3">
              <label className="text-xs text-muted">Meta Description</label>
              <div
                className="snippet-content-box"
                style={{ maxHeight: '80px', minHeight: '40px', padding: '0.5rem 0.85rem', fontStyle: 'italic' }}
                dangerouslySetInnerHTML={{
                  __html: getHighlightedHtml(activeSnippet.description, activeSearchKeyword || activeSnippet.url, 'No description metadata extracted.')
                }}
              />
            </div>

            <div className="mb-3">
              <label className="text-xs text-muted">Matched Context Snippet</label>
              <div
                className="snippet-content-box"
                dangerouslySetInnerHTML={{
                  __html: getHighlightedHtml(activeSnippet.snippet, activeSearchKeyword || activeSnippet.url, 'Snippet text details will go here...')
                }}
              />
            </div>

            <div className="mb-3">
              <label className="text-xs text-muted">Full Scraped Content</label>
              <div
                className="snippet-content-box"
                style={{ maxHeight: '200px' }}
                dangerouslySetInnerHTML={{
                  __html: getHighlightedHtml(activeSnippet.full_content, activeSearchKeyword || activeSnippet.url, 'No scraped body content stored.')
                }}
              />
            </div>

            <div className="mb-3">
              <label className="text-xs text-muted">Images & Videos Present</label>
              <div className="snippet-content-box" style={{ maxHeight: '150px', overflowY: 'auto', padding: '0.5rem 0.85rem' }}>
                {imageLinks.length === 0 && videoLinks.length === 0 ? (
                  <span className="text-muted text-sm" style={{ fontStyle: 'italic' }}>
                    No images/videos present
                  </span>
                ) : (
                  <div>
                    {imageLinks.length > 0 && (
                      <div style={{ marginBottom: '0.75rem' }}>
                        <label className="text-xs text-muted" style={{ display: 'block', marginBottom: '0.25rem', fontWeight: 'bold' }}>
                          Images ({imageLinks.length})
                        </label>
                        <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', maxHeight: '100px', overflowY: 'auto', padding: '0.25rem 0' }}>
                          {imageLinks.map((url, uIdx) => (
                            <a
                              key={uIdx}
                              href={url}
                              target="_blank"
                              rel="noreferrer"
                              style={{ display: 'block', width: '60px', height: '45px', borderRadius: '4px', border: '1px solid var(--border-glass)', overflow: 'hidden', backgroundColor: 'rgba(0,0,0,0.35)' }}
                              title={`View image: ${url}`}
                            >
                              <img src={url} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                            </a>
                          ))}
                        </div>
                      </div>
                    )}
                    {videoLinks.length > 0 && (
                      <div>
                        <label className="text-xs text-muted" style={{ display: 'block', marginBottom: '0.25rem', fontWeight: 'bold' }}>
                          Videos ({videoLinks.length})
                        </label>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
                          {videoLinks.map((url, vIdx) => (
                            <a
                              key={vIdx}
                              href={url}
                              target="_blank"
                              rel="noreferrer"
                              style={{ fontSize: '0.75rem', color: 'var(--accent-cyan)', textDecoration: 'none', textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap', display: 'block', padding: '0.25rem 0.5rem', backgroundColor: 'rgba(0,0,0,0.2)', border: '1px solid var(--border-glass)', borderRadius: '4px' }}
                              title="Watch video"
                            >
                              <i className="fa-solid fa-circle-play"></i> Watch Video: {url}
                            </a>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </Modal>
    </section>
  )
}
