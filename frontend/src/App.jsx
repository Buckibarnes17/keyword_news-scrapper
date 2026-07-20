import React, { useEffect } from 'react'
import useAppStore from './store/appStore'
import Sidebar from './components/layout/Sidebar'
import TopHeader from './components/layout/TopHeader'
import DashboardView from './views/DashboardView'
import NewCrawlView from './views/NewCrawlView'
import ResultsView from './views/ResultsView'
import HistoryView from './views/HistoryView'
import SchedulesView from './views/SchedulesView'
import ConfigManagerView from './views/ConfigManagerView'

import LoginView from './views/LoginView'
import SignupView from './views/SignupView'

import './styles/global.css'
import './styles/components.css'

export default function App() {
  const activeView = useAppStore(state => state.activeView)
  const isAuthenticated = useAppStore(state => state.isAuthenticated)
  const authView = useAppStore(state => state.authView)
  const hydrateFromStorage = useAppStore(state => state.hydrateFromStorage)

  useEffect(() => {
    hydrateFromStorage()
  }, [])

  if (!isAuthenticated) {
    if (authView === 'signup') {
      return <SignupView />
    }
    return <LoginView />
  }

  const renderActiveView = () => {
    switch (activeView) {
      case 'dashboard':
        return <DashboardView />
      case 'new-crawl':
        return <NewCrawlView />
      case 'results':
        return <ResultsView />
      case 'history':
        return <HistoryView />
      case 'schedules':
        return <SchedulesView />
      case 'config-manager':
        return <ConfigManagerView />
      default:
        return <DashboardView />
    }
  }

  return (
    <div className="app-container">
      <Sidebar />
      <main className="main-content">
        <TopHeader />
        {renderActiveView()}
      </main>
    </div>
  )
}
