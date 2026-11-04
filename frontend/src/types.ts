export type Role = 'admin' | 'viewer'
export interface Me { id: number; login: string; role: Role; buyer: string }

export interface Metrics {
  spend: number; impressions: number; clicks: number; fb_results: number
  k_clicks: number; regs: number; deps: number; revenue: number; profit: number
  roi: number | null; cpr: number | null; cpd: number | null; reg2dep: number | null
  ctr: number | null; cpc: number | null; cpm: number | null
}

export interface StatRow extends Metrics {
  key: string; label: string
  supplier?: string; buyer?: string; profile?: string; account?: string
  account_id?: string; campaign_id?: string; profile_id?: number
  status?: number; missing?: boolean
}

export interface DailyPoint { day: string; spend: number; revenue: number; regs: number; deps: number }

export interface StatsResponse {
  group: string; rows: StatRow[]; total: Metrics
  unmatched: (Metrics & { sub_value: string })[]
  unknown_currencies: string[]; daily: DailyPoint[]; since: string; until: string
}

export interface MetaResponse {
  suppliers: string[]; buyers: string[]; geos: string[]; scope_buyer: string | null
  profiles: { id: number; name: string }[]; accounts: { id: string; name: string }[]
  keitaro_enabled: boolean; keitaro_sub: string; sync_minutes: number; sync_running: boolean
  last_sync: { at: string; ok: boolean | null; message: string; finished: boolean } | null
  statuses: Record<string, string>; fx_currencies: string[]
  fx_updated_at: string | null; keitaro_days_back: number
}

export interface Account {
  id: string; name: string; label: string; geo: string; status: number; status_text: string
  active: boolean; missing: boolean; disable_reason: number; currency: string; timezone: string
  amount_spent: number; spend_cap: number; balance: number; cap_used: number | null
  spend_today_usd: number; today: string; profile_id: number; profile: string; supplier: string; buyer: string
  last_error: string; updated_at: string
}

export interface Profile {
  id: number; name: string; supplier: string; buyer: string; notes: string; active: boolean
  token_ok: boolean | null; fb_user: string; token_masked: string; proxy_masked: string; key_changed: boolean
  last_sync_at: string | null; last_error: string; accounts: number; accounts_active: number
}

export interface SyncRun {
  id: number; trigger: string; days: number; ok: boolean | null; message: string
  started_at: string; finished_at: string | null
}

export interface Alert { key: string; level: 'crit' | 'warn' | 'info'; text: string; sent: boolean; created_at: string }

export interface Settings {
  fx_to_usd: Record<string, number>; keitaro_currency: string; alerts_enabled: boolean
  tg_chat_id: string; alert_spend_no_reg_usd: number; alert_cap_ratio: number; tg_bot_configured: boolean
  fx_updated_at: string | null; fx_auto: boolean; fx_manual: Record<string, number>; fx_source: string
}

export interface HealthCheck {
  name: string; level: 'ok' | 'warn' | 'fail'; text: string
  details?: Record<string, string | number | string[]>
}
export interface Health { status: 'ok' | 'warn' | 'fail'; checked_at: string; checks: HealthCheck[] }

export interface User { id: number; login: string; role: Role; buyer: string }
