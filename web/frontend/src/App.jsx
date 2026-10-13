import { useEffect, useState } from 'react'
import HitsList from './HitsList.jsx'
import Login from './Login.jsx'
import Slate from './Slate.jsx'

const pct = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
const pts = (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)} pts`)
const num = (v) => (v == null ? '—' : v.toLocaleString())

// The ranking gap is the number that decides whether the model is bettable at all, so
// it gets an explicit verdict rather than leaving the reader to interpret a decimal.

// A gap this size, ESTABLISHED, is what "usable" means.
const USABLE_GAP = 0.08

function verdictFor(gap, ciLow, ciHigh) {
  if (gap == null) return { label: 'not enough data', tone: 'neutral' }
  // The interval decides, not the point estimate. Thresholds alone read noise as a
  // finding: +6.9pt on 696 rows carries a CI of [-0.2, +14.0] and means nothing yet.
  if (ciLow == null || ciHigh == null) return { label: `${(gap * 100).toFixed(1)} pts`, tone: 'neutral' }
  if (ciHigh < 0) return { label: 'inverted — ranks worse than chance', tone: 'bad' }
  if (ciLow <= 0) return { label: 'inconclusive — interval still contains zero', tone: 'warn' }
  // Compare the LOWER BOUND, not the point estimate. This line used to read
  // `gap < USABLE_GAP`, which is the very mistake the comment above warns about — and it
  // misfired on 2026-09-30: tripling the sample pulled the point estimate from +8.9 to
  // +7.4 while the interval TIGHTENED (width 10.5 -> 6.6) and its lower bound ROSE
  // (+3.6 -> +4.1). Strictly better evidence, and the badge downgraded itself, which read
  // as the model decaying. Testing ciLow means the badge only ever claims what the data
  // establishes, and more data can no longer make the verdict worse on its own.
  if (ciLow < USABLE_GAP) return { label: 'weak but real signal', tone: 'warn' }
  return { label: 'usable signal', tone: 'good' }
}

/**
 * The one number a view leads with. Exactly one per page — a second "hero" is just
 * two medium numbers. Proportional figures on purpose: tabular digits are for
 * columns, and at this size they read loose.
 */
function Hero({ label, value, tone, note }) {
  return (
    <div className="hero">
      <div className="hero-label">{label}</div>
      <div className={`hero-value ${tone || ''}`}>{value}</div>
      {note && <div className="hero-note">{note}</div>}
    </div>
  )
}

/** A headline figure in the divided KPI band. Label is sentence case, no colon. */
function Kpi({ label, value, sub }) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  )
}

function Buckets({ buckets }) {
  if (!buckets?.length) return null
  return (
    <table className="narrow">
      <thead>
        <tr>
          <th>Predicted range</th>
          <th className="n">N</th>
          <th className="n">Predicted</th>
          <th className="n">Actual</th>
          <th className="n">Delta</th>
        </tr>
      </thead>
      <tbody>
        {buckets.map((b) => {
          const delta = b.actual - b.predicted
          return (
            <tr key={b.range}>
              <td>{b.range}</td>
              <td className="n">{num(b.n)}</td>
              <td className="n">{pct(b.predicted)}</td>
              <td className="n">{pct(b.actual)}</td>
              {/* Signed, so direction never rests on hue alone. */}
              <td className={`n ${delta >= 0 ? 'good' : 'bad'}`}>{pts(delta)}</td>
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
    <table className="narrow">
      <thead>
        <tr><th>Date</th><th className="n">Logged</th><th className="n">Graded</th></tr>
      </thead>
      <tbody>
        {days.map((d) => (
          <tr key={d.date}>
            <td>{d.date}</td>
            <td className="n">{num(d.n)}</td>
            <td className="n">{num(d.graded ?? 0)}</td>
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
    <p className="note">
      <strong>Research only.</strong> Simulated predictions, no money staked. Real P&amp;L
      lives in <code>bets.db</code> on the local machine and is deliberately not deployed here.
    </p>
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

      {/* No KPI band here, deliberately. /api/summary returns four numbers and the hero
          plus its note already state all four — a band of "legs graded / hit rate /
          model says" was three restatements of what sits directly above it. Admin keeps
          its band because six of those figures appear nowhere else on the page. */}
      {sum && sum.graded > 0 && (
        <Hero
          label="Hit rate, graded legs"
          value={pct(sum.actual)}
          note={
            <>
              model said {pct(sum.predicted)} over {num(sum.graded)} graded legs
              <span className="dim"> · {num(sum.logged)} logged</span>
            </>
          }
        />
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
  // Fetched here rather than in App(): the slate is a research view, and App() was
  // requesting it on every page load without ever rendering it.
  const { data: slate } = useJson('/api/slate')

  if (error?.includes('403'))
    return (
      <main>
        <header className="bar"><h1>Admin</h1><nav><a href="/">← dashboard</a></nav></header>
        <p className="bad">Your account does not have the admin role.</p>
      </main>
    )
  if (error) return <main><h1>Admin</h1><p className="bad">Failed to load: {error}</p></main>
  if (!data) return <main><h1>Admin</h1><p className="muted">Loading…</p></main>
  if (data.error) return <main><h1>Admin</h1><p className="bad">{data.error}</p></main>

  const v = verdictFor(data.gap, data.gap_ci_low, data.gap_ci_high)

  return (
    <main>
      <header className="bar">
        <h1>Admin · Model calibration</h1>
        <nav>
          <a href="/">← dashboard</a>
          <SignOut />
        </nav>
      </header>
      <Banner />

      {data.graded === 0 ? (
        <p>{num(data.logged)} logged, none graded yet — grading runs after games finish.</p>
      ) : (
        <>
          {/* The gap is the number this page exists to report, so it leads. */}
          <Hero
            label="Ranking gap"
            value={pts(data.gap)}
            tone={v.tone}
            note={
              <>
                <span className={`verdict ${v.tone}`}>{v.label}</span>
                {data.gap_ci_low != null && (
                  <span className="ci">
                    {' '}· 95% CI [{pts(data.gap_ci_low)}, {pts(data.gap_ci_high)}]
                  </span>
                )}
              </>
            }
          />

          <div className="kpis">
            <Kpi label="Graded" value={num(data.graded)} sub={`${num(data.logged)} logged`} />
            <Kpi label="Model says" value={pct(data.predicted)} />
            <Kpi label="Actually hit" value={pct(data.actual)} />
            <Kpi label="Overconfidence" value={pts(data.overconfidence)} />
            <Kpi label="Lower half" value={pct(data.lower)} sub="by model probability" />
            <Kpi label="Upper half" value={pct(data.upper)} sub="by model probability" />
          </div>

          <h2>Calibration by bucket</h2>
          <Buckets buckets={data.buckets} />

          <h2>Today's slate</h2>
          <Slate slate={slate} />

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
