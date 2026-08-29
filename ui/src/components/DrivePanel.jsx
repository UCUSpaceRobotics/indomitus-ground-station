import { Gamepad2 } from 'lucide-react';
import Panel from './Panel';
import { Bar } from './Readout';
import { useConfig } from '../config';
import { isStale, useTick, useTopic } from '../ros/useTopic';
import { clamp, fmtNumber, NO_VALUE } from '../lib/format';
import { isArmMode } from '../lib/roverFunctions';

/** Two-axis stick position — the quickest way to see a dead axis, an inverted
 *  sign or a deadzone that is set too wide.
 *
 *  Both screen axes are flipped, not just the vertical one. These are REP-103
 *  values, where +y is *left* and +z is a *left* turn, so a left push arriving
 *  as +1.0 is correct and has to be drawn to the left. Flipping only the
 *  vertical is what made the pad mirror every horizontal axis. */
function AxisPad({ x, y, label, live }) {
  const px = 50 - clamp(x ?? 0, -1, 1) * 42;
  const py = 50 - clamp(y ?? 0, -1, 1) * 42;
  return (
    <figure className={`axis-pad ${live ? '' : 'is-nodata'}`}>
      <svg viewBox="0 0 100 100" role="img" aria-label={label}>
        <rect x="2" y="2" width="96" height="96" rx="10" className="axis-pad-frame" />
        <line x1="50" y1="8" x2="50" y2="92" className="axis-pad-cross" />
        <line x1="8" y1="50" x2="92" y2="50" className="axis-pad-cross" />
        <circle cx="50" cy="50" r="42" className="axis-pad-ring" />
        {live && <circle cx={px} cy={py} r="7" className="axis-pad-dot" />}
      </svg>
      <figcaption>{label}</figcaption>
    </figure>
  );
}

function AxisRow({ label, value, live }) {
  return (
    <div className="axis-row">
      <span className="axis-row-label">{label}</span>
      <Bar value={live ? clamp(value ?? 0, -1, 1) : 0} min={-1} max={1} bipolar tone="accent" />
      <span className="axis-row-value mono">{live ? fmtNumber(value, 2) : NO_VALUE}</span>
    </div>
  );
}

/**
 * Command-path monitor for the joystick bridge this repository implements:
 * raw `/joy` axes on one side, the `/cmd_vel` and MoveIt Servo twists they are
 * translated into on the other. If the rover is not moving, this panel shows
 * where the chain broke.
 *
 * Subscriptions here are deliberately unthrottled so the reported rates are the
 * publishers' real rates; only the React render is throttled.
 */
export default function DrivePanel() {
  const config = useConfig();
  const now = useTick(500);

  const joy = useTopic(config.topics.joy, 'sensor_msgs/Joy', { throttleMs: 0, renderMs: 100 });
  const cmdVel = useTopic(config.topics.cmdVel, 'geometry_msgs/Twist', { throttleMs: 0, renderMs: 100 });
  const servo = useTopic(config.topics.servoTwist, 'geometry_msgs/TwistStamped', {
    throttleMs: 0,
    renderMs: 200,
  });

  const joyLive = !isStale(joy.receivedAt, now, 1000);
  const cmdLive = !isStale(cmdVel.receivedAt, now, 1000);
  const servoLive = !isStale(servo.receivedAt, now, 1000);

  const axes = joy.message?.axes || [];
  // J2 exists on the board at all times, but only the arm reads it: while the
  // mode switch says drive, joy_to_cmd_vel_node uses J0 and J1 and nothing
  // touches axes 4-5. Showing a stick that moves the dot and nothing else is
  // how an operator concludes the arm is broken, so it appears with the mode.
  const armMode = joyLive && isArmMode(joy.message);
  const twist = cmdVel.message;
  const servoTwist = servo.message?.twist;

  return (
    <Panel
      icon={Gamepad2}
      title="Command path"
      bodyClassName="stack"
      actions={
        <>
          <span className={`chip ${armMode ? 'is-warn' : 'is-ok'}`}>
            {joyLive ? (armMode ? 'arm' : 'drive') : 'no sticks'}
          </span>
          <span className="mono muted">
            joy {joyLive ? `${fmtNumber(joy.hz, 0)} Hz` : NO_VALUE} · cmd_vel{' '}
            {cmdLive ? `${fmtNumber(cmdVel.hz, 0)} Hz` : NO_VALUE}
          </span>
        </>
      }
    >
      <div className="axis-pads">
        <AxisPad x={axes[0]} y={axes[1]} label="Stick 1" live={joyLive && axes.length >= 2} />
        <AxisPad x={axes[2]} y={axes[3]} label="Stick 2" live={joyLive && axes.length >= 4} />
        {armMode && (
          <AxisPad x={axes[4]} y={axes[5]} label="Stick 3" live={axes.length >= 6} />
        )}
      </div>

      <div className="subhead">
        <span>/cmd_vel</span>
        <span className={`chip ${cmdLive ? 'is-ok' : 'is-idle'}`}>{cmdLive ? 'active' : 'silent'}</span>
      </div>
      <AxisRow label="lin.x" value={twist?.linear?.x} live={cmdLive} />
      <AxisRow label="lin.y" value={twist?.linear?.y} live={cmdLive} />
      <AxisRow label="ang.z" value={twist?.angular?.z} live={cmdLive} />

      <div className="subhead">
        <span>servo Δtwist</span>
        <span className={`chip ${servoLive ? 'is-ok' : 'is-idle'}`}>
          {servoLive ? 'active' : 'silent'}
        </span>
      </div>
      <div className="servo-grid mono">
        <span className="muted">lin</span>
        <span>{servoLive ? fmtNumber(servoTwist?.linear?.x, 2) : NO_VALUE}</span>
        <span>{servoLive ? fmtNumber(servoTwist?.linear?.y, 2) : NO_VALUE}</span>
        <span>{servoLive ? fmtNumber(servoTwist?.linear?.z, 2) : NO_VALUE}</span>
        <span className="muted">ang</span>
        <span>{servoLive ? fmtNumber(servoTwist?.angular?.x, 2) : NO_VALUE}</span>
        <span>{servoLive ? fmtNumber(servoTwist?.angular?.y, 2) : NO_VALUE}</span>
        <span>{servoLive ? fmtNumber(servoTwist?.angular?.z, 2) : NO_VALUE}</span>
      </div>
    </Panel>
  );
}
