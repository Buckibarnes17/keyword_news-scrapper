import React, { useState, useEffect } from 'react'
import { toGroupSlug } from '../../utils/slugify'

const FIXED_ORDER = [
  'china',
  'myanmar',
  'northeast_india',
  'pakistan_central_asia',
  'general'
]

export default function GroupSelect({ configData, value, onChange, required }) {
  const [isCustom, setIsCustom] = useState(false)
  const [customLabel, setCustomLabel] = useState('')
  const [error, setError] = useState('')

  const groups = configData?.groups || {}

  // Sort groups: fixed ones first (preserving FIXED_ORDER), then others alphabetically
  const groupKeys = Object.keys(groups)
  
  const fixedKeys = FIXED_ORDER.filter(k => groupKeys.includes(k))
  const customKeys = groupKeys
    .filter(k => !FIXED_ORDER.includes(k))
    .sort((a, b) => groups[a].localeCompare(groups[b]))

  const sortedKeys = [...fixedKeys, ...customKeys]

  useEffect(() => {
    // If value changes from parent, check if it's __custom__ or a standard group
    if (value === '__custom__') {
      setIsCustom(true)
    } else {
      setIsCustom(false)
      setCustomLabel('')
      setError('')
    }
  }, [value])

  const handleSelectChange = (e) => {
    const val = e.target.value
    if (val === '__custom__') {
      setIsCustom(true)
      onChange({ groupKey: '__custom__', groupLabel: '' })
    } else {
      setIsCustom(false)
      setCustomLabel('')
      setError('')
      onChange({ groupKey: val, groupLabel: '' })
    }
  }

  const handleCustomLabelChange = (e) => {
    const label = e.target.value
    setCustomLabel(label)
    
    if (label.trim() === '') {
      setError('Please enter a group name')
      onChange({ groupKey: '', groupLabel: '' })
    } else {
      setError('')
      onChange({ groupKey: toGroupSlug(label), groupLabel: label.trim() })
    }
  }

  const slug = toGroupSlug(customLabel)

  return (
    <div className="flex-col gap-1 w-full">
      <select
        className="form-control"
        value={isCustom ? '__custom__' : (value || '')}
        onChange={handleSelectChange}
        required={required}
      >
        <option value="" disabled>-- Select Group --</option>
        {sortedKeys.map(key => (
          <option key={key} value={key}>
            {groups[key]}
          </option>
        ))}
        <option value="__custom__">＋ Custom group…</option>
      </select>

      {isCustom && (
        <div className="custom-group-row">
          <input
            type="text"
            className="form-control"
            placeholder="Group name, e.g. Southeast Asia"
            maxLength={60}
            value={customLabel}
            onChange={handleCustomLabelChange}
            required={required}
          />
          <span className={`group-slug-preview ${slug ? 'visible' : ''}`}>
            {slug}
          </span>
        </div>
      )}
      {error && <span className="config-form-error">{error}</span>}
    </div>
  )
}
