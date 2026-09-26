import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv, type PluginOption } from 'vite';

// VITE_API_BASE_URL points the site at the backend (docs/API.md). When it is
// unset, `npm run dev` serves the simulated mock API at /__mock on the dev
// server itself. The mock is imported only here, only for the dev server, so it
// never reaches a production build (scripts/check-dist.mjs verifies that).
export default defineConfig(async ({ command, mode, isPreview }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  const apiBase = env.VITE_API_BASE_URL?.trim();
  const plugins: PluginOption[] = [react()];

  if (command === 'serve' && !isPreview && !apiBase) {
    const { mockApiPlugin } = await import('./mock/vitePlugin.ts');
    plugins.push(mockApiPlugin());
  }

  if (command === 'build' && !apiBase) {
    if (process.env.VERCEL) {
      throw new Error(
        'VITE_API_BASE_URL is not set. In Vercel, add it under Settings > Environment Variables ' +
          '(the Cloud Run URL, e.g. https://fleet-sim-xxxx.us-east1.run.app) and redeploy.',
      );
    }
    console.warn(
      '\n[fleet-sim] VITE_API_BASE_URL is not set. This build will show a "not connected" notice.\n',
    );
  }

  return {
    plugins,
    server: { port: 5173 },
    preview: { port: 4173 },
    build: { target: 'es2022', sourcemap: true },
  };
});
