import React, { useState } from 'react'

export default function Accordion({ title, children, defaultOpen = false }) {
  const [isOpen, setIsOpen] = useState(defaultOpen)

  return (
    <div className={`accordion-container ${isOpen ? 'open' : ''}`}>
      <div className="accordion-trigger" onClick={() => setIsOpen(!isOpen)}>
        <span>{title}</span>
        <i className="fa-solid fa-chevron-down accordion-arrow"></i>
      </div>
      <div className="accordion-content">
        {isOpen && children}
      </div>
    </div>
  )
}
