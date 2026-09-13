// The client-side half of domain normalization: what the field shows on blur.
//
// The API normalizes again and is the authority — this exists so the user sees
// `theinformation.com` before pressing Add, not after, and so a pasted article URL turns
// into the domain it belongs to without a round trip. Keep in step with
// `motet_db.connectors.normalize_domain`.

export function normalizeDomain(raw: string): string {
  let value = raw.trim().toLowerCase()
  const scheme = value.indexOf('://')
  if (scheme >= 0) value = value.slice(scheme + 3)
  value = value.replace(/[/?#].*$/, '')
  value = value.replace(/^.*@/, '')
  value = value.replace(/:.*$/, '')
  if (value.startsWith('www.')) value = value.slice(4)
  return value.replace(/^\.+|\.+$/g, '')
}
