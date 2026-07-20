import React, { useEffect, useState } from 'react'
import useAppStore from '../store/appStore'
import { api } from '../api/client'
import Badge from '../components/ui/Badge'
import DataTable from '../components/ui/DataTable'

export default function HistoryView() {
  const {
    historyList,
    setHistoryList,
    setActiveView,
    setActiveSearch
  } = useAppStore()

  const [loading, setLoading] = useState(false)
  const mounted = React.useRef(true)

  const loadHistory = async () => {
    setLoading(true)
    try {
      const data = await api.getHistory()
      if (mounted.current) {
        setHistoryList(data)
      }
    } catch (err) {
      if (mounted.current) {
        alert(`Failed to load history list: ${err.message}`)
      }
    } finally {
      if (mounted.current) {
        setLoading(false)
      }
    }
  }

  useEffect(() => {
    mounted.current = true
    loadHistory()
    return () => {
      mounted.current = false
    }
  }, [])

  const handleMonitor = (run) => {
    setActiveSearch(run.id, run.keyword)
    setActiveView('results')
  }

  const handleDelete = async (id) => {
    if (!confirm(`Are you sure you want to delete crawl run #${id}?`)) return
    try {
      await api.deleteSearch(id)
      loadHistory()
    } catch (err) {
      alert(`Failed to delete crawl run: ${err.message}`)
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
      <div className="content-card">
        <div className="card-header border-bottom">
          <h2>Execution History Log</h2>
          <button
            className="btn btn-sm btn-outline"
            onClick={loadHistory}
            disabled={loading}
          >
            <i className={`fa-solid fa-arrows-rotate ${loading ? 'fa-spin' : ''}`}></i> Refresh History
          </button>
        </div>
        <div className="card-body p-0 scroll-table-container">
          <DataTable>
            {loading && historyList.length === 0 ? (
              <tr>
                <td colSpan="10" className="text-center py-5 text-muted">
                  <i className="fa-solid fa-spinner fa-spin" style={{ marginRight: '8px' }}></i> Loading history...
                </td>
              </tr>
            ) : !loading && historyList.length === 0 ? (
              <tr>
                <td colSpan="10" className="text-center py-5 text-muted">
                  No crawl histories logged yet.
                </td>
              </tr>
            ) : (
              historyList.map(item => {
                const execDate = new Date(item.created_at).toLocaleString()
                return (
                  <tr key={item.id}>
                    <td>{item.id}</td>
                    <td title={item.keyword} style={{ cursor: item.keyword && item.keyword.includes(',') ? 'help' : 'default' }}>
                      <strong>{formatKeyword(item.keyword)}</strong>
                    </td>
                    <td>
                      <span className="badge badge-skipped">
                        {(item.source_type || '').toUpperCase()}
                      </span>
                    </td>
                    <td>
                      {item.engine === 'fast'
                        ? 'Fast'
                        : item.engine === 'lightpanda'
                        ? 'Lightpanda'
                        : 'Dynamic'}
                    </td>
                    <td>{item.total_urls_found}</td>
                    <td>{item.total_urls_crawled}</td>
                    <td>{item.total_urls_matched}</td>
                    <td>
                      <Badge status={item.status} />
                    </td>
                    <td className="text-muted text-xs">{execDate}</td>
                    <td>
                      <div className="flex-row-gap">
                        <button
                          className="btn btn-xs btn-outline"
                          onClick={() => handleMonitor(item)}
                          title="View Results"
                        >
                          Monitor
                        </button>
                        <button
                          className="table-act-btn delete-btn"
                          onClick={() => handleDelete(item.id)}
                          title="Delete Run"
                        >
                          <i className="fa-solid fa-trash-can"></i>
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })
            )}
          </DataTable>
        </div>
      </div>
    </section>
  )
}
