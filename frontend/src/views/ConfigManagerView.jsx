import React from 'react'
import UrlsPanel from './panels/UrlsPanel'
import KeywordsPanel from './panels/KeywordsPanel'

export default function ConfigManagerView() {
  return (
    <section className="view-section active">
      <div className="section-header">
        <div style={{ marginBottom: '1.5rem' }}>
          <h2 className="section-title" style={{ fontSize: '1.5rem', fontWeight: 700, fontFamily: 'var(--font-title)', marginBottom: '0.25rem' }}>Config Manager</h2>
          <p className="section-subtitle text-muted" style={{ fontSize: '0.9rem' }}>
            Manage the persistent URL and keyword config files that auto-load into every crawl session. Changes are saved to disk immediately.
          </p>
        </div>
      </div>

      <div className="config-manager-split">
        <UrlsPanel />
        <KeywordsPanel />
      </div>
    </section>
  )
}
