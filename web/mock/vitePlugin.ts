// Serves the SIMULATED mock API at /__mock inside `vite` (dev server only).
// vite.config.ts loads this only for `vite` without VITE_API_BASE_URL, so it
// never touches a production build.

import type { Plugin } from 'vite';
import { createMockApi, mockOptionsFromEnv, type MockApi } from './api.ts';

export function mockApiPlugin(basePath = '/__mock'): Plugin {
  let api: MockApi | null = null;
  return {
    name: 'fleet-sim-mock-api',
    apply: 'serve',
    configureServer(server) {
      api = createMockApi({ basePath, ...mockOptionsFromEnv(process.env) });
      const mock = api;
      server.middlewares.use((req, res, next) => {
        mock.handle(req, res).then((handled) => {
          if (!handled) next();
        }, next);
      });
      server.httpServer?.on('upgrade', (req, socket, head) => {
        mock.upgrade(req, socket, head);
      });
      server.httpServer?.on('close', () => mock.close());
      server.config.logger.info(
        `  ➜  Mock API: ${basePath} (simulated fleet; real ERCOT prices for May 16–17, 2025). Set VITE_API_BASE_URL to use a real backend.`,
      );
    },
  };
}
