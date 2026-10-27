import { useEffect, useState } from 'react'
import HitsList from './HitsList.jsx'
import Login from './Login.jsx'
import MethodParlays from './MethodParlays.jsx'
import NbaSlate, { MARKET } from './NbaSlate.jsx'
import Slate from './Slate.jsx'

const pct = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
const pts = (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)} pts`)
const num = (v) => (v == null ? '—' : v.toLocaleString())
const signed = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}$${Math.abs(v).toFixed(2)}`)

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

function Buckets({ buckets, label = 'Predicted range' }) {
  if (!buckets?.length) return null
  return (
    <table className="narrow">
      <thead>
        <tr>
          <th>{label}</th>
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

/**
 * Collection AND grading per slate. Rendered even when nothing is graded — that empty
 * column is the signal. NBA's grading settled zero rows for six days (2026-10-20..26)
 * while collection looked healthy, and no screen said so.
 */
function Health({ rows }) {
  if (!rows?.length) return <p className="muted">Nothing collected yet.</p>
  return (
    <table className="narrow">
      <thead>
        <tr>
          <th>Slate</th>
          <th className="n">Logged</th>
          <th className="n">Scored</th>
          <th className="n">Priced</th>
          <th className="n">Graded</th>
          <th className="n">Void</th>
          <th className="n">Pending</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((d) => (
          <tr key={d.date}>
            <td>{d.date}</td>
            <td className="n">{num(d.logged)}</td>
            <td className="n">{num(d.scored)}</td>
            <td className="n">{num(d.priced)}</td>
            <td className="n">{num(d.graded)}</td>
            <td className="n dim">{num(d.void)}</td>
            {/* Worded as well as coloured, so a stuck slate reads without the hue. */}
            <td className={`n${d.stale ? ' warn' : ''}`}>
              {num(d.pending)}{d.stale ? ' · stuck' : ''}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/**
 * Ranking gap, headline figures and calibration buckets: the part of an admin page that
 * is identical for every sport, because each sport's report() computes it the same way
 * from model_prob and outcome.
 */
function Calibration({ data }) {
  const v = verdictFor(data.gap, data.gap_ci_low, data.gap_ci_high)
  return (
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
    </>
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
          <a href="/nba">NBA</a>
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

  return (
    <main>
      <header className="bar">
        <h1>Admin · Model calibration</h1>
        <nav>
          <a href="/">← dashboard</a>
          <a href="/nba/admin">NBA</a>
          <SignOut />
        </nav>
      </header>
      <Banner />

      {data.graded === 0 ? (
        <p>{num(data.logged)} logged, none graded yet — grading runs after games finish.</p>
      ) : (
        <>
          <Calibration data={data} />

          <h2>Today's slate</h2>
          <Slate slate={slate} />

          <h2>Recent days</h2>
          <Days days={data.by_day} />
        </>
      )}
    </main>
  )
}

function NbaHome() {
  const [day, setDay] = useState('')          // '' = follow the latest slate
  const { data: sum } = useJson('/api/nba/summary')
  const { data: parlays } = useJson('/api/nba/parlays')
  const { data: slate } = useJson(day ? `/api/nba/slate?date=${day}` : '/api/nba/slate')

  return (
    <main>
      <header className="bar">
        <h1>NBA Model Research</h1>
        <nav>
          <a href="/">MLB</a>
          {sum?.role === 'admin' && <a href="/nba/admin">admin →</a>}
          <SignOut />
        </nav>
      </header>
      <Banner />

      {sum?.error && <p className="bad">{sum.error}</p>}
      {sum && !sum.error && (sum.graded > 0 ? (
        <Hero
          label="Over rate, graded props"
          value={pct(sum.actual)}
          note={
            <>
              model said {pct(sum.predicted)} over {num(sum.graded)} graded props
              <span className="dim"> · {num(sum.logged)} logged</span>
            </>
          }
        />
      ) : (
        <p>
          {num(sum.logged)} props logged, none graded yet — grading runs at 11:00 UTC, once
          every game on the slate is final.
        </p>
      ))}
      {/* Silence is a value: a grader that settles nothing must say so on the page. */}
      {sum?.stale_pending > 0 && (
        <p className="warn">
          {num(sum.stale_pending)} props more than a day old are still ungraded (oldest{' '}
          {sum.stale_since}) — grading looks stuck.
        </p>
      )}

      <h2>Method parlays</h2>
      <p className="muted">
        The written-down parlay method, tracked on paper: rebuilt each run until its first leg
        tips, then frozen and graded from box scores. No money staked.
      </p>
      {parlays?.record?.length > 0 ? (
        <div className="kpis">
          {parlays.record.map((r) => (
            <Kpi
              key={r.tier}
              label={`${r.tier} parlays`}
              value={`${r.won}–${r.lost}`}
              sub={`${signed(r.profit)} on $${r.staked}${r.void ? ` · ${r.void} void` : ''}`}
            />
          ))}
        </div>
      ) : (
        parlays && !parlays.error && <p className="muted">No method parlay has settled yet.</p>
      )}
      <MethodParlays data={parlays} latest={sum?.slates?.[0]} />

      <h2>Props</h2>
      <NbaSlate slate={slate} slates={sum?.slates} onPick={setDay} />
    </main>
  )
}

function NbaAdmin() {
  const { data, error } = useJson('/api/nba/report')

  const bar = (
    <header className="bar">
      <h1>NBA admin · Model calibration</h1>
      <nav>
        <a href="/nba">← dashboard</a>
        <a href="/admin">MLB</a>
        <SignOut />
      </nav>
    </header>
  )
  if (error?.includes('403'))
    return <main>{bar}<p className="bad">Your account does not have the admin role.</p></main>
  if (error) return <main>{bar}<p className="bad">Failed to load: {error}</p></main>
  if (!data) return <main>{bar}<p className="muted">Loading…</p></main>
  if (data.error) return <main>{bar}<p className="bad">{data.error}</p></main>

  return (
    <main>
      {bar}
      <Banner />

      {data.graded === 0 ? (
        <p>
          {num(data.logged)} logged, none graded yet — grading runs at 11:00 UTC, once every
          game on the slate is final.
        </p>
      ) : (
        <>
          <Calibration data={data} />

          <h2>By market</h2>
          <Buckets
            label="Market"
            buckets={data.by_market?.map((m) => ({ ...m, range: MARKET[m.market] || m.market }))}
          />
        </>
      )}

      <h2>Collection and grading by slate</h2>
      <Health rows={data.health} />
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

  // Two sports x two views still do not justify a router dependency; the server serves
  // index.html for all four paths.
  const path = window.location.pathname
  if (path.startsWith('/nba/admin')) return <NbaAdmin />
  if (path.startsWith('/nba')) return <NbaHome />
  return path.startsWith('/admin') ? <Admin /> : <Home />
}
