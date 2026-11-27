import type { ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { cn } from '../lib/utils'

export type Column<T> = { key: string; header: string; render?: (row: T) => ReactNode; className?: string }
export function RecordTable<T extends object>({ rows, columns, rowHref, rowKey, empty }: {
  rows: T[]; columns: Column<T>[]; rowHref?: (row: T) => string; rowKey?: (row: T) => string | number; empty?: string
}) {
  const nav = useNavigate()
  if (!rows.length) return <div className="card p-6 text-center muted">{empty || 'No records.'}</div>
  const field = (r: T, key: string): unknown => (r as Record<string, unknown>)[key]
  const keyOf = (r: T, i: number) => rowKey ? rowKey(r) : String(field(r, 'id') ?? field(r, 'key') ?? i)
  const cell = (r: T, c: Column<T>, i: number, href?: string) => {
    const content = c.render ? c.render(r) : String(field(r, c.key) ?? '—')
    return href && i === 0 ? <Link to={href} className="underline decoration-dotted underline-offset-2">{content}</Link> : content
  }
  const navigate = (e: React.MouseEvent, href?: string) => {
    if (href && !(e.target instanceof Element && e.target.closest('a,button,input,select,textarea'))) nav(href)
  }
  return (
    <>
      <div className="card hidden overflow-hidden md:block">
        <div className="table-wrap"><table>
          <thead><tr>{columns.map((c) => <th scope="col" key={c.key} className={c.className}>{c.header}</th>)}</tr></thead>
          <tbody>{rows.map((r, i) => {
            const href = rowHref?.(r)
            return <tr key={keyOf(r, i)} className={cn('hover:bg-black/5 dark:hover:bg-white/5', href && 'cursor-pointer')}
              onClick={e => navigate(e, href)}>
              {columns.map((c, j) => <td key={c.key} className={c.className}>{cell(r, c, j, href)}</td>)}
            </tr>
          })}</tbody>
        </table></div>
      </div>
      <div className="space-y-2 md:hidden">{rows.map((r, i) => {
        const href = rowHref?.(r)
        return <div key={keyOf(r, i)} className={cn('card p-3', href && 'cursor-pointer')} onClick={e => navigate(e, href)}>
          {columns.map((c, j) => <div key={c.key} className="flex justify-between gap-3 border-b border-[hsl(var(--border))] py-1 last:border-0">
            <span className="muted text-xs uppercase">{c.header}</span><span className="text-right">{cell(r, c, j, href)}</span>
          </div>)}
        </div>
      })}</div>
    </>
  )
}
