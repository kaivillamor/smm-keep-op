import { useState } from 'react'

const pct = (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
const american = (o) => (o == null ? '—' : o > 0 ? `+${o}` : `${o}`)

/**
 * Today's slate — the pool a parlay gets built from, ranked by the model.
 *
 * Shows BOTH probabilities on purpose. The raw model number is what research.db logs;
 * the calibrated one is what the betting gate uses, and they diverge sharply at the top
 * because the measured calibration slope is ~0.17. Presenting only the raw figure would
 * imply an edge the model has not demonstrated.
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

  const legs = showAll ? slate.legs : slate.legs.slice(0, 20)
  const clearing = slate.legs.filter((l) => l.clears_gate).length

  return (
    <>
      <p className="muted slate-meta">
        {slate.date} · {slate.count} legs scored ·{' '}
        {clearing > 0
          ? `${clearing} clear the ${pct(slate.threshold)} betting gate`
          : `none clear the ${pct(slate.threshold)} betting gate`}
      </p>

      <table className="slate">
        <thead>
          <tr>
            <th>batter</th><th>vs</th><th>spot</th>
            <th>model</th><th>calibrated</th><th>odds</th>
          </tr>
        </thead>
        <tbody>
          {legs.map((l, i) => (
            <tr key={`${l.name}-${i}`} className={l.clears_gate ? 'clears' : undefined}>
              <td>
                <span className="hit-name">{l.name}</span>
                <span className="hit-meta"> {l.team}</span>
              </td>
              <td className="hit-meta">{l.pitcher || '—'}</td>
              <td>{l.spot ?? '—'}</td>
              <td>{pct(l.model_prob)}</td>
              <td className={l.clears_gate ? 'good' : 'muted'}>{pct(l.calibrated)}</td>
              <td>{american(l.odds)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {slate.legs.length > 20 && (
        <button className="link-btn" onClick={() => setShowAll((v) => !v)}>
          {showAll ? 'Show top 20' : `Show all ${slate.legs.length}`}
        </button>
      )}
    </>
  )
}
