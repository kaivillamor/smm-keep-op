import { useState } from 'react'
import { MARKET, Result, american } from './NbaSlate.jsx'

const money = (v) => (v == null ? '—' : `$${v.toFixed(2)}`)
const signed = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '−'}$${Math.abs(v).toFixed(2)}`)
// The method only takes legs with a full 10- and 20-game history, so these are exact.
const frac = (rate, n) => (rate == null ? '—' : `${Math.round(rate * n)}/${n}`)
// Games over the line out of games counted. Legs stored before 10-29 have no counts.
const count = (hits, n) => (hits == null || !n ? '—' : `${hits}/${n}`)

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
 * The method's paper parlays, one slate at a time, newest first. `latest` is the newest
 * slate with props: one with props but no parlay says nothing fit.
 */
export default function MethodParlays({ data, latest }) {
  const [day, setDay] = useState('')          // '' = the newest date

  if (!data) return <p className="muted">Loading…</p>
  if (data.error) return <p className="bad">{data.error}</p>

  // The API sends date DESC, so first-seen order is newest first.
  const dates = [...new Set(data.parlays.map((p) => p.date))]
  if (latest && !dates.includes(latest)) dates.unshift(latest)
  if (!dates.length) return null
  const shown = dates.includes(day) ? day : dates[0]
  const i = dates.indexOf(shown)
  const parlays = data.parlays.filter((p) => p.date === shown)

  return (
    <>
      <div className="filters">
        <button className="link-btn" disabled={i >= dates.length - 1}
                onClick={() => setDay(dates[i + 1])}>
          ← older
        </button>
        <select value={shown} onChange={(e) => setDay(e.target.value)} aria-label="Parlay date">
          {dates.map((d) => (
            <option key={d} value={d}>{d}</option>
          ))}
        </select>
        <button className="link-btn" disabled={i === 0} onClick={() => setDay(dates[i - 1])}>
          newer →
        </button>
      </div>

      {!parlays.length && (
        <p className="muted">No method parlay fits the targets for {shown} yet.</p>
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
                    <th className="n">H2H</th>
                    <th className="n">Sim D</th>
                    <th className="n">Pos D</th>
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
                      <td className="n">{count(l.h2h_hits, l.h2h_games)}</td>
                      <td className="n">{count(l.sim_hits, l.sim_games)}</td>
                      <td className="n nowrap">
                        {l.pos_rank ? `#${l.pos_rank} vs ${l.position}` : '—'}
                      </td>
                      <td className="nowrap"><Result outcome={l.outcome} actual={l.actual} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )
      })}
    </>
  )
}
