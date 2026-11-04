import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Bar, CartesianGrid, ComposedChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, qs } from '../api'
import { useMe } from '../App'
import { ErrorBox, ProfitBar, Spinner, StatusBadge, useLoad, useToast } from '../components/ui'
import { ago, int, isoDay, money, pct, shiftDays, shortDay, signedMoney } from '../format'
import type { MetaResponse, StatRow, StatsResponse } from '../types'

const ALL_GROUPS: { id: string; label: string }[] = [
  { id: 'supplier', label: 'Supplier' },
  { id: 'buyer', label: 'Buyer' },
  { id: 'geo', label: 'Geo' },
  { id: 'profile', label: 'Profile' },
  { id: 'account', label: 'Ad account' },
  { id: 'campaign', label: 'Campaign' },
  { id: 'day', label: 'Day' },
]
const DRILL: Record<string, { param: string; field: keyof StatRow; next: string }> = {
  supplier: { param: 'supplier', field: 'key', next: 'profile' },
  buyer: { param: 'buyer', field: 'key', next: 'account' },
  geo: { param: 'geo', field: 'key', next: 'account' },
  profile: { param: 'profile_id', field: 'profile_id', next: 'account' },
  account: { param: 'account_id', field: 'account_id', next: 'campaign' },
}

function presets() {
  const t = new Date()
  const first = new Date(t.getFullYear(), t.getMonth(), 1)
  return [
    { id: 'today', label: 'Today', since: isoDay(t), until: isoDay(t) },
    { id: 'yesterday', label: 'Yesterday', since: isoDay(shiftDays(t, -1)), until: isoDay(shiftDays(t, -1)) },
    { id: '7d', label: '7 days', since: isoDay(shiftDays(t, -6)), until: isoDay(t) },
    { id: '30d', label: '30 days', since: isoDay(shiftDays(t, -29)), until: isoDay(t) },
    { id: 'month', label: 'This month', since: isoDay(first), until: isoDay(t) },
  ]
}

type SortKey = keyof StatRow
type Col = { key: SortKey; label: string; render: (r: StatRow) => React.ReactNode; fb?: boolean; title?: string }

export default function DashboardPage() {
  const me = useMe()
  const toast = useToast()
  const GROUPS = me.buyer && me.role !== 'admin' ? ALL_GROUPS.filter((g) => g.id !== 'buyer') : ALL_GROUPS
  const [sp, setSp] = useSearchParams()
  const pr = presets() // recomputed every render: a tab left open overnight must not keep yesterday's "today"
  const since = sp.get('since') || pr[2].since
  const until = sp.get('until') || pr[2].until
  const group = sp.get('group') || 'account'
  const filters = {
    supplier: sp.get('supplier') || '', buyer: sp.get('buyer') || '',
    profile_id: sp.get('profile_id') || '', account_id: sp.get('account_id') || '', geo: sp.get('geo') || '',
  }
  const [showFb, setShowFb] = useState(false)
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'spend', dir: -1 })
  const [showUnmatched, setShowUnmatched] = useState(false)

  // drill-down pushes history so the browser Back button walks back up; filter tweaks replace
  const set = (patch: Record<string, string>, push = false) => {
    const next = new URLSearchParams(sp)
    Object.entries(patch).forEach(([k, v]) => (v ? next.set(k, v) : next.delete(k)))
    setSp(next, { replace: !push })
  }

  const meta = useLoad(() => api<MetaResponse>('/api/meta'), [])
  const query = qs({ since, until, group, ...filters })
  const stats = useLoad(() => api<StatsResponse>(`/api/stats${query}`), [query])

  // poll while a sync is running, then refresh numbers; also refresh every 5 minutes
  const wasRunning = useRef(false)
  useEffect(() => {
    const running = meta.data?.sync_running
    if (wasRunning.current && !running) stats.reload()
    wasRunning.current = !!running
    const t = setTimeout(meta.reload, running ? 5000 : 300000)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta.data])

  const syncNow = async () => {
    try {
      await api('/api/sync', { method: 'POST', body: { days: 3 } })
      toast('Sync started')
      meta.reload()
    } catch (e) {
      toast((e as Error).message, 'error')
    }
  }

  const keitaroOn = meta.data?.keitaro_enabled ?? true
  const rows = useMemo(() => {
    const r = [...(stats.data?.rows || [])]
    r.sort((a, b) => {
      const av = a[sort.key] ?? -Infinity, bv = b[sort.key] ?? -Infinity
      return (av > bv ? 1 : av < bv ? -1 : 0) * sort.dir
    })
    return r
  }, [stats.data, sort])
  const scale = useMemo(() => Math.max(1, ...rows.map((r) => Math.abs(r.profit))), [rows])

  const drill = (r: StatRow) => {
    const d = DRILL[group]
    if (!d) return
    const value = r[d.field]
    if (value === undefined || value === null) return
    set({ [d.param]: String(value), group: d.next }, true)
  }

  const cols: Col[] = [
    { key: 'spend', label: 'Spend', render: (r) => money(r.spend) },
    { key: 'impressions', label: 'Impressions', render: (r) => int(r.impressions), fb: true },
    { key: 'clicks', label: 'Clicks', render: (r) => int(r.clicks), fb: true },
    { key: 'ctr', label: 'CTR', render: (r) => pct(r.ctr), fb: true },
    { key: 'cpc', label: 'CPC', render: (r) => money(r.cpc, 2), fb: true },
    { key: 'cpm', label: 'CPM', render: (r) => money(r.cpm, 2), fb: true },
    { key: 'fb_results', label: 'FB leads', render: (r) => int(r.fb_results), fb: true, title: 'As the pixel sees them' },
    { key: 'k_clicks', label: 'Tracker clicks', render: (r) => int(r.k_clicks), fb: true },
    { key: 'regs', label: 'Regs', render: (r) => int(r.regs) },
    { key: 'deps', label: 'Deps', render: (r) => int(r.deps) },
    { key: 'cpr', label: 'Cost per reg', render: (r) => money(r.cpr, 2) },
    { key: 'cpd', label: 'Cost per dep', render: (r) => money(r.cpd, 2) },
    { key: 'reg2dep', label: 'Reg-to-dep', render: (r) => pct(r.reg2dep) },
    { key: 'revenue', label: 'Revenue', render: (r) => money(r.revenue) },
  ]
  const visible = cols.filter((c) => showFb || !c.fb)
  const t = stats.data?.total
  const activeFilters = Object.entries(filters).filter(([, v]) => v)
  const filterName = (k: string, v: string) => {
    if (k === 'profile_id') return meta.data?.profiles.find((p) => String(p.id) === v)?.name || v
    if (k === 'account_id') return meta.data?.accounts.find((a) => a.id === v)?.name || v
    return v
  }
  const groupLabel = GROUPS.find((g) => g.id === group)?.label || ''

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Overview</h1>
        <div className="row sub">
          {meta.data?.sync_running ? (
            <span className="row"><Spinner /> Sync in progress…</span>
          ) : (
            <span>Updated {ago(meta.data?.last_sync?.at)}</span>
          )}
          {me.role === 'admin' && (
            <button className="btn small" onClick={syncNow} disabled={!!meta.data?.sync_running}>Refresh now</button>
          )}
        </div>
      </div>

      {/* filters */}
      <div className="filters">
        <div className="seg" role="group" aria-label="Period">
          {pr.map((p) => (
            <button key={p.id} aria-pressed={since === p.since && until === p.until}
              onClick={() => set({ since: p.since, until: p.until })}>{p.label}</button>
          ))}
        </div>
        <div className="dates">
          <input className="input" type="date" value={since} max={until} aria-label="From"
            onChange={(e) => e.target.value && set({ since: e.target.value })} />
          <span className="sub">—</span>
          <input className="input" type="date" value={until} min={since} aria-label="To"
            onChange={(e) => e.target.value && set({ until: e.target.value })} />
        </div>
        <select className="input" value={filters.supplier} onChange={(e) => set({ supplier: e.target.value })} aria-label="Supplier">
          <option value="">All suppliers</option>
          {meta.data?.suppliers.map((s) => <option key={s}>{s}</option>)}
        </select>
        {meta.data?.scope_buyer ? (
          <span className="badge" title="Your access is limited to this buyer's profiles">Buyer: {meta.data.scope_buyer}</span>
        ) : (
          <select className="input" value={filters.buyer} onChange={(e) => set({ buyer: e.target.value })} aria-label="Buyer">
            <option value="">All buyers</option>
            {meta.data?.buyers.map((s) => <option key={s}>{s}</option>)}
          </select>
        )}
        <select className="input" value={filters.geo} onChange={(e) => set({ geo: e.target.value })} aria-label="Geo">
          <option value="">All geos</option>
          {meta.data?.geos.map((s) => <option key={s}>{s}</option>)}
        </select>
        <select className="input" value={filters.profile_id} onChange={(e) => set({ profile_id: e.target.value })} aria-label="Profile">
          <option value="">All profiles</option>
          {meta.data?.profiles.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <select className="input" value={filters.account_id} onChange={(e) => set({ account_id: e.target.value })} aria-label="Ad account">
          <option value="">All ad accounts</option>
          {meta.data?.accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        {activeFilters.length > 0 && (
          <button className="btn ghost small" onClick={() => set({ supplier: '', buyer: '', profile_id: '', account_id: '', geo: '' })}>
            Reset filters
          </button>
        )}
      </div>

      {meta.data && !meta.data.keitaro_enabled && (
        <div className="notice info">Keitaro is not connected — registrations, deposits and profit will be empty. Add KEITARO_URL and KEITARO_API_KEY to the service variables.</div>
      )}
      {meta.data?.fx_updated_at && fxAgeDays(meta.data.fx_updated_at) > 45 && me.role === 'admin' && (
        <div className="notice">Currency rates haven't been updated for {fxAgeDays(meta.data.fx_updated_at)} days — check them in Settings, otherwise non-USD spend uses an outdated rate.</div>
      )}
      {stats.data && stats.data.unknown_currencies.length > 0 && (
        <div className="notice">
          No rate for {stats.data.unknown_currencies.join(', ')} — spend in those ad accounts is counted as USD.
          {me.role === 'admin' ? ' Add the rate in Settings.' : ''}
        </div>
      )}
      {stats.error && <ErrorBox text={stats.error} onRetry={stats.reload} />}

      {/* ledger */}
      {t && (
        <section className="panel ledger" aria-label="Period total">
          <div className="ledger-result">
            <div className="cap">Profit for the period</div>
            <div className={`big num ${t.profit > 0 ? 'gain' : t.profit < 0 ? 'loss' : ''}`}>{signedMoney(t.profit)}</div>
            <div className="eq num">{money(t.revenue)} revenue − {money(t.spend)} spend</div>
            <div className="eq">ROI {pct(t.roi)}</div>
          </div>
          <div className="ledger-figs">
            <Fig k="Spend" v={money(t.spend)} />
            <Fig k="Registrations" v={int(t.regs)} />
            <Fig k="Deposits" v={int(t.deps)} />
            <Fig k="Reg-to-dep" v={pct(t.reg2dep)} />
            <Fig k="Cost per reg" v={money(t.cpr, 2)} />
            <Fig k="Cost per dep" v={money(t.cpd, 2)} />
            <Fig k="FB clicks" v={int(t.clicks)} />
            <Fig k="CPC" v={money(t.cpc, 2)} />
          </div>
        </section>
      )}

      {/* chart */}
      {stats.data && stats.data.daily.length > 1 && (
        <section className="panel panel-pad">
          <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>By day</h2>
            <div className="legend">
              <span><i style={{ background: 'var(--faint)' }} />Spend</span>
              <span><i style={{ background: 'var(--accent)' }} />Revenue</span>
            </div>
          </div>
          <div className="chart-wrap">
            <ResponsiveContainer>
              <ComposedChart data={stats.data.daily} margin={{ top: 4, right: 0, left: -8, bottom: 0 }} barGap={2}>
                <CartesianGrid vertical={false} stroke="var(--line)" />
                <XAxis dataKey="day" tickFormatter={shortDay} tick={{ fill: 'var(--muted)', fontSize: 12 }} axisLine={false} tickLine={false} minTickGap={12} />
                <YAxis tickFormatter={(v) => `$${v}`} tick={{ fill: 'var(--muted)', fontSize: 12 }} axisLine={false} tickLine={false} width={56} />
                <Tooltip
                  cursor={{ fill: 'var(--surface-2)' }}
                  contentStyle={{ background: 'var(--surface)', border: '1px solid var(--line)', borderRadius: 8, color: 'var(--ink)' }}
                  labelFormatter={(d) => shortDay(String(d))}
                  formatter={(v: number, name: string) => [money(v), name === 'spend' ? 'Spend' : 'Revenue']}
                />
                <Bar dataKey="spend" fill="var(--faint)" radius={[3, 3, 0, 0]} maxBarSize={22} />
                <Bar dataKey="revenue" fill="var(--accent)" radius={[3, 3, 0, 0]} maxBarSize={22} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </section>
      )}

      {/* table */}
      <section className="panel">
        <div className="panel-pad row" style={{ justifyContent: 'space-between' }}>
          <div className="seg" role="group" aria-label="Group by">
            {GROUPS.map((g) => (
              <button key={g.id} aria-pressed={group === g.id} onClick={() => set({ group: g.id })}>{g.label}</button>
            ))}
          </div>
          <div className="row">
            <label className="check"><input type="checkbox" checked={showFb} onChange={(e) => setShowFb(e.target.checked)} />FB metrics</label>
            <a className="btn small" href={`/api/stats/export.csv${query}`}>Download CSV</a>
          </div>
        </div>
        {activeFilters.length > 0 && (
          <div className="panel-pad sub" style={{ paddingTop: 0 }}>
            Filter: {activeFilters.map(([k, v]) => filterName(k, v)).join(', ')}
          </div>
        )}
        <div className="table-scroll">
          <table className="t">
            <thead>
              <tr>
                <th>{groupLabel}</th>
                {visible.map((c) => (
                  <th key={c.key} className="n sortable" title={c.title}
                    aria-sort={sort.key === c.key ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'}
                    onClick={() => setSort((s) => ({ key: c.key, dir: s.key === c.key ? (-s.dir as 1 | -1) : -1 }))}>
                    {c.label}{sort.key === c.key && <span className="arrow">{sort.dir === 1 ? '↑' : '↓'}</span>}
                  </th>
                ))}
                <th aria-hidden="true" />
                <th className="n sortable" onClick={() => setSort((s) => ({ key: 'profit', dir: s.key === 'profit' ? (-s.dir as 1 | -1) : -1 }))}>
                  Profit{sort.key === 'profit' && <span className="arrow">{sort.dir === 1 ? '↑' : '↓'}</span>}
                </th>
                <th className="n sortable" onClick={() => setSort((s) => ({ key: 'roi', dir: s.key === 'roi' ? (-s.dir as 1 | -1) : -1 }))}>
                  ROI{sort.key === 'roi' && <span className="arrow">{sort.dir === 1 ? '↑' : '↓'}</span>}
                </th>
              </tr>
            </thead>
            <tbody>
              {stats.loading && !stats.data && (
                <tr><td colSpan={visible.length + 4}><div className="empty"><Spinner /></div></td></tr>
              )}
              {stats.data && rows.length === 0 && (
                <tr><td colSpan={visible.length + 4}>
                  <div className="empty">
                    <strong>No spend in this period</strong>
                    {meta.data?.profiles.length ? 'Pick another period or reset the filters.' : 'First add an agency profile in Profiles.'}
                  </div>
                </td></tr>
              )}
              {rows.map((r) => {
                const burning = keitaroOn && r.spend >= 10 && r.regs === 0 && group !== 'day'
                return (
                  <tr key={r.key}>
                    <td className="name">
                      {DRILL[group] ? (
                        <button className="btn ghost small" style={{ padding: 0, height: 'auto', fontWeight: 600 }} onClick={() => drill(r)} title="Drill down">
                          {r.label}
                        </button>
                      ) : <span style={{ fontWeight: 600 }}>{r.label}</span>}
                      {group === 'account' && r.status !== undefined && (
                        <> <StatusBadge active={[1, 201].includes(r.status)} missing={r.missing}
                          text={meta.data?.statuses[String(r.status)] || String(r.status)} /></>
                      )}
                      {burning && <> <span className="flag" title="Spending with no registrations">· no regs</span></>}
                      <small>
                        {group === 'campaign' && `${r.account} · ${r.key}`}
                        {group === 'account' && `${r.profile} · ${r.supplier || 'no supplier'}`}
                        {group === 'profile' && `${r.supplier || 'no supplier'}${r.buyer ? ' · ' + r.buyer : ''}`}
                      </small>
                    </td>
                    {visible.map((c) => <td key={c.key} className="n">{c.render(r)}</td>)}
                    <td className="bar"><ProfitBar profit={r.profit} scale={scale} /></td>
                    <td className={`n ${r.profit > 0 ? 'gain' : r.profit < 0 ? 'loss' : ''}`}>{signedMoney(r.profit)}</td>
                    <td className="n">{pct(r.roi)}</td>
                  </tr>
                )
              })}
            </tbody>
            {t && rows.length > 1 && (
              <tfoot>
                <tr>
                  <td>Total</td>
                  {visible.map((c) => <td key={c.key} className="n">{c.render({ ...t, key: '', label: '' })}</td>)}
                  <td />
                  <td className={`n ${t.profit > 0 ? 'gain' : t.profit < 0 ? 'loss' : ''}`}>{signedMoney(t.profit)}</td>
                  <td className="n">{pct(t.roi)}</td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      </section>

      {stats.data && stats.data.unmatched.length > 0 && (
        <section className="panel panel-pad">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div>
              <h2 style={{ margin: 0 }}>Conversions without an FB campaign</h2>
              <div className="sub">
                {int(stats.data.unmatched.reduce((a, u) => a + u.regs, 0))} regs and {int(stats.data.unmatched.reduce((a, u) => a + u.deps, 0))} deps in Keitaro
                didn't match any campaign. Check that the ad link has {meta.data?.keitaro_sub} = {'{{campaign.id}}'}.
              </div>
            </div>
            <button className="btn small" onClick={() => setShowUnmatched((x) => !x)}>{showUnmatched ? 'Hide' : 'Show'}</button>
          </div>
          {showUnmatched && (
            <div className="table-scroll" style={{ marginTop: 12 }}>
              <table className="t">
                <thead><tr><th>{meta.data?.keitaro_sub} value</th><th className="n">Clicks</th><th className="n">Regs</th><th className="n">Deps</th><th className="n">Revenue</th></tr></thead>
                <tbody>
                  {stats.data.unmatched.map((u) => (
                    <tr key={u.sub_value}><td>{u.sub_value}</td><td className="n">{int(u.k_clicks)}</td><td className="n">{int(u.regs)}</td><td className="n">{int(u.deps)}</td><td className="n">{money(u.revenue)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  )
}

function fxAgeDays(iso: string): number {
  return Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)
}

function Fig({ k, v }: { k: string; v: string }) {
  return <div className="fig"><div className="k">{k}</div><div className="v num">{v}</div></div>
}
