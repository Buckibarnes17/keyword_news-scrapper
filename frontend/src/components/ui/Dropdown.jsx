import React, { useState, useEffect, useRef } from 'react'

export default function Dropdown({ trigger, children }) {
  const [isOpen, setIsOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    const handleOutsideClick = (e) => {
      if (ref.current && !ref.current.contains(e.target)) {
        setIsOpen(false)
      }
    }
    if (isOpen) {
      document.addEventListener('click', handleOutsideClick)
    }
    return () => {
      document.removeEventListener('click', handleOutsideClick)
    }
  }, [isOpen])

  return (
    <div className={`dropdown ${isOpen ? 'open' : ''}`} ref={ref}>
      {React.cloneElement(trigger, {
        onClick: (e) => {
          e.stopPropagation()
          setIsOpen(!isOpen)
        }
      })}
      <div className="dropdown-content">
        {React.Children.map(children, child => {
          if (!child) return null
          return React.cloneElement(child, {
            onClick: (e) => {
              setIsOpen(false)
              if (child.props.onClick) {
                child.props.onClick(e)
              }
            }
          })
        })}
      </div>
    </div>
  )
}
