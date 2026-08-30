/**
 * Renders the Control box panel and asserts every kind of claim on a console
 * control is labelled there.
 *
 * The panel is what an operator reads to answer "what does this switch do?",
 * so a claim it does not know about is drawn as "unassigned" — which reads as
 * the bind not having taken. Console modes were missing for exactly that
 * reason: they live on the drive node, not in functionBinds.
 */
import { createServer } from 'vite';

const storage = new Map();
globalThis.window = globalThis.window || {};
Object.assign(globalThis.window, {
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  },
  location: {
    href: 'http://gs.local/left', protocol: 'http:', hostname: 'gs.local',
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

const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label.padEnd(30)}${detail}`);
  if (!ok) failures.push(label);
};

try {
  const React = (await import('react')).default;
  const { renderToString } = await import('react-dom/server');
  const { updateConfig } = await vite.ssrLoadModule('/src/config.js');

  updateConfig({
    functionBinds: [
      { id: 'a', function: 'drive_power', source: 'switches', index: 8, invert: false },
      { id: 'b', function: 'spotlight', source: 'joy', index: 4, invert: false },
    ],
    // Console modes, spread across both boards.
    driveModeBind: { source: 'switches', index: 15 },
    vyBind: { source: 'switches', index: 16 },
    grannyBind: { source: 'joy', index: 6 },
    muteBind: { source: 'joy', index: 7 },
  });

  const { default: SwitchPanel } = await vite.ssrLoadModule('/src/components/SwitchPanel.jsx');
  const { default: RosProvider } = await vite.ssrLoadModule('/src/ros/RosProvider.jsx');
  const html = renderToString(
    React.createElement(RosProvider, null, React.createElement(SwitchPanel, null)),
  );

  // Split at the second board so each label is checked on the board it is on.
  const split = html.indexOf('Joystick board');
  const buttonBoard = html.slice(0, split);
  const joyBoard = html.slice(split);

  check('function on button board', buttonBoard.includes('Drive power'));
  check('function on joy board', joyBoard.includes('Spotlight'));
  check('steering mode labelled', buttonBoard.includes('Steering mode'));
  check('strafe labelled', buttonBoard.includes('Strafe'));
  check('granny on joy board', joyBoard.includes('Granny mode'));
  check('mute on joy board', joyBoard.includes('No output'));
  check('mode stays on its own board', !buttonBoard.includes('Granny mode'));
  check('cameras still labelled', buttonBoard.includes('Nano Camera 1'));
  check('free controls read unassigned', html.includes('unassigned'));
} finally {
  await vite.close();
}

if (failures.length) {
  console.error(`\n${failures.length} failed: ${failures.join(', ')}`);
  process.exit(1);
}
console.log('\nControl box labels every claim on a console control.');
