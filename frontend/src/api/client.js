const BASE = window.location.origin

async function request(url, options = {}) {
  // 1. Get current access token
  const token = localStorage.getItem('kws_access_token')
  
  // 2. Set headers
  const headers = { ...options.headers }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  
  // 3. Perform fetch
  let res = await fetch(url, { ...options, headers })
  
  // 4. Handle 401 Unauthorized with token refresh once
  if (res.status === 401 && !url.includes('/api/auth/refresh') && !url.includes('/api/auth/login')) {
    const refreshToken = localStorage.getItem('kws_refresh_token')
    if (refreshToken) {
      try {
        const refreshRes = await fetch(`${BASE}/api/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refreshToken })
        })
        if (refreshRes.ok) {
          const tokens = await refreshRes.json()
          // Update tokens in localStorage
          localStorage.setItem('kws_access_token', tokens.access_token)
          localStorage.setItem('kws_refresh_token', tokens.refresh_token)
          
          // Retry the request with the new token
          headers['Authorization'] = `Bearer ${tokens.access_token}`
          res = await fetch(url, { ...options, headers })
        } else {
          // Refresh failed, trigger logout
          throw new Error('Refresh token invalid')
        }
      } catch (err) {
        console.error('Session expired, logging out...', err)
        // Clean up localStorage and notify store to reset state
        localStorage.removeItem('kws_access_token')
        localStorage.removeItem('kws_refresh_token')
        localStorage.removeItem('kws_user')
        try {
          const { default: store } = await import('../store/appStore')
          store.getState().logout()
        } catch (e) {
          // ignore
        }
        throw new Error('Session expired. Please log in again.')
      }
    }
  }
  
  if (!res.ok) {
    const t = await res.text().catch(() => res.statusText)
    let detail = t
    try {
      const parsed = JSON.parse(t)
      if (parsed.detail) detail = parsed.detail
    } catch (_) {}
    throw new Error(detail || `HTTP ${res.status}`)
  }
  
  return res
}

export const api = {
  // Auth endpoints
  login:             (p)         => request(`${BASE}/api/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) }).then(r => r.json()),
  signup:            (p)         => request(`${BASE}/api/auth/signup`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) }).then(r => r.json()),
  refresh:           (token)     => request(`${BASE}/api/auth/refresh`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: token }) }).then(r => r.json()),
  me:                ()          => request(`${BASE}/api/auth/me`).then(r => r.json()),
  logout:            ()          => request(`${BASE}/api/auth/logout`, { method: 'POST' }).then(r => r.json()),

  // Scraper endpoints
  postSearch:        (p)         => request(`${BASE}/api/search`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) }).then(r => r.json()),
  getResults:        (id)        => request(`${BASE}/api/results/${id}`).then(r => r.json()),
  getHistory:        ()          => request(`${BASE}/api/history`).then(r => r.json()),
  deleteSearch:      (id)        => request(`${BASE}/api/search/${id}`, { method: 'DELETE' }),
  stopSearch:        (id)        => request(`${BASE}/api/search/${id}/stop`, { method: 'POST' }).then(r => r.json()),
  retrySearch:       (id)        => request(`${BASE}/api/search/${id}/retry`, { method: 'POST' }).then(r => r.json()),
  getExport:         (id, p)     => request(`${BASE}/api/export/${id}?${new URLSearchParams(p)}`),
  exportPostgres:    (id)        => request(`${BASE}/api/export/${id}/postgres`, { method: 'POST' }).then(r => r.json()),
  getSchedules:      ()          => request(`${BASE}/api/schedules`).then(r => r.json()),
  postSchedule:      (p)         => request(`${BASE}/api/schedules`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) }).then(r => r.json()),
  deleteSchedule:    (id)        => request(`${BASE}/api/schedules/${id}`, { method: 'DELETE' }),
  getHealth:         ()          => request(`${BASE}/api/health`).then(r => r.json()),
  getTorStatus:      ()          => request(`${BASE}/api/tor/status`).then(r => r.json()),
  getConfigUrls:     ()          => request(`${BASE}/api/config/urls`).then(r => r.json()),
  postConfigUrl:     (p)         => request(`${BASE}/api/config/urls`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) }).then(r => r.json()),
  postConfigUrlsBulk: (p)        => request(`${BASE}/api/config/urls/bulk`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) }).then(r => r.json()),
  deleteConfigUrl:   (idx)       => request(`${BASE}/api/config/urls/${idx}`, { method: 'DELETE' }).then(r => r.json()),
  uploadConfigUrls:  (file)      => { const fd = new FormData(); fd.append('file', file); return request(`${BASE}/api/config/urls/upload`, { method: 'POST', body: fd }).then(r => r.json()) },
  getConfigKeywords: ()          => request(`${BASE}/api/config/keywords`).then(r => r.json()),
  postConfigKeyword: (p)         => request(`${BASE}/api/config/keywords`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) }).then(r => r.json()),
  postConfigKeywordsBulk: (p)    => request(`${BASE}/api/config/keywords/bulk`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) }).then(r => r.json()),
  putConfigKeyword:  (idx, p)    => request(`${BASE}/api/config/keywords/${idx}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) }).then(r => r.json()),
  deleteConfigKeyword: (idx)     => request(`${BASE}/api/config/keywords/${idx}`, { method: 'DELETE' }).then(r => r.json()),
}
