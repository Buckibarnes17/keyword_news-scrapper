import React from 'react'

export default function Badge({ status, label }) {
  const normalized = (status || '').toLowerCase()
  const displayLabel = label || status || ''
  
  let badgeClass = `badge-${normalized}`
  if (normalized === 'active') badgeClass = 'badge-completed'
  if (normalized === 'inactive') badgeClass = 'badge-pending'
  
  return (
    <span className={`badge ${badgeClass}`}>
      {displayLabel}
    </span>
  )
}
