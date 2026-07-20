import React from 'react'

export default function Pagination({ page, total, limit, onPageChange }) {
  const start = total === 0 ? 0 : (page - 1) * limit + 1
  const end = Math.min(total, page * limit)
  
  const hasPrev = page > 1
  const hasNext = page * limit < total

  return (
    <div className="card-footer border-top pagination-footer">
      <span className="text-sm text-muted">
        Showing {start}–{end} of {total} records
      </span>
      <div className="pagination-controls">
        <button
          className="btn btn-xs btn-outline"
          disabled={!hasPrev}
          onClick={() => onPageChange(page - 1)}
        >
          <i className="fa-solid fa-chevron-left"></i> Prev
        </button>
        <span className="text-sm px-2 font-medium">
          {page}
        </span>
        <button
          className="btn btn-xs btn-outline"
          disabled={!hasNext}
          onClick={() => onPageChange(page + 1)}
        >
          Next <i className="fa-solid fa-chevron-right"></i>
        </button>
      </div>
    </div>
  )
}
