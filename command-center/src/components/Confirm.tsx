import { useEffect, useId, useRef, type ReactNode } from 'react'
import { Loader2 } from 'lucide-react'
import { cn } from '../lib/utils'

export function Confirm({ open, onOpenChange, title, description, onConfirm, busy, destructive, confirmDisabled, confirmLabel = 'Confirm', children }: {
  open: boolean; onOpenChange: (v: boolean) => void; title: string; description?: string;
  onConfirm: () => void; busy?: boolean; destructive?: boolean; confirmDisabled?: boolean;
  confirmLabel?: string; children?: ReactNode
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  const titleId = useId()
  const descriptionId = useId()
  useEffect(() => {
    if (!open || !dialog.current) return
    const element = dialog.current
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    element.showModal()
    cancel.current?.focus()
    return () => {
      if (element.open) element.close()
      if (previous?.isConnected) previous.focus()
    }
  }, [open])
  return (
    <dialog ref={dialog} aria-labelledby={titleId} aria-describedby={description ? descriptionId : undefined} aria-modal="true"
      className="m-auto w-[calc(100%-2rem)] max-w-lg overflow-auto rounded-lg bg-transparent p-0 backdrop:bg-black/50"
      onKeyDown={e => {
        if (e.key !== 'Tab') return
        const focusable = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex="0"]')).filter(el => el.getClientRects().length > 0)
        const first = focusable[0], last = focusable[focusable.length - 1]
        if (!first || !last) { e.preventDefault(); return }
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
      }}
      onCancel={e => { e.preventDefault(); if (!busy) onOpenChange(false) }}
      onMouseDown={e => { if (e.target === e.currentTarget && !busy) onOpenChange(false) }}>
      <div className="card w-full p-5">
        <h2 id={titleId} className="text-lg font-semibold">{title}</h2>
        {description && <p id={descriptionId} className="muted mt-1">{description}</p>}
        {children && <div className="mt-4">{children}</div>}
        <div className="mt-5 flex justify-end gap-2">
          <button ref={cancel} className="btn" onClick={() => onOpenChange(false)} disabled={busy}>Cancel</button>
          <button className={cn('btn', destructive ? 'btn-danger' : 'btn-primary')} onClick={onConfirm} disabled={busy || confirmDisabled}>
            {busy && <Loader2 className="animate-spin" size={14} />} {confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  )
}
