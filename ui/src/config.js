import { useSyncExternalStore } from 'react';
import gripperCam from './assets/placeholders/gripper_cam.png';
import mastCam from './assets/placeholders/mast_cam.jpg';


/**
 * Runtime configuration for the ground station.
 *
 * Resolution order (highest priority first):
 *   1. URL query params   -> ?ros=ws://10.0.0.5:9090&video=http://10.0.0.5:8080
 *   2. localStorage       -> whatever was saved from the settings dialog
 *   3. Build-time env     -> VITE_ROSBRIDGE_URL / VITE_VIDEO_SERVER_URL
 *   4. Derived from the host the page was served from
 *
 * Step 4 matters in the field: the UI is usually served from the ground station
 * laptop and opened on a second machine, so hard-coding `localhost` breaks it.
 */

const STORAGE_KEY = 'indomitus.config.v2';

/** Video transport for camera tiles. */
export const VIDEO_MODES = {
  /** MJPEG over HTTP from `web_video_server`. Cheap, but no per-frame metadata. */
  mjpeg: 'mjpeg',
  /** `sensor_msgs/CompressedImage` over rosbridge. Heavier, gives real FPS + age. */
  ros: 'ros',
};

/**
 * `group` decides which monitor a camera lands on, replacing the old
 * "slice the array by index" coupling.
 * `switchIndex` is the bit in /switches (from switch_reader_node) that gates it.
 */
export const DEFAULT_CAMERAS = [
  { id: 'cam1', name: 'Front Navigation', topic: '/camera/front/image_raw', switchIndex: 0, group: 'main' },
  { id: 'cam2', name: 'Arm End Effector', topic: '/camera/arm/image_raw', switchIndex: 1, group: 'aux' },
  { id: 'cam3', name: 'Rear View', topic: '/camera/rear/image_raw', switchIndex: 2, group: 'main' },
  { id: 'cam4', name: 'Mast Pan/Tilt', topic: '/camera/mast/image_raw', switchIndex: 3, group: 'main' },
  { id: 'cam5', name: 'Left Stereo', topic: '/camera/left/image_raw', switchIndex: 4, group: 'main' },
  { id: 'cam6', name: 'Right Stereo', topic: '/camera/right/image_raw', switchIndex: 5, group: 'main' },
  { id: 'cam7', name: 'Underbelly', topic: '/camera/belly/image_raw', switchIndex: 6, group: 'aux' },
  { id: 'cam8', name: 'Science Payload', topic: '/camera/science/image_raw', switchIndex: 7, group: 'aux' },
];

/**
 * TEMPORARY: still frames standing in for cameras that are not streaming yet.
 *
 * A placeholder is only drawn when the real feed is *not* live, and the pane is
 * badged "PLACEHOLDER" in amber whenever one is showing — a stand-in must never
 * be mistakable for live video.
 *
 * Matched on camera id first, then on topic, so renaming a camera in settings
 * does not silently drop the stand-in. Delete this block, the two files in
 * `assets/placeholders/`, and the `feed-still` rules in index.css once the real
 * cameras are publishing.
 */
const PLACEHOLDER_BY_ID = {
  cam2: gripperCam,
  cam4: mastCam,
};

const PLACEHOLDER_BY_TOPIC = {
  '/camera/arm/image_raw': gripperCam,
  '/camera/mast/image_raw': mastCam,
};

export function placeholderFor(camera) {
  if (!camera) return null;
  return PLACEHOLDER_BY_ID[camera.id] ?? PLACEHOLDER_BY_TOPIC[camera.topic] ?? null;
}

/** Topics the panels read. Overridable so the UI can follow a remapped rover. */
export const DEFAULT_TOPICS = {
  switches: '/switches',
  joy: '/joy',
  /** Uncalibrated 0..1000 stick values — what the calibration wizard reads. */
  joyRaw: '/joy/raw',
  cmdVel: '/cmd_vel',
  servoTwist: '/servo_node/delta_twist_cmds',
  odom: '/odom',
  battery: '/battery_state',
  imu: '/imu/data',
  gps: '/gps/fix',
  rosout: '/rosout',
};

function pageHost() {
  const host = window.location.hostname;
  return host && host !== '' ? host : 'localhost';
}

function envOr(key, fallback) {
  const value = import.meta.env?.[key];
  return typeof value === 'string' && value.length > 0 ? value : fallback;
}

function defaults() {
  const host = pageHost();
  return {
    rosbridgeUrl: envOr('VITE_ROSBRIDGE_URL', `ws://${host}:9090`),
    videoServerUrl: envOr('VITE_VIDEO_SERVER_URL', `http://${host}:8080`),
    videoMode: envOr('VITE_VIDEO_MODE', VIDEO_MODES.mjpeg),
    /** MJPEG re-encode quality handed to web_video_server (1-100). */
    videoQuality: 70,
    /** Optional downscale for MJPEG streams; 0 keeps the native width. */
    videoWidth: 0,
    theme: 'dark',
    cameras: DEFAULT_CAMERAS,
    topics: DEFAULT_TOPICS,
    /** Node the calibration wizard reconfigures, for its parameter services. */
    joyNode: '/serial_joy_node',
  };
}

function readStorage() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function readQuery() {
  const params = new URLSearchParams(window.location.search);
  const patch = {};
  const ros = params.get('ros');
  const video = params.get('video');
  const mode = params.get('mode');
  const theme = params.get('theme');
  if (ros) patch.rosbridgeUrl = ros;
  if (video) patch.videoServerUrl = video;
  if (mode && mode in VIDEO_MODES) patch.videoMode = mode;
  if (theme === 'dark' || theme === 'light') patch.theme = theme;
  return patch;
}

/** Drops unknown keys and repairs partially-saved camera entries. */
function normalizeCameras(cameras) {
  if (!Array.isArray(cameras) || cameras.length === 0) return DEFAULT_CAMERAS;
  return cameras
    .filter((cam) => cam && typeof cam.id === 'string' && typeof cam.topic === 'string')
    .map((cam, i) => ({
      id: cam.id,
      name: typeof cam.name === 'string' && cam.name ? cam.name : cam.id,
      topic: cam.topic,
      switchIndex: Number.isInteger(cam.switchIndex) ? cam.switchIndex : i,
      group: cam.group === 'aux' ? 'aux' : 'main',
    }));
}

function normalize(raw) {
  const base = defaults();
  const merged = { ...base, ...raw };
  return {
    ...merged,
    rosbridgeUrl: String(merged.rosbridgeUrl || base.rosbridgeUrl),
    videoServerUrl: String(merged.videoServerUrl || base.videoServerUrl).replace(/\/+$/, ''),
    videoMode: merged.videoMode in VIDEO_MODES ? merged.videoMode : base.videoMode,
    videoQuality: Math.min(100, Math.max(1, Number(merged.videoQuality) || base.videoQuality)),
    videoWidth: Math.max(0, Number(merged.videoWidth) || 0),
    theme: merged.theme === 'light' ? 'light' : 'dark',
    cameras: normalizeCameras(merged.cameras),
    topics: { ...DEFAULT_TOPICS, ...(merged.topics || {}) },
    joyNode: String(merged.joyNode || base.joyNode).replace(/\/+$/, ''),
  };
}

let current = normalize({ ...readStorage(), ...readQuery() });
const listeners = new Set();

function persist() {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(current));
  } catch {
    // Private-mode browsers: run with in-memory config rather than failing.
  }
}

function emit() {
  for (const listener of listeners) listener();
}

export function getConfig() {
  return current;
}

export function subscribeConfig(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function updateConfig(patch) {
  current = normalize({ ...current, ...patch });
  persist();
  emit();
  return current;
}

/** Shipped defaults, without touching the live config or storage. */
export function defaultConfig() {
  return normalize(readQuery());
}

export function resetConfig() {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
  current = defaultConfig();
  emit();
  return current;
}

export function useConfig() {
  return useSyncExternalStore(subscribeConfig, getConfig, getConfig);
}

/** Builds the `web_video_server` MJPEG URL for a camera. */
export function mjpegUrl(config, topic, { quality, width } = {}) {
  const params = new URLSearchParams({
    topic,
    type: 'mjpeg',
    quality: String(quality ?? config.videoQuality),
  });
  const w = width ?? config.videoWidth;
  if (w > 0) params.set('width', String(w));
  return `${config.videoServerUrl}/stream?${params.toString()}`;
}

/** Single still frame, used for camera thumbnails so we do not open N streams. */
export function snapshotUrl(config, topic, { quality, width } = {}) {
  const params = new URLSearchParams({
    topic,
    quality: String(quality ?? Math.min(config.videoQuality, 50)),
  });
  const w = width ?? 320;
  if (w > 0) params.set('width', String(w));
  return `${config.videoServerUrl}/snapshot?${params.toString()}`;
}
