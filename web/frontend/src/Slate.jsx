import { useState } from 'react'

const pct = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
const pts = (v) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}`)
const american = (o) => (o == null ? '—' : o > 0 ? `+${o}` : `${o}`)

/**
 * Today's MLB slate ranked by edge: raw and calibrated probability, and the server's
 * gate verdict per leg.
 */
export default function Slate({ slate }) {
  const [showAll, setShowAll] = useState(false)

  if (!slate) return <p className="muted">Loading…</p>
  if (slate.error) return <p className="bad">{slate.error}</p>
  if (!slate.legs?.length)
    return (
      <p className="muted">
        No ungraded predictions right now — the slate fills in as lineups confirm
        (roughly 3 hours before first pitch).
      </p>
    )

  const gate = slate.gate || {}
  const legs = showAll ? slate.legs : slate.legs.slice(0, 20)

  return (
    <>
      <p className="slate-meta">
        {slate.date}
        <span className="sep">·</span>
        {slate.count} legs scored
        <span className="sep">·</span>
        {gate.clearing ?? 0} clear a {pct(gate.min_edge)} edge
        {gate.live === false && (
          <>
            <span className="sep">·</span>
            <span className="warn">gate not live — awaiting out-of-sample validation</span>
          </>
        )}
      </p>

      <div className="scroll-x">
        <table className="slate">
          <thead>
            <tr>
              <th>Batter</th>
              <th>Vs</th>
              <th className="n">Spot</th>
              <th className="n">Model</th>
              <th className="n">Calibrated</th>
              <th className="n">Odds</th>
              <th className="n">Implied</th>
              <th className="n">Edge</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {legs.map((l, i) => (
              <tr key={`${l.name}-${i}`} className={l.clears_gate ? 'clears' : undefined}>
                <td>
                  <span className="hit-name">{l.name}</span>{' '}
                  <span className="hit-meta">{l.team}</span>
                </td>
                <td className="hit-meta">{l.pitcher || '—'}</td>
                <td className="n">{l.spot ?? '—'}</td>
                <td className="n">{pct(l.model_prob)}</td>
                <td className="n">{pct(l.calibrated)}</td>
                <td className="n">{american(l.odds)}</td>
                <td className="n dim">{pct(l.book_implied)}</td>
                {/* Signed number, so direction is legible without the colour. */}
                <td className={`n ${l.edge == null ? 'dim' : l.edge >= 0 ? 'good' : 'bad'}`}>
                  {pts(l.edge)}
                </td>
                <td className={l.clears_gate ? 'good' : 'blocked'}>
                  {l.clears_gate ? 'clears' : l.blocked_by || '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {slate.legs.length > 20 && (
        <button className="link-btn" onClick={() => setShowAll((v) => !v)}>
          {showAll ? 'Show top 20' : `Show all ${slate.legs.length}`}
        </button>
      )}
    </>
  )
}
