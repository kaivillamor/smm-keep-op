import { useEffect, useRef, useState } from 'react'

/**
 * Ambient feed: winning legs drift down the screen and fade out. Each item is removed
 * when its animation ends, so the DOM never accumulates.
 */
export default function FallingHits({ hits }) {
  const [items, setItems] = useState([])
  const idx = useRef(0)
  const seq = useRef(0)

  useEffect(() => {
    if (!hits?.length) return
    const spawn = () => {
      const hit = hits[idx.current % hits.length]
      idx.current += 1
      const id = seq.current++
      setItems((cur) => [...cur, {
        id,
        hit,
        lane: 8 + Math.random() * 84,        // vh-relative horizontal position, %
        dur: 7 + Math.random() * 5,          // seconds
        drift: (Math.random() - 0.5) * 40,   // px of horizontal wander
        scale: 0.85 + Math.random() * 0.3,
      }])
      // Self-cleanup: matches the longest possible duration plus a margin.
      setTimeout(() => setItems((cur) => cur.filter((i) => i.id !== id)), 13000)
    }
    spawn()
    const t = setInterval(spawn, 1400)
    return () => clearInterval(t)
  }, [hits])

  if (!hits?.length) {
    return (
      <div className="feed feed-empty">
        <p className="muted">No graded hits yet — the feed fills in once games finish.</p>
      </div>
    )
  }

  return (
    <div className="feed" aria-hidden="true">
      {items.map(({ id, hit, lane, dur, drift, scale }) => (
        <span
          key={id}
          className="drop"
          style={{
            left: `${lane}%`,
            animationDuration: `${dur}s`,
            transform: `scale(${scale})`,
            '--drift': `${drift}px`,
          }}
        >
          <b>{hit.name}</b> <em>+{hit.hits} {hit.hits === 1 ? 'Hit' : 'Hits'}</em>
        </span>
      ))}
    </div>
  )
}
