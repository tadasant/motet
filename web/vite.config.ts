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
// Where `npm run dev` proxies API calls, and the second half of one number. `bin/dev`
// starts uvicorn on this port and exports MOTET_DEV_API_PORT to the Vite child, so the
// port the API listens on and the port this proxies to cannot disagree. They used to:
// the target was a literal here, so moving uvicorn off 8000 made `/v1/...` return
// index.html and fail as a JSON parse error pointing nowhere near the cause (motet#83).
//
// `process` is declared locally rather than by adding @types/node: this file is in a
// tsconfig that otherwise describes browser code, and one ambient declaration is a
// smaller change than a Node type surface the SPA's own sources would then see.
declare const process: { env: Record<string, string | undefined> }

// `||`, not `??`: an exported-but-empty MOTET_DEV_API_PORT is not a port, and `??`
// would build a target with nothing after the colon.
const DEV_API = `http://127.0.0.1:${process.env.MOTET_DEV_API_PORT || '8000'}`

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
