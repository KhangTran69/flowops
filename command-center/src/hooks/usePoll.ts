import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'

export function usePoll<T = any>(path: string, query?: Record<string, unknown>, intervalMs = 12000) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState(0)
  const [loading, setLoading] = useState(true)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const lastGood = useRef<T | null>(null)
  const inFlight = useRef(false)

  const reload = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const r = await api<T>('GET', path, { query })
      setStatus(r.status)
      if (r.ok) { setData(r.data ?? null); lastGood.current = r.data ?? null; setError(null); setUpdatedAt(new Date()) }
      else { setError(r.error || 'Request failed.'); setData(lastGood.current) }
    } finally { inFlight.current = false; setLoading(false) }
  }, [path, JSON.stringify(query || {})])

  useEffect(() => {
    setLoading(true); setData(null); lastGood.current = null; setError(null)
    reload()
    const tick = () => { if (document.visibilityState === 'visible') reload() }
    const id = window.setInterval(tick, intervalMs)
    const onVis = () => { if (document.visibilityState === 'visible') reload() }
    document.addEventListener('visibilitychange', onVis)
    return () => { window.clearInterval(id); document.removeEventListener('visibilitychange', onVis) }
  }, [reload, intervalMs])

  return { data, error, status, loading, updatedAt, reload }
}
