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
      toast(`Синхронизация «${p.name}» запущена`)
      pollUntilDone(p.id)
    } catch (e) {
      toast((e as Error).message, 'error')
      setBusyId(null)
    }
  }

  const remove = async (p: Profile) => {
    if (!confirm(`Удалить профиль «${p.name}» вместе с его кабинетами и всей статистикой? Это не отменить.`)) return
    try {
      await api(`/api/profiles/${p.id}`, { method: 'DELETE' })
      toast('Профиль удалён')
      reload()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Профили</h1>
          <div className="sub">Один профиль — один доступ от агентства или поставщика: токен и его прокси. Кабинеты подтягиваются сами.</div>
        </div>
        {admin && <button className="btn primary" onClick={() => setEditing('new')}>Добавить профиль</button>}
      </div>
      {error && <ErrorBox text={error} onRetry={reload} />}
      <section className="panel table-scroll">
        <table className="t">
          <thead>
            <tr>
              <th>Профиль</th><th>Токен</th><th>Прокси</th><th className="n">Кабинеты</th><th>Синхронизация</th>
              {admin && <th />}
            </tr>
          </thead>
          <tbody>
            {loading && !data && <tr><td colSpan={6}><div className="empty"><Spinner /></div></td></tr>}
            {data && data.length === 0 && (
              <tr><td colSpan={6}><div className="empty">
                <strong>Профилей пока нет</strong>
                {admin ? 'Добавь первый доступ от агентства — токен и прокси, через который байер в него заходит.' : me.buyer ? `На байера «${me.buyer}» ещё не записан ни один профиль.` : 'Попроси админа добавить профили.'}
              </div></td></tr>
            )}
            {data?.map((p) => (
              <tr key={p.id} style={p.active ? undefined : { opacity: 0.55 }}>
                <td className="name">
                  <span style={{ fontWeight: 600 }}>{p.name}</span>
                  <small>{p.supplier || 'без поставщика'}{p.buyer ? ` · ${p.buyer}` : ''}{!p.active ? ' · выключен' : ''}</small>
                </td>
                <td>
                  {p.key_changed ? <span className="badge bad">Ключ изменён</span>
                    : p.token_ok === false ? <span className="badge bad">Не работает</span>
                    : p.token_ok ? <span className="badge ok">{p.fb_user || 'Работает'}</span>
                      : <span className="badge">Не проверен</span>}
                  <small className="sub" style={{ display: 'block' }}>{p.token_masked}</small>
                </td>
                <td>{p.proxy_masked || <span className="sub">без прокси</span>}</td>
                <td className="n">{p.accounts_active} / {p.accounts}</td>
                <td>
                  {ago(p.last_sync_at)}
                  {p.last_error && <div className="err-text">{p.last_error}</div>}
                </td>
                {admin && (
                  <td>
                    <div className="row" style={{ flexWrap: 'nowrap' }}>
                      <button className="btn small" onClick={() => syncOne(p)} disabled={busyId === p.id || !p.active}>Синхронизировать</button>
                      <button className="btn small" onClick={() => setEditing(p)}>Изменить</button>
                      <button className="btn small danger" onClick={() => remove(p)}>Удалить</button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <div className="notice info">
        Где взять токен: в профиле агентства (в антидетекте, через его прокси) открой Graph API Explorer или возьми токен так же, как для FBTool/Dolphin.
        Токены живут ограниченное время — когда токен умрёт, профиль подсветится красным и придёт алерт.
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
    if (!f.name.trim()) return toast('Укажи название профиля', 'error')
    if (!profile && !f.token.trim()) return toast('Нужен токен', 'error')
    setBusy('save')
    try {
      const body = {
        name: f.name, supplier: f.supplier, buyer: f.buyer, notes: f.notes, active: f.active,
        token: f.token.trim() || null,
        proxy: clearProxy ? '' : f.proxy.trim() ? f.proxy.trim() : profile ? null : '',
      }
      if (profile) {
        const r = await api<{ sync_started?: boolean }>(`/api/profiles/${profile.id}`, { method: 'PATCH', body })
        onSaved(r.sync_started ? 'Профиль сохранён, проверяю новый доступ и обновляю статистику' : 'Профиль сохранён')
      } else {
        await api('/api/profiles', { method: 'POST', body })
        onSaved('Профиль добавлен — подтягиваю кабинеты, расход за 30 дней и конверсии из Keitaro')
      }
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <Modal title={profile ? `Профиль «${profile.name}»` : 'Новый профиль'} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn" onClick={runCheck} disabled={!!busy || (!profile && !f.token.trim())}>
          {busy === 'check' ? 'Проверяю…' : 'Проверить доступ'}
        </button>
        <button className="btn primary" onClick={save} disabled={!!busy}>{busy === 'save' ? 'Сохраняю…' : profile ? 'Сохранить' : 'Добавить'}</button>
      </>}>
      <div className="field">
        <label htmlFor="pn">Название</label>
        <input id="pn" className="input" value={f.name} onChange={(e) => upd('name', e.target.value)} placeholder="Агентство X — логин 1" maxLength={120} />
      </div>
      <div className="grid2">
        <div className="field">
          <label htmlFor="ps">Поставщик / агентство</label>
          <input id="ps" className="input" value={f.supplier} onChange={(e) => upd('supplier', e.target.value)} placeholder="Агентство X" />
        </div>
        <div className="field">
          <label htmlFor="pb">Байер</label>
          <input id="pb" className="input" value={f.buyer} onChange={(e) => upd('buyer', e.target.value)} placeholder="Имя байера" />
        </div>
      </div>
      <div className="field">
        <label htmlFor="pt">Токен доступа</label>
        <textarea id="pt" className="input" rows={3} value={f.token} onChange={(e) => upd('token', e.target.value)}
          placeholder={profile ? `Сейчас: ${profile.token_masked}. Оставь пустым, чтобы не менять` : 'EAAB…'} spellCheck={false} autoComplete="off" />
      </div>
      <div className="field">
        <label htmlFor="pp">Прокси</label>
        <input id="pp" className="input" value={f.proxy} onChange={(e) => upd('proxy', e.target.value)} disabled={clearProxy}
          placeholder={profile?.proxy_masked ? `Сейчас: ${profile.proxy_masked}. Оставь пустым, чтобы не менять` : 'host:port:user:pass'} spellCheck={false} autoComplete="off" />
        <span className="hint">Тот же прокси, через который байер заходит в этот профиль. Форматы: host:port:user:pass, user:pass@host:port, http://… или socks5://…</span>
        {profile?.proxy_masked && (
          <label className="check"><input type="checkbox" checked={clearProxy} onChange={(e) => setClearProxy(e.target.checked)} />Убрать прокси</label>
        )}
      </div>
      <div className="field">
        <label htmlFor="pno">Заметки</label>
        <textarea id="pno" className="input" rows={2} value={f.notes} onChange={(e) => upd('notes', e.target.value)} />
      </div>
      <label className="check"><input type="checkbox" checked={f.active} onChange={(e) => upd('active', e.target.checked)} />Синхронизировать этот профиль</label>
      {check && (check.ok ? (
        <div className="notice info">
          Доступ есть: {check.user}. Видно кабинетов: {check.total}.
          {check.accounts && check.accounts.length > 0 && (
            <div className="sub" style={{ marginTop: 6 }}>
              {check.accounts.slice(0, 8).map((a) => `${a.name} (${a.status}, ${a.currency})`).join('; ')}
              {check.total! > 8 ? ` и ещё ${check.total! - 8}` : ''}
            </div>
          )}
        </div>
      ) : <div className="notice bad">Доступа нет: {check.error}</div>)}
    </Modal>
  )
}
