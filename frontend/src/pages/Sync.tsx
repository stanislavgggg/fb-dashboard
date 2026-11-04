import { useEffect, useState } from 'react'
import { api } from '../api'
import { useMe } from '../App'
import { ErrorBox, Spinner, useLoad, useToast } from '../components/ui'
import { dateTime } from '../format'
import type { Alert, Health, SyncRun } from '../types'

const LEVEL = {
  ok: { cls: 'ok', label: 'OK' }, warn: { cls: 'warn', label: 'Warning' }, fail: { cls: 'bad', label: 'Problem' },
} as const

const TRIGGERS: Record<string, string> = {
  schedule: 'Scheduled', startup: 'On startup', manual: 'Manual',
  manual_profile: 'Manual, one profile', new_profile: 'New profile', cli: 'From console',
}

export default function SyncPage() {
  const me = useMe()
  const toast = useToast()
  const runs = useLoad(() => api<SyncRun[]>('/api/sync/runs?limit=50'), [])
  const alerts = useLoad(() => api<Alert[]>('/api/alerts?limit=100'), [])
  const health = useLoad(() => (me.role === 'admin' ? api<Health>('/api/health') : Promise.resolve(null)), [])
  const [days, setDays] = useState(3)
  const running = runs.data?.some((r) => !r.finished_at)

  useEffect(() => {
    if (!running) return
    const t = setInterval(() => { runs.reload(); alerts.reload(); health.reload() }, 5000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running])

  const start = async () => {
    try {
      await api('/api/sync', { method: 'POST', body: { days } })
      toast('Sync started')
      setTimeout(runs.reload, 800)
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Sync and alerts</h1>
        {me.role === 'admin' && (
          <div className="row">
            <label htmlFor="days" className="sub">Days to fetch</label>
            <select id="days" className="input" value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {[1, 3, 7, 14, 30, 60, 90].map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
            <button className="btn primary" onClick={start} disabled={running}>{running ? 'Running…' : 'Run'}</button>
          </div>
        )}
      </div>

      {me.role === 'admin' && (
        <section className="panel panel-pad">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <h2 style={{ margin: 0 }}>
              Service health{' '}
              {health.data && <span className={`badge ${LEVEL[health.data.status].cls}`}>{LEVEL[health.data.status].label}</span>}
            </h2>
            <button className="btn small" onClick={health.reload} disabled={health.loading}>Check</button>
          </div>
          {health.error && <ErrorBox text={health.error} onRetry={health.reload} />}
          {health.loading && !health.data && <Spinner />}
          {health.data?.checks.map((c, i) => (
            <div className="alert-item" key={i}>
              <span className={`badge ${LEVEL[c.level].cls}`}>{LEVEL[c.level].label}</span>
              <span className="txt">
                <strong>{c.name}:</strong> {c.text}
                {Object.entries(c.details || {}).filter(([, v]) => Array.isArray(v)).map(([k, v]) => (
                  <span key={k} className="sub" style={{ display: 'block' }}>{(v as string[]).join('; ')}</span>
                ))}
              </span>
            </div>
          ))}
        </section>
      )}

      <section className="panel panel-pad">
        <h2>Alerts</h2>
        {alerts.error && <ErrorBox text={alerts.error} onRetry={alerts.reload} />}
        {alerts.loading && !alerts.data && <Spinner />}
        {alerts.data?.length === 0 && <div className="sub">All quiet — no alerts yet.</div>}
        {alerts.data?.map((a) => (
          <div className="alert-item" key={a.key}>
            <span className={`badge ${a.level === 'crit' ? 'bad' : a.level === 'info' ? 'ok' : 'warn'}`}>
              {a.level === 'crit' ? 'Critical' : a.level === 'info' ? 'Info' : 'Warning'}
            </span>
            {/* alert text is generated server-side from escaped values; strip tags for display */}
            <span className="txt">{a.text.replace(/<[^>]+>/g, '')}</span>
            <span className="when">{dateTime(a.created_at)}{a.sent ? '' : ' · not sent'}</span>
          </div>
        ))}
      </section>

      <section className="panel">
        <div className="panel-pad"><h2 style={{ margin: 0 }}>Sync log</h2></div>
        {runs.error && <div className="panel-pad"><ErrorBox text={runs.error} onRetry={runs.reload} /></div>}
        <div className="table-scroll">
          <table className="t">
            <thead><tr><th>Started</th><th>Trigger</th><th className="n">Days</th><th>Result</th><th>Details</th></tr></thead>
            <tbody>
              {runs.data?.length === 0 && <tr><td colSpan={5}><div className="empty">No runs yet</div></td></tr>}
              {runs.data?.map((r) => (
                <tr key={r.id}>
                  <td>{dateTime(r.started_at)}</td>
                  <td>{TRIGGERS[r.trigger] || r.trigger}</td>
                  <td className="n">{r.days}</td>
                  <td>
                    {!r.finished_at ? <span className="row"><Spinner /> running</span>
                      : r.ok ? <span className="badge ok">Success</span> : <span className="badge bad">With errors</span>}
                  </td>
                  <td style={{ whiteSpace: 'normal', minWidth: 300 }}>
                    {r.message.split(' | ').map((m, i) => <div key={i} className={/error|stopped working|not updated|proxy|network|key changed/i.test(m) ? 'err-text' : ''}>{m}</div>)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
