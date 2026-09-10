import { useState } from 'react'
import FallingHits from './FallingHits.jsx'

/**
 * Centred sign-in over the ambient feed.
 *
 * The feed runs on the PUBLIC /api/hits, so the background is alive before anyone signs
 * in. That endpoint withholds the model's predicted probability while signed out —
 * player names and hit counts are public box-score facts, the model's output is not.
 */
export default function Login({ hits, onSignedIn }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const r = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      if (!r.ok) {
        setError(r.status === 401 ? 'Incorrect username or password' : `Error ${r.status}`)
        setPassword('')
        return
      }
      onSignedIn(await r.json())
    } catch {
      setError('Could not reach the server')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-screen">
      <div className="login-bg">
        <FallingHits hits={hits} />
      </div>

      <form className="login-card" onSubmit={submit}>
        <h1>MLB Model Research</h1>
        <p className="muted">Sign in to continue</p>

        <label>
          Username
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        {/* One message for both failure modes — saying which half was wrong would
            confirm whether a username exists. */}
        {error && <p className="bad login-error">{error}</p>}

        <button type="submit" disabled={busy || !username || !password}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
