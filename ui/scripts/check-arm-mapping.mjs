/**
 * Arm mapping round-trip — `npm run check:arm`.
 *
 * Needs a live bridge and a running `arm_gamepad` node; point it elsewhere
 * with BRIDGE_URL. Unlike the other checks this one talks to real ROS,
 * because what it is proving only exists on the wire.
 *
 * Drives the mapping through rosbridge exactly as the UI does:
 * GetParameters -> SetParameters(string) -> GetParameters.
 *
 * The string ParameterValue is the part worth proving. roslib serializes
 * exactly what it is handed and rclpy rejects a partial ParameterValue, so a
 * wrong type tag or a missing field fails only against a real node.
 */
import * as ROSLIB from 'roslib';
import { createServer } from 'vite';

// Through Vite rather than a plain import: the app's modules use
// extensionless specifiers, which Node's ESM resolver rejects. This way the
// check exercises the very builder the UI ships, not a copy of it.
const vite = await createServer({ server: { middlewareMode: true }, appType: 'custom', logLevel: 'error' });
const { parameterRequest } = await vite.ssrLoadModule('/src/ros/useService.js');

const URL = process.env.BRIDGE_URL || 'ws://127.0.0.1:9090';
const NODE = '/arm_gamepad';
const KEYS = ['safe_pose', 'gripper_open', 'left_x'];
const names = KEYS.map((k) => `bind.${k}`);

const ros = new ROSLIB.Ros({ url: URL });
const call = (name, serviceType, values) =>
  new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${name} timed out`)), 8000);
    new ROSLIB.Service({ ros, name, serviceType }).callService(
      values,
      (r) => { clearTimeout(timer); resolve(r); },
      (e) => { clearTimeout(timer); reject(new Error(String(e))); },
    );
  });

const read = async () => {
  const r = await call(`${NODE}/get_parameters`, 'rcl_interfaces/srv/GetParameters', { names });
  return Object.fromEntries(KEYS.map((k, i) => [k, r.values[i]?.string_value ?? '']));
};

let failures = 0;
const check = (label, ok, detail = '') => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
  if (!ok) failures += 1;
};

await new Promise((resolve, reject) => {
  ros.on('connection', resolve);
  ros.on('error', reject);
});

const before = await read();
check('GetParameters returns the mapping', Object.keys(before).length === 3, JSON.stringify(before));

// The UI's own request builder, with the string tag.
const setResp = await call(
  `${NODE}/set_parameters`,
  'rcl_interfaces/srv/SetParameters',
  parameterRequest([
    ['bind.safe_pose', 'joy:3:inv', 'string'],
    ['bind.gripper_open', 'switches:7', 'string'],
  ]),
);
check(
  'SetParameters(string) accepted',
  (setResp.results || []).every((r) => r.successful),
  (setResp.results || []).map((r) => r.reason).filter(Boolean).join('; '),
);

const after = await read();
check('safe_pose round-tripped', after.safe_pose === 'joy:3:inv', after.safe_pose);
check('gripper_open round-tripped', after.gripper_open === 'switches:7', after.gripper_open);
check('untouched slot unchanged', after.left_x === before.left_x, after.left_x);

// A bad value must come back with the node's reason, not a silent success.
const bad = await call(
  `${NODE}/set_parameters`,
  'rcl_interfaces/srv/SetParameters',
  parameterRequest([['bind.left_x', 'switches:2', 'string']]),
);
const rejected = (bad.results || []).find((r) => !r.successful);
check('axis slot rejects a button, with a reason', Boolean(rejected?.reason), rejected?.reason || '');

// Clearing a slot is the empty string, which must also survive the wire.
await call(
  `${NODE}/set_parameters`,
  'rcl_interfaces/srv/SetParameters',
  parameterRequest([['bind.gripper_open', '', 'string']]),
);
check('a slot can be cleared', (await read()).gripper_open === '');

// This runs against a real console, so put the mapping back the way it was
// found — otherwise a check leaves the panel bound to whatever it last tested.
await call(
  `${NODE}/set_parameters`,
  'rcl_interfaces/srv/SetParameters',
  parameterRequest(KEYS.map((k) => [`bind.${k}`, before[k], 'string'])),
);
const restored = await read();
check(
  'the original mapping is restored',
  KEYS.every((k) => restored[k] === before[k]),
  JSON.stringify(restored),
);

console.log(failures === 0 ? '\nAll bridge checks passed.' : `\n${failures} check(s) failed.`);
ros.close();
await vite.close();
process.exit(failures === 0 ? 0 : 1);
