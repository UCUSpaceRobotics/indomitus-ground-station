/**
 * Per-camera video on/off, for the traffic it costs.
 *
 * The link back from the rover is the scarce resource on a run: every open pane
 * is an MJPEG connection the rover encodes for, or a CompressedImage
 * subscription rosbridge pushes down the same wire. This store lets an operator
 * park a camera they are not watching without touching the layout — the pane
 * stays exactly where it is and comes back the moment it is switched on again.
 *
 * "Off" means off at the source: `CameraFeed` renders no <img> and opens no
 * subscription, so nothing is requested and nothing is sent. It is not a hidden
 * pane still pulling frames.
 *
 * Same shape and reasoning as lib/rotation.js: a per-screen viewing preference,
 * kept out of `config.js` so it never travels with a config export, and stored
 * per camera id so the thumbnail strip, the main pane and the `#/cam/<id>`
 * route all agree the instant the switch is flipped.
 */
import { useSyncExternalStore } from 'react';

const STORAGE_KEY = 'indomitus.videoStream.v1';

function read() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== 'object') return {};
    // Only `false` is stored state; anything else is noise from a hand-edited
    // or half-written value and must not be able to leave a camera dark.
    return Object.fromEntries(
      Object.entries(parsed).filter(([, on]) => on === false),
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
    // Private-mode browsers: the choice stays for this session only.
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

/** Cameras stream unless switched off, so an unknown id reads as on. */
export function isVideoOn(cameraId) {
  return current[cameraId] !== false;
}

export function setVideoOn(cameraId, on) {
  // A new object rather than a mutation: useSyncExternalStore compares
  // snapshots by identity, and mutating in place would render nothing.
  const next = { ...current };
  if (on) delete next[cameraId];
  else next[cameraId] = false;
  current = next;
  persist();
  emit();
  return on;
}

/** Flips one camera and returns its new state. */
export function toggleVideo(cameraId) {
  return setVideoOn(cameraId, !isVideoOn(cameraId));
}

export function resetVideoStreams() {
  current = {};
  persist();
  emit();
}

export function useVideoOn(cameraId) {
  const all = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return all[cameraId] !== false;
}
