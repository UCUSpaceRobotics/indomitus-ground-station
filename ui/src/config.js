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
 * `switchIndex` is the bit in /switches (from console_boards) that gates it.
 */
export const DEFAULT_CAMERAS = [
  // Topics here are always the *base* image topic. Both transports append the
  // `/compressed` suffix themselves — `ros` mode subscribes to
  // `<topic>/compressed`, and web_video_server's `ros_compressed` type resolves
  // the same companion — so a topic written with `/compressed` already on it
  // ends up looking for `<topic>/compressed/compressed`, which nothing
  // publishes. The ZED2i wrapper publishes through image_transport, so the base
  // topic below has that companion. The other entries are still aspirational.
  { id: 'cam1', name: 'Front Navigation', topic: '/zed2i/rgb/image_rect_color', switchIndex: 0, group: 'main' },
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
    joyNode: '/console_boards',
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

const LEGACY_JOY_NODES = ['/serial_joy_node', '/switch_reader_node'];

function migrateJoyNode(name, fallback) {
  return LEGACY_JOY_NODES.includes(name) ? fallback : name;
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
    // serial_joy_node and switch_reader_node were merged into one node. A
    // console that saved its settings before that has the old name in
    // localStorage, and pointing the wizard at a node that no longer exists
    // fails as "calibration did not save" with nothing in the log.
    joyNode: migrateJoyNode(String(merged.joyNode || base.joyNode).replace(/\/+$/, ''), base.joyNode),
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

// `ros_compressed` hands the rover's existing JPEG payload straight to the
// browser: no decode, no re-encode, no extra subscriber on the raw topic.
//
// Do NOT change this to `mjpeg`. That type makes web_video_server subscribe to
// the *raw* image topic and transcode it. Raw is 1.73 MB/frame at 960x600, i.e.
// ~415 Mbit/s at 30 fps, which is roughly four times the whole Wi-Fi link and
// takes the rover offline. web_video_server is also started with
// `default_stream_type`/`default_snapshot_type` set to `ros_compressed` so a URL
// that somehow omits `type` still cannot fall back to raw.
//
// The trade is that `quality` and `width` are advisory here — there is no
// transcode to apply them to. Frame size is controlled at the source instead,
// via gscam's `camera.image_raw.jpeg_quality`.
const PASSTHROUGH_TYPE = 'ros_compressed';

// web_video_server does not percent-decode its query parameters: it hands the
// raw string straight to the ROS topic-name validator. URLSearchParams escapes
// '/' as %2F, so every tile failed with
//   Invalid topic name ... '%2Fcamera%2Fleft%2Fimage_raw'
// and the UI showed placeholders while the server log filled with warnings.
// A '/' is legal in a query string unescaped, and a valid ROS topic name
// contains nothing else that needs escaping, so build the query by hand.
function videoQuery(topic, quality, width) {
  const parts = [
    `topic=${topic}`,
    `type=${PASSTHROUGH_TYPE}`,
    `quality=${quality}`,
  ];
  if (width > 0) parts.push(`width=${width}`);
  return parts.join('&');
}

/** Builds the `web_video_server` stream URL for a camera. */
export function mjpegUrl(config, topic, { quality, width } = {}) {
  const q = videoQuery(topic, quality ?? config.videoQuality, width ?? config.videoWidth);
  return `${config.videoServerUrl}/stream?${q}`;
}

/** Single still frame, used for camera thumbnails so we do not open N streams. */
export function snapshotUrl(config, topic, { quality, width } = {}) {
  const q = videoQuery(
    topic,
    quality ?? Math.min(config.videoQuality, 50),
    width ?? 320,
  );
  return `${config.videoServerUrl}/snapshot?${q}`;
}
