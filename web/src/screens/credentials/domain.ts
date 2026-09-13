// The client-side half of domain normalization: what the field shows on blur.
//
// The API normalizes again and is the authority — this exists so the owner sees
// `example.com` before pressing Add rather than after, and so a pasted article URL turns
// into the site it belongs to without a round trip. Keep in step with
// `motet_db.connectors.normalize_domain`.

export function normalizeDomain(raw: string): string {
  let value = raw.trim().toLowerCase()
  const scheme = value.indexOf('://')
  if (scheme >= 0) value = value.slice(scheme + 3)
  value = value.replace(/[/?#].*$/, '')
  value = value.replace(/^.*@/, '')
  value = value.replace(/:.*$/, '')
  value = value.replace(/^\.+|\.+$/g, '')
  if (value.startsWith('www.')) value = value.slice(4)
  return value
}

/** A host name with at least one dot and a letter-led top label — never an IP literal. */
export function looksLikeDomain(value: string): boolean {
  return /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(value)
}
