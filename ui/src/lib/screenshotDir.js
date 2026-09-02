/**
 * Directory the "save frame" button on a camera feed writes into.
 *
 * There is no backend here — the UI is a static SPA — so the only way to write
 * a file to a chosen folder is the File System Access API. A
 * `FileSystemDirectoryHandle` cannot be JSON-serialized, so it cannot live in
 * `config.js`'s localStorage blob; it is kept in IndexedDB instead, under a
 * single fixed key. IndexedDB is shared by every tab of this origin, so a
 * folder picked from one monitor window is picked up by the others too.
 */

const DB_NAME = 'indomitus-fs';
const STORE = 'handles';
const KEY = 'screenshotDir';

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbGet(key) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const req = db.transaction(STORE, 'readonly').objectStore(STORE).get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbSet(key, value) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

/** Firefox and Safari do not implement the File System Access API. */
export function screenshotsSupported() {
  return typeof window.showDirectoryPicker === 'function';
}

/** Opens the browser's folder picker and remembers the choice. Must be called from a click handler. */
export async function chooseScreenshotDir() {
  const handle = await window.showDirectoryPicker({ id: 'indomitus-screenshots', mode: 'readwrite' });
  await idbSet(KEY, handle);
  return handle;
}

export async function forgetScreenshotDir() {
  await idbSet(KEY, null);
}

/** The remembered handle, or null if none has been picked yet. */
export async function getScreenshotDirHandle() {
  return (await idbGet(KEY)) || null;
}

/**
 * A handle surviving in IndexedDB does not mean the browser still lets us
 * write through it — that permission can lapse between sessions. Re-request
 * it, which only prompts the user if it actually needs to.
 */
export async function ensureWritePermission(handle) {
  const opts = { mode: 'readwrite' };
  if ((await handle.queryPermission(opts)) === 'granted') return true;
  return (await handle.requestPermission(opts)) === 'granted';
}
