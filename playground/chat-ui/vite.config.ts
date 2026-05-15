import { loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_GATEWAY_TARGET || 'http://127.0.0.1:8787'

  return {
    plugins: [react()],
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.ts'],
      passWithNoTests: false,
    },
    server: {
      proxy: {
        // Same-origin in dev to avoid CORS when the UI calls the local gateway.
        '/__mb_gateway': {
          target,
          changeOrigin: true,
          configure: (proxy) => {
            proxy.on('proxyReq', (proxyReq) => {
              proxyReq.path = proxyReq.path.replace(/^\/__mb_gateway/, '') || '/'
            })
          },
        },
      },
    },
  }
})
