import React, { useEffect, useState } from 'react'
import useAppStore from '../store/appStore'
import { api } from '../api/client'
import Badge from '../components/ui/Badge'
import DataTable from '../components/ui/DataTable'

export default function DashboardView() {
  const {
    historyList,
    scheduleList,
    setHistoryList,
    setScheduleList,
    setActiveView,
    setCrawlTab,
    setActiveSearch
  } = useAppStore()

  const [loading, setLoading] = useState(false)

  const fetchData = async () => {
    setLoading(true)
    try {
      const [history, schedules] = await Promise.all([
        api.getHistory(),
        api.getSchedules()
      ])
      setHistoryList(history)
      setScheduleList(schedules)
    } catch (err) {
      console.error('Failed to load dashboard data:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
  }, [])

  // Aggregate metrics
  let totalCrawled = 0
  let totalMatched = 0
  let completedRuns = 0

  historyList.forEach(item => {
    totalCrawled += (item.total_urls_crawled || 0)
    totalMatched += (item.total_urls_matched || 0)
    if (item.status === 'completed') {
      completedRuns++
    }
  })

  const activeSchedules = scheduleList.filter(s => s.active).length
  const recentRuns = historyList.slice(0, 5)

  const handleMonitor = (run) => {
    setActiveSearch(run.id, run.keyword)
    setActiveView('results')
  }

  const formatKeyword = (keyword) => {
    if (!keyword) return '(No Keyword)'
    const list = keyword.split(',').map(s => s.trim()).filter(Boolean)
    if (list.length <= 2) return keyword
    return `${list.slice(0, 2).join(', ')} (+${list.length - 2} more)`
  }

  return (
    <section className="view-section active">
      {/* Metrics Grid */}
      <div className="metrics-grid">
        <div className="metric-card">
          <div className="metric-icon bg-cyan">
            <i className="fa-solid fa-circle-nodes"></i>
          </div>
          <div className="metric-info">
            <span className="metric-label">Total Crawled URLs</span>
            <h3 className="metric-val">{totalCrawled}</h3>
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-icon bg-violet">
            <i className="fa-solid fa-bullseye"></i>
          </div>
          <div className="metric-info">
            <span className="metric-label">Total Keyword Matches</span>
            <h3 className="metric-val">{totalMatched}</h3>
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-icon bg-pink">
            <i className="fa-solid fa-calendar-check"></i>
          </div>
          <div className="metric-info">
            <span className="metric-label">Active Schedules</span>
            <h3 className="metric-val">{activeSchedules}</h3>
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-icon bg-amber">
            <i className="fa-solid fa-database"></i>
          </div>
          <div className="metric-info">
            <span className="metric-label">Completed Crawl Runs</span>
            <h3 className="metric-val">{completedRuns}</h3>
          </div>
        </div>
      </div>

      {/* Dashboard Split Layout */}
      <div className="dashboard-split">
        {/* Recent Runs Table */}
        <div className="content-card flex-2">
          <div className="card-header">
            <h2>Recent Crawl Runs</h2>
            <button
              className="btn btn-sm btn-outline"
              onClick={fetchData}
              disabled={loading}
            >
              <i className={`fa-solid fa-arrows-rotate ${loading ? 'fa-spin' : ''}`}></i> Refresh
            </button>
          </div>
          <div className="card-body scroll-table-container">
            <DataTable>
              {recentRuns.length === 0 ? (
                <tr>
                  <td colSpan="7" className="text-center py-4 text-muted">
                    No runs executed yet.
                  </td>
                </tr>
              ) : (
                recentRuns.map(run => (
                  <tr key={run.id}>
                    <td>{run.id}</td>
                    <td title={run.keyword} style={{ cursor: run.keyword && run.keyword.includes(',') ? 'help' : 'default' }}>
                      <strong>{formatKeyword(run.keyword)}</strong>
                    </td>
                    <td>
                      {run.engine === 'fast'
                        ? 'Fast'
                        : run.engine === 'lightpanda'
                        ? 'Lightpanda'
                        : 'Dynamic'}
                    </td>
                    <td>
                      <span className="badge badge-skipped">
                        {(run.source_type || '').toUpperCase()}
                      </span>
                    </td>
                    <td>
                      {run.total_urls_crawled} / {run.total_urls_found}
                    </td>
                    <td>
                      <Badge status={run.status} />
                    </td>
                    <td>
                      <button
                        className="btn btn-xs btn-outline"
                        onClick={() => handleMonitor(run)}
                      >
                        Monitor
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </DataTable>
          </div>
        </div>

        {/* Quick Actions Panel */}
        <div className="content-card flex-1">
          <div className="card-header">
            <h2>Quick Actions</h2>
          </div>
          <div className="card-body flex-col gap-3">
            <button
              className="quick-action-btn qa-cyan"
              onClick={() => {
                setActiveView('new-crawl')
                setCrawlTab('search')
              }}
            >
              <i className="fa-solid fa-magnifying-glass"></i>
              <div className="qa-details">
                <h4>Start New Keyword Scrape</h4>
                <p>Submit keyword to crawl search results</p>
              </div>
            </button>

            <button
              className="quick-action-btn qa-violet"
              onClick={() => {
                setActiveView('new-crawl')
                setCrawlTab('direct')
              }}
            >
              <i className="fa-solid fa-list-check"></i>
              <div className="qa-details">
                <h4>Scan Specific URLs</h4>
                <p>Input sitemaps or URL list directly</p>
              </div>
            </button>

            <button
              className="quick-action-btn qa-pink"
              onClick={() => {
                setActiveView('schedules')
              }}
            >
              <i className="fa-solid fa-calendar-plus"></i>
              <div className="qa-details">
                <h4>Schedule Keyword Scan</h4>
                <p>Configure recurring daily/weekly scans</p>
              </div>
            </button>
          </div>
        </div>
      </div>
    </section>
  )
}
