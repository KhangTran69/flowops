import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

export type Role = 'admin' | 'operator' | 'viewer'
type Session = { role: Role; email: string; configured: boolean; loading: boolean }
const Ctx = createContext<Session>({ role: 'viewer', email: '', configured: false, loading: true })
export const useSession = () => useContext(Ctx)

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session>({ role: 'viewer', email: '', configured: false, loading: true })
  useEffect(() => {
    fetch('/api/session').then((r) => r.json()).then((s) => setSession({ role: ['admin', 'operator', 'viewer'].includes(s.role) ? s.role : 'viewer', email: s.email || '', configured: s.configured === true, loading: false })).catch(() => setSession((s) => ({ ...s, configured: false, loading: false })))
  }, [])
  return <Ctx.Provider value={session}>{children}</Ctx.Provider>
}
