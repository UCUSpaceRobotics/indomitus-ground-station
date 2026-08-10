import { ToggleLeft } from 'lucide-react';
import Panel from './Panel';
import { useConfig } from '../config';
import { isStale, useTick, useTopic } from '../ros/useTopic';
import { fmtNumber, NO_VALUE } from '../lib/format';

/**
 * Live state of the physical toggles on the control box (`/switches`).
 *
 * Worth a panel of its own: when a camera does not appear, this answers whether
 * the feed is down or the switch is simply off — and whether the box is
 * publishing at all.
 */
export default function SwitchPanel() {
  const config = useConfig();
  const now = useTick(500);
  const switches = useTopic(config.topics.switches, 'std_msgs/Int32MultiArray', {
    throttleMs: 0,
    renderMs: 150,
  });

  const data = switches.message?.data;
  const live = Array.isArray(data) && !isStale(switches.receivedAt, now, 2000);
  // The reader publishes nine bits; fall back to that width so the layout is
  // stable before the first message arrives.
  const count = Array.isArray(data) && data.length > 0 ? data.length : 9;

  const labelFor = (index) => {
    const cam = config.cameras.find((c) => c.switchIndex === index);
    return cam ? cam.name : 'unassigned';
  };

  return (
    <Panel
      icon={ToggleLeft}
      title="Control box"
      actions={
        <span className="mono muted">{live ? `${fmtNumber(switches.hz, 0)} Hz` : NO_VALUE}</span>
      }
    >
      {!live && (
        <p className="panel-note">
          No data on <span className="mono">{config.topics.switches}</span> — check that
          <span className="mono"> switch_reader_node</span> is running and the box is plugged in.
        </p>
      )}
      <ul className="switch-list">
        {Array.from({ length: count }, (_, index) => {
          const on = live && data[index] === 1;
          return (
            <li key={index} className={`switch-item ${live ? (on ? 'is-on' : 'is-off') : 'is-unknown'}`}>
              <span className="switch-led" />
              <span className="switch-index mono">{index}</span>
              <span className="switch-label">{labelFor(index)}</span>
              <span className="switch-state mono">{live ? (on ? 'ON' : 'OFF') : NO_VALUE}</span>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
