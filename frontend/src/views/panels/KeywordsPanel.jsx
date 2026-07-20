import React, { useState, useEffect } from 'react'
import useAppStore from '../../store/appStore'
import { api } from '../../api/client'
import GroupSelect from '../../components/ui/GroupSelect'

export default function KeywordsPanel() {
  const {
    keywordsConfig,
    setKeywordsConfig,
    setActiveView,
    setCrawlTab,
    setNewCrawlKeyword
  } = useAppStore()

  // UI state
  const [formOpen, setFormOpen] = useState(false)
  const [formMode, setFormMode] = useState('single') // 'single' | 'bulk'
  const [editIndex, setEditIndex] = useState(-1) // -1 = Add, >=0 = Edit

  // Form fields state
  const [keyword, setKeyword] = useState('')
  const [group, setGroup] = useState('general')
  const [groupLabel, setGroupLabel] = useState('')
  const [matchType, setMatchType] = useState('phrase')
  const [notes, setNotes] = useState('')
  const [bulkText, setBulkText] = useState('')

  const [formError, setFormError] = useState({ show: false, type: 'error', message: '' })

  const loadKeywords = async () => {
    try {
      const data = await api.getConfigKeywords()
      setKeywordsConfig(data)
    } catch (err) {
      console.error('Failed to load keywords config:', err)
    }
  }

  useEffect(() => {
    loadKeywords()
  }, [])

  const resetForm = () => {
    setKeyword('')
    setGroup('general')
    setGroupLabel('')
    setMatchType('phrase')
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
        keyword: keyword.trim(),
        group: group,
        match_type: matchType,
        notes: notes.trim()
      }

      if (groupLabel) {
        entry.group_label = groupLabel
      }

      if (!entry.keyword) {
        setFormError({ show: true, type: 'error', message: 'Keyword cannot be empty.' })
        return
      }

      try {
        let result
        if (editIndex === -1) {
          result = await api.postConfigKeyword(entry)
        } else {
          result = await api.putConfigKeyword(editIndex, entry)
        }

        if (result.groups && keywordsConfig) {
          keywordsConfig.groups = result.groups
        }

        setFormOpen(false)
        resetForm()
        loadKeywords()
      } catch (err) {
        setFormError({ show: true, type: 'error', message: err.message })
      }
    } else {
      // Bulk Mode
      const lines = bulkText.split('\n')
      const entries = []

      for (let line of lines) {
        line = line.trim()
        if (!line) continue
        const entryItem = {
          keyword: line,
          group: group,
          match_type: matchType,
          notes: notes.trim()
        }

        if (groupLabel) {
          entryItem.group_label = groupLabel
        }

        entries.push(entryItem)
      }

      if (entries.length === 0) {
        setFormError({ show: true, type: 'error', message: 'Please enter at least one keyword.' })
        return
      }

      if (entries.length > 500) {
        setFormError({ show: true, type: 'error', message: `Payload exceeds the limit of 500 entries (got ${entries.length}).` })
        return
      }

      try {
        const result = await api.postConfigKeywordsBulk({ keywords: entries })
        if (result.groups && keywordsConfig) {
          keywordsConfig.groups = result.groups
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

        loadKeywords()

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
    setKeyword(entry.keyword || '')
    setGroup(entry.group || 'general')
    setGroupLabel('')
    setMatchType(entry.match_type || 'phrase')
    setNotes(entry.notes || '')
    setFormMode('single')
    setFormOpen(true)
  }

  const handleDelete = async (idx, kw) => {
    if (!confirm(`Delete keyword "${kw}"?`)) return
    try {
      await api.deleteConfigKeyword(idx)
      loadKeywords()
    } catch (err) {
      alert(`Delete failed: ${err.message}`)
    }
  }

  const handleUseKeyword = (kw) => {
    setNewCrawlKeyword(kw)
    setCrawlTab('search')
    setActiveView('new-crawl')
  }

  const keywords = keywordsConfig?.keywords || []
  const groupObj = keywordsConfig?.groups || {}

  return (
    <div className="content-card config-panel" id="config-keywords-panel">
      {/* Header */}
      <div className="card-header border-bottom config-panel-header">
        <div>
          <h3 className="card-title">
            <i className="fa-solid fa-tags"></i> Keywords
          </h3>
          <small className="form-hint" style={{ marginTop: '2px', display: 'block' }}>
            Saved to <code>config/keywords.json</code> &middot; <span>{keywords.length} keyword{keywords.length !== 1 ? 's' : ''}</span>
          </small>
        </div>
        <button className="btn btn-primary btn-sm" onClick={handleAddClick}>
          <i className="fa-solid fa-plus"></i> Add Keyword
        </button>
      </div>

      {/* Slide form panel */}
      {formOpen && (
        <div id="keyword-form-panel" className="config-entry-form">
          <div className="config-form-tabs" id="kw-form-tabs">
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
                <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                  <label className="form-label">Keyword / Phrase <span className="required">*</span></label>
                  <input
                    type="text"
                    className="form-control"
                    placeholder="e.g. Myanmar coup OR military junta"
                    value={keyword}
                    onChange={(e) => setKeyword(e.target.value)}
                    required
                  />
                </div>
              ) : (
                <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                  <label className="form-label">Bulk Keywords (one per line) <span className="required">*</span></label>
                  <textarea
                    className="form-control"
                    rows="6"
                    placeholder="Myanmar coup&#10;military junta&#10;Balochistan protest"
                    style={{ fontFamily: 'monospace', fontSize: '0.78rem', resize: 'vertical' }}
                    value={bulkText}
                    onChange={(e) => setBulkText(e.target.value)}
                    required
                  ></textarea>
                </div>
              )}

              {/* GroupSelect */}
              <div className="form-group" id="kw-group-field">
                <label className="form-label">Group</label>
                <GroupSelect
                  configData={keywordsConfig}
                  value={group}
                  onChange={handleGroupChange}
                />
              </div>

              <div className="form-group">
                <label className="form-label">Match type</label>
                <select
                  className="form-control"
                  value={matchType}
                  onChange={(e) => setMatchType(e.target.value)}
                >
                  <option value="phrase">Phrase match</option>
                  <option value="boolean">Boolean expression</option>
                </select>
              </div>

              <div className="form-group" style={{ gridColumn: '1 / -1' }}>
                <label className="form-label">Notes</label>
                <input
                  type="text"
                  className="form-control"
                  placeholder="What this keyword monitors"
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
              </div>
            </div>

            <div className="config-form-actions">
              <button className="btn btn-primary btn-sm" type="submit">
                <i className="fa-solid fa-floppy-disk"></i>
                <span>
                  {formMode === 'bulk' ? 'Bulk Add Keywords' : (editIndex === -1 ? 'Add Keyword' : 'Save Changes')}
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

      {/* Keywords list */}
      <div id="config-keywords-list" className="config-entry-list">
        {keywords.length === 0 ? (
          <div className="config-list-loading">
            No keywords yet. Click <strong>Add Keyword</strong> to get started.
          </div>
        ) : (
          keywords.map((entry, idx) => (
            <div className="config-entry-row" key={idx}>
              <div className="config-entry-info">
                <div className="config-entry-kw">{entry.keyword}</div>
                <div className="config-entry-meta">
                  <span className="config-entry-badge">
                    {groupObj[entry.group] || entry.group || ''}
                  </span>
                  <span className="config-entry-badge">{entry.match_type || 'phrase'}</span>
                  {entry.notes && <span>{entry.notes}</span>}
                </div>
              </div>
              <div className="config-entry-actions">
                <button
                  className="config-action-btn"
                  title="Use in new crawl"
                  onClick={() => handleUseKeyword(entry.keyword)}
                  style={{ color: 'var(--accent-cyan,#00e5ff)' }}
                >
                  <i className="fa-solid fa-rocket"></i>
                </button>
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
                  onClick={() => handleDelete(idx, entry.keyword)}
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
