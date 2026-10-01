import { cameraBaseUrl } from '../config';

/*
 * Dataset capture, against the camera servers on the rover
 * (cameras/camera_mjpeg_server.py): /capture saves one full-quality frame on
 * the rover's disk, /record?on=1|0 saves one per second, /health reports
 * `recording=` and `saved=`. The rover's DualSense drives the same endpoints
 * (rover_teleop's dataset_trigger), so the state is always read back from the
 * servers, never kept here, and a toggle sets the same explicit target the
 * gamepad does: on unless any camera is already recording.
 */

const TIMEOUT_MS = 1500;
// A capture may have to open a closed camera first, which takes about a second.
const CAPTURE_TIMEOUT_MS = 5000;

/** Distinct server origins of every direct-URL camera in the config. */
export function datasetServers(cameras) {
  return [...new Set(cameras.map((cam) => cameraBaseUrl(cam.topic)).filter(Boolean))];
}

async function getText(url, timeoutMs = TIMEOUT_MS) {
  try {
    const res = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(timeoutMs) });
    return res.ok ? await res.text() : null;
  } catch {
    return null; // camera not running on that port, or the rover is out of reach
  }
}

function field(text, name) {
  const match = new RegExp(`\\b${name}=(\\S+)`).exec(text || '');
  return match ? match[1] : null;
}

/**
 * `{ live, recording, saved, answered }` across every server that answered:
 * `live` is how many did, `recording` whether any of them is recording,
 * `saved` the frames they have written since they started.
 */
export async function fetchDatasetState(servers) {
  const health = await Promise.all(servers.map((base) => getText(`${base}/health`)));
  const answered = servers.filter((_, i) => health[i] !== null);
  const live = health.filter((text) => text !== null);
  return {
    live: live.length,
    recording: live.some((text) => field(text, 'recording') === '1'),
    saved: live.reduce((sum, text) => sum + (Number(field(text, 'saved')) || 0), 0),
    answered,
  };
}

/** Saves one frame from every running camera; resolves to how many saved. */
export async function captureAll(servers) {
  const results = await Promise.all(servers.map((base) => getText(`${base}/capture`, CAPTURE_TIMEOUT_MS)));
  return results.filter((text) => text !== null).length;
}

/**
 * Flips recording the way the gamepad does: on unless any camera already
 * records. Resolves to `{ target, count }`, target null when nothing answered.
 */
export async function toggleRecording(servers) {
  const state = await fetchDatasetState(servers);
  if (state.live === 0) return { target: null, count: 0 };
  const target = !state.recording;
  const results = await Promise.all(state.answered.map((base) => getText(`${base}/record?on=${target ? 1 : 0}`)));
  return { target, count: results.filter((text) => text !== null).length };
}
