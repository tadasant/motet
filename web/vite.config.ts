import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// The SPA is built to static files and served by nginx on Cloud Run (web/Dockerfile). It
// talks to the Motet API and to nothing else — invariant 1: the client never speaks a
// vendor protocol.
//
// The API's origin is deliberately NOT configured here. Vite inlines `import.meta.env` at
// build time, so an origin set at build time would mean one image per environment; the
// deployed bundle reads it from `/config.js`, which the container writes at start-up.
// See web/src/api/client.ts.
// Where `npm run dev` proxies API calls. Hardcoded rather than read from the
// environment: reading `process.env` here would mean pulling Node's type definitions
// into a tsconfig that otherwise describes browser code, which is a bigger change than
// this one line deserves.
const DEV_API = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  // In DEV the SPA uses same-origin relative paths — no `/config.js` value is set, so
  // there is no API origin to prefix — and this proxy is what stands in for the
  // deployment's two-hostname routing. Without it, `npm run dev` reaches Vite for
  // `/v1/...` and gets index.html back, which surfaces as a JSON parse error rather than
  // as anything that points at the real cause.
  //
  // Deployed, this does not apply: `app.` and `api.` are different origins, the browser
  // calls the API directly, and CORS on the API is what permits it.
  server: {
    proxy: {
      '/v1': DEV_API,
      '/internal/health': DEV_API,
      '/feed.xml': DEV_API,
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
  },
})
