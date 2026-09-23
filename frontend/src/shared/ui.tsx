import { useEffect, useRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { X } from 'lucide-react';

export function Button({ children, className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button type="button" className={`button ${className}`} {...props}>{children}</button>;
}
export function IconButton({ label, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return <button type="button" className="icon-button" aria-label={label} title={label} {...props}>{children}</button>;
}
export function Avatar({ name }: { name: string }) { return <span className="avatar" aria-hidden="true">{name.slice(0, 1).toUpperCase()}</span>; }
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><h2>{title}</h2>{children && <p>{children}</p>}</div>;
}
export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <div className="field-row"><div className="field-label">{label}</div><div className="field-control">{children}{hint && <p className="hint">{hint}</p>}</div></div>;
}
export function Dialog({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = ref.current!; dialog.showModal(); return () => dialog.close(); }, []);
  return <dialog ref={ref} className="dialog" aria-label={title} onCancel={(event) => { event.preventDefault(); close(); }} onClick={(event) => { if (event.target === event.currentTarget) close(); }}>
    <div className="dialog-heading"><h2>{title}</h2><IconButton label="关闭" onClick={close}><X /></IconButton></div>{children}
  </dialog>;
}
