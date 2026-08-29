import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  const target = env.BACKEND_PROXY_TARGET;
  if (!target) throw new Error('BACKEND_PROXY_TARGET must be set in frontend/.env');
  return {
    plugins: [tailwindcss(), sveltekit()],
    // MapLibre owns its worker bundle. Letting Vite prebundle the worker as a
    // normal dependency produced stale `.vite/deps/maplibre-gl-worker.mjs`
    // references after hot updates.
    optimizeDeps: { exclude: ['maplibre-gl'] },
    server: {
      host: env.FRONTEND_HOST || '127.0.0.1',
      port: Number(env.FRONTEND_PORT || 5173),
      strictPort: true,
      proxy: {
        '/api': target,
        '/schema': target,
        '/tiles': target,
        '/exports': target
      }
    }
  };
});
