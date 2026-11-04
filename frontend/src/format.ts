const nf0 = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 })
const nf2 = new Intl.NumberFormat('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export const money = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined ? '—' : `$${(digits ? nf2 : nf0).format(v)}`

export const signedMoney = (v: number) => (v > 0 ? '+' : v < 0 ? '−' : '') + money(Math.abs(v))

export const int = (v: number | null | undefined) => (v === null || v === undefined ? '—' : nf0.format(v))

export const pct = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${v.toFixed(1)}%`)

export const native = (v: number, cur: string) => `${nf0.format(v)} ${cur}`

export function ago(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`
  return new Date(iso).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

// local YYYY-MM-DD (not UTC — avoids the "yesterday after midnight" bug)
export function isoDay(d: Date): string {
  const z = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`
}

export function shiftDays(d: Date, n: number): Date {
  const c = new Date(d)
  c.setDate(c.getDate() + n)
  return c
}

export function shortDay(iso: string): string {
  const [, m, d] = iso.split('-')
  return `${Number(d)}.${m}`
}
