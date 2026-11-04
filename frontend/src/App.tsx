import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { Navigate, NavLink, Route, Routes } from 'react-router-dom'
import { api } from './api'
import { Spinner } from './components/ui'
import AccountsPage from './pages/Accounts'
import DashboardPage from './pages/Dashboard'
import LoginPage from './pages/Login'
import ProfilesPage from './pages/Profiles'
import SettingsPage from './pages/Settings'
import SyncPage from './pages/Sync'
import type { Me } from './types'

const MeCtx = createContext<Me | null>(null)
export const useMe = () => useContext(MeCtx)!

function Logo() {
  return (
    <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="var(--accent)" />
      <path d="M8 20h4v4H8zM14 14h4v10h-4zM20 8h4v16h-4z" fill="var(--accent-ink)" />
    </svg>
  )
}

export default function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined)

  const load = useCallback(() => {
    api<Me>('/api/auth/me').then(setMe).catch(() => setMe(null))
  }, [])
  useEffect(load, [load])
  useEffect(() => {
    const out = () => setMe(null)
    window.addEventListener('auth:logout', out)
    return () => window.removeEventListener('auth:logout', out)
  }, [])

  if (me === undefined) return <div className="login"><Spinner /></div>
  if (me === null) return <LoginPage onLogin={setMe} />

  const logout = async () => {
    await api('/api/auth/logout', { method: 'POST' }).catch(() => {})
    setMe(null)
  }

  return (
    <MeCtx.Provider value={me}>
      <header className="topbar">
        <div className="brand"><Logo /><span>FB Accounts</span></div>
        <nav className="nav" aria-label="Sections">
          <NavLink to="/" end>Overview</NavLink>
          <NavLink to="/accounts">Ad accounts</NavLink>
          <NavLink to="/profiles">Profiles</NavLink>
          <NavLink to="/sync">Sync</NavLink>
          {me.role === 'admin' && <NavLink to="/settings">Settings</NavLink>}
        </nav>
        <div className="who">
          <span className="login-name">{me.login}</span>
          <button className="btn small" onClick={logout}>Log out</button>
        </div>
      </header>
      <main className="page">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/accounts" element={<AccountsPage />} />
          <Route path="/profiles" element={<ProfilesPage />} />
          <Route path="/sync" element={<SyncPage />} />
          <Route path="/settings" element={me.role === 'admin' ? <SettingsPage /> : <Navigate to="/" />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
    </MeCtx.Provider>
  )
}
