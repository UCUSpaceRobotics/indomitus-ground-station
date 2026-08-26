/**
 * Per-camera view rotation, in 90 degree steps.
 *
 * This rotates the *picture in the pane*, not the camera. Nothing on the rover
 * moves — there is no pan/tilt actuator in `indomitus-rover-core`, and this
 * console subscribes but never publishes. It exists because a camera bolted to
 * the mast ends up at whatever angle the bracket allows, and the operator
 * should not have to read a sideways image.
 *
 * Kept out of `config.js` deliberately. Config is the operator's endpoint and
 * camera *setup*, shared with the settings dialog and exported as a unit;
 * rotation is a per-screen viewing preference that changes often and should
 * never travel with a config export. Separate store, separate storage key.
 *
 * Stored per camera id so the thumbnail strip, the main pane and the
 * `#/cam/<id>` fullscreen route all agree, and all three update together the
 * moment the button is pressed.
 */
import { useSyncExternalStore } from 'react';

const STORAGE_KEY = 'indomitus.rotation.v1';
export const ROTATIONS = [0, 90, 180, 270];

function read() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== 'object') return {};
    // Drop anything that is not a legal quarter turn: a hand-edited or
    // half-written value must not leave a pane stuck at an angle the CSS has
    // no rule for.
    return Object.fromEntries(
      Object.entries(parsed).filter(([, deg]) => ROTATIONS.includes(deg)),
    );
  } catch {
    return {};
  }
}

let current = read();
const listeners = new Set();

function persist() {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(current));
  } catch {
    // Private-mode browsers: rotation stays for this session only.
  }
}

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot() {
  return current;
}

export function getRotation(cameraId) {
  return current[cameraId] ?? 0;
}

/** Advances one quarter turn clockwise and returns the new angle. */
export function rotateCamera(cameraId) {
  const next = (getRotation(cameraId) + 90) % 360;
  // A new object rather than a mutation: useSyncExternalStore compares
  // snapshots by identity, and mutating in place would render nothing.
  current = { ...current, [cameraId]: next };
  persist();
  emit();
  return next;
}

export function resetRotations() {
  current = {};
  persist();
  emit();
}

export function useRotation(cameraId) {
  const all = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return all[cameraId] ?? 0;
}
