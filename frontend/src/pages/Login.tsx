import { FormEvent, useState } from 'react'
import { api } from '../api'
import type { Me } from '../types'

export default function LoginPage({ onLogin }: { onLogin: (m: Me) => void }) {
  const [login, setLogin] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      onLogin(await api<Me>('/api/auth/login', { method: 'POST', body: { login, password } }))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <form className="panel" onSubmit={submit}>
        <div>
          <h1>Кабинеты FB</h1>
          <div className="sub">Сводка по всем агентским кабинетам</div>
        </div>
        <div className="field">
          <label htmlFor="l">Логин</label>
          <input id="l" className="input" autoComplete="username" value={login} onChange={(e) => setLogin(e.target.value)} required autoFocus />
        </div>
        <div className="field">
          <label htmlFor="p">Пароль</label>
          <input id="p" className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </div>
        {error && <div className="notice bad">{error}</div>}
        <button className="btn primary" disabled={busy}>{busy ? 'Вхожу…' : 'Войти'}</button>
      </form>
    </div>
  )
}
