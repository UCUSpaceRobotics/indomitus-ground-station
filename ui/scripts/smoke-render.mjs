/**
 * Render smoke test — `npm run check:render`.
 *
 * Mounts every route through Vite's SSR pipeline against a minimal DOM stub, to
 * catch import errors, hook misuse and render-time crashes without needing a
 * browser or any extra dependency. Needs no rosbridge: with nothing connected
 * the panels render their "no data" state, which is exactly what should be
 * verified anyway.
 */
import { createServer } from 'vite';

function makeStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    clear: () => map.clear(),
  };
}

function installDom(hash) {
  const storage = makeStorage();
  const win = {
    location: {
      href: `http://ground-station.local:5173/${hash}`,
      origin: 'http://ground-station.local:5173',
      protocol: 'http:',
      host: 'ground-station.local:5173',
      hostname: 'ground-station.local',
      port: '5173',
      pathname: '/',
      search: '',
      hash,
    },
    history: {
      state: null,
      length: 1,
      scrollRestoration: 'auto',
      pushState(s) {
        this.state = s;
      },
      replaceState(s) {
        this.state = s;
      },
      go() {},
      back() {},
      forward() {},
    },
    localStorage: storage,
    sessionStorage: makeStorage(),
    addEventListener() {},
    removeEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    requestAnimationFrame: (fn) => setTimeout(fn, 16),
    cancelAnimationFrame: clearTimeout,
  };
  const doc = {
    defaultView: win,
    documentElement: { dataset: {} },
    fullscreenElement: null,
    addEventListener() {},
    removeEventListener() {},
    createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
    querySelector: () => null,
    querySelectorAll: () => [],
    head: { appendChild() {} },
    body: { appendChild() {} },
  };
  win.document = doc;
  globalThis.window = win;
  globalThis.document = doc;
  globalThis.localStorage = storage;
}

const ROUTES = [
  ['#/', ['Indomitus Ground Station', 'Left monitor', 'Right monitor', 'Keyboard',
    // The settings dialog is always in the DOM, so its function list is
    // covered here: every rover function must be listed whether bound or not.
    'Rover functions', 'Drive power', 'Spotlight', 'Custom services']],
  // Both console boards are listed, including the stick board's own switches,
  // which arrive in Joy.buttons rather than on /switches.
  ['#/left', ['Telemetry', 'Command path', 'Control box', 'Button board', 'Joystick board',
    'drive / arm mode', 'Rover log', 'Ground speed']],
  ['#/right', ['Camera wall', 'feeds', 'Focus', 'Grid']],
  // cam1 is a Nano feed: an absolute MJPEG URL rather than a ROS topic, which
  // the full-screen route prints under the "No signal" overlay when the camera
  // is not reachable from wherever this is running. See "Cameras outside ROS".
  ['#/cam/cam1', ['Nano Camera 1', 'http://10.42.0.1:8090']],
  ['#/calibrate', ['Stick calibration', 'Hold X at maximum', 'Centre deadzone', 'Panel buttons']],
  ['#/arm-mapping', ['Arm mapping', 'Home pose + start servo', 'Astrobio home', 'Open gripper',
    'Sticks']],
  ['#/nope', []], // redirect resolves client-side; here it just must not throw
];

installDom('#/');

const vite = await createServer({
  root: new URL('..', import.meta.url).pathname,
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
});

const React = (await import('react')).default;
const { renderToString } = await import('react-dom/server');

let failures = 0;
for (const [hash, expected] of ROUTES) {
  installDom(hash);
  vite.moduleGraph.invalidateAll();
  try {
    const { default: App } = await vite.ssrLoadModule('/src/App.jsx');
    const html = renderToString(React.createElement(App));
    const missing = expected.filter((needle) => !html.includes(needle));
    if (missing.length) {
      failures += 1;
      console.error(`FAIL ${hash} — missing: ${missing.join(', ')}`);
    } else {
      console.log(`ok   ${hash} (${html.length} bytes)`);
    }
  } catch (err) {
    failures += 1;
    console.error(`FAIL ${hash} — threw:\n${err?.stack || err}`);
  }
}

await vite.close();
console.log(failures === 0 ? '\nAll routes rendered.' : `\n${failures} route(s) failed.`);
process.exit(failures === 0 ? 0 : 1);
