import React from 'react'

export default function ProgressBar({ value }) {
  const pct = Math.min(100, Math.max(0, Math.round(value || 0)))

  return (
    <div className="progress-bar-container">
      <div
        className="progress-bar-fill"
        style={{ width: `${pct}%` }}
      ></div>
    </div>
  )
}
