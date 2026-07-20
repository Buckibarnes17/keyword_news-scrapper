import React from 'react'
import useAppStore from '../../store/appStore'

const PAGE_TITLES = {
  'dashboard':      ['Dashboard Overview', 'Real-time keyword discovery and content analysis'],
  'new-crawl':      ['Launch Crawl Process', 'Search the web or target direct list of websites'],
  'results':        ['Live Results Monitor', 'Real-time crawl progress and matched keyword results'],
  'history':        ['Execution History Log', 'Review past scraping operations and export reports'],
  'schedules':      ['Scheduled Cron Tasks', 'Review and manage periodic scraper automation'],
  'config-manager': ['Config File Manager', 'Manage persistent sources and keyword matching logs'],
}

export default function TopHeader() {
  const activeView = useAppStore(state => state.activeView)
  const user = useAppStore(state => state.user)
  const logout = useAppStore(state => state.logout)

  const [title, subtitle] = PAGE_TITLES[activeView] || ['', '']
  const userEmail = user?.email || 'operator'
  const userName = userEmail.split('@')[0]
  const userInitials = userName.slice(0, 2).toUpperCase()

  return (
    <header className="top-header">
      <div className="page-title-area">
        <h1>{title}</h1>
        <p className="text-muted">{subtitle}</p>
      </div>
      <div className="user-profile" style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <div className="user-avatar">{userInitials}</div>
          <div className="user-info">
            <span className="user-name" title={userEmail}>{userName}</span>
            <span className="user-role">Scout Operator</span>
          </div>
        </div>
        <button
          onClick={logout}
          className="btn btn-xs btn-outline"
          style={{ borderColor: 'rgba(239, 68, 68, 0.4)', color: '#ef4444', cursor: 'pointer' }}
        >
          <i className="fa-solid fa-right-from-bracket"></i> Logout
        </button>
      </div>
    </header>
  )
}
