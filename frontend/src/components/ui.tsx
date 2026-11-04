import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from 'react'

// ---------------------------------------------------------------- toasts
type Toast = { id: number; text: string; kind: 'ok' | 'error' }
const ToastCtx = createContext<(text: string, kind?: 'ok' | 'error') => void>(() => {})

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])
  const seq = useRef(0)
  const push = useCallback((text: string, kind: 'ok' | 'error' = 'ok') => {
    const id = ++seq.current
    setItems((x) => [...x, { id, text, kind }])
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), kind === 'error' ? 7000 : 3500)
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind === 'error' ? 'error' : ''}`}>{t.text}</div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}
export const useToast = () => useContext(ToastCtx)

// ---------------------------------------------------------------- modal
export function Modal({ title, onClose, children, footer }: {
  title: string; onClose: () => void; children: ReactNode; footer?: ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
    }
  }, [onClose])
  return (
    <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="btn ghost small" onClick={onClose} aria-label="Закрыть">✕</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- data loading hook
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const req = useRef(0)
  const reload = useCallback(() => {
    const id = ++req.current
    setLoading(true)
    fn()
      .then((d) => { if (id === req.current) { setData(d); setError(null) } })
      .catch((e: Error) => { if (id === req.current) setError(e.message) })
      .finally(() => { if (id === req.current) setLoading(false) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => { reload() }, [reload])
  return { data, error, loading, reload }
}

// ---------------------------------------------------------------- misc
export function Spinner() {
  return <span className="spin" aria-label="Загрузка" />
}

export function ProfitBar({ profit, scale }: { profit: number; scale: number }) {
  const w = scale > 0 ? Math.min(50, (Math.abs(profit) / scale) * 50) : 0
  return (
    <span className="pbar" title={profit >= 0 ? 'В плюсе' : 'В минусе'}>
      {profit !== 0 && <span className={profit > 0 ? 'g' : 'l'} style={{ width: `${w}%` }} />}
    </span>
  )
}

export function StatusBadge({ active, missing, text }: { active: boolean; missing?: boolean; text: string }) {
  if (missing) return <span className="badge warn">Нет доступа</span>
  return <span className={`badge ${active ? 'ok' : 'bad'}`}>{text}</span>
}

export function ErrorBox({ text, onRetry }: { text: string; onRetry?: () => void }) {
  return (
    <div className="notice bad row" style={{ justifyContent: 'space-between' }}>
      <span>{text}</span>
      {onRetry && <button className="btn small" onClick={onRetry}>Повторить</button>}
    </div>
  )
}
