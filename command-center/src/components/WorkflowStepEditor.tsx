import { useState } from 'react'
import { api } from '../lib/api'

type Step = Record<string, unknown>
type Preview = { checksum: string; steps: Step[] }

function parseSteps(value: string): Step[] | null {
  try {
    const result: unknown = JSON.parse(value)
    return Array.isArray(result) && result.every(s => s && typeof s === 'object' && !Array.isArray(s)) ? result as Step[] : null
  } catch { return null }
}

export function WorkflowStepEditor({ value, onChange, requestType }: {
  value: string; onChange: (value: string) => void; requestType: string
}) {
  const steps = parseSteps(value)
  const [preview, setPreview] = useState<{ input: string; message: string; ok: boolean } | null>(null)
  const [busy, setBusy] = useState(false)
  const input = JSON.stringify([requestType, value])
  function save(next: Step[]) { onChange(JSON.stringify(next, null, 2)); setPreview(null) }
  function edit(index: number, patch: Step) {
    if (steps) save(steps.map((step, i) => i === index ? { ...step, ...patch } : step))
  }
  function append() {
    if (!steps) return
    const keys = new Set(steps.map(step => String(step.key)))
    let n = steps.length + 1
    while (keys.has('step_' + n)) n++
    save([...steps, { key: 'step_' + n, step: 'New step', queue: 'it-hardware', action: 'none', requires_approval: false, depends_on: [], join: 'all', when: [] }])
  }
  async function validate() {
    if (!steps) return
    setBusy(true)
    const result = await api<Preview>('POST', '/workflow-definitions/validate', { body: { request_type: requestType, steps } })
    setPreview({ input, ok: result.ok, message: result.ok ? 'DAG validated by FlowOps. No version was saved.' : result.error || 'Validation failed.' })
    setBusy(false)
  }
  return <div className="space-y-3">
    <p className="muted text-sm">Edit the guided fields or use advanced JSON for expressions and actions. The backend validates all dependencies before saving.</p>
    {steps === null ? <p role="alert">Enter a valid JSON array of step objects to enable guided editing.</p> : <>
      {steps.map((step, index) => <fieldset key={index} className="rounded border border-[hsl(var(--border))] p-3">
        <legend className="px-1 font-medium">Step {index + 1}</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          <label><span className="label">Key</span><input className="input" value={String(step.key ?? '')} onChange={e => edit(index, { key: e.target.value })}/></label>
          <label><span className="label">Name</span><input className="input" value={String(step.step ?? '')} onChange={e => edit(index, { step: e.target.value })}/></label>
          <label><span className="label">Queue</span><input className="input" value={String(step.queue ?? '')} onChange={e => edit(index, { queue: e.target.value })}/></label>
          <label><span className="label">Dependencies (comma-separated keys)</span><input className="input" value={Array.isArray(step.depends_on) ? step.depends_on.join(',') : ''} onChange={e => edit(index, { depends_on: e.target.value.split(',') })} onBlur={e => edit(index, { depends_on: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })}/></label>
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2"><label><input type="checkbox" checked={step.requires_approval === true} onChange={e => edit(index, { requires_approval: e.target.checked })}/> Requires approval</label><button type="button" className="btn" onClick={() => save(steps.filter((_, i) => i !== index))}>Remove step {index + 1}</button></div>
      </fieldset>)}
      <div className="flex gap-2"><button type="button" className="btn" onClick={append}>Add step</button><button type="button" className="btn" disabled={busy || !steps.length} onClick={validate}>Validate DAG</button></div>
    </>}
    {preview?.input === input && <p role={preview.ok ? 'status' : 'alert'}>{preview.message}</p>}
    <label><span className="label">Advanced steps JSON</span><textarea className="input min-h-[220px] font-mono text-xs" value={value} onChange={e => onChange(e.target.value)} spellCheck={false}/></label>
  </div>
}
