/**
 * Renders the settings dialog open, with binds in the draft, and asserts the
 * rover-function section is actually there — the route smoke test only proves
 * the module imports, since the dialog is closed on every page it renders.
 */
import { createServer } from 'vite';

globalThis.window = globalThis.window || {};
const storage = new Map();
Object.assign(globalThis.window, {
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  },
  location: {
    href: 'http://gs.local/', protocol: 'http:', hostname: 'gs.local',
    host: 'gs.local', search: '', hash: '', origin: 'http://gs.local',
  },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  addEventListener() {}, removeEventListener() {},
});
globalThis.document = globalThis.document || {
  documentElement: { style: {}, setAttribute() {}, classList: { add() {}, remove() {} } },
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
  addEventListener() {}, removeEventListener() {},
  body: { appendChild() {}, removeChild() {} },
};

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  root: new URL('..', import.meta.url).pathname,
});

const React = (await import('react')).default;
const { renderToString } = await import('react-dom/server');

try {
  const { updateConfig } = await vite.ssrLoadModule('/src/config.js');
  updateConfig({
    functionBinds: [
      { id: 'a', function: 'drive_power', source: 'switches', index: 0, invert: false },
      { id: 'b', function: 'drive_clear_errors', source: 'joy', index: 4, invert: false },
      { id: 'c', function: 'custom', source: 'joy', index: 5, invert: false, service: '/science/pump' },
    ],
  });

  const { default: SettingsDialog } = await vite.ssrLoadModule('/src/components/SettingsDialog.jsx');
  const { default: RosProvider } = await vite.ssrLoadModule('/src/ros/RosProvider.jsx');

  const html = renderToString(
    React.createElement(RosProvider, null,
      React.createElement(SettingsDialog, { open: true, onClose() {} })),
  );

  const must = [
    ['section heading', 'Rover functions'],
    ['catalogue entry', 'Drive power'],
    ['clear errors', 'Clear drive errors'],
    ['custom option', 'Custom service'],
    ['resolved SetBool call', '/drive/power'],
    ['resolved Trigger call', '/drive/clear_errors'],
    ['custom service value', '/science/pump'],
    ['apply button', 'Apply to rover'],
    ['still has cameras', 'Image topic'],
  ];

  let bad = 0;
  for (const [what, needle] of must) {
    const ok = html.includes(needle);
    if (!ok) bad += 1;
    console.log(`${ok ? 'ok  ' : 'FAIL'} ${what.padEnd(22)} ${needle}`);
  }
  console.log(bad ? `\n${bad} check(s) failed` : '\nSettings dialog renders the bind list.');
  process.exitCode = bad ? 1 : 0;
} catch (err) {
  console.error('render failed:', err);
  process.exitCode = 1;
} finally {
  await vite.close();
}
