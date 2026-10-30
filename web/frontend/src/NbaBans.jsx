import { useEffect, useState } from 'react'

const playerLabel = (p) => `${p.name} · ${p.team}`
const teamLabel = (t) => `${t.name} · whole team`

/**
 * The method's ban list, admins only: pick any player with a logged prop, or any team, by
 * name; ban or unban. The collector reads the list at the start of each run.
 */
export default function NbaBans() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [pick, setPick] = useState('')
  const [why, setWhy] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    fetch('/api/nba/bans')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setData)
      .catch((e) => setError(e.message))
  }, [])

  // POST and DELETE both answer with the whole list.
  const send = async (method, url, body) => {
    setBusy(true)
    setError(null)
    try {
      const r = await fetch(url, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      })
      const d = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : `Error ${r.status}`)
      setData((old) => ({ ...old, bans: d.bans }))
      return true
    } catch (e) {
      setError(e.message)
      return false
    } finally {
      setBusy(false)
    }
  }

  if (!data) return error ? <p className="bad">{error}</p> : <p className="muted">Loading…</p>

  const taken = new Set(data.bans.map((b) => `${b.kind}:${b.espn_id}`))
  const choices = new Map()
  for (const t of data.teams)
    if (!taken.has(`team:${t.espn_id}`))
      choices.set(teamLabel(t), { kind: 'team', espn_id: t.espn_id })
  for (const p of data.players)
    if (!taken.has(`player:${p.espn_id}`))
      choices.set(playerLabel(p), { kind: 'player', espn_id: p.espn_id })

  const submit = async (e) => {
    e.preventDefault()
    const choice = choices.get(pick.trim())
    if (!choice) {
      setError('Pick a name from the list.')
      return
    }
    if (await send('POST', '/api/nba/bans', { ...choice, why })) {
      setPick('')
      setWhy('')
    }
  }

  return (
    <>
      <p className="muted">
        A banned player, and everyone on a banned team, is left out of the method's parlays
        from the next collection run (hourly, 21:00–01:00 UTC). A slip already locked at first
        tip keeps its legs. Their props are still logged.
      </p>
      {data.error && <p className="bad">{data.error}</p>}

      <form className="filters" onSubmit={submit}>
        <input
          list="nba-ban-choices"
          value={pick}
          onChange={(e) => setPick(e.target.value)}
          placeholder="Player or team"
          aria-label="Player or team to ban"
        />
        <datalist id="nba-ban-choices">
          {[...choices.keys()].map((label) => (
            <option key={label} value={label} />
          ))}
        </datalist>
        <input
          value={why}
          onChange={(e) => setWhy(e.target.value)}
          maxLength={200}
          placeholder="Reason (optional)"
          aria-label="Reason"
        />
        <button type="submit" className="link-btn" disabled={busy || !pick.trim()}>
          Ban
        </button>
      </form>
      {error && <p className="bad">{error}</p>}

      {data.bans.length === 0 ? (
        <p className="muted">Nobody is banned.</p>
      ) : (
        <div className="scroll-x">
          <table className="narrow">
            <thead>
              <tr>
                <th>Banned</th>
                <th></th>
                <th>Since</th>
                <th>Reason</th>
                <th>By</th>
              </tr>
            </thead>
            <tbody>
              {data.bans.map((b) => (
                <tr key={`${b.kind}-${b.espn_id}`}>
                  <td>
                    <span className="hit-name">{b.name}</span>{' '}
                    <span className="hit-meta">{b.kind === 'team' ? 'whole team' : b.team}</span>
                  </td>
                  {/* Beside the pinned name, so phones see it without scrolling. */}
                  <td>
                    <button
                      className="link-btn"
                      disabled={busy}
                      onClick={() => send('DELETE', `/api/nba/bans/${b.kind}/${b.espn_id}`)}
                    >
                      unban
                    </button>
                  </td>
                  <td className="nowrap">{b.added_at.slice(0, 10)}</td>
                  <td>{b.why || '—'}</td>
                  <td className="dim">{b.added_by}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
