import { useState } from 'react'
import { MARKET, Result, american } from './NbaSlate.jsx'

const money = (v) => (v == null ? '—' : `$${v.toFixed(2)}`)
const signed = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}$${Math.abs(v).toFixed(2)}`)
// The method only takes legs with a full 10- and 20-game history, so these are exact.
const frac = (rate, n) => (rate == null ? '—' : `${Math.round(rate * n)}/${n}`)

function status(p) {
  if (p.outcome === 'win') return { text: `won ${signed(p.profit)}`, tone: 'good' }
  if (p.outcome === 'loss') return { text: `lost ${signed(p.profit)}`, tone: 'bad' }
  if (p.outcome === 'void') return { text: 'void — stake returned', tone: 'dim' }
  const tip = p.first_tip ? Date.parse(p.first_tip) : NaN
  if (Number.isNaN(tip)) return { text: 'pending', tone: 'muted' }
  const at = new Date(tip).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  // Mirrors method.save(): rebuilt on every run until the first leg tips, then frozen.
  return Date.now() >= tip
    ? { text: `locked at first tip (${at}) — awaiting results`, tone: 'muted' }
    : { text: `can still change until first tip (${at})`, tone: 'muted' }
}

/**
 * The method's paper parlays, newest first — one per tier per slate, legs underneath.
 *
 * `latest` is the newest slate with any props. A slate with props but no parlay means
 * the method ran and nothing fit its targets, which is said rather than left blank: an
 * empty list is otherwise indistinguishable from a method that never ran.
 */
export default function MethodParlays({ data, latest }) {
  const [showAll, setShowAll] = useState(false)

  if (!data) return <p className="muted">Loading…</p>
  if (data.error) return <p className="bad">{data.error}</p>

  const parlays = showAll ? data.parlays : data.parlays.slice(0, 6)

  return (
    <>
      {latest && !data.parlays.some((p) => p.date === latest) && (
        <p className="muted">No method parlay fits the targets for {latest} yet.</p>
      )}

      {parlays.map((p) => {
        const s = status(p)
        return (
          <div key={`${p.date}-${p.tier}`} className="parlay">
            <p className="slate-meta">
              <strong>{p.date} · {p.tier}</strong>
              <span className="sep">·</span>
              {p.n_legs} legs
              <span className="sep">·</span>
              {american(p.american_odds)}
              <span className="sep">·</span>
              {money(p.profit_if_win)} profit on {money(p.stake)}
              <span className="sep">·</span>
              <span className={s.tone}>{s.text}</span>
            </p>
            <div className="scroll-x">
              <table className="narrow">
                <thead>
                  <tr>
                    <th>Player</th>
                    <th>Mkt</th>
                    <th className="n">Line</th>
                    <th>Rung</th>
                    <th className="n">Odds</th>
                    <th className="n">L10</th>
                    <th className="n">L20</th>
                    <th>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {p.legs.map((l, i) => (
                    <tr key={`${l.name}-${i}`}>
                      <td>
                        <span className="hit-name">{l.name}</span>{' '}
                        <span className="hit-meta">{l.team} vs {l.opp}</span>
                      </td>
                      <td>{MARKET[l.market] || l.market}</td>
                      <td className="n">{l.line}</td>
                      <td className="dim nowrap">
                        {l.undercut ? `${l.undercut} under ${l.main_line}` : 'main line'}
                      </td>
                      <td className="n">{american(l.odds)}</td>
                      <td className="n">{frac(l.l10, 10)}</td>
                      <td className="n">{frac(l.l20, 20)}</td>
                      <td className="nowrap"><Result outcome={l.outcome} actual={l.actual} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )
      })}

      {data.parlays.length > 6 && (
        <button className="link-btn" onClick={() => setShowAll((v) => !v)}>
          {showAll ? 'Show latest 6' : `Show all ${data.parlays.length}`}
        </button>
      )}
    </>
  )
}
