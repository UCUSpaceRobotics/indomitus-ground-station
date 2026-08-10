/**
 * Connection state-machine check — `npm run check:bridge [ws://host:9090]`.
 *
 * Drives src/ros/connection.js against a real rosbridge and exercises the paths
 * that are painful to reproduce by hand: a mid-session drop, a re-open raced
 * against a still-closing socket, a refused connection, and a host that accepts
 * the TCP connection then never answers.
 *
 * Requires a running rosbridge_websocket; skipped by `npm run check`.
 */
import * as ROSLIB from 'roslib';
import { createRosConnection } from '../src/ros/connection.js';

const URL_ = process.argv[2] || 'ws://localhost:9090';
const DEAD = 'ws://127.0.0.1:9099'; // nothing listening
const BLACKHOLE = 'ws://10.255.255.1:9090'; // routable-but-silent: exercises the watchdog
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failures = 0;
const check = (label, ok) => {
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}`);
  if (!ok) failures += 1;
};

function track(url, timeoutMs) {
  const seen = [];
  const conn = createRosConnection({
    url,
    timeoutMs,
    onStatus: (s) => seen.push(s),
  });
  return { conn, seen, last: () => seen[seen.length - 1] };
}

// --- 1. happy path -----------------------------------------------------------
console.log('\n1. connect to a live bridge');
{
  const { conn, last } = track(URL_);
  await sleep(1200);
  check('reaches connected', last()?.status === 'connected');
  check('generation is 1', last()?.generation === 1);

  const msgs = [];
  const topic = new ROSLIB.Topic({ ros: conn.ros, name: '/switches', messageType: 'std_msgs/Int32MultiArray' });
  topic.subscribe((m) => msgs.push(m));
  await sleep(1000);
  check(`receives messages (${msgs.length})`, msgs.length > 0);

  // --- 2. drop the socket underneath it -------------------------------------
  console.log('\n2. socket drops mid-session');
  conn.ros.close();
  await sleep(300);
  check('notices the drop', last()?.status === 'reconnecting');

  await sleep(3000);
  check('reconnects on its own', last()?.status === 'connected');
  check('generation advanced to 2', last()?.generation === 2);
  check('attempt counter reset', last()?.attempt === 0);

  const after = msgs.length;
  await sleep(1200);
  check(`messages resume (${msgs.length - after})`, msgs.length > after);

  // --- 3. immediate re-open while the socket is still CLOSING ---------------
  // This is the case roslib's connect() silently ignores.
  console.log('\n3. re-open raced against a CLOSING socket');
  conn.ros.close();
  conn.ros.connect(URL_).catch(() => {}); // what a naive retry would do
  await sleep(4000);
  check('still recovers', last()?.status === 'connected');

  conn.dispose();
  await sleep(200);
  const settled = last();
  await sleep(1500);
  check('dispose stops all activity', last() === settled);
}

// --- 4. nothing listening ----------------------------------------------------
console.log('\n4. connection refused');
{
  const { conn, seen, last } = track(DEAD);
  await sleep(3500);
  check('keeps retrying', last()?.status === 'reconnecting');
  check(`backs off rather than spinning (${last()?.attempt} attempts in 3.5s)`, last()?.attempt <= 5);
  check('never claims connected', !seen.some((s) => s.status === 'connected'));
  conn.dispose();
}

// --- 5. silent host: exercises the connect watchdog ---------------------------
console.log('\n5. host accepts nothing and never answers (watchdog)');
{
  const { conn, last } = track(BLACKHOLE, 1500);
  await sleep(1000);
  check('sits in connecting', last()?.status === 'connecting');
  await sleep(2000);
  check('watchdog gives up and retries', last()?.status === 'reconnecting');
  conn.dispose();
}

console.log(failures === 0 ? '\nAll connection checks passed.' : `\n${failures} check(s) failed.`);
process.exit(failures ? 1 : 0);
