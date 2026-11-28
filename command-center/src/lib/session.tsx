import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

export type Role = 'admin' | 'operator' | 'viewer'
type Session = { role: Role; email: string; configured: boolean; loading: boolean; workforceDemo: boolean }
const denied: Session = { role: 'viewer', email: '', configured: false, loading: false, workforceDemo: false }
const Ctx = createContext<Session>({ ...denied, loading: true })
export const useSession = () => useContext(Ctx)

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session>({ ...denied, loading: true })
  useEffect(() => {
    let generation = 0
    let controller: AbortController | undefined
    async function verify() {
      controller?.abort()
      const current = ++generation
      controller = new AbortController()
      try {
        const response = await fetch('/api/session', { cache: 'no-store', signal: controller.signal })
        const s = await response.json()
        if (current !== generation) return
        if (!response.ok || s.configured !== true || typeof s.email !== 'string' || !s.email || !['admin', 'operator', 'viewer'].includes(s.role)) {
          setSession(denied)
          return
        }
        setSession({ role: s.role, email: s.email, configured: true, loading: false, workforceDemo: s.workforceDemo === true && s.role === 'admin' })
      } catch {
        if (current === generation) setSession(denied)
      }
    }
    function invalidate() {
      generation++
      controller?.abort()
      setSession(denied)
    }
    function onVisibility() {
      if (document.visibilityState === 'visible') void verify()
    }
    void verify()
    const timer = window.setInterval(onVisibility, 30000)
    window.addEventListener('flowops:unauthorized', invalidate)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      generation++
      controller?.abort()
      window.clearInterval(timer)
      window.removeEventListener('flowops:unauthorized', invalidate)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])
  return <Ctx.Provider value={session}>{children}</Ctx.Provider>
}
