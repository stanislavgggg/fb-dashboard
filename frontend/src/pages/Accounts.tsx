import { useMemo, useState } from 'react'
import { api } from '../api'
import { useMe } from '../App'
import { ErrorBox, Modal, Spinner, StatusBadge, useLoad, useToast } from '../components/ui'
import { ago, money, native } from '../format'
import type { Account } from '../types'

type Filter = 'all' | 'active' | 'problem'

export default function AccountsPage() {
  const me = useMe()
  const toast = useToast()
  const { data, error, loading, reload } = useLoad(() => api<Account[]>('/api/accounts'), [])
  const [q, setQ] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [edit, setEdit] = useState<Account | null>(null)

  const counts = useMemo(() => {
    const all = data || []
    return {
      all: all.length,
      active: all.filter((a) => a.active).length,
      problem: all.filter((a) => !a.active || a.last_error).length,
    }
  }, [data])

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (data || []).filter((a) => {
      if (filter === 'active' && !a.active) return false
      if (filter === 'problem' && a.active && !a.last_error) return false
      if (!needle) return true
      return [a.name, a.id, a.label, a.geo, a.profile, a.supplier, a.buyer].some((x) => x?.toLowerCase().includes(needle))
    })
  }, [data, q, filter])

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Ad accounts</h1>
        <span className="sub">{counts.active} of {counts.all} active</span>
      </div>
      <div className="filters">
        <div className="seg" role="group" aria-label="Status">
          <button aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>All {counts.all}</button>
          <button aria-pressed={filter === 'active'} onClick={() => setFilter('active')}>Active {counts.active}</button>
          <button aria-pressed={filter === 'problem'} onClick={() => setFilter('problem')}>Problems {counts.problem}</button>
        </div>
        <input className="input" placeholder="Search: name, ID, geo, supplier" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 320 }} />
      </div>
      {error && <ErrorBox text={error} onRetry={reload} />}
      <section className="panel table-scroll">
        <table className="t">
          <thead>
            <tr>
              <th>Ad account</th><th>Status</th><th>Geo</th><th>Profile</th>
              <th className="n">Spend today</th><th className="n">Lifetime spend</th>
              <th className="n">Spend cap</th><th className="n">Balance</th><th>Updated</th>
              {me.role === 'admin' && <th />}
            </tr>
          </thead>
          <tbody>
            {loading && !data && <tr><td colSpan={10}><div className="empty"><Spinner /></div></td></tr>}
            {data && rows.length === 0 && (
              <tr><td colSpan={10}><div className="empty">
                <strong>{data.length ? 'Nothing found' : 'No ad accounts yet'}</strong>
                {data.length ? 'Change the search or filter.' : 'They appear after the first profile sync.'}
              </div></td></tr>
            )}
            {rows.map((a) => (
              <tr key={a.id}>
                <td className="name">
                  <span style={{ fontWeight: 600 }}>{a.name}</span>
                  <small>{a.id}{a.label ? ` · ${a.label}` : ''}</small>
                  {a.last_error && <div className="err-text">{a.last_error}</div>}
                </td>
                <td><StatusBadge active={a.active} missing={a.missing} text={a.status_text} /></td>
                <td>{a.geo || <span className="sub">—</span>}</td>
                <td className="name">{a.profile}<small>{a.supplier || 'no supplier'}{a.buyer ? ` · ${a.buyer}` : ''}</small></td>
                <td className="n" title={a.timezone ? `Today in the account time zone (${a.timezone}): ${a.today}` : undefined}>
                  {money(a.spend_today_usd)}
                  {a.timezone && !a.timezone.startsWith('Europe/') && <small className="sub" style={{ display: 'block' }}>{a.timezone.split('/').pop()}</small>}
                </td>
                <td className="n">{native(a.amount_spent, a.currency)}</td>
                <td className="n">
                  {a.spend_cap ? (
                    <span className={a.cap_used !== null && a.cap_used >= 90 ? 'flag' : ''}>
                      {a.cap_used}% of {native(a.spend_cap, a.currency)}
                    </span>
                  ) : <span className="sub">none</span>}
                </td>
                <td className="n">{a.balance ? native(a.balance, a.currency) : '—'}</td>
                <td className="sub">{ago(a.updated_at)}</td>
                {me.role === 'admin' && <td><button className="btn small" onClick={() => setEdit(a)}>Edit</button></td>}
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {edit && (
        <EditAccount account={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); reload(); toast('Ad account saved') }} />
      )}
    </div>
  )
}

function EditAccount({ account, onClose, onSaved }: { account: Account; onClose: () => void; onSaved: () => void }) {
  const toast = useToast()
  const [label, setLabel] = useState(account.label)
  const [geo, setGeo] = useState(account.geo)
  const [busy, setBusy] = useState(false)
  const save = async () => {
    setBusy(true)
    try {
      await api(`/api/accounts/${account.id}`, { method: 'PATCH', body: { label, geo } })
      onSaved()
    } catch (e) {
      toast((e as Error).message, 'error')
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal title={account.name} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" onClick={save} disabled={busy}>Save</button></>}>
      <div className="field">
        <label htmlFor="geo">Geo</label>
        <input id="geo" className="input" value={geo} onChange={(e) => setGeo(e.target.value)} placeholder="LT" maxLength={40} />
      </div>
      <div className="field">
        <label htmlFor="lbl">Note</label>
        <input id="lbl" className="input" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. casino, funnel #3" maxLength={200} />
        <span className="hint">Only visible here, nothing changes in Facebook.</span>
      </div>
    </Modal>
  )
}
