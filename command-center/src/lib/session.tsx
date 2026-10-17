import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

export type Role = 'admin' | 'operator' | 'viewer'
type Session = { role: Role; email: string; configured: boolean }
const Ctx = createContext<Session>({ role: 'operator', email: '', configured: true })
export const useSession = () => useContext(Ctx)

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session>({ role: 'operator', email: '', configured: true })
  useEffect(() => {
    fetch('/api/session').then((r) => r.json()).then((s) => setSession({ role: s.role || 'operator', email: s.email || '', configured: s.configured !== false })).catch(() => setSession((s) => ({ ...s, configured: false })))
  }, [])
  return <Ctx.Provider value={session}>{children}</Ctx.Provider>
}
