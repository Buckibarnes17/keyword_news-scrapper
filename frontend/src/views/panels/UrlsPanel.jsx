import React, { useState, useEffect } from 'react'
import useAppStore from '../../store/appStore'
import { api } from '../../api/client'
import GroupSelect from '../../components/ui/GroupSelect'
import { escapeHtml } from '../../utils/highlight'

export default function UrlsPanel() {
  const { urlsConfig, setUrlsConfig } = useAppStore()

  // UI state
  const [formOpen, setFormOpen] = useState(false)
  const [formMode, setFormMode] = useState('single') // 'single' | 'bulk'
  const [editIndex, setEditIndex] = useState(-1) // -1 = Add, >=0 = Edit

  // Form fields state
  const [url, setUrl] = useState('')
  const [label, setLabel] = useState('')
  const [group, setGroup] = useState('general')
  const [groupLabel, setGroupLabel] = useState('')
  const [type, setType] = useState('news')
  const [language, setLanguage] = useState('en')
  const [notes, setNotes] = useState('')
  const [bulkText, setBulkText] = useState('')

  const [formError, setFormError] = useState({ show: false, type: 'error', message: '' })

  const loadUrls = async () => {
    try {
      const data = await api.getConfigUrls()
      setUrlsConfig(data)
    } catch (err) {
      console.error('Failed to load URLs config:', err)
    }
  }

  useEffect(() => {
    loadUrls()
  }, [])

  const resetForm = () => {
    setUrl('')
    setLabel('')
    setGroup('general')
    setGroupLabel('')
    setType('news')
    setLanguage('en')
    setNotes('')
    setBulkText('')
    setFormError({ show: false, type: 'error', message: '' })
    setEditIndex(-1)
  }

  const handleAddClick = () => {
    resetForm()
    setFormOpen(!formOpen)
  }

  const handleGroupChange = ({ groupKey, groupLabel }) => {
    setGroup(groupKey)
    setGroupLabel(groupLabel)
  }

  const handleSave = async (e) => {
    e.preventDefault()
    setFormError({ show: false, type: 'error', message: '' })

    if (group === '__custom__' || !group) {
      setFormError({ show: true, type: 'error', message: 'Please enter a custom group name.' })
      return
    }

    if (formMode === 'single') {
      const entry = {
        url: url.trim(),
        label: label.trim(),
        group: group,
        type: type,
        language: language.trim() || 'en',
        notes: notes.trim()
      }

      if (groupLabel) {
        entry.group_label = groupLabel
      }

      if (!entry.url || !entry.label) {
        setFormError({ show: true, type: 'error', message: 'URL and Label are required.' })
        return
      }

      if (!entry.url.startsWith('http://') && !entry.url.startsWith('https://')) {
        setFormError({ show: true, type: 'error', message: 'URL must start with http:// or https://' })
        return
      }

      try {
        if (editIndex === -1) {
          // Add URL
          const result = await api.postConfigUrl(entry)
          if (result.groups && urlsConfig) {
            urlsConfig.groups = result.groups
          }
        } else {
          // Edit URL: delete at index first, then post new URL
          await api.deleteConfigUrl(editIndex)
          // Re-fetch fresh config to verify state
          await api.getConfigUrls()
          const result = await api.postConfigUrl(entry)
          if (result.groups && urlsConfig) {
            urlsConfig.groups = result.groups
          }
        }
        setFormOpen(false)
        resetForm()
        loadUrls()
      } catch (err) {
        setFormError({ show: true, type: 'error', message: err.message })
      }
    } else {
      // Bulk add URLs
      const lines = bulkText.split('\n')
      const entries = []

      for (let line of lines) {
        line = line.trim()
        if (!line) continue
        const parts = line.split(',').map(p => p.trim())
        const itemUrl = parts[0]
        const itemLabel = parts[1] || itemUrl
        const itemGroup = parts[2] || group
        const itemType = parts[3] || type
        const itemLang = parts[4] || language
        const itemNotes = parts[5] || notes

        const entryItem = {
          url: itemUrl,
          label: itemLabel,
          group: itemGroup,
          type: itemType,
          language: itemLang,
          notes: itemNotes
        }

        if (itemGroup === group && groupLabel) {
          entryItem.group_label = groupLabel
        }

        entries.push(entryItem)
      }

      if (entries.length === 0) {
        setFormError({ show: true, type: 'error', message: 'Please enter at least one URL entry.' })
        return
      }

      if (entries.length > 500) {
        setFormError({ show: true, type: 'error', message: `Payload exceeds the limit of 500 entries (got ${entries.length}).` })
        return
      }

      try {
        const result = await api.postConfigUrlsBulk({ urls: entries })
        if (result.groups && urlsConfig) {
          urlsConfig.groups = result.groups
        }

        const addedCount = result.added ? result.added.length : 0
        const skippedCount = result.skipped ? result.skipped.length : 0

        const skipReasons = skippedCount > 0
          ? ': ' + Array.from(new Set(result.skipped.map(s => s.reason))).join(', ')
          : ''

        const successMessage = `${addedCount} added, ${skippedCount} skipped${skipReasons}`
        setFormError({
          show: true,
          type: skippedCount > 0 ? 'warning' : 'success',
          message: successMessage
        })

        loadUrls()

        if (skippedCount === 0) {
          setTimeout(() => {
            setFormOpen(false)
            resetForm()
          }, 1500)
        }
      } catch (err) {
        setFormError({ show: true, type: 'error', message: err.message })
      }
    }
  }

  const handleEdit = (idx, entry) => {
    setEditIndex(idx)
    setUrl(entry.url || '')
    setLabel(entry.label || '')
    setGroup(entry.group || 'general')
    setGroupLabel('')
    setType(entry.type || 'news')
    setLanguage(entry.language || 'en')
    setNotes(entry.notes || '')
    setFormMode('single')
    setFormOpen(true)
  }

  const handleDelete = async (idx, label, url) => {
    if (!confirm(`Delete "${label || url}"?`)) return
    try {
      await api.deleteConfigUrl(idx)
      loadUrls()
    } catch (err) {
      alert(`Delete failed: ${err.message}`)
    }
  }

  const urls = urlsConfig?.urls || []
  const groupObj = urlsConfig?.groups || {}

  return (
    <div className="content-card config-panel" id="config-urls-panel">
      {/* Header */}
      <div className="card-header border-bottom config-panel-header">
        <div>
          <h3 className="card-title">
            <i className="fa-solid fa-link"></i> URL Sources
          </h3>
          <small className="form-hint" style={{ marginTop: '2px', display: 'block' }}>
            Saved to <code>config/urls.json</code> &middot; <span>{urls.length} URL{urls.length !== 1 ? 's' : ''}</span>
          </small>
        </div>
        <button className="btn btn-primary btn-sm" onClick={handleAddClick}>
          <i className="fa-solid fa-plus"></i> Add URL
        </button>
      </div>

      {/* Slide form panel */}
      {formOpen && (
        <div id="url-form-panel" className="config-entry-form">
          <div className="config-form-tabs" id="url-form-tabs">
            <button
              type="button"
              className={`form-tab-btn ${formMode === 'single' ? 'active' : ''}`}
              onClick={() => { setFormMode('single'); setFormError({ show: false, type: 'error', message: '' }) }}
              disabled={editIndex >= 0}
            >
              Single Entry
            </button>
            <button
              type="button"
              className={`form-tab-btn ${formMode === 'bulk' ? 'active' : ''}`}
              onClick={() => { setFormMode('bulk'); setFormError({ show: false, type: 'error', message: '' }) }}
              disabled={editIndex >= 0}
            >
              Bulk Add
            </button>
          </div>

          <form onSubmit={handleSave}>
            <div className="form-grid-2">
              {formMode === 'single' ? (
                <React.Fragment>
                  <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                    <label className="form-label">URL <span className="required">*</span></label>
                    <input
                      type="url"
                      className="form-control"
                      placeholder="https://example.com/"
                      value={url}
                      onChange={(e) => setUrl(e.target.value)}
                      required
                    />
                  </div>
                  <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                    <label className="form-label">Label <span className="required">*</span></label>
                    <input
                      type="text"
                      className="form-control"
                      placeholder="e.g. China Daily"
                      value={label}
                      onChange={(e) => setLabel(e.target.value)}
                      required
                    />
                  </div>
                </React.Fragment>
              ) : (
                <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                  <label className="form-label">
                    Bulk URLs (one per line, format: <code>url[, label, group, type, language, notes]</code>) <span className="required">*</span>
                  </label>
                  <textarea
                    className="form-control"
                    rows="6"
                    placeholder="https://example.com/source1, Source 1&#10;https://example.com/source2, Source 2, china, news, en, Notes here"
                    style={{ fontFamily: 'monospace', fontSize: '0.78rem', resize: 'vertical' }}
                    value={bulkText}
                    onChange={(e) => setBulkText(e.target.value)}
                    required
                  ></textarea>
                </div>
              )}

              {/* GroupSelect */}
              <div className="form-group" id="url-group-field">
                <label className="form-label">Group <span className="required">*</span></label>
                <GroupSelect
                  configData={urlsConfig}
                  value={group}
                  onChange={handleGroupChange}
                  required
                />
              </div>

              <div className="form-group">
                <label className="form-label">Type</label>
                <select
                  className="form-control"
                  value={type}
                  onChange={(e) => setType(e.target.value)}
                >
                  <option value="news">News</option>
                  <option value="government">Government</option>
                  <option value="think_tank">Think Tank</option>
                  <option value="journal">Journal</option>
                  <option value="other">Other</option>
                </select>
              </div>

              <div className="form-group">
                <label className="form-label">Language (ISO)</label>
                <input
                  type="text"
                  className="form-control"
                  placeholder="en"
                  maxLength={5}
                  value={language}
                  onChange={(e) => setLanguage(e.target.value)}
                />
              </div>

              <div className="form-group">
                <label className="form-label">Notes</label>
                <input
                  type="text"
                  className="form-control"
                  placeholder="Brief description"
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
              </div>
            </div>

            <div className="config-form-actions">
              <button className="btn btn-primary btn-sm" type="submit">
                <i className="fa-solid fa-floppy-disk"></i>
                <span>
                  {formMode === 'bulk' ? 'Bulk Add URLs' : (editIndex === -1 ? 'Add URL' : 'Save Changes')}
                </span>
              </button>
              <button className="btn btn-secondary btn-sm" type="button" onClick={() => setFormOpen(false)}>
                Cancel
              </button>
              {formError.show && (
                <span className={`config-form-error ${formError.type === 'success' ? 'success' : (formError.type === 'warning' ? 'warning' : '')}`}>
                  {formError.message}
                </span>
              )}
            </div>
          </form>
        </div>
      )}

      {/* URL list */}
      <div id="config-urls-list" className="config-entry-list">
        {urls.length === 0 ? (
          <div className="config-list-loading">
            No URLs yet. Click <strong>Add URL</strong> to get started.
          </div>
        ) : (
          urls.map((entry, idx) => (
            <div className="config-entry-row" key={idx}>
              <div className="config-entry-info">
                <div className="config-entry-label">{entry.label || entry.url}</div>
                <div className="config-entry-url">{entry.url}</div>
                <div className="config-entry-meta">
                  <span className="config-entry-badge">
                    {groupObj[entry.group] || entry.group || ''}
                  </span>
                  <span className="config-entry-badge">{entry.type || ''}</span>
                  <span className="config-entry-badge">{entry.language || ''}</span>
                  {entry.notes && <span>{entry.notes}</span>}
                </div>
              </div>
              <div className="config-entry-actions">
                <button
                  className="config-action-btn"
                  title="Edit"
                  onClick={() => handleEdit(idx, entry)}
                >
                  <i className="fa-solid fa-pen"></i>
                </button>
                <button
                  className="config-action-btn delete"
                  title="Delete"
                  onClick={() => handleDelete(idx, entry.label, entry.url)}
                >
                  <i className="fa-solid fa-trash"></i>
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
