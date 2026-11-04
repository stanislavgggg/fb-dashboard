import { useEffect, useState } from 'react'
import { api } from '../api'
import { useMe } from '../App'
import { ErrorBox, Modal, Spinner, useLoad, useToast } from '../components/ui'
import type { Role, Settings, User } from '../types'

export default function SettingsPage() {
  return (
    <div className="stack">
      <div className="page-head"><h1>Настройки</h1></div>
      <GeneralSettings />
      <Users />
      <ChangePassword />
    </div>
  )
}

function GeneralSettings() {
  const toast = useToast()
  const { data, error, reload } = useLoad(() => api<Settings>('/api/settings'), [])
  const [fx, setFx] = useState<{ cur: string; rate: string }[]>([])
  const [f, setF] = useState({ keitaro_currency: 'USD', alerts_enabled: true, tg_chat_id: '', noreg: '30', cap: '90', fx_auto: true })
  const [busy, setBusy] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    if (!data) return
    // with auto rates on, the editable list is the manual overrides; otherwise it's the whole table
    const src = data.fx_auto ? data.fx_manual : data.fx_to_usd
    setFx(Object.entries(src || {}).filter(([c]) => c !== 'USD').map(([cur, rate]) => ({ cur, rate: String(rate) })))
    setF({
      keitaro_currency: data.keitaro_currency, alerts_enabled: data.alerts_enabled, tg_chat_id: data.tg_chat_id,
      noreg: String(data.alert_spend_no_reg_usd), cap: String(Math.round(data.alert_cap_ratio * 100)), fx_auto: data.fx_auto,
    })
  }, [data])

  const refreshFx = async () => {
    setRefreshing(true)
    try {
      const r = await api<{ message: string }>('/api/settings/fx-refresh', { method: 'POST' })
      toast(r.message)
      reload()
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setRefreshing(false)
    }
  }

  const save = async () => {
    const rates: Record<string, number> = {}
    for (const r of fx) {
      const cur = r.cur.trim().toUpperCase()
      const rate = Number(r.rate.replace(',', '.'))
      if (!cur) continue
      if (!/^[A-Z]{3}$/.test(cur) || !(rate > 0)) return toast(`Проверь курс ${cur || '?'}: нужен код из 3 букв и число больше нуля`, 'error')
      rates[cur] = rate
    }
    const noreg = Number(f.noreg.replace(',', '.'))
    const cap = Number(f.cap)
    if (!(noreg >= 0)) return toast('Порог «расход без регов» должен быть числом', 'error')
    if (!(cap >= 0 && cap <= 100)) return toast('Порог лимита — от 0 до 100%', 'error')
    setBusy(true)
    try {
      await api('/api/settings', {
        method: 'PUT',
        body: {
          ...(f.fx_auto ? { fx_manual: rates } : { fx_to_usd: rates }), fx_auto: f.fx_auto,
          keitaro_currency: f.keitaro_currency, alerts_enabled: f.alerts_enabled,
          tg_chat_id: f.tg_chat_id.trim(), alert_spend_no_reg_usd: noreg, alert_cap_ratio: cap / 100,
        },
      })
      toast('Настройки сохранены')
      reload()
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }

  const testTg = async () => {
    try {
      // send to the chat id typed in the form, even if it hasn't been saved yet
      await api('/api/settings/test-telegram', { method: 'POST', body: { tg_chat_id: f.tg_chat_id.trim() } })
      toast('Тестовое сообщение отправлено')
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  if (error) return <ErrorBox text={error} onRetry={reload} />
  if (!data) return <Spinner />

  return (
    <>
      <section className="panel panel-pad stack">
        <div>
          <h2>Курсы валют к доллару</h2>
          <div className="sub">
            Кабинеты в других валютах переводятся в доллары по этим курсам. Сколько долларов стоит 1 единица валюты.
            {data.fx_updated_at ? ` Курсы обновлены ${new Date(data.fx_updated_at).toLocaleDateString('ru-RU')}${data.fx_source === 'auto' ? ' автоматически' : ' вручную'}.` : ' Курсы ещё ни разу не сохранялись.'}
          </div>
        </div>
        <label className="check"><input type="checkbox" checked={f.fx_auto} onChange={(e) => setF((x) => ({ ...x, fx_auto: e.target.checked }))} />Обновлять курсы автоматически раз в день (ЕЦБ через frankfurter.app)</label>
        {f.fx_auto && (
          <div className="row" style={{ alignItems: 'baseline', gap: 12 }}>
            <span className="sub">
              Сейчас: {Object.entries(data.fx_to_usd).filter(([c]) => c !== 'USD').map(([c, r]) => `${c} ${r}`).join(', ') || 'ничего'}
            </span>
            <button className="btn small" onClick={refreshFx} disabled={refreshing}>{refreshing ? 'Обновляю…' : 'Обновить сейчас'}</button>
          </div>
        )}
        <div className="sub">{f.fx_auto ? 'Ручные курсы ниже имеют приоритет над автоматическими — для валют, которых нет у ЕЦБ, или если нужен фиксированный курс.' : 'Все курсы задаются вручную.'}</div>
        {fx.map((r, i) => (
          <div className="row" key={i}>
            <input className="input" style={{ width: 90 }} value={r.cur} maxLength={3} aria-label="Валюта"
              onChange={(e) => setFx((x) => x.map((y, j) => (j === i ? { ...y, cur: e.target.value.toUpperCase() } : y)))} />
            <input className="input" style={{ width: 120 }} value={r.rate} inputMode="decimal" aria-label="Курс"
              onChange={(e) => setFx((x) => x.map((y, j) => (j === i ? { ...y, rate: e.target.value } : y)))} />
            <button className="btn small ghost danger" onClick={() => setFx((x) => x.filter((_, j) => j !== i))}>Убрать</button>
          </div>
        ))}
        <div><button className="btn small" onClick={() => setFx((x) => [...x, { cur: '', rate: '' }])}>Добавить валюту</button></div>
        <div className="field" style={{ maxWidth: 260 }}>
          <label htmlFor="kc">Валюта отчётов Keitaro</label>
          <input id="kc" className="input" value={f.keitaro_currency} maxLength={3}
            onChange={(e) => setF((x) => ({ ...x, keitaro_currency: e.target.value.toUpperCase() }))} />
        </div>
      </section>

      <section className="panel panel-pad stack">
        <div>
          <h2>Алерты в Telegram</h2>
          {!data.tg_bot_configured && <div className="notice">Бот не настроен: добавь TG_BOT_TOKEN в переменные сервиса. Пока алерты видны только в разделе «Синхронизация».</div>}
        </div>
        <label className="check"><input type="checkbox" checked={f.alerts_enabled} onChange={(e) => setF((x) => ({ ...x, alerts_enabled: e.target.checked }))} />Отправлять алерты</label>
        <div className="grid2">
          <div className="field">
            <label htmlFor="chat">ID чата или группы</label>
            <input id="chat" className="input" value={f.tg_chat_id} onChange={(e) => setF((x) => ({ ...x, tg_chat_id: e.target.value }))} placeholder="-1001234567890" />
            <span className="hint">Добавь бота в группу, отправь там любое сообщение и возьми ID через @userinfobot или getUpdates.</span>
          </div>
          <div className="field">
            <label>&nbsp;</label>
            <div><button className="btn" onClick={testTg} disabled={!data.tg_bot_configured || !f.tg_chat_id.trim()}>Отправить тест</button></div>
          </div>
          <div className="field">
            <label htmlFor="nr">Расход без регистраций за день, $</label>
            <input id="nr" className="input" value={f.noreg} inputMode="decimal" onChange={(e) => setF((x) => ({ ...x, noreg: e.target.value }))} />
            <span className="hint">Кампания потратила столько сегодня, а регистраций 0. Ноль — не следить.</span>
          </div>
          <div className="field">
            <label htmlFor="cp">Израсходован лимит кабинета, %</label>
            <input id="cp" className="input" value={f.cap} inputMode="numeric" onChange={(e) => setF((x) => ({ ...x, cap: e.target.value }))} />
            <span className="hint">Предупредить, когда кабинет подходит к лимиту расходов. Ноль — не следить.</span>
          </div>
        </div>
        <div className="sub">Всегда приходят: токен профиля перестал работать, кабинет отключили или он снова активен.</div>
      </section>
      <div><button className="btn primary" onClick={save} disabled={busy}>{busy ? 'Сохраняю…' : 'Сохранить настройки'}</button></div>
    </>
  )
}

function Users() {
  const me = useMe()
  const toast = useToast()
  const { data, error, reload } = useLoad(() => api<User[]>('/api/users'), [])
  const meta = useLoad(() => api<{ buyers: string[] }>('/api/meta'), [])
  const buyers = meta.data?.buyers || []
  const [adding, setAdding] = useState(false)
  const [resetFor, setResetFor] = useState<User | null>(null)

  const setBuyer = async (u: User, buyer: string) => {
    try {
      await api(`/api/users/${u.id}`, { method: 'PATCH', body: { buyer } })
      reload()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  const setRole = async (u: User, role: Role) => {
    try {
      await api(`/api/users/${u.id}`, { method: 'PATCH', body: { role } })
      reload()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }
  const remove = async (u: User) => {
    if (!confirm(`Удалить пользователя ${u.login}?`)) return
    try {
      await api(`/api/users/${u.id}`, { method: 'DELETE' })
      reload()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  return (
    <section className="panel">
      <div className="panel-pad row" style={{ justifyContent: 'space-between' }}>
        <div>
          <h2 style={{ margin: 0 }}>Пользователи</h2>
          <div className="sub">Админ управляет профилями и настройками. Просмотр — только смотрит статистику; если указан байер, видит только его профили, кабинеты и алерты.</div>
        </div>
        <button className="btn" onClick={() => setAdding(true)}>Добавить</button>
      </div>
      {error && <div className="panel-pad"><ErrorBox text={error} onRetry={reload} /></div>}
      <div className="table-scroll">
        <table className="t">
          <thead><tr><th>Логин</th><th>Доступ</th><th>Байер</th><th /></tr></thead>
          <tbody>
            {data?.map((u) => (
              <tr key={u.id}>
                <td>{u.login}{u.id === me.id && <span className="sub"> (это ты)</span>}</td>
                <td>
                  <select className="input" value={u.role} disabled={u.id === me.id} onChange={(e) => setRole(u, e.target.value as Role)} aria-label="Доступ">
                    <option value="admin">Админ</option>
                    <option value="viewer">Просмотр</option>
                  </select>
                </td>
                <td>
                  {u.role === 'admin' ? <span className="sub">все</span> : (
                    <BuyerSelect value={u.buyer} buyers={buyers} onChange={(b) => setBuyer(u, b)} />
                  )}
                </td>
                <td>
                  <div className="row" style={{ justifyContent: 'flex-end', flexWrap: 'nowrap' }}>
                    <button className="btn small" onClick={() => setResetFor(u)}>Сменить пароль</button>
                    {u.id !== me.id && <button className="btn small danger" onClick={() => remove(u)}>Удалить</button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {adding && <UserForm buyers={buyers} onClose={() => setAdding(false)} onSaved={() => { setAdding(false); reload(); toast('Пользователь добавлен') }} />}
      {resetFor && <UserForm user={resetFor} onClose={() => setResetFor(null)} onSaved={() => { setResetFor(null); toast('Пароль изменён') }} />}
    </section>
  )
}

function BuyerSelect({ value, buyers, onChange }: { value: string; buyers: string[]; onChange: (b: string) => void }) {
  const opts = Array.from(new Set([...buyers, value].filter(Boolean)))
  return (
    <select className="input" value={value} onChange={(e) => onChange(e.target.value)} aria-label="Байер">
      <option value="">все байеры</option>
      {opts.map((b) => <option key={b} value={b}>{b}</option>)}
    </select>
  )
}

function UserForm({ user, buyers = [], onClose, onSaved }: { user?: User; buyers?: string[]; onClose: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [login, setLogin] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('viewer')
  const [buyer, setBuyer] = useState('')
  const [busy, setBusy] = useState(false)
  const save = async () => {
    if (password.length < 8) return toast('Пароль — минимум 8 символов', 'error')
    setBusy(true)
    try {
      if (user) await api(`/api/users/${user.id}`, { method: 'PATCH', body: { password } })
      else await api('/api/users', { method: 'POST', body: { login, password, role, buyer: role === 'viewer' ? buyer : '' } })
      onSaved()
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal title={user ? `Пароль для ${user.login}` : 'Новый пользователь'} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Отмена</button><button className="btn primary" onClick={save} disabled={busy}>Сохранить</button></>}>
      {!user && (
        <div className="grid2">
          <div className="field"><label htmlFor="ul">Логин</label><input id="ul" className="input" value={login} onChange={(e) => setLogin(e.target.value)} autoComplete="off" /></div>
          <div className="field">
            <label htmlFor="ur">Доступ</label>
            <select id="ur" className="input" value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="viewer">Просмотр</option><option value="admin">Админ</option>
            </select>
          </div>
          {role === 'viewer' && (
            <div className="field">
              <label>Байер</label>
              <BuyerSelect value={buyer} buyers={buyers} onChange={setBuyer} />
              <span className="hint">Если выбран — пользователь видит только профили этого байера (как написано в поле «Байер» у профиля).</span>
            </div>
          )}
        </div>
      )}
      <div className="field">
        <label htmlFor="up">Пароль</label>
        <input id="up" className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        <span className="hint">Минимум 8 символов.</span>
      </div>
    </Modal>
  )
}

function ChangePassword() {
  const toast = useToast()
  const [oldPw, setOld] = useState('')
  const [newPw, setNew] = useState('')
  const save = async () => {
    if (newPw.length < 8) return toast('Новый пароль — минимум 8 символов', 'error')
    try {
      await api('/api/auth/password', { method: 'POST', body: { old_password: oldPw, new_password: newPw } })
      setOld(''); setNew('')
      toast('Пароль изменён')
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }
  return (
    <section className="panel panel-pad stack">
      <h2 style={{ margin: 0 }}>Мой пароль</h2>
      <div className="grid2">
        <div className="field"><label htmlFor="op">Текущий</label><input id="op" className="input" type="password" value={oldPw} onChange={(e) => setOld(e.target.value)} autoComplete="current-password" /></div>
        <div className="field"><label htmlFor="np">Новый</label><input id="np" className="input" type="password" value={newPw} onChange={(e) => setNew(e.target.value)} autoComplete="new-password" /></div>
      </div>
      <div><button className="btn" onClick={save}>Сменить пароль</button></div>
    </section>
  )
}
