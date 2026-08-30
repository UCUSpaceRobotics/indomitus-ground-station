/**
 * The console runs as several windows on several screens, each with its own
 * copy of the config module. This asserts that a settings change saved in one
 * reaches the others, which is what makes the Control box on the left monitor
 * relabel a switch that was rebound from another window.
 */
import { createServer } from 'vite';

const storage = new Map();
const handlers = new Map();

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
  addEventListener(type, fn) {
    if (!handlers.has(type)) handlers.set(type, []);
    handlers.get(type).push(fn);
  },
  removeEventListener() {},
});

/** What the browser delivers to the *other* windows when one writes. */
const fireStorage = (key, newValue) => {
  for (const fn of handlers.get('storage') || []) fn({ key, newValue });
};

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
  root: new URL('..', import.meta.url).pathname,
});

const failures = [];
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? `   ${detail}` : ''}`);
  if (!ok) failures.push(label);
};

try {
  const { getConfig, subscribeConfig, updateConfig } =
    await vite.ssrLoadModule('/src/config.js');

  check('storage listener registered', (handlers.get('storage') || []).length === 1);

  let notified = 0;
  subscribeConfig(() => { notified += 1; });

  // What another window's settings dialog would have written on Apply.
  const applied = {
    functionBinds: [
      { id: 'a', function: 'spotlight', source: 'joy', index: 4, invert: false },
    ],
  };
  fireStorage('indomitus.config.v2', JSON.stringify(applied));

  const binds = getConfig().functionBinds;
  check('followed the other window', binds.length === 1 && binds[0].function === 'spotlight',
    JSON.stringify(binds));
  check('kept the bind on its own board', binds[0]?.source === 'joy' && binds[0]?.index === 4);
  check('subscribers were told', notified === 1, `notified ${notified}x`);

  // Defaults still fill in, so a partial write cannot strip the rest.
  check('untouched settings survive', getConfig().topics.joy === '/joy');

  // Noise from other keys, and an unparseable value, must not disturb it.
  fireStorage('some.other.key', '{"functionBinds":[]}');
  fireStorage('indomitus.config.v2', '{not json');
  check('ignores other keys and bad JSON',
    getConfig().functionBinds.length === 1 && notified === 1);

  // A local update still works after all that.
  updateConfig({ functionBinds: [] });
  check('local updates still apply', getConfig().functionBinds.length === 0);
} finally {
  await vite.close();
}

if (failures.length) {
  console.error(`\n${failures.length} failed: ${failures.join(', ')}`);
  process.exit(1);
}
console.log('\nConfig follows settings saved in another window.');
