import React from 'react'

export default function DataTable({ headers, children, id, minWidth }) {
  return (
    <div className="scroll-table-container">
      <table className="data-table" id={id} style={{ minWidth: minWidth }}>
        {headers && (
          <thead>
            <tr>
              {headers.map((h, i) => (
                <th
                  key={i}
                  className={h.sortable ? 'sortable' : ''}
                  onClick={h.onClick}
                  style={h.style}
                >
                  {h.label} {h.sortable && h.icon}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {children}
        </tbody>
      </table>
    </div>
  )
}
