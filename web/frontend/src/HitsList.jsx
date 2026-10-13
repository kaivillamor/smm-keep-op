/**
 * Recent winning legs as a plain list.
 *
 * The dashboard deliberately does NOT use the falling animation — that belongs on the
 * sign-in screen where it is decoration. Here the same data is something you actually
 * read, so it gets a scannable list with the team and date the animation had to omit.
 */
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
          {/* Name and team share a cell, the same pattern the slate table uses, so the
              two views read as one system and the count keeps its own column. */}
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
