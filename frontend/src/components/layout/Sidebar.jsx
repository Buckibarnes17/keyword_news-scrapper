import React from 'react'
import useAppStore from '../../store/appStore'
import { useApiStatus } from '../../hooks/useApiStatus'

const NAV_ITEMS = [
  { id: 'dashboard',      label: 'Dashboard',      icon: 'fa-chart-pie' },
  { id: 'new-crawl',      label: 'New Crawl Run',  icon: 'fa-magnifying-glass-plus' },
  { id: 'history',        label: 'Crawl History',  icon: 'fa-clock-rotate-left' },
  { id: 'schedules',      label: 'Scheduled Jobs', icon: 'fa-calendar-days' },
  { id: 'config-manager', label: 'Config Manager', icon: 'fa-sliders' },
]

export default function Sidebar() {
  const { activeView, setActiveView, apiConnected } = useAppStore()
  const clock = useApiStatus()

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <div className="brand-logo">
          <i className="fa-solid fa-newspaper brand-icon"></i>
        </div>
        <div className="brand-text">
          <span className="brand-name">KEYWORD NEWS</span>
          <span className="brand-sub">SCRAPER</span>
        </div>
      </div>

      <nav className="sidebar-menu">
        {NAV_ITEMS.map(item => (
          <button
            key={item.id}
            className={`menu-item ${activeView === item.id ? 'active' : ''}`}
            onClick={() => setActiveView(item.id)}
          >
            <i className={`fa-solid ${item.icon}`}></i>
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-footer">
        <div className="api-status">
          <span className={`status-indicator ${apiConnected ? 'online' : 'offline'}`}></span>
          <span className="status-text">
            {apiConnected ? 'API Connected' : 'API Disconnected'}
          </span>
        </div>
        <div className="system-time">
          <i className="fa-regular fa-clock"></i>
          <span>{clock}</span>
        </div>
      </div>
    </aside>
  )
}
