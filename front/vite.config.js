import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

import { resolve } from 'path'

// Vite 开发服务器配置
// 核心：proxy 把 /api 和 /health 转发到后端 FastAPI(8000)。
// 这样前端代码里只写相对路径 '/api/answer'，浏览器访问的是同源的 5173，
// 由 Vite 在服务端转发 —— 彻底绕开浏览器 CORS 问题。
// 生产部署时把 build 产物交给 nginx，同样用反向代理指向后端即可。
//
// 多页面：除对话主页 index.html 外，新增评估中心 eval.html。
// dev 模式下 Vite 自动服务任意 html；build 需在 rollupOptions.input 显式声明两个入口。
export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        eval: resolve(__dirname, 'eval.html'),
      },
    },
  },
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
