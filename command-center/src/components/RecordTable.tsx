import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { cn } from '../lib/utils'

export type Column<T> = { key: string; header: string; render?: (row: T) => ReactNode; className?: string }
export function RecordTable<T extends Record<string, any>>({ rows, columns, rowHref, rowKey, empty }: {
  rows: T[]; columns: Column<T>[]; rowHref?: (row: T) => string; rowKey?: (row: T) => string | number; empty?: string
}) {
  const nav = useNavigate()
  if (!rows.length) return <div className="card p-6 text-center muted">{empty || 'No records.'}</div>
  const keyOf = (r: T, i: number) => rowKey ? rowKey(r) : (r.id ?? r.key ?? i)
  return (
    <>
      <div className="card hidden overflow-hidden md:block">
        <div className="table-wrap"><table>
          <thead><tr>{columns.map((c) => <th key={c.key} className={c.className}>{c.header}</th>)}</tr></thead>
          <tbody>{rows.map((r, i) => {
            const href = rowHref?.(r)
            return <tr key={keyOf(r, i)} className={cn('hover:bg-black/5 dark:hover:bg-white/5', href && 'cursor-pointer')}
              tabIndex={href ? 0 : undefined} role={href ? 'link' : undefined}
              onClick={() => href && nav(href)} onKeyDown={(e) => { if (href && e.key === 'Enter') nav(href) }}>
              {columns.map((c) => <td key={c.key} className={c.className}>{c.render ? c.render(r) : String(r[c.key] ?? '—')}</td>)}
            </tr>
          })}</tbody>
        </table></div>
      </div>
      <div className="space-y-2 md:hidden">{rows.map((r, i) => {
        const href = rowHref?.(r)
        return <div key={keyOf(r, i)} className={cn('card p-3', href && 'cursor-pointer')} tabIndex={href ? 0 : undefined}
          onClick={() => href && nav(href)} onKeyDown={(e) => { if (href && e.key === 'Enter') nav(href) }}>
          {columns.map((c) => <div key={c.key} className="flex justify-between gap-3 border-b border-[hsl(var(--border))] py-1 last:border-0">
            <span className="muted text-xs uppercase">{c.header}</span><span className="text-right">{c.render ? c.render(r) : String(r[c.key] ?? '—')}</span>
          </div>)}
        </div>
      })}</div>
    </>
  )
}
