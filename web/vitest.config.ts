import { defineConfig } from 'vitest/config';

// Tests are pure logic (formatters, the frame reducer, reconnect logic, the mock),
// so they run in Node without the React plugin or the dev-server mock.
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts', 'mock/**/*.test.ts'],
    environment: 'node',
    // The contrast test reads the design tokens and the stylesheets as text (tokens.css?raw, app.css?raw, map.css?raw).
    css: { include: [/(tokens|app|map)\.css/] },
  },
});
