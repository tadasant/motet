import { describe, expect, it } from 'vitest'

import { isSafeClientRedirect } from './oauth'

describe('isSafeClientRedirect', () => {
  it('allows https, loopback http, and a desktop client scheme', () => {
    expect(isSafeClientRedirect('https://claude.example/callback?code=c')).toBe(true)
    expect(isSafeClientRedirect('http://localhost:33418/callback?code=c')).toBe(true)
    expect(isSafeClientRedirect('http://127.0.0.1:9000/cb')).toBe(true)
    expect(isSafeClientRedirect('vscode://publisher.extension/callback?code=c')).toBe(true)
  })

  it('refuses anything that would run or render in this origin', () => {
    expect(isSafeClientRedirect('javascript:alert(document.domain)//?code=c')).toBe(false)
    expect(isSafeClientRedirect('JavaScript:alert(1)')).toBe(false)
    expect(isSafeClientRedirect('data:text/html,<script>alert(1)</script>')).toBe(false)
    expect(isSafeClientRedirect('vbscript:msgbox(1)')).toBe(false)
    expect(isSafeClientRedirect('file:///etc/passwd')).toBe(false)
    expect(isSafeClientRedirect('blob:https://app.example/1')).toBe(false)
  })

  it('refuses plain http off loopback, and anything that is not a URL', () => {
    expect(isSafeClientRedirect('http://attacker.example/cb')).toBe(false)
    expect(isSafeClientRedirect('not a url')).toBe(false)
    expect(isSafeClientRedirect('')).toBe(false)
  })
})
