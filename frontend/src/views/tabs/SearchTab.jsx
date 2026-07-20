import React from 'react'
import useAppStore from '../../store/appStore'

export default function SearchTab({ keyword, setKeyword }) {
  const keywordsConfig = useAppStore(state => state.keywordsConfig)
  const keywords = keywordsConfig?.keywords || []

  return (
    <div className="form-group" id="form-group-keyword">
      <label htmlFor="keyword-input" className="form-label">Search Keyword / Phrase</label>
      <div className="input-with-icon">
        <i className="fa-solid fa-magnifying-glass"></i>
        <input
          type="text"
          id="keyword-input"
          placeholder="e.g. Python programming OR FastAPI AND Celery"
          className="form-control"
          list="config-keywords-datalist"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          required
        />
      </div>
      <small className="form-hint">
        Supports Boolean search operators: AND, OR, NOT and parentheses (e.g. <code>machine learning AND (python OR pytorch)</code>).
      </small>

      <datalist id="config-keywords-datalist">
        {keywords.map((item, idx) => (
          <option key={idx} value={item.keyword} />
        ))}
      </datalist>
    </div>
  )
}
