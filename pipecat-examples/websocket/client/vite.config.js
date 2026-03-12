import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react-swc';

export default defineConfig({
    plugins: [react()],
    server: {
        proxy: {
            // Proxy /api requests to the backend server
            '/connect': {
                target: 'http://localhost:7860',
                changeOrigin: true,
            },
        },
    },
});
