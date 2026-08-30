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
 * The Jetson Nano's cameras are NOT ROS topics — it runs Ubuntu 18.04, where
 * Humble has no binaries, so there is no v4l2_camera_node. mast/nano-camera.sh
 * serves each one over plain HTTP instead, one port per camera counting up from
 * 8090. A camera row holding an absolute URL is read straight by the browser;
 * see isDirectUrl() below and "Cameras outside ROS" in README.md.
 *
 * Overridable because the rover's link address is the one thing that changes
 * between the bench and a competition network.
 */
const NANO_HOST = envOr('VITE_NANO_HOST', '10.42.0.1');
const nanoCamera = (port) => `http://${NANO_HOST}:${port}/?action=stream`;

/**
 * `group` decides which monitor a camera lands on, replacing the old
 * "slice the array by index" coupling.
 * `switchIndex` is the bit in /gs/switches (from console_boards) that gates it.
 */
export const DEFAULT_CAMERAS = [
  // Two kinds of entry live here.
  //
  // cam1/cam3 are absolute URLs: the two live Nano feeds, read straight by the
  // browser with no ROS in the path. Rename them in the settings dialog once
  // their mounting is settled — the operator reads these names.
  //
  // Every other row is a ROS *base* image topic, and all of them are currently
  // aspirational: the rover Jetson was replaced by a Nano running no ROS, so
  // nothing publishes them today. Both transports append the `/compressed`
  // suffix themselves — `ros` mode subscribes to `<topic>/compressed`, and
  // web_video_server's `ros_compressed` type resolves the same companion — so a
  // topic written with `/compressed` already on it ends up looking for
  // `<topic>/compressed/compressed`, which nothing publishes.
  { id: 'cam1', name: 'Nano Camera 1', topic: nanoCamera(8090), switchIndex: 0, group: 'main' },
  { id: 'cam2', name: 'Arm End Effector', topic: '/camera/arm/image_raw', switchIndex: 1, group: 'aux' },
  { id: 'cam3', name: 'Nano Camera 2', topic: nanoCamera(8091), switchIndex: 2, group: 'main' },
  { id: 'cam4', name: 'Mast Pan/Tilt', topic: '/camera/mast/image_raw', switchIndex: 3, group: 'main' },
  { id: 'cam5', name: 'Nano Camera 3', topic: nanoCamera(8092), switchIndex: 4, group: 'main' },
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

/**
 * Topics the panels read. Overridable so the UI can follow a remapped rover.
 *
 * switches/joy/joyRaw live under the gs_bringup /gs namespace. armJoy,
 * cmdVel and servoTwist are deliberately absolute — they're where the gs
 * nodes hand off to the rover/arm side, so they stay outside /gs on both ends.
 */
export const DEFAULT_TOPICS = {
  switches: '/gs/switches',
  joy: '/gs/joy',
  /** Uncalibrated 0..1000 stick values — what the calibration wizard reads. */
  joyRaw: '/gs/joy/raw',
  /** The console dressed as an SDL gamepad, which is what the arm reads. */
  armJoy: '/arm/joy',
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
    joyNode: '/gs/console_boards',
    /** Node the arm-mapping page reconfigures. */
    armNode: '/gs/arm_gamepad',
    /** Node that turns console switches into rover service calls. */
    interpreterNode: '/gs/gs_interpreter',
    /** Node that turns the sticks into /cmd_vel_gs, for the steering mode. */
    driveNode: '/gs/joy_to_cmd_vel_node',
    /**
     * Console control -> rover function. The node is authoritative once these
     * are applied; this copy is the editing draft, so the dialog opens on what
     * was last sent rather than empty while the rover is unreachable.
     */
    functionBinds: [],
    /**
     * Panel control that flips the steering mode on joy_to_cmd_vel_node.
     * index -1 leaves the node on whatever its `twist_mode` parameter says.
     */
    driveModeBind: { source: 'switches', index: -1 },
    /** Steering mode used when no switch is bound, and the fallback when one is. */
    twistMode: 'row',
    /**
     * Strafe. Off by default, matching the rover: with vy live a diagonal
     * stick crabs instead of turning, which is not how most driving is done.
     */
    vyBind: { source: 'switches', index: -1 },
    vyEnabled: false,
    /** Everything scaled down for fine work. */
    grannyBind: { source: 'switches', index: -1 },
    grannyMode: false,
    /** Stop commanding the rover from this console without killing the node. */
    muteBind: { source: 'switches', index: -1 },
    mute: false,
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
/**
 * Drop anything malformed rather than letting it reach the node, which would
 * refuse the whole set and leave the operator with an Apply that just fails.
 * Exclusivity is not enforced here - the dialog releases a control as it is
 * claimed, and the node is the backstop.
 */
function normalizeBinds(binds) {
  if (!Array.isArray(binds)) return [];
  return binds
    .filter((b) => b && typeof b.function === 'string' && b.function)
    .map((b, i) => ({
      id: String(b.id || `bind${i}`),
      function: b.function,
      source: b.source === 'joy' ? 'joy' : 'switches',
      // -1 is meaningful: it is a bind whose control was claimed by something
      // else. Clamping it to 0 silently re-bound the loser to the first bit on
      // the board, which is a switch doing something nobody asked for.
      index: Number.isFinite(Number(b.index)) ? Math.round(Number(b.index)) : -1,
      invert: Boolean(b.invert),
      ...(b.function === 'custom' ? { service: String(b.service || '') } : {}),
    }));
}

function normalizeModeBind(bind) {
  const source = bind?.source === 'joy' ? 'joy' : 'switches';
  const index = Number.isFinite(Number(bind?.index)) ? Math.round(Number(bind.index)) : -1;
  return { source, index };
}

function normalizeCameras(cameras) {
  // An empty list is a real answer — the settings dialog saves the camera table
  // as it is edited, so deleting the last row has to stay deleted instead of
  // resurrecting the shipped set on the next write. A fresh install still gets
  // DEFAULT_CAMERAS, from `defaults()`.
  if (!Array.isArray(cameras)) return DEFAULT_CAMERAS;
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

/**
 * Node names that moved under the /gs namespace, same refactor as
 * LEGACY_TOPICS below.
 *
 * This one bites harder than the topic version, because a wrong node name
 * breaks *writing* rather than reading: the settings dialog calls
 * `${interpreterNode}/set_parameters`, and against a stale `/gs_interpreter`
 * rosbridge answers "Service /gs_interpreter/set_parameters does not exist" —
 * so Apply fails and no bind can be saved at all.
 */
const LEGACY_NODES = {
  '/gs_interpreter': '/gs/gs_interpreter',
  '/console_boards': '/gs/console_boards',
  '/arm_gamepad': '/gs/arm_gamepad',
  '/joy_to_cmd_vel_node': '/gs/joy_to_cmd_vel_node',
};

function migrateNode(name, fallback) {
  return LEGACY_NODES[name] ?? name;
}

function migrateJoyNode(name, fallback) {
  // Two migrations stack here: serial_joy_node and switch_reader_node were
  // merged into one node, and then that node moved into /gs.
  if (LEGACY_JOY_NODES.includes(name)) return fallback;
  return migrateNode(name, fallback);
}

/**
 * Console-board topics that moved under the /gs namespace when gs_bringup
 * started pushing one.
 *
 * Saved topics override DEFAULT_TOPICS, so a console that stored its settings
 * before that refactor keeps subscribing to the old absolute names. Nothing
 * publishes them any more, and the failure is silent in the worst way:
 * rosbridge connects, the header says "Connected", and every stick and switch
 * panel just stays empty with no error anywhere. Keyed by setting, so a value
 * that only *looks* legacy under some other key is left alone.
 */
const LEGACY_TOPICS = {
  switches: { '/switches': '/gs/switches' },
  joy: { '/joy': '/gs/joy' },
  joyRaw: { '/joy/raw': '/gs/joy/raw' },
};

function migrateTopics(saved) {
  const out = { ...DEFAULT_TOPICS };
  for (const [key, value] of Object.entries(saved || {})) {
    if (typeof value !== 'string' || !value) continue;
    out[key] = LEGACY_TOPICS[key]?.[value] ?? value;
  }
  return out;
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
    topics: migrateTopics(merged.topics),
    // serial_joy_node and switch_reader_node were merged into one node. A
    // console that saved its settings before that has the old name in
    // localStorage, and pointing the wizard at a node that no longer exists
    // fails as "calibration did not save" with nothing in the log.
    joyNode: migrateJoyNode(String(merged.joyNode || base.joyNode).replace(/\/+$/, ''), base.joyNode),
    armNode: migrateNode(
      String(merged.armNode || base.armNode).replace(/\/+$/, ''), base.armNode),
    interpreterNode: migrateNode(
      String(merged.interpreterNode || base.interpreterNode).replace(/\/+$/, ''),
      base.interpreterNode),
    driveNode: migrateNode(
      String(merged.driveNode || base.driveNode).replace(/\/+$/, ''), base.driveNode),
    functionBinds: normalizeBinds(merged.functionBinds),
    driveModeBind: normalizeModeBind(merged.driveModeBind),
    twistMode: merged.twistMode === 'curvature' ? 'curvature' : 'row',
    vyBind: normalizeModeBind(merged.vyBind),
    vyEnabled: Boolean(merged.vyEnabled),
    grannyBind: normalizeModeBind(merged.grannyBind),
    grannyMode: Boolean(merged.grannyMode),
    muteBind: normalizeModeBind(merged.muteBind),
    mute: Boolean(merged.mute),
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

/**
 * Follow settings applied in another window.
 *
 * The console is not one page: `/`, `/left` and `/right` are opened as separate
 * windows on separate screens, and each runs its own copy of this module with
 * its own `current`. The Control box is only on the left monitor, while the
 * settings dialog is usually driven from another one — so applying a bind
 * updated the window that sent it and left the panel that labels the switches
 * showing the previous wiring until someone reloaded it. From the operator's
 * seat that is a bind the UI said it applied and then did not show.
 *
 * localStorage is the one thing the windows already share, and the browser
 * fires `storage` in every *other* window of the origin when one writes, which
 * `persist()` does on every update. Query params keep their precedence over
 * the stored copy, the same order `current` is first built in — a window
 * opened with ?ros=... must not lose it because another window saved.
 */
window.addEventListener?.('storage', (event) => {
  if (event.key !== STORAGE_KEY || event.newValue === null) return;
  let stored;
  try {
    stored = JSON.parse(event.newValue);
  } catch {
    // Another tab wrote something unparseable; keep what this window has.
    return;
  }
  // No persist() here: this window is following a write, not making one.
  current = normalize({ ...stored, ...readQuery() });
  emit();
});

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

/**
 * A camera whose "topic" is written as an absolute http(s) URL is not a ROS
 * topic at all: it is an MJPEG source the browser talks to directly, with no
 * rover-side ROS and no web_video_server in the path.
 *
 * This exists for cameras hanging off a machine that cannot run Humble — the
 * Jetson Nano on Ubuntu 18.04, where the whole ROS layer is unavailable but
 * `mjpg-streamer` relaying the camera's native MJPEG is not. Such a feed loses
 * what ROS transport buys (recording, per-frame timestamps, the `ros` mode's
 * frame-age readout); it keeps switch gating, which is decided in the UI.
 */
export function isDirectUrl(topic) {
  return /^https?:\/\//i.test(String(topic || ''));
}

/** Builds the `web_video_server` stream URL for a camera. */
export function mjpegUrl(config, topic, { quality, width } = {}) {
  if (isDirectUrl(topic)) return topic;
  const q = videoQuery(topic, quality ?? config.videoQuality, width ?? config.videoWidth);
  return `${config.videoServerUrl}/stream?${q}`;
}

/** Single still frame, used for camera thumbnails so we do not open N streams. */
export function snapshotUrl(config, topic, { quality, width } = {}) {
  if (isDirectUrl(topic)) {
    // mjpg-streamer's own convention. Anything else keeps the stream URL, so a
    // thumbnail costs a second connection rather than showing nothing.
    return topic.replace('action=stream', 'action=snapshot');
  }
  const q = videoQuery(
    topic,
    quality ?? Math.min(config.videoQuality, 50),
    width ?? 320,
  );
  return `${config.videoServerUrl}/snapshot?${q}`;
}
