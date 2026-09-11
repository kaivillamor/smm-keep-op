import { useEffect, useState } from 'react'
import HitsList from './HitsList.jsx'
import Login from './Login.jsx'

const pct = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
const pts = (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)} pts`)

// The ranking gap is the number that decides whether the model is bettable at all, so
// it gets an explicit verdict rather than leaving the reader to interpret a decimal.
function verdictFor(gap) {
  if (gap == null) return { label: 'not enough data', tone: 'neutral' }
  if (gap < 0.02) return { label: 'no usable ranking signal', tone: 'bad' }
  if (gap < 0.08) return { label: 'weak signal', tone: 'warn' }
  return { label: 'usable signal', tone: 'good' }
}

function Stat({ label, value, sub }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}

function Buckets({ buckets }) {
  if (!buckets?.length) return null
  return (
    <table>
      <thead>
        <tr><th>predicted range</th><th>n</th><th>predicted</th><th>actual</th><th>delta</th></tr>
      </thead>
      <tbody>
        {buckets.map((b) => {
          const delta = b.actual - b.predicted
          return (
            <tr key={b.range}>
              <td>{b.range}</td>
              <td>{b.n}</td>
              <td>{pct(b.predicted)}</td>
              <td>{pct(b.actual)}</td>
              <td className={delta >= 0 ? 'good' : 'bad'}>{pts(delta)}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function Days({ days }) {
  if (!days?.length) return null
  return (
    <table>
      <thead><tr><th>date</th><th>logged</th><th>graded</th></tr></thead>
      <tbody>
        {days.map((d) => (
          <tr key={d.date}>
            <td>{d.date}</td><td>{d.n}</td><td>{d.graded ?? 0}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function useJson(url) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    const load = () =>
      fetch(url)
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then(setData)
        .catch((e) => setError(e.message))
    load()
    // The scheduler collects at most hourly, so anything faster is wasted requests.
    const t = setInterval(load, 5 * 60 * 1000)
    return () => clearInterval(t)
  }, [url])
  return { data, error }
}

function SignOut() {
  const go = async () => {
    await fetch('/api/logout', { method: 'POST' })
    window.location.href = '/'
  }
  return <a href="/" onClick={(e) => { e.preventDefault(); go() }}>sign out</a>
}

function Banner() {
  return (
    <div className="note">
      <strong>Research only.</strong> Simulated predictions, no money staked. Real P&amp;L
      lives in <code>bets.db</code> on the local machine and is deliberately not deployed here.
    </div>
  )
}

function Home() {
  // /api/summary, not /api/report — report is admin-only now and a non-admin would 403.
  const { data: sum } = useJson('/api/summary')
  const { data: feed } = useJson('/api/hits')

  return (
    <main>
      <header className="bar">
        <h1>MLB Model Research</h1>
        <nav>
          {sum?.role === 'admin' && <a href="/admin">admin →</a>}
          <SignOut />
        </nav>
      </header>
      <Banner />


      {sum && sum.graded > 0 && (
        <div className="stats">
          <Stat label="legs graded" value={sum.graded.toLocaleString()} sub={`${sum.logged.toLocaleString()} logged`} />
          <Stat label="hit rate" value={pct(sum.actual)} />
          <Stat label="model says" value={pct(sum.predicted)} />
        </div>
      )}
      <h2>Recent hits</h2>
      <HitsList hits={feed?.hits} />

      <p className="muted">
        Collection is running. These are legs the model predicted that went on to hit —
        not parlays, and not real money.
      </p>
    </main>
  )
}

function Admin() {
  const { data, error } = useJson('/api/report')

  if (error?.includes('403'))
    return (
      <main>
        <h1>Admin</h1>
        <p className="bad">Your account does not have the admin role.</p>
        <p><a href="/">← back to dashboard</a></p>
      </main>
    )
  if (error) return <main><h1>Admin</h1><p className="bad">Failed to load: {error}</p></main>
  if (!data) return <main><h1>Admin</h1><p className="muted">Loading…</p></main>
  if (data.error) return <main><h1>Admin</h1><p className="bad">{data.error}</p></main>

  const v = verdictFor(data.gap)

  return (
    <main>
      <header className="bar">
        <h1>Admin · model calibration</h1>
        <nav>
          <a href="/">← dashboard</a>
          <SignOut />
        </nav>
      </header>
      <Banner />

      {data.graded === 0 ? (
        <p>{data.logged.toLocaleString()} logged, none graded yet — grading runs after games finish.</p>
      ) : (
        <>
          <div className="stats">
            <Stat label="graded" value={data.graded.toLocaleString()} sub={`${data.logged.toLocaleString()} logged`} />
            <Stat label="model says" value={pct(data.predicted)} />
            <Stat label="actually hit" value={pct(data.actual)} />
            <Stat label="overconfidence" value={pts(data.overconfidence)} />
          </div>

          <h2>Ranking signal</h2>
          <p>
            lower half <strong>{pct(data.lower)}</strong> &nbsp;·&nbsp;
            upper half <strong>{pct(data.upper)}</strong> &nbsp;·&nbsp;
            gap <strong className={v.tone}>{pts(data.gap)}</strong>
          </p>
          <p className={v.tone}>{v.label}</p>

          <h2>Calibration by bucket</h2>
          <Buckets buckets={data.buckets} />

          <h2>Recent days</h2>
          <Days days={data.by_day} />
        </>
      )}
    </main>
  )
}

export default function App() {
  const [me, setMe] = useState(undefined)          // undefined = still checking
  const { data: feed } = useJson('/api/hits')

  useEffect(() => {
    fetch('/api/me')
      .then((r) => r.json())
      .then((d) => setMe(d.user ? d : null))
      .catch(() => setMe(null))
  }, [])

  // Render nothing while the session check is in flight — flashing the login screen at
  // an already-signed-in user is worse than a brief blank.
  if (me === undefined) return null
  if (me === null) return <Login hits={feed?.hits} onSignedIn={setMe} />

  // Two routes do not justify a router dependency; the server serves index.html for both.
  return window.location.pathname.startsWith('/admin') ? <Admin /> : <Home />
}
