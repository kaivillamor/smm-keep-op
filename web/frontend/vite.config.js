import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist' },
  // `npm run dev` proxies the API to the local FastAPI process so the front-end can be
  // developed with hot reload without CORS or a second auth setup.
  server: { proxy: { '/api': 'http://127.0.0.1:8877' } },
})
