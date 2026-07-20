import React from 'react'

export default function Toggle({ id, checked, onChange, label, disabled }) {
  return (
    <label className="toggle-control" style={{ cursor: disabled ? 'not-allowed' : 'pointer' }}>
      <input
        type="checkbox"
        id={id}
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        disabled={disabled}
      />
      <span className="toggle-slider"></span>
      {label && <span className="toggle-label">{label}</span>}
    </label>
  )
}
