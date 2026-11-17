import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 开发服务器配置
// 核心：proxy 把 /api 和 /health 转发到后端 FastAPI(8000)。
// 这样前端代码里只写相对路径 '/api/answer'，浏览器访问的是同源的 5173，
// 由 Vite 在服务端转发 —— 彻底绕开浏览器 CORS 问题。
// 生产部署时把 build 产物交给 nginx，同样用反向代理指向后端即可。
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
