import { useState } from 'react'
import { api } from '../api'
import { useMe } from '../App'
import { ErrorBox, Modal, Spinner, useLoad, useToast } from '../components/ui'
import { ago } from '../format'
import type { Profile } from '../types'

type CheckResult = { ok: boolean; user?: string; total?: number; error?: string; accounts?: { id: string; name: string; status: string; currency: string }[] }

export default function ProfilesPage() {
  const me = useMe()
  const toast = useToast()
  const { data, error, loading, reload } = useLoad(() => api<Profile[]>('/api/profiles'), [])
  const [editing, setEditing] = useState<Profile | 'new' | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const admin = me.role === 'admin'

  // after a manual sync, refresh the row until last_sync_at moves (max ~2 min)
  const pollUntilDone = (id: number) => {
    const before = data?.find((x) => x.id === id)?.last_sync_at
    let tries = 0
    const tick = async () => {
      tries++
      try {
        const fresh = await api<Profile[]>('/api/profiles')
        const p = fresh.find((x) => x.id === id)
        if (!p || p.last_sync_at !== before || tries >= 24) { reload(); setBusyId(null); return }
      } catch { /* keep polling */ }
      setTimeout(tick, 5000)
    }
    setTimeout(tick, 3000)
  }

  const syncOne = async (p: Profile) => {
    setBusyId(p.id)
    try {
      await api(`/api/profiles/${p.id}/sync?days=7`, { method: 'POST' })
      toast(`Sync of "${p.name}" started`)
      pollUntilDone(p.id)
    } catch (e) {
      toast((e as Error).message, 'error')
      setBusyId(null)
    }
  }

  const remove = async (p: Profile) => {
    if (!confirm(`Delete profile "${p.name}" together with its ad accounts and all their stats? This can't be undone.`)) return
    try {
      await api(`/api/profiles/${p.id}`, { method: 'DELETE' })
      toast('Profile deleted')
      reload()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Profiles</h1>
          <div className="sub">One profile is one access from an agency or supplier: a token and its proxy. Ad accounts are pulled in automatically.</div>
        </div>
        {admin && <button className="btn primary" onClick={() => setEditing('new')}>Add profile</button>}
      </div>
      {error && <ErrorBox text={error} onRetry={reload} />}
      <section className="panel table-scroll">
        <table className="t">
          <thead>
            <tr>
              <th>Profile</th><th>Token</th><th>Proxy</th><th className="n">Ad accounts</th><th>Last sync</th>
              {admin && <th />}
            </tr>
          </thead>
          <tbody>
            {loading && !data && <tr><td colSpan={6}><div className="empty"><Spinner /></div></td></tr>}
            {data && data.length === 0 && (
              <tr><td colSpan={6}><div className="empty">
                <strong>No profiles yet</strong>
                {admin ? 'Add the first agency access — the token and the proxy the buyer uses to log into it.' : me.buyer ? `No profiles are assigned to buyer "${me.buyer}" yet.` : 'Ask an admin to add profiles.'}
              </div></td></tr>
            )}
            {data?.map((p) => (
              <tr key={p.id} style={p.active ? undefined : { opacity: 0.55 }}>
                <td className="name">
                  <span style={{ fontWeight: 600 }}>{p.name}</span>
                  <small>{p.supplier || 'no supplier'}{p.buyer ? ` · ${p.buyer}` : ''}{!p.active ? ' · disabled' : ''}</small>
                </td>
                <td>
                  {p.key_changed ? <span className="badge bad">Key changed</span>
                    : p.token_ok === false ? <span className="badge bad">Not working</span>
                    : p.token_ok ? <span className="badge ok">{p.fb_user || 'Working'}</span>
                      : <span className="badge">Not checked</span>}
                  <small className="sub" style={{ display: 'block' }}>{p.token_masked}</small>
                </td>
                <td>{p.proxy_masked || <span className="sub">no proxy</span>}</td>
                <td className="n">{p.accounts_active} / {p.accounts}</td>
                <td>
                  {ago(p.last_sync_at)}
                  {p.last_error && <div className="err-text">{p.last_error}</div>}
                </td>
                {admin && (
                  <td>
                    <div className="row" style={{ flexWrap: 'nowrap' }}>
                      <button className="btn small" onClick={() => syncOne(p)} disabled={busyId === p.id || !p.active}>Sync</button>
                      <button className="btn small" onClick={() => setEditing(p)}>Edit</button>
                      <button className="btn small danger" onClick={() => remove(p)}>Delete</button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <div className="notice info">
        Where to get a token: in the agency profile (in the anti-detect browser, through its proxy) open Graph API Explorer, or take the token the same way as for FBTool/Dolphin.
        Tokens expire — when one dies, the profile turns red and an alert is sent.
      </div>
      {editing && (
        <ProfileForm profile={editing === 'new' ? null : editing} onClose={() => setEditing(null)}
          onSaved={(msg) => { setEditing(null); reload(); toast(msg) }} />
      )}
    </div>
  )
}

function ProfileForm({ profile, onClose, onSaved }: { profile: Profile | null; onClose: () => void; onSaved: (m: string) => void }) {
  const toast = useToast()
  const [f, setF] = useState({
    name: profile?.name || '', supplier: profile?.supplier || '', buyer: profile?.buyer || '',
    notes: profile?.notes || '', active: profile?.active ?? true, token: '', proxy: '',
  })
  const [clearProxy, setClearProxy] = useState(false)
  const [check, setCheck] = useState<CheckResult | null>(null)
  const [busy, setBusy] = useState<'check' | 'save' | null>(null)
  const upd = (k: keyof typeof f, v: string | boolean) => { setF((x) => ({ ...x, [k]: v })); if (k === 'token' || k === 'proxy') setCheck(null) }

  const runCheck = async () => {
    setBusy('check')
    try {
      const body = profile
        ? { profile_id: profile.id, token: f.token || undefined, proxy: clearProxy ? '' : f.proxy || undefined }
        : { token: f.token, proxy: f.proxy }
      setCheck(await api<CheckResult>('/api/profiles/check', { method: 'POST', body }))
    } catch (e) {
      setCheck({ ok: false, error: (e as Error).message })
    } finally {
      setBusy(null)
    }
  }

  const save = async () => {
    if (!f.name.trim()) return toast('Enter a profile name', 'error')
    if (!profile && !f.token.trim()) return toast('A token is required', 'error')
    setBusy('save')
    try {
      const body = {
        name: f.name, supplier: f.supplier, buyer: f.buyer, notes: f.notes, active: f.active,
        token: f.token.trim() || null,
        proxy: clearProxy ? '' : f.proxy.trim() ? f.proxy.trim() : profile ? null : '',
      }
      if (profile) {
        const r = await api<{ sync_started?: boolean }>(`/api/profiles/${profile.id}`, { method: 'PATCH', body })
        onSaved(r.sync_started ? 'Profile saved, checking the new access and refreshing stats' : 'Profile saved')
      } else {
        await api('/api/profiles', { method: 'POST', body })
        onSaved('Profile added — pulling ad accounts, 30 days of spend and Keitaro conversions')
      }
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <Modal title={profile ? `Profile "${profile.name}"` : 'New profile'} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn" onClick={runCheck} disabled={!!busy || (!profile && !f.token.trim())}>
          {busy === 'check' ? 'Checking…' : 'Check access'}
        </button>
        <button className="btn primary" onClick={save} disabled={!!busy}>{busy === 'save' ? 'Saving…' : profile ? 'Save' : 'Add'}</button>
      </>}>
      <div className="field">
        <label htmlFor="pn">Name</label>
        <input id="pn" className="input" value={f.name} onChange={(e) => upd('name', e.target.value)} placeholder="Agency X — login 1" maxLength={120} />
      </div>
      <div className="grid2">
        <div className="field">
          <label htmlFor="ps">Supplier / agency</label>
          <input id="ps" className="input" value={f.supplier} onChange={(e) => upd('supplier', e.target.value)} placeholder="Agency X" />
        </div>
        <div className="field">
          <label htmlFor="pb">Buyer</label>
          <input id="pb" className="input" value={f.buyer} onChange={(e) => upd('buyer', e.target.value)} placeholder="Buyer name" />
        </div>
      </div>
      <div className="field">
        <label htmlFor="pt">Access token</label>
        <textarea id="pt" className="input" rows={3} value={f.token} onChange={(e) => upd('token', e.target.value)}
          placeholder={profile ? `Current: ${profile.token_masked}. Leave empty to keep it` : 'EAAB…'} spellCheck={false} autoComplete="off" />
      </div>
      <div className="field">
        <label htmlFor="pp">Proxy</label>
        <input id="pp" className="input" value={f.proxy} onChange={(e) => upd('proxy', e.target.value)} disabled={clearProxy}
          placeholder={profile?.proxy_masked ? `Current: ${profile.proxy_masked}. Leave empty to keep it` : 'host:port:user:pass'} spellCheck={false} autoComplete="off" />
        <span className="hint">The same proxy the buyer uses for this profile. Formats: host:port:user:pass, user:pass@host:port, http://… or socks5://…</span>
        {profile?.proxy_masked && (
          <label className="check"><input type="checkbox" checked={clearProxy} onChange={(e) => setClearProxy(e.target.checked)} />Remove proxy</label>
        )}
      </div>
      <div className="field">
        <label htmlFor="pno">Notes</label>
        <textarea id="pno" className="input" rows={2} value={f.notes} onChange={(e) => upd('notes', e.target.value)} />
      </div>
      <label className="check"><input type="checkbox" checked={f.active} onChange={(e) => upd('active', e.target.checked)} />Sync this profile</label>
      {check && (check.ok ? (
        <div className="notice info">
          Access OK: {check.user}. Ad accounts visible: {check.total}.
          {check.accounts && check.accounts.length > 0 && (
            <div className="sub" style={{ marginTop: 6 }}>
              {check.accounts.slice(0, 8).map((a) => `${a.name} (${a.status}, ${a.currency})`).join('; ')}
              {check.total! > 8 ? ` and ${check.total! - 8} more` : ''}
            </div>
          )}
        </div>
      ) : <div className="notice bad">No access: {check.error}</div>)}
    </Modal>
  )
}
