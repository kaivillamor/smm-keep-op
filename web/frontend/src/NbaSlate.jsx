import { useState } from 'react'

export const MARKET = {
  player_points: 'PTS',
  player_rebounds: 'REB',
  player_assists: 'AST',
  player_made_threes: '3PM',
}

const pct = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
const pct0 = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`)
const pts = (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}`)
export const american = (o) => (o == null ? '—' : o > 0 ? `+${o}` : `${o}`)

/** A prop's result, worded so it never rests on colour alone. */
export function Result({ outcome, actual }) {
  if (outcome === 'win') return <span className="good">over · {actual}</span>
  if (outcome === 'loss') return <span className="bad">under · {actual}</span>
  if (outcome === 'void') return <span className="dim">void</span>
  return <span className="dim">—</span>
}

/**
 * Every prop logged for one slate, ranked by model probability. Quant and Rules columns
 * appear when an owner rule fired; otherwise one line says none did.
 */
export default function NbaSlate({ slate, slates, onPick }) {
  const [market, setMarket] = useState('')
  const [showAll, setShowAll] = useState(false)

  if (!slate) return <p className="muted">Loading…</p>
  if (slate.error) return <p className="bad">{slate.error}</p>
  if (!slate.props?.length)
    return (
      <p className="muted">
        No props logged yet — collection runs hourly from 21:00 UTC (5pm ET).
      </p>
    )

  const rows = market ? slate.props.filter((p) => p.market === market) : slate.props
  const shown = showAll ? rows : rows.slice(0, 20)
  const rulesFired = slate.props.some((p) => p.rules)
  const graded = slate.props.filter((p) => p.outcome === 'win' || p.outcome === 'loss').length
  // The server ranks players listed out last.
  const out = slate.props.filter((p) => p.injury === 'out').length

  return (
    <>
      <div className="filters">
        {slates?.length > 0 && (
          <select value={slate.date} onChange={(e) => onPick(e.target.value)} aria-label="Slate">
            {slates.map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
        )}
        {['', ...Object.keys(MARKET)].map((m) => (
          <button
            key={m || 'all'}
            className={`link-btn${market === m ? ' on' : ''}`}
            onClick={() => setMarket(m)}
          >
            {m ? MARKET[m] : 'All'}
          </button>
        ))}
      </div>

      <p className="slate-meta">
        {slate.count} props
        <span className="sep">·</span>
        {graded} graded
        <span className="sep">·</span>
        <span className="warn">no calibration fitted — model probabilities are raw</span>
        {out > 0 && (
          <>
            <span className="sep">·</span>
            {out} listed out, ranked last
          </>
        )}
        {!rulesFired && (
          <>
            <span className="sep">·</span>
            no personal rule fired, so Model is the quant number alone
          </>
        )}
      </p>

      <div className="scroll-x">
        <table className="slate">
          <thead>
            <tr>
              <th>Player</th>
              <th>Opp</th>
              <th>Mkt</th>
              <th className="n">Line</th>
              <th className="n">Odds</th>
              <th className="n">Implied</th>
              <th className="n">Model</th>
              {rulesFired && <th className="n">Quant</th>}
              {rulesFired && <th className="n">Rules</th>}
              <th className="n">L10</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((p, i) => (
              <tr key={`${p.name}-${p.market}-${i}`}>
                <td>
                  <span className="hit-name">{p.name}</span>{' '}
                  <span className="hit-meta">{p.team}</span>
                  {p.injury && p.injury !== 'active' && <span className="warn"> {p.injury}</span>}
                </td>
                <td className="hit-meta">{p.opp}</td>
                <td>{MARKET[p.market] || p.market}</td>
                <td className="n">{p.line ?? '—'}</td>
                <td className="n">{american(p.odds)}</td>
                <td className="n dim">{pct(p.implied)}</td>
                <td className="n">{pct(p.model)}</td>
                {rulesFired && <td className="n dim">{pct(p.quant)}</td>}
                {rulesFired && <td className="n">{p.rules ? pts(p.rules) : '—'}</td>}
                {/* Share of the last 10 games over THIS line — fewer than 10 early in a season. */}
                <td className="n">{pct0(p.l10)}</td>
                <td className="nowrap"><Result outcome={p.outcome} actual={p.actual} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {rows.length > 20 && (
        <button className="link-btn" onClick={() => setShowAll((v) => !v)}>
          {showAll ? 'Show top 20' : `Show all ${rows.length}`}
        </button>
      )}
    </>
  )
}
