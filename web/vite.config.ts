import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// The SPA is built to static files and served by Cloudflare. It talks to the Motet API
// and to nothing else — invariant 1: the client never speaks a vendor protocol.
export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
  },
})
