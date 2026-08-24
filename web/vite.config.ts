import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// The SPA is built to static files and served by Cloudflare. It talks to the Motet API
// and to nothing else — invariant 1: the client never speaks a vendor protocol.
// Where `npm run dev` proxies API calls. Hardcoded rather than read from the
// environment: reading `process.env` here would mean pulling Node's type definitions
// into a tsconfig that otherwise describes browser code, which is a bigger change than
// this one line deserves.
const DEV_API = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  // The SPA talks to the API over same-origin relative paths, so the dev server has to
  // stand in for the deployment's routing. Without this, `npm run dev` reaches Vite for
  // `/v1/...` and gets index.html back — which surfaces as a JSON parse error rather
  // than as anything that points at the real cause.
  server: {
    proxy: {
      '/v1': DEV_API,
      '/healthz': DEV_API,
      '/feed.xml': DEV_API,
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
  },
})
