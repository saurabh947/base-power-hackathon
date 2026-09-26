// Standalone mock API: `npm run mock` (port 8787, or MOCK_PORT).
// SIMULATED data except the real ERCOT prices for the replay preset days. See mock/README.md.

import { createServer } from 'node:http';
import { createMockApi, mockOptionsFromEnv } from './api.ts';

const port = Number(process.env.MOCK_PORT ?? 8787);
const api = createMockApi({ basePath: '', ...mockOptionsFromEnv(process.env) });

const server = createServer((req, res) => {
  api.handle(req, res).then(
    (handled) => {
      if (!handled) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'not_found', message: 'Not found.' }));
      }
    },
    () => {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'internal', message: 'The practice server hit an error.' }));
    },
  );
});

server.on('upgrade', (req, socket, head) => {
  if (!api.upgrade(req, socket, head)) socket.destroy();
});

server.listen(port, () => {
  console.info(`Mock fleet-sim API (simulated; real ERCOT prices for the replay preset days) on http://localhost:${port}`);
  console.info(`Point the site at it with VITE_API_BASE_URL=http://localhost:${port}`);
});

const stop = () => {
  api.close();
  server.close(() => process.exit(0));
};
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
