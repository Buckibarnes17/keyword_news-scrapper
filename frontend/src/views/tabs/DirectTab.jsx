import React from 'react'
import Toggle from '../../components/ui/Toggle'
import useAppStore from '../../store/appStore'

export default function DirectTab({
  directUrls,
  setDirectUrls,
  disableKeywordFilter,
  setDisableKeywordFilter,
  directKeywords,
  setDirectKeywords
}) {
  const keywordsConfig = useAppStore(state => state.keywordsConfig)
  const keywords = keywordsConfig?.keywords || []

  return (
    <div className="flex-col gap-3">
      <div className="form-group" id="form-group-direct-urls">
        <label htmlFor="direct-urls-input" className="form-label">Target URLs or Sitemap URLs</label>
        <textarea
          id="direct-urls-input"
          rows={8}
          placeholder="https://example.com/article1&#10;https://myblog.com/sitemap.xml&#10;https://anotherdomain.org/about"
          className="form-control text-monospace"
          value={directUrls}
          onChange={(e) => setDirectUrls(e.target.value)}
          required
        ></textarea>
        <small className="form-hint">
          Paste target URLs or sitemap links (one per line). XML sitemaps will be auto-parsed to extract all child URLs.
        </small>
      </div>

      <div className="form-group" id="form-group-disable-keyword-filter">
        <Toggle
          id="chk-disable-keyword-filter"
          checked={disableKeywordFilter}
          onChange={setDisableKeywordFilter}
          label="Disable keyword filtering (Scrape all pages)"
        />
      </div>

      {!disableKeywordFilter && (
        <div className="form-group" id="form-group-direct-keywords">
          <label htmlFor="direct-keywords-input" className="form-label">Keywords to Search</label>
          <div className="input-with-icon">
            <i className="fa-solid fa-tags"></i>
            <input
              type="text"
              id="direct-keywords-input"
              placeholder="e.g. Python, FastAPI, Celery (comma-separated)"
              className="form-control"
              list="config-keywords-datalist"
              value={directKeywords}
              onChange={(e) => setDirectKeywords(e.target.value)}
              required
            />
          </div>
          <small className="form-hint">
            Enter the list of keywords to search for, separated by commas or newlines.
          </small>
        </div>
      )}
    </div>
  )
}
