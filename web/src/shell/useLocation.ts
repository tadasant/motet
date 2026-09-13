// The URL as state, and nothing more.
//
// Still not a router. A router owns matching, nesting, loaders and a link component; this
// owns one string — `window.location.pathname` — and keeps a React state in step with it
// through `pushState` and `popstate`. Which section a path means is decided in
// `sections.tsx`, and the callback path is still read by `oauth.ts`, once, at boot.
//
// The reason the URL now reflects the section at all is the realistic thing a person does
// while a multi-minute pipeline runs: reload. A tab strip held in component state lost
// its place on every reload (the shape of motet#44); a path does not.

import { useCallback, useEffect, useState } from 'react'

/** Trailing slashes off, so `/backlog/` and `/backlog` are one place. */
export function normalizePath(pathname: string): string {
  const trimmed = pathname.replace(/\/+$/, '')
  return trimmed === '' ? '/' : trimmed
}

export function usePath(): [string, (to: string, options?: { replace?: boolean }) => void] {
  const [path, setPath] = useState(() => normalizePath(window.location.pathname))

  useEffect(() => {
    const onPop = () => setPath(normalizePath(window.location.pathname))
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const navigate = useCallback((to: string, options: { replace?: boolean } = {}) => {
    const next = normalizePath(to)
    if (next !== normalizePath(window.location.pathname)) {
      if (options.replace) window.history.replaceState({}, '', next)
      else window.history.pushState({}, '', next)
    }
    setPath(next)
  }, [])

  return [path, navigate]
}
