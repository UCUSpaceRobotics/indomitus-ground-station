import { ToggleLeft } from 'lucide-react';
import Panel from './Panel';
import { useConfig } from '../config';
import { isStale, useTick, useTopic } from '../ros/useTopic';
import { fmtNumber, NO_VALUE } from '../lib/format';
import { armSlotAt, useArmBindings } from '../hooks/useArmBindings';
import { SLOTS_BY_KEY } from '../lib/armSlots';
import {
  MODE_SWITCH_INDEX,
  SOURCE_JOY,
  SOURCE_LIMITS,
  SOURCE_SWITCHES,
  functionLabel,
} from '../lib/roverFunctions';

/** A board is considered dead after this long without a message. */
const STALE_MS = 2000;

export default function SwitchPanel() {
  const config = useConfig();
  const now = useTick(500);

  // The panel is two boards on two ports, and they fail independently: the
  // button board can be unplugged while the sticks keep publishing. One list
  // per board, each with its own liveness, so an outage names the board.
  const switches = useTopic(config.topics.switches, 'std_msgs/Int32MultiArray', {
    throttleMs: 0,
    renderMs: 150,
  });
  const joy = useTopic(config.topics.joy, 'sensor_msgs/Joy', {
    throttleMs: 0,
    renderMs: 150,
  });

  // Three things claim console controls and none of them knows about the other
  // two: cameras and rover functions live in the UI config, the arm mapping
  // lives in arm_gamepad. A panel that reads only the first two calls every
  // arm control "unassigned", which is exactly the bit an operator is trying
  // to look up when a press does nothing.
  const { bindings: armBindings } = useArmBindings();

  const labelFor = (source, index) => {
    if (source === SOURCE_SWITCHES) {
      const cam = config.cameras.find((c) => c.switchIndex === index);
      if (cam) return { text: cam.name, tag: 'camera' };
    }
    if (source === SOURCE_JOY && index === MODE_SWITCH_INDEX) {
      return { text: 'drive / arm mode', tag: 'sticks' };
    }

    const fn = config.functionBinds.find((b) => b.source === source && b.index === index);
    if (fn) return { text: functionLabel(fn), tag: 'rover' };

    const slot = armSlotAt(armBindings, source, index);
    if (slot) {
      const known = SLOTS_BY_KEY[slot.key];
      return {
        text: known ? known.action : slot.key,
        tag: 'arm',
        // The same control can read backwards for the arm; say so, because it
        // explains an LED that is lit while the arm sees the button released.
        inverted: slot.bind.invert,
      };
    }
    return { text: 'unassigned', tag: '' };
  };

  // The mode switch does not have an off position so much as a second job.
  const joyState = (index, on) => {
    if (index === MODE_SWITCH_INDEX) return on ? 'ARM' : 'DRIVE';
    return on ? 'ON' : 'OFF';
  };

  return (
    <Panel icon={ToggleLeft} title="Control box">
      <Board
        name="Button board"
        topic={config.topics.switches}
        hz={switches.hz}
        // `data` is the Int32MultiArray payload; `buttons` is the Joy one.
        data={switches.message?.data}
        receivedAt={switches.receivedAt}
        now={now}
        width={SOURCE_LIMITS[SOURCE_SWITCHES]}
        labelFor={(index) => labelFor(SOURCE_SWITCHES, index)}
      />
      <Board
        name="Joystick board"
        topic={config.topics.joy}
        hz={joy.hz}
        // The stick board's own 9 switches ride along in the Joy frame rather
        // than on /switches — see console_boards_node.publish_joy.
        data={joy.message?.buttons}
        receivedAt={joy.receivedAt}
        now={now}
        width={SOURCE_LIMITS[SOURCE_JOY]}
        labelFor={(index) => labelFor(SOURCE_JOY, index)}
        stateFor={joyState}
      />
    </Panel>
  );
}

function Board({ name, topic, hz, data, receivedAt, now, width, labelFor, stateFor }) {
  const live = Array.isArray(data) && !isStale(receivedAt, now, STALE_MS);
  // Fall back to the board's wired width so the layout is stable before the
  // first message, and does not jump when a short frame arrives.
  const count = Array.isArray(data) && data.length > 0 ? data.length : width;

  return (
    <div className="switch-board">
      <div className="switch-board-head">
        <span className="switch-board-name">{name}</span>
        <span className="mono muted switch-board-topic">{topic}</span>
        <span className="mono muted">{live ? `${fmtNumber(hz, 0)} Hz` : NO_VALUE}</span>
      </div>
      {!live && (
        <p className="panel-note">
          Nothing on <span className="mono">{topic}</span> — check that
          <span className="mono"> console_boards</span> is running and this board is plugged in.
        </p>
      )}
      <ul className="switch-list">
        {Array.from({ length: count }, (_, index) => {
          const on = live && data[index] === 1;
          const state = stateFor ? stateFor(index, on) : on ? 'ON' : 'OFF';
          const label = labelFor(index);
          return (
            <li
              key={index}
              className={`switch-item ${live ? (on ? 'is-on' : 'is-off') : 'is-unknown'}`}
            >
              <span className="switch-led" />
              <span className="switch-index mono">{index}</span>
              <span className="switch-label">
                {label.text}
                {label.inverted && <span className="switch-tag">inv</span>}
                {label.tag && <span className="switch-tag">{label.tag}</span>}
              </span>
              <span className="switch-state mono">{live ? state : NO_VALUE}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
