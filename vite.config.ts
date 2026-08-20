import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    watch: {
      ignored: ['**/backend/**', '**/.claude/**', '**/dist/**'],
    },
    proxy: {
      '/api/naver-stock': {
        target: 'https://m.stock.naver.com',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/naver-stock/, ''),
        headers: {
          'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
          Referer: 'https://m.stock.naver.com/',
        },
      },
      '/api/naver-chart': {
        target: 'https://fchart.stock.naver.com',
        changeOrigin: true,
        secure: false,
        rewrite: (path) => path.replace(/^\/api\/naver-chart/, ''),
        headers: {
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
          Referer: 'https://finance.naver.com/',
          Origin: 'https://finance.naver.com',
        },
      },
      '/api/naver-search': {
        target: 'https://ac.stock.naver.com',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/naver-search/, ''),
        headers: {
          'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
          Referer: 'https://m.stock.naver.com/',
        },
      },
    },
  },
})
