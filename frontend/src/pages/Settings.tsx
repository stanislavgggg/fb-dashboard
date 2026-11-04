import { useEffect, useState } from 'react'
import { api } from '../api'
import { useMe } from '../App'
import { ErrorBox, Modal, Spinner, useLoad, useToast } from '../components/ui'
import type { Role, Settings, User } from '../types'

export default function SettingsPage() {
  return (
    <div className="stack">
      <div className="page-head"><h1>Settings</h1></div>
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
      if (!/^[A-Z]{3}$/.test(cur) || !(rate > 0)) return toast(`Check the rate for ${cur || '?'}: needs a 3-letter code and a number above zero`, 'error')
      rates[cur] = rate
    }
    const noreg = Number(f.noreg.replace(',', '.'))
    const cap = Number(f.cap)
    if (!(noreg >= 0)) return toast('The "spend without regs" threshold must be a number', 'error')
    if (!(cap >= 0 && cap <= 100)) return toast('The spend cap threshold must be 0–100%', 'error')
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
      toast('Settings saved')
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
      toast('Test message sent')
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
          <h2>Currency rates to USD</h2>
          <div className="sub">
            Ad accounts in other currencies are converted to USD at these rates: how many USD one unit of the currency is worth.
            {data.fx_updated_at ? ` Rates updated ${new Date(data.fx_updated_at).toLocaleDateString('en-GB')}${data.fx_source === 'auto' ? ' automatically' : ' manually'}.` : ' Rates have never been saved.'}
          </div>
        </div>
        <label className="check"><input type="checkbox" checked={f.fx_auto} onChange={(e) => setF((x) => ({ ...x, fx_auto: e.target.checked }))} />Update rates automatically once a day (ECB via Frankfurter)</label>
        {f.fx_auto && (
          <div className="row" style={{ alignItems: 'baseline', gap: 12 }}>
            <span className="sub">
              Current: {Object.entries(data.fx_to_usd).filter(([c]) => c !== 'USD').map(([c, r]) => `${c} ${r}`).join(', ') || 'none'}
            </span>
            <button className="btn small" onClick={refreshFx} disabled={refreshing}>{refreshing ? 'Updating…' : 'Update now'}</button>
          </div>
        )}
        <div className="sub">{f.fx_auto ? 'Manual rates below override the automatic ones — for currencies the ECB doesn\'t publish, or when you need a fixed rate.' : 'All rates are set manually.'}</div>
        {fx.map((r, i) => (
          <div className="row" key={i}>
            <input className="input" style={{ width: 90 }} value={r.cur} maxLength={3} aria-label="Currency"
              onChange={(e) => setFx((x) => x.map((y, j) => (j === i ? { ...y, cur: e.target.value.toUpperCase() } : y)))} />
            <input className="input" style={{ width: 120 }} value={r.rate} inputMode="decimal" aria-label="Rate"
              onChange={(e) => setFx((x) => x.map((y, j) => (j === i ? { ...y, rate: e.target.value } : y)))} />
            <button className="btn small ghost danger" onClick={() => setFx((x) => x.filter((_, j) => j !== i))}>Remove</button>
          </div>
        ))}
        <div><button className="btn small" onClick={() => setFx((x) => [...x, { cur: '', rate: '' }])}>Add currency</button></div>
        <div className="field" style={{ maxWidth: 260 }}>
          <label htmlFor="kc">Keitaro report currency</label>
          <input id="kc" className="input" value={f.keitaro_currency} maxLength={3}
            onChange={(e) => setF((x) => ({ ...x, keitaro_currency: e.target.value.toUpperCase() }))} />
        </div>
      </section>

      <section className="panel panel-pad stack">
        <div>
          <h2>Telegram alerts</h2>
          {!data.tg_bot_configured && <div className="notice">Bot is not configured: add TG_BOT_TOKEN to the service variables. Until then alerts are only visible on the Sync page.</div>}
        </div>
        <label className="check"><input type="checkbox" checked={f.alerts_enabled} onChange={(e) => setF((x) => ({ ...x, alerts_enabled: e.target.checked }))} />Send alerts</label>
        <div className="grid2">
          <div className="field">
            <label htmlFor="chat">Chat or group ID</label>
            <input id="chat" className="input" value={f.tg_chat_id} onChange={(e) => setF((x) => ({ ...x, tg_chat_id: e.target.value }))} placeholder="-1001234567890" />
            <span className="hint">Add the bot to the group, send any message there and get the ID via @userinfobot or getUpdates.</span>
          </div>
          <div className="field">
            <label>&nbsp;</label>
            <div><button className="btn" onClick={testTg} disabled={!data.tg_bot_configured || !f.tg_chat_id.trim()}>Send test</button></div>
          </div>
          <div className="field">
            <label htmlFor="nr">Daily spend without registrations, $</label>
            <input id="nr" className="input" value={f.noreg} inputMode="decimal" onChange={(e) => setF((x) => ({ ...x, noreg: e.target.value }))} />
            <span className="hint">A campaign spent this much today with 0 registrations. Zero turns it off.</span>
          </div>
          <div className="field">
            <label htmlFor="cp">Ad account spend cap used, %</label>
            <input id="cp" className="input" value={f.cap} inputMode="numeric" onChange={(e) => setF((x) => ({ ...x, cap: e.target.value }))} />
            <span className="hint">Warn when an ad account gets close to its spend cap. Zero turns it off.</span>
          </div>
        </div>
        <div className="sub">Always sent: a profile token stopped working, the proxy is down, an ad account was disabled, re-enabled or removed from a profile.</div>
      </section>
      <div><button className="btn primary" onClick={save} disabled={busy}>{busy ? 'Saving…' : 'Save settings'}</button></div>
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
    if (!confirm(`Delete user ${u.login}?`)) return
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
          <h2 style={{ margin: 0 }}>Users</h2>
          <div className="sub">Admins manage profiles and settings. Viewers only see stats; a viewer with a buyer set sees only that buyer's profiles, ad accounts and alerts.</div>
        </div>
        <button className="btn" onClick={() => setAdding(true)}>Add</button>
      </div>
      {error && <div className="panel-pad"><ErrorBox text={error} onRetry={reload} /></div>}
      <div className="table-scroll">
        <table className="t">
          <thead><tr><th>Login</th><th>Role</th><th>Buyer</th><th /></tr></thead>
          <tbody>
            {data?.map((u) => (
              <tr key={u.id}>
                <td>{u.login}{u.id === me.id && <span className="sub"> (you)</span>}</td>
                <td>
                  <select className="input" value={u.role} disabled={u.id === me.id} onChange={(e) => setRole(u, e.target.value as Role)} aria-label="Role">
                    <option value="admin">Admin</option>
                    <option value="viewer">Viewer</option>
                  </select>
                </td>
                <td>
                  {u.role === 'admin' ? <span className="sub">all</span> : (
                    <BuyerSelect value={u.buyer} buyers={buyers} onChange={(b) => setBuyer(u, b)} />
                  )}
                </td>
                <td>
                  <div className="row" style={{ justifyContent: 'flex-end', flexWrap: 'nowrap' }}>
                    <button className="btn small" onClick={() => setResetFor(u)}>Change password</button>
                    {u.id !== me.id && <button className="btn small danger" onClick={() => remove(u)}>Delete</button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {adding && <UserForm buyers={buyers} onClose={() => setAdding(false)} onSaved={() => { setAdding(false); reload(); toast('User added') }} />}
      {resetFor && <UserForm user={resetFor} onClose={() => setResetFor(null)} onSaved={() => { setResetFor(null); toast('Password changed') }} />}
    </section>
  )
}

function BuyerSelect({ value, buyers, onChange }: { value: string; buyers: string[]; onChange: (b: string) => void }) {
  const opts = Array.from(new Set([...buyers, value].filter(Boolean)))
  return (
    <select className="input" value={value} onChange={(e) => onChange(e.target.value)} aria-label="Buyer">
      <option value="">all buyers</option>
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
    if (password.length < 8) return toast('Password must be at least 8 characters', 'error')
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
    <Modal title={user ? `Password for ${user.login}` : 'New user'} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" onClick={save} disabled={busy}>Save</button></>}>
      {!user && (
        <div className="grid2">
          <div className="field"><label htmlFor="ul">Login</label><input id="ul" className="input" value={login} onChange={(e) => setLogin(e.target.value)} autoComplete="off" /></div>
          <div className="field">
            <label htmlFor="ur">Role</label>
            <select id="ur" className="input" value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="viewer">Viewer</option><option value="admin">Admin</option>
            </select>
          </div>
          {role === 'viewer' && (
            <div className="field">
              <label>Buyer</label>
              <BuyerSelect value={buyer} buyers={buyers} onChange={setBuyer} />
              <span className="hint">If set, the user only sees this buyer's profiles (matched against the Buyer field of each profile).</span>
            </div>
          )}
        </div>
      )}
      <div className="field">
        <label htmlFor="up">Password</label>
        <input id="up" className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        <span className="hint">At least 8 characters.</span>
      </div>
    </Modal>
  )
}

function ChangePassword() {
  const toast = useToast()
  const [oldPw, setOld] = useState('')
  const [newPw, setNew] = useState('')
  const save = async () => {
    if (newPw.length < 8) return toast('The new password must be at least 8 characters', 'error')
    try {
      await api('/api/auth/password', { method: 'POST', body: { old_password: oldPw, new_password: newPw } })
      setOld(''); setNew('')
      toast('Password changed')
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }
  return (
    <section className="panel panel-pad stack">
      <h2 style={{ margin: 0 }}>My password</h2>
      <div className="grid2">
        <div className="field"><label htmlFor="op">Current</label><input id="op" className="input" type="password" value={oldPw} onChange={(e) => setOld(e.target.value)} autoComplete="current-password" /></div>
        <div className="field"><label htmlFor="np">New</label><input id="np" className="input" type="password" value={newPw} onChange={(e) => setNew(e.target.value)} autoComplete="new-password" /></div>
      </div>
      <div><button className="btn" onClick={save}>Change password</button></div>
    </section>
  )
}
