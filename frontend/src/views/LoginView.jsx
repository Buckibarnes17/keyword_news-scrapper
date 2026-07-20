import React, { useState } from 'react'
import useAppStore from '../store/appStore'
import { api } from '../api/client'

export default function LoginView() {
  const { login, setAuthView } = useAppStore()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!email || !password) {
      setError('Email and password are required.')
      return
    }
    if (!/\S+@\S+\.\S+/.test(email)) {
      setError('Please enter a valid email address.')
      return
    }
    if (password.length < 6) {
      setError('Password must be at least 6 characters long.')
      return
    }

    setLoading(true)
    setError('')
    try {
      const res = await api.login({ email: email.trim(), password })
      // res is { access_token, refresh_token, token_type }
      await login(res.access_token, res.refresh_token, { email: email.trim() })
    } catch (err) {
      setError(err.message || 'Login failed. Please check your credentials.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      minHeight: '100vh',
      width: '100vw',
      background: 'var(--color-bg, #0b0f19)',
      padding: '20px'
    }}>
      <div className="content-card" style={{ maxWidth: '400px', width: '100%' }}>
        <div className="card-header border-bottom" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '8px', padding: '24px' }}>
          <div className="brand-logo" style={{ marginBottom: '8px' }}>
            <i className="fa-solid fa-newspaper brand-icon"></i>
          </div>
          <h2 style={{ margin: 0, fontSize: '1.5rem', color: 'var(--color-text, #ffffff)' }}>Welcome to KeywordScout</h2>
          <p style={{ margin: 0, fontSize: '0.8rem', color: 'var(--color-text-muted, #8b9bb4)' }}>Sign in to operate the scraper</p>
        </div>
        <div className="card-body" style={{ padding: '24px' }}>
          {error && (
            <div className="alert alert-error" style={{
              background: 'rgba(239, 68, 68, 0.08)',
              border: '1px solid rgba(239, 68, 68, 0.2)',
              color: '#ef4444',
              padding: '10px 14px',
              borderRadius: '8px',
              fontSize: '0.8rem',
              marginBottom: '16px',
              display: 'flex',
              alignItems: 'center',
              gap: '8px'
            }}>
              <i className="fa-solid fa-circle-exclamation"></i>
              <span>{error}</span>
            </div>
          )}

          <form onSubmit={handleSubmit} className="flex-col gap-3">
            <div className="form-group">
              <label htmlFor="login-email" className="form-label">Email Address</label>
              <div className="input-with-icon">
                <i className="fa-solid fa-envelope"></i>
                <input
                  type="email"
                  id="login-email"
                  className="form-control"
                  placeholder="operator@domain.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                />
              </div>
            </div>

            <div className="form-group">
              <label htmlFor="login-password" className="form-label">Password</label>
              <div className="input-with-icon">
                <i className="fa-solid fa-lock"></i>
                <input
                  type="password"
                  id="login-password"
                  className="form-control"
                  placeholder="••••••••"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </div>
            </div>

            <button
              type="submit"
              className="btn btn-primary w-full"
              style={{ marginTop: '12px' }}
              disabled={loading}
            >
              {loading ? (
                <>
                  <i className="fa-solid fa-circle-notch fa-spin"></i> Authenticating...
                </>
              ) : (
                <>
                  <i className="fa-solid fa-right-to-bracket"></i> Sign In
                </>
              )}
            </button>
          </form>

          <div style={{ marginTop: '24px', textAlign: 'center', fontSize: '0.8rem', color: 'var(--color-text-muted)' }}>
            Don't have an operator account?{' '}
            <button
              type="button"
              className="link-btn"
              onClick={() => setAuthView('signup')}
              style={{
                background: 'none',
                border: 'none',
                color: 'var(--accent-cyan, #00f0ff)',
                cursor: 'pointer',
                fontWeight: 600,
                padding: 0,
                textDecoration: 'underline'
              }}
            >
              Request Access (Sign Up)
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
