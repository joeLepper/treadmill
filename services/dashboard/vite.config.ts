/// <reference types="vitest" />
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Treadmill operator dashboard (ADR-0055 dashboard plan, 2026-05-26).
// `VITE_DEV_API_URL` overrides the proxy target (e.g. http://treadmill-api:8088
// when nginx is reverse-proxying alongside the API in a docker network).
// Defaults to the dev-local API port (`treadmill-local up -d personal`).
const apiTarget = process.env['VITE_DEV_API_URL'] ?? 'http://localhost:8088'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
      '/ws': {
        target: apiTarget.replace(/^http/, 'ws'),
        ws: true,
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
})
