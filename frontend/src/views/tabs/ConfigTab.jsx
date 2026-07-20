import React, { useState, useEffect, useRef } from 'react'
import useAppStore from '../../store/appStore'
import { api } from '../../api/client'
import { escapeHtml } from '../../utils/highlight'
import Toggle from '../../components/ui/Toggle'

export default function ConfigTab({
  configDisableFilter,
  setConfigDisableFilter
}) {
  const {
    urlsConfig,
    setUrlsConfig,
    configSelected,
    setConfigSelected,
    keywordsConfig,
    setKeywordsConfig,
    configSelectedKeywords,
    setConfigSelectedKeywords,
  } = useAppStore()

  const [sourceMode, setSourceMode] = useState('disk') // 'disk' | 'upload'
  const [loadStatus, setLoadStatus] = useState({ show: false, type: '', message: '' })
  const [uploadStatus, setUploadStatus] = useState({ show: false, type: '', message: '' })
  const [openGroups, setOpenGroups] = useState({})
  const [isDragOver, setIsDragOver] = useState(false)
  const fileInputRef = useRef(null)

  // Keyword panel state
  const [kwLoadStatus, setKwLoadStatus] = useState({ show: false, type: '', message: '' })
  const [openKwGroups, setOpenKwGroups] = useState({})

  // Fetch disk URLs
  const loadDiskConfig = async () => {
    setLoadStatus({
      show: true,
      type: 'loading',
      message: '<i class="fa-solid fa-circle-notch fa-spin"></i> Loading config/urls.json…'
    })
    try {
      const data = await api.getConfigUrls()
      setUrlsConfig(data)
      setLoadStatus({ show: false, type: '', message: '' })
      
      // Auto-select all by default
      const all = new Set()
      ;(data.urls || []).forEach(e => all.add(e.url))
      setConfigSelected(all)

      // Open first group by default
      const byGroup = {}
      ;(data.urls || []).forEach(e => {
        const g = e.group || 'ungrouped'
        if (!byGroup[g]) byGroup[g] = []
        byGroup[g].push(e)
      })
      const groupKeys = Object.keys(byGroup)
      if (groupKeys.length > 0) {
        setOpenGroups({ [groupKeys[0]]: true })
      }
    } catch (err) {
      setLoadStatus({
        show: true,
        type: 'error',
        message: `
          <i class="fa-solid fa-triangle-exclamation"></i>
          <strong>Failed to load config file:</strong> ${err.message}
          <br><small>Make sure <code>config/urls.json</code> exists at the project root and the backend is running.</small>
        `
      })
    }
  }

  // Load keywords from disk
  const loadKeywordsConfig = async () => {
    setKwLoadStatus({ show: true, type: 'loading', message: '<i class="fa-solid fa-circle-notch fa-spin"></i> Loading config/keywords.json…' })
    try {
      const data = await api.getConfigKeywords()
      setKeywordsConfig(data)
      setKwLoadStatus({ show: false, type: '', message: '' })

      // Auto-select all keywords by default
      const all = new Set()
      ;(data.keywords || []).forEach(e => all.add(e.keyword))
      setConfigSelectedKeywords(all)

      // Open first keyword group by default
      const byG = {}
      ;(data.keywords || []).forEach(e => {
        const g = e.group || 'general'
        if (!byG[g]) byG[g] = []
        byG[g].push(e)
      })
      const keys = Object.keys(byG)
      if (keys.length > 0) setOpenKwGroups({ [keys[0]]: true })
    } catch (err) {
      setKwLoadStatus({
        show: true, type: 'error',
        message: `<i class="fa-solid fa-triangle-exclamation"></i> <strong>Failed to load keywords:</strong> ${err.message}`
      })
    }
  }

  useEffect(() => {
    if (sourceMode === 'disk' && !urlsConfig) {
      loadDiskConfig()
    }
    // Load keywords on mount (always from disk — keywords have no upload mode)
    if (!keywordsConfig) {
      loadKeywordsConfig()
    }
  }, [sourceMode])

  // URL grouping computation
  const allUrls = urlsConfig?.urls || []
  const groups = urlsConfig?.groups || {}
  const byGroup = {}
  allUrls.forEach(entry => {
    const g = entry.group || 'ungrouped'
    if (!byGroup[g]) byGroup[g] = []
    byGroup[g].push(entry)
  })

  // Keyword grouping computation
  const allKeywords = keywordsConfig?.keywords || []
  const kwGroups = keywordsConfig?.groups || {}
  const byKwGroup = {}
  allKeywords.forEach(entry => {
    const g = entry.group || 'general'
    if (!byKwGroup[g]) byKwGroup[g] = []
    byKwGroup[g].push(entry)
  })

  // Select all / Deselect all handlers
  const handleSelectAll = () => {
    const next = new Set()
    allUrls.forEach(e => next.add(e.url))
    setConfigSelected(next)
  }

  const handleDeselectAll = () => {
    setConfigSelected(new Set())
  }

  // Individual selection handlers
  const handleUrlToggle = (url, groupKey) => {
    const next = new Set(configSelected)
    if (next.has(url)) {
      next.delete(url)
    } else {
      next.add(url)
    }
    setConfigSelected(next)
  }

  const handleGroupToggle = (groupKey, entries, checked) => {
    const next = new Set(configSelected)
    entries.forEach(e => {
      if (checked) {
        next.add(e.url)
      } else {
        next.delete(e.url)
      }
    })
    setConfigSelected(next)
  }

  // Accordion open/close
  const toggleGroupOpen = (groupKey) => {
    setOpenGroups(prev => ({
      ...prev,
      [groupKey]: !prev[groupKey]
    }))
  }

  // Keyword selection handlers
  const handleSelectAllKeywords = () => {
    const next = new Set()
    allKeywords.forEach(e => next.add(e.keyword))
    setConfigSelectedKeywords(next)
  }

  const handleDeselectAllKeywords = () => {
    setConfigSelectedKeywords(new Set())
  }

  const handleKeywordToggle = (keyword) => {
    const next = new Set(configSelectedKeywords)
    if (next.has(keyword)) {
      next.delete(keyword)
    } else {
      next.add(keyword)
    }
    setConfigSelectedKeywords(next)
  }

  const handleKwGroupToggle = (groupKey, entries, checked) => {
    const next = new Set(configSelectedKeywords)
    entries.forEach(e => {
      if (checked) next.add(e.keyword)
      else next.delete(e.keyword)
    })
    setConfigSelectedKeywords(next)
  }

  const toggleKwGroupOpen = (groupKey) => {
    setOpenKwGroups(prev => ({
      ...prev,
      [groupKey]: !prev[groupKey]
    }))
  }

  // Validation
  const validateConfigSchema = (data) => {
    const errors = []
    if (!data || typeof data !== 'object') {
      errors.push('File is not a JSON object.')
      return errors
    }
    if (!Array.isArray(data.urls)) {
      errors.push("Missing or invalid 'urls' array.")
    }
    if (!data.groups || typeof data.groups !== 'object') {
      errors.push("Missing or invalid 'groups' object.")
    }
    const requiredFields = ['url', 'label', 'group', 'type', 'language']
    ;(data.urls || []).forEach((entry, i) => {
      const missing = requiredFields.filter(f => !(f in entry))
      if (missing.length > 0) {
        errors.push(`Entry #${i + 1} ("${entry.label || '?'}") missing: ${missing.join(', ')}`)
      }
      if (entry.url && !entry.url.startsWith('http://') && !entry.url.startsWith('https://')) {
        errors.push(`Entry #${i + 1} has invalid URL: ${entry.url}`)
      }
    })
    return errors.slice(0, 5)
  }

  const processUploadedFile = (file) => {
    setUploadStatus({
      show: true,
      type: 'loading',
      message: `<i class="fa-solid fa-circle-notch fa-spin"></i><span>Parsing <strong>${escapeHtml(file.name)}</strong>&hellip;</span>`
    })

    if (file.size > 5 * 1024 * 1024) {
      showUploadError(`File too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Maximum is 5 MB.`)
      return
    }

    if (!file.name.toLowerCase().endsWith('.json')) {
      showUploadError(`File must be a .json file. Got: <strong>${escapeHtml(file.name)}</strong>`)
      return
    }

    const reader = new FileReader()
    reader.onload = (e) => {
      try {
        const data = JSON.parse(e.target.result)
        const errors = validateConfigSchema(data)
        if (errors.length > 0) {
          showUploadError(
            `<strong>${escapeHtml(file.name)}</strong> failed validation:<br>` +
            errors.map(err => `&bull; ${escapeHtml(err)}`).join('<br>')
          )
          return
        }

        // Set Upload Success
        const urlCount = (data.urls || []).length
        setUploadStatus({
          show: true,
          type: 'success',
          message: `
            <i class="fa-solid fa-circle-check"></i>
            <span>
              <strong>${escapeHtml(file.name)}</strong> loaded &mdash; ${urlCount} URL${urlCount !== 1 ? 's' : ''} ready.
              <span style="color:var(--text-muted);font-size:0.68rem;margin-left:4px;">(session only &mdash; not saved to disk)</span>
            </span>
          `
        })

        setUrlsConfig(data)
        const all = new Set()
        ;(data.urls || []).forEach(entry => all.add(entry.url))
        setConfigSelected(all)

        const byG = {}
        ;(data.urls || []).forEach(e => {
          const g = e.group || 'ungrouped'
          if (!byG[g]) byG[g] = []
          byG[g].push(e)
        })
        const groupKeys = Object.keys(byG)
        if (groupKeys.length > 0) {
          setOpenGroups({ [groupKeys[0]]: true })
        }
      } catch (parseErr) {
        showUploadError(`Invalid JSON in <strong>${escapeHtml(file.name)}</strong>: ${escapeHtml(parseErr.message)}`)
      }
    }
    reader.onerror = () => {
      showUploadError(`Failed to read file: <strong>${escapeHtml(file.name)}</strong>`)
    }
    reader.readAsText(file, 'utf-8')
  }

  const showUploadError = (html) => {
    setUploadStatus({
      show: true,
      type: 'error',
      message: `<i class="fa-solid fa-triangle-exclamation"></i><span>${html}</span>`
    })
  }

  const handleDragOver = (e) => {
    e.preventDefault()
    setIsDragOver(true)
  }

  const handleDragLeave = () => {
    setIsDragOver(false)
  }

  const handleDrop = (e) => {
    e.preventDefault()
    setIsDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file) {
      processUploadedFile(file)
    }
  }

  const handleFileChange = (e) => {
    const file = e.target.files[0]
    if (file) {
      processUploadedFile(file)
    }
    e.target.value = ''
  }

  const handleClearUpload = () => {
    setUploadStatus({ show: false, type: '', message: '' })
    setUrlsConfig(null)
    setConfigSelected(new Set())
    setSourceMode('disk')
  }

  return (
    <div id="form-group-config">
      {/* Source toggle */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 0, marginBottom: '12px',
        border: '1px solid var(--border-color)', borderRadius: '8px', overflow: 'hidden',
        width: 'fit-content'
      }}>
        <button
          type="button"
          className="config-src-btn"
          style={{
            padding: '6px 14px', fontSize: '0.75rem', fontWeight: 500,
            background: sourceMode === 'disk' ? 'rgba(0,229,255,0.08)' : 'transparent',
            color: sourceMode === 'disk' ? 'var(--accent-cyan,#00e5ff)' : 'var(--text-muted)',
            border: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '6px',
            borderRight: '1px solid var(--border-color)'
          }}
          onClick={() => setSourceMode('disk')}
        >
          <i className="fa-solid fa-hard-drive"></i> Load from disk
        </button>
        <button
          type="button"
          className="config-src-btn"
          style={{
            padding: '6px 14px', fontSize: '0.75rem', fontWeight: 500,
            background: sourceMode === 'upload' ? 'rgba(0,229,255,0.08)' : 'transparent',
            color: sourceMode === 'upload' ? 'var(--accent-cyan,#00e5ff)' : 'var(--text-muted)',
            border: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '6px'
          }}
          onClick={() => setSourceMode('upload')}
        >
          <i className="fa-solid fa-upload"></i> Upload file
        </button>
      </div>

      {/* Disk Panel */}
      {sourceMode === 'disk' && (
        <div id="config-src-disk-panel">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px' }}>
            <div>
              <label className="form-label" style={{ marginBottom: '2px' }}>Config File URLs</label>
              <small className="form-hint">
                Loaded from <code>config/urls.json</code> at the project root.
                Edit that file directly to add or remove sources.
              </small>
            </div>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              style={{ flexShrink: 0, marginLeft: '12px' }}
              onClick={loadDiskConfig}
            >
              <i className="fa-solid fa-rotate-right"></i> Refresh
            </button>
          </div>
        </div>
      )}

      {/* Upload Panel */}
      {sourceMode === 'upload' && (
        <div id="config-src-upload-panel" style={{ marginBottom: '10px' }}>
          {/* Drop zone */}
          {(!uploadStatus.show || uploadStatus.type === 'error') && (
            <div
              id="config-upload-zone"
              className={isDragOver ? 'drag-over' : ''}
              style={{
                border: '2px dashed rgba(255,255,255,0.18)', borderRadius: '10px',
                padding: '24px 16px', textAlign: 'center', cursor: 'pointer',
                transition: 'border-color 0.2s,background 0.2s',
                background: 'rgba(255,255,255,0.025)'
              }}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current.click()}
            >
              <i className="fa-solid fa-file-arrow-up" style={{ fontSize: '24px', color: 'var(--text-muted)', marginBottom: '8px', display: 'block' }}></i>
              <div style={{ fontSize: '0.82rem', fontWeight: 500, color: 'var(--text-primary)', marginBottom: '4px' }}>
                Drop your urls.json here, or click to browse
              </div>
              <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                Must be a valid <code>urls.json</code> config file &middot; Max 5MB
              </div>
            </div>
          )}

          {/* Hidden file input */}
          <input
            type="file"
            id="config-file-input"
            ref={fileInputRef}
            accept=".json,application/json"
            style={{ display: 'none' }}
            onChange={handleFileChange}
          />

          {/* Upload status / filename badge */}
          {uploadStatus.show && (
            <div
              id="config-upload-status"
              style={{
                display: 'flex',
                marginTop: '8px',
                padding: '8px 12px',
                borderRadius: '8px',
                fontSize: '0.75rem',
                alignItems: 'center',
                gap: '8px',
                background: uploadStatus.type === 'success' ? 'rgba(16,185,129,0.08)' : (uploadStatus.type === 'error' ? 'rgba(239,68,68,0.08)' : 'rgba(255,255,255,0.04)'),
                border: uploadStatus.type === 'success' ? '1px solid rgba(16,185,129,0.2)' : (uploadStatus.type === 'error' ? '1px solid rgba(239,68,68,0.2)' : '1px solid var(--border-color)'),
                color: uploadStatus.type === 'success' ? '#10b981' : (uploadStatus.type === 'error' ? '#ef4444' : 'var(--text-muted)')
              }}
              dangerouslySetInnerHTML={{ __html: uploadStatus.message }}
            />
          )}

          {/* Clear upload button */}
          {uploadStatus.show && (
            <button
              type="button"
              id="btn-config-clear-upload"
              style={{ marginTop: '6px', fontSize: '0.72rem' }}
              className="btn btn-secondary btn-sm"
              onClick={handleClearUpload}
            >
              <i className="fa-solid fa-xmark"></i> Clear &mdash; reload from disk
            </button>
          )}
        </div>
      )}

      {/* Loading / error state for disk config */}
      {loadStatus.show && (
        <div
          id="config-load-status"
          style={{
            padding: '10px 14px', borderRadius: '8px', fontSize: '0.78rem', marginBottom: '10px',
            background: loadStatus.type === 'error' ? 'rgba(239,68,68,0.08)' : 'rgba(255,255,255,0.04)',
            border: loadStatus.type === 'error' ? '1px solid rgba(239,68,68,0.2)' : '1px solid var(--border-color)',
            color: loadStatus.type === 'error' ? '#ef4444' : 'var(--text-muted)'
          }}
          dangerouslySetInnerHTML={{ __html: loadStatus.message }}
        />
      )}

      {/* Select-all / deselect-all controls */}
      {urlsConfig && (
        <div id="config-select-controls" style={{ marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={handleSelectAll}
          >
            <i className="fa-solid fa-check-double"></i> Select All
          </button>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={handleDeselectAll}
          >
            <i className="fa-solid fa-xmark"></i> Deselect All
          </button>
          <span
            id="config-selection-count"
            style={{
              fontSize: '0.75rem',
              color: configSelected.size === 0 ? '#ef4444' : 'var(--text-muted)',
              marginLeft: '4px'
            }}
          >
            {configSelected.size} URL{configSelected.size !== 1 ? 's' : ''} selected
          </span>
        </div>
      )}

      {/* URL groups accordion list */}
      {urlsConfig && (
        <div id="config-groups-container" style={{ border: '1px solid var(--border-color)', borderRadius: '10px', overflow: 'hidden' }}>
          {Object.keys(byGroup).map((groupKey, idx) => {
            const groupLabel = groups[groupKey] || groupKey
            const entries = byGroup[groupKey]
            const isOpen = !!openGroups[groupKey]
            const selectedInGroup = entries.filter(e => configSelected.has(e.url)).length
            const isAllSelected = selectedInGroup === entries.length

            return (
              <div key={groupKey} style={{ borderBottom: '1px solid var(--border-color)' }}>
                {/* Header */}
                <button
                  type="button"
                  style={{
                    width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    padding: '10px 14px', background: 'rgba(255,255,255,0.02)', border: 'none', cursor: 'pointer',
                    color: 'var(--text-primary)', fontSize: '0.82rem', fontWeight: 600, textAlign: 'left'
                  }}
                  onClick={() => toggleGroupOpen(groupKey)}
                >
                  <span style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <input
                      type="checkbox"
                      className="config-group-chk"
                      checked={isAllSelected}
                      onChange={(e) => handleGroupToggle(groupKey, entries, e.target.checked)}
                      style={{ width: '15px', height: '15px', cursor: 'pointer', flexShrink: 0 }}
                      onClick={(e) => e.stopPropagation()}
                    />
                    <span>{groupLabel}</span>
                    <span style={{ fontSize: '0.68rem', fontWeight: 400, color: 'var(--text-muted)', background: 'rgba(255,255,255,0.06)', padding: '2px 7px', borderRadius: '10px' }}>
                      {selectedInGroup}/{entries.length}
                    </span>
                  </span>
                  <i className={`fa-solid fa-chevron-${isOpen ? 'up' : 'down'} config-group-arrow`} style={{ fontSize: '11px', color: 'var(--text-muted)' }}></i>
                </button>

                {/* Collapsible Body */}
                <div style={{ display: isOpen ? 'block' : 'none' }}>
                  {entries.map((entry, entryIdx) => {
                    const isChecked = configSelected.has(entry.url)
                    return (
                      <div
                        key={entry.url}
                        style={{
                          display: 'flex', alignItems: 'flex-start', gap: '10px', padding: '9px 14px 9px 32px',
                          borderTop: '1px solid rgba(255,255,255,0.04)',
                          background: entryIdx % 2 === 0 ? '' : 'rgba(255,255,255,0.01)'
                        }}
                      >
                        <input
                          type="checkbox"
                          className="config-url-chk"
                          checked={isChecked}
                          onChange={() => handleUrlToggle(entry.url, groupKey)}
                          style={{ width: '14px', height: '14px', marginTop: '3px', cursor: 'pointer', flexShrink: 0 }}
                        />
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{
                            fontSize: '0.78rem', fontWeight: 500,
                            color: isChecked ? 'var(--accent-cyan, #00e5ff)' : 'var(--text-primary)',
                            marginBottom: '2px'
                          }}>{entry.label}</div>
                          <div style={{ fontSize: '0.68rem', color: 'var(--accent-cyan,#00e5ff)', wordBreak: 'break-all', marginBottom: '2px' }}>{entry.url}</div>
                          <div style={{ fontSize: '0.67rem', color: 'var(--text-muted)' }}>{entry.notes || ''}</div>
                        </div>
                        <span style={{ fontSize: '0.62rem', padding: '2px 6px', borderRadius: '8px', background: 'rgba(255,255,255,0.06)', color: 'var(--text-muted)', flexShrink: 0, marginTop: '2px' }}>
                          {entry.type || ''}
                        </span>
                      </div>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* ── Keywords Section ─────────────────────────────────────────── */}
      <div style={{ marginTop: '20px' }}>
        {/* Section header */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px' }}>
          <div>
            <label className="form-label" style={{ marginBottom: '2px' }}>
              <i className="fa-solid fa-tags" style={{ marginRight: '6px', color: 'var(--accent-cyan)' }}></i>
              Keywords to Search
            </label>
            <small className="form-hint">
              Loaded from <code>config/keywords.json</code> — all selected keywords will be
              searched across every selected URL. URLs where no keyword is found are skipped
              and labeled automatically.
            </small>
          </div>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            style={{ flexShrink: 0, marginLeft: '12px' }}
            onClick={loadKeywordsConfig}
          >
            <i className="fa-solid fa-rotate-right"></i> Refresh
          </button>
        </div>

        {/* Keyword load status */}
        {kwLoadStatus.show && (
          <div
            style={{
              padding: '10px 14px', borderRadius: '8px', fontSize: '0.78rem', marginBottom: '10px',
              background: kwLoadStatus.type === 'error' ? 'rgba(239,68,68,0.08)' : 'rgba(255,255,255,0.04)',
              border: kwLoadStatus.type === 'error' ? '1px solid rgba(239,68,68,0.2)' : '1px solid var(--border-color)',
              color: kwLoadStatus.type === 'error' ? '#ef4444' : 'var(--text-muted)'
            }}
            dangerouslySetInnerHTML={{ __html: kwLoadStatus.message }}
          />
        )}

        {/* Empty keywords.json notice */}
        {keywordsConfig && allKeywords.length === 0 && (
          <div style={{
            padding: '14px 16px', borderRadius: '10px', fontSize: '0.78rem',
            border: '1px solid var(--border-color)', background: 'rgba(255,255,255,0.02)',
            color: 'var(--text-muted)', textAlign: 'center'
          }}>
            <i className="fa-solid fa-circle-info" style={{ marginRight: '6px', color: 'var(--accent-amber)' }}></i>
            No keywords in <code>config/keywords.json</code> yet.
            Go to <strong>Config Manager</strong> to add keywords, or enable
            <strong> Scrape all pages</strong> below to scrape without keyword filtering.
          </div>
        )}

        {/* Select all / Deselect all controls */}
        {keywordsConfig && allKeywords.length > 0 && (
          <div style={{ marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={handleSelectAllKeywords}
            >
              <i className="fa-solid fa-check-double"></i> Select All
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={handleDeselectAllKeywords}
            >
              <i className="fa-solid fa-xmark"></i> Deselect All
            </button>
            <span style={{
              fontSize: '0.75rem',
              color: configSelectedKeywords.size === 0 ? '#ef4444' : 'var(--text-muted)',
              marginLeft: '4px'
            }}>
              {configSelectedKeywords.size} keyword{configSelectedKeywords.size !== 1 ? 's' : ''} selected
            </span>
          </div>
        )}

        {/* Keyword groups accordion */}
        {keywordsConfig && allKeywords.length > 0 && (
          <div style={{ border: '1px solid var(--border-color)', borderRadius: '10px', overflow: 'hidden', marginBottom: '12px' }}>
            {Object.keys(byKwGroup).map((groupKey) => {
              const groupLabel = kwGroups[groupKey] || groupKey
              const entries = byKwGroup[groupKey]
              const isOpen = !!openKwGroups[groupKey]
              const selectedInGroup = entries.filter(e => configSelectedKeywords.has(e.keyword)).length
              const isAllSelected = selectedInGroup === entries.length

              return (
                <div key={groupKey} style={{ borderBottom: '1px solid var(--border-color)' }}>
                  {/* Group header */}
                  <button
                    type="button"
                    style={{
                      width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      padding: '10px 14px', background: 'rgba(255,255,255,0.02)', border: 'none',
                      cursor: 'pointer', color: 'var(--text-primary)', fontSize: '0.82rem',
                      fontWeight: 600, textAlign: 'left'
                    }}
                    onClick={() => toggleKwGroupOpen(groupKey)}
                  >
                    <span style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <input
                        type="checkbox"
                        checked={isAllSelected}
                        onChange={(e) => handleKwGroupToggle(groupKey, entries, e.target.checked)}
                        style={{ width: '15px', height: '15px', cursor: 'pointer', flexShrink: 0 }}
                        onClick={(e) => e.stopPropagation()}
                      />
                      <span>{groupLabel}</span>
                      <span style={{
                        fontSize: '0.68rem', fontWeight: 400, color: 'var(--text-muted)',
                        background: 'rgba(255,255,255,0.06)', padding: '2px 7px', borderRadius: '10px'
                      }}>
                        {selectedInGroup}/{entries.length}
                      </span>
                    </span>
                    <i className={`fa-solid fa-chevron-${isOpen ? 'up' : 'down'}`}
                       style={{ fontSize: '11px', color: 'var(--text-muted)', alignSelf: 'center' }}></i>
                  </button>

                  {/* Keyword rows */}
                  {isOpen && (
                    <div>
                      {entries.map((entry, entryIdx) => {
                        const isChecked = configSelectedKeywords.has(entry.keyword)
                        return (
                          <div
                            key={entry.keyword}
                            style={{
                              display: 'flex', alignItems: 'flex-start', gap: '10px',
                              padding: '9px 14px 9px 32px',
                              borderTop: '1px solid rgba(255,255,255,0.04)',
                              background: entryIdx % 2 === 0 ? '' : 'rgba(255,255,255,0.01)'
                            }}
                          >
                            <input
                              type="checkbox"
                              checked={isChecked}
                              onChange={() => handleKeywordToggle(entry.keyword)}
                              style={{ width: '14px', height: '14px', marginTop: '3px', cursor: 'pointer', flexShrink: 0 }}
                            />
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{
                                fontSize: '0.8rem', fontWeight: 500,
                                color: isChecked ? 'var(--accent-cyan, #00f0ff)' : 'var(--text-primary)',
                                marginBottom: '2px'
                              }}>
                                {entry.keyword}
                              </div>
                              {entry.notes && (
                                <div style={{ fontSize: '0.67rem', color: 'var(--text-muted)' }}>
                                  {entry.notes}
                                </div>
                              )}
                            </div>
                            <div style={{ display: 'flex', gap: '4px', flexShrink: 0, marginTop: '2px' }}>
                              <span style={{
                                fontSize: '0.62rem', padding: '2px 6px', borderRadius: '8px',
                                background: 'rgba(139,92,246,0.12)', color: 'var(--accent-violet)',
                              }}>
                                {entry.match_type || 'phrase'}
                              </span>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}

        {/* Disable keyword filter toggle */}
        <div className="form-group" id="form-group-config-disable-filter">
          <Toggle
            id="chk-config-disable-filter"
            checked={configDisableFilter}
            onChange={setConfigDisableFilter}
            label="Scrape all pages (ignore keyword matching)"
          />
          {configDisableFilter && (
            <small className="form-hint" style={{ color: 'var(--accent-amber)', marginTop: '4px', display: 'block' }}>
              <i className="fa-solid fa-triangle-exclamation" style={{ marginRight: '4px' }}></i>
              All pages from selected URLs will be scraped regardless of content.
            </small>
          )}
        </div>
      </div>
    </div>
  )
}
