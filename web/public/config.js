// Runtime configuration for the SPA. Vite copies `public/` verbatim into the build, so
// this file ships as `/config.js` and index.html loads it before the bundle.
//
// THIS COPY IS THE DEVELOPMENT DEFAULT and sets nothing: `npm run dev` and `npm run
// preview` want same-origin paths, because vite.config.ts proxies /v1 to the local API.
//
// In a deployed environment the web container's entrypoint OVERWRITES it with the real
// API origin (see web/docker-entrypoint.d/10-motet-config.sh). That is the whole reason
// it is a separate file rather than a value in the bundle: `import.meta.env` is inlined
// at build time, so one image could otherwise only ever serve one environment.
window.__MOTET_CONFIG__ = { apiBaseUrl: '' }
