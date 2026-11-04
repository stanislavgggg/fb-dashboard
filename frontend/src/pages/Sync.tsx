import { useEffect, useState } from 'react'
import { api } from '../api'
import { useMe } from '../App'
import { ErrorBox, Spinner, useLoad, useToast } from '../components/ui'
import { dateTime } from '../format'
import type { Alert, SyncRun } from '../types'

const TRIGGERS: Record<string, string> = {
  schedule: 'По расписанию', startup: 'При запуске', manual: 'Вручную',
  manual_profile: 'Вручную, один профиль', new_profile: 'Новый профиль', cli: 'Из консоли',
}

export default function SyncPage() {
  const me = useMe()
  const toast = useToast()
  const runs = useLoad(() => api<SyncRun[]>('/api/sync/runs?limit=50'), [])
  const alerts = useLoad(() => api<Alert[]>('/api/alerts?limit=100'), [])
  const [days, setDays] = useState(3)
  const running = runs.data?.some((r) => !r.finished_at)

  useEffect(() => {
    if (!running) return
    const t = setInterval(() => { runs.reload(); alerts.reload() }, 5000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running])

  const start = async () => {
    try {
      await api('/api/sync', { method: 'POST', body: { days } })
      toast('Синхронизация запущена')
      setTimeout(runs.reload, 800)
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Синхронизация и алерты</h1>
        {me.role === 'admin' && (
          <div className="row">
            <label htmlFor="days" className="sub">За сколько дней</label>
            <select id="days" className="input" value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {[1, 3, 7, 14, 30, 60, 90].map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
            <button className="btn primary" onClick={start} disabled={running}>{running ? 'Идёт…' : 'Запустить'}</button>
          </div>
        )}
      </div>

      <section className="panel panel-pad">
        <h2>Алерты</h2>
        {alerts.error && <ErrorBox text={alerts.error} onRetry={alerts.reload} />}
        {alerts.loading && !alerts.data && <Spinner />}
        {alerts.data?.length === 0 && <div className="sub">Пока тихо — ни одного алерта.</div>}
        {alerts.data?.map((a) => (
          <div className="alert-item" key={a.key}>
            <span className={`badge ${a.level === 'crit' ? 'bad' : a.level === 'info' ? 'ok' : 'warn'}`}>
              {a.level === 'crit' ? 'Важно' : a.level === 'info' ? 'Инфо' : 'Внимание'}
            </span>
            {/* alert text is generated server-side from escaped values; strip tags for display */}
            <span className="txt">{a.text.replace(/<[^>]+>/g, '')}</span>
            <span className="when">{dateTime(a.created_at)}{a.sent ? '' : ' · не отправлен'}</span>
          </div>
        ))}
      </section>

      <section className="panel">
        <div className="panel-pad"><h2 style={{ margin: 0 }}>Журнал запусков</h2></div>
        {runs.error && <div className="panel-pad"><ErrorBox text={runs.error} onRetry={runs.reload} /></div>}
        <div className="table-scroll">
          <table className="t">
            <thead><tr><th>Начало</th><th>Источник</th><th className="n">Дней</th><th>Результат</th><th>Детали</th></tr></thead>
            <tbody>
              {runs.data?.length === 0 && <tr><td colSpan={5}><div className="empty">Запусков ещё не было</div></td></tr>}
              {runs.data?.map((r) => (
                <tr key={r.id}>
                  <td>{dateTime(r.started_at)}</td>
                  <td>{TRIGGERS[r.trigger] || r.trigger}</td>
                  <td className="n">{r.days}</td>
                  <td>
                    {!r.finished_at ? <span className="row"><Spinner /> идёт</span>
                      : r.ok ? <span className="badge ok">Успешно</span> : <span className="badge bad">С ошибками</span>}
                  </td>
                  <td style={{ whiteSpace: 'normal', minWidth: 300 }}>
                    {r.message.split(' | ').map((m, i) => <div key={i} className={/ошибк|error|не работает/i.test(m) ? 'err-text' : ''}>{m}</div>)}
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
