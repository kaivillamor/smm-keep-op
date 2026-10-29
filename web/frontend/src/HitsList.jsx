/** Recent winning legs as a plain list. */
export default function HitsList({ hits, limit = 60 }) {
  if (!hits) return <p className="muted">Loading…</p>
  if (!hits.length)
    return <p className="muted">No graded hits yet — the list fills in once games finish.</p>

  const shown = hits.slice(0, limit)

  return (
    <>
      <p className="muted list-meta">
        showing {shown.length} of {hits.length}
      </p>
      <ul className="hits">
      {shown.map((h, i) => (
        <li key={`${h.date}-${h.name}-${i}`}>
          <span>
            <span className="hit-name">{h.name}</span>{' '}
            <span className="hit-meta">{h.team}</span>
          </span>
          <span className="hit-count">+{h.hits} {h.hits === 1 ? 'Hit' : 'Hits'}</span>
          <span className="hit-date">{h.date}</span>
        </li>
      ))}
      </ul>
    </>
  )
}
