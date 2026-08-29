import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Gamepad2 } from 'lucide-react';
import Panel from './Panel';
import { useConfig } from '../config';
import { useTopic } from '../ros/useTopic';
import { useRos } from '../ros/context';
import { parameterRequest, useServiceCaller } from '../ros/useService';
import { usePanelButtons } from '../hooks/usePanelButtons';
import {
  ALL_KEYS,
  AXIS_SLOTS,
  BUTTON_SLOTS,
  bindLabel,
  findConflicts,
  formatBind,
  parseBind,
} from '../lib/armSlots';

const BIND_PREFIX = 'bind.';

/** How far a stick must move from where it sat to count as "that one". */
const AXIS_LEARN_THRESHOLD = 0.4;

const emptyBindings = () => Object.fromEntries(ALL_KEYS.map((key) => [key, '']));

/**
 * Binds console controls to arm functions.
 *
 * The arm reads a canonical SDL gamepad; the console is three sticks and two
 * boards of switches. `arm_gamepad_node` bridges the two, and this page is how
 * the mapping gets set — by pressing the control, never by typing an index,
 * because nobody holding the console knows which bit on which board is under a
 * given label.
 *
 * Learning is deliberately one-at-a-time and edge-triggered: while a slot is
 * learning, panel presses are captured instead of acted on, so binding a
 * control cannot also fire whatever it was previously bound to.
 */
export default function ArmMappingPanel() {
  const config = useConfig();
  const callService = useServiceCaller();
  const { connected } = useRos();

  const [bindings, setBindings] = useState(emptyBindings);
  const [applied, setApplied] = useState(emptyBindings);
  const [learning, setLearning] = useState(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(null);

  // The frame the arm actually receives — the only real confirmation that a
  // binding took, since the rover is the one acting on it.
  const armJoy = useTopic(config.topics.armJoy, 'sensor_msgs/Joy', {
    throttleMs: 100,
    renderMs: 100,
  });
  // Raw sticks, for learning an axis by moving it.
  const joy = useTopic(config.topics.joy, 'sensor_msgs/Joy', { throttleMs: 0, renderMs: 50 });

  const dirty = useMemo(
    () => ALL_KEYS.some((key) => (bindings[key] || '') !== (applied[key] || '')),
    [bindings, applied],
  );
  const conflicts = useMemo(() => findConflicts(bindings), [bindings]);

  // ── reading the node ───────────────────────────────────────────────────────

  const reload = useCallback(async () => {
    setBusy(true);
    setStatus(null);
    try {
      const response = await callService(
        `${config.armNode}/get_parameters`,
        'rcl_interfaces/srv/GetParameters',
        { names: ALL_KEYS.map((key) => BIND_PREFIX + key) },
      );
      const values = response?.values || [];
      const next = emptyBindings();
      ALL_KEYS.forEach((key, i) => {
        next[key] = values[i]?.string_value || '';
      });
      setBindings(next);
      setApplied(next);
      setStatus({ tone: 'ok', text: `Read the mapping from ${config.armNode}.` });
    } catch (err) {
      setStatus({ tone: 'crit', text: String(err.message || err) });
    } finally {
      setBusy(false);
    }
  }, [callService, config.armNode]);

  // Load once the bridge is up. Without this the page opens showing everything
  // unbound, which reads as "the mapping is gone" rather than "not read yet".
  const loadedFor = useRef(null);
  useEffect(() => {
    if (!connected || loadedFor.current === config.armNode) return;
    loadedFor.current = config.armNode;
    reload();
  }, [connected, config.armNode, reload]);

  // ── learning ───────────────────────────────────────────────────────────────

  const capture = useCallback(
    (key, bind) => {
      setBindings((prev) => ({ ...prev, [key]: formatBind(bind) }));
      setLearning(null);
    },
    [],
  );

  const onPanelButton = useCallback(
    (button) => {
      if (!learning || learning.kind !== 'button') return;
      // Keep whichever inversion the slot already had: an operator re-binding a
      // switch that reads backwards should not have to flip it again.
      const previous = parseBind(bindings[learning.key]);
      capture(learning.key, { ...button, invert: previous?.invert ?? false });
    },
    [learning, bindings, capture],
  );

  usePanelButtons(onPanelButton);

  // Axis learning watches for the biggest departure from where the sticks sat
  // when learning started. A resting stick is not always exactly zero, so the
  // baseline has to be sampled rather than assumed.
  const axisBaseline = useRef(null);
  const axes = joy.message?.axes;

  useEffect(() => {
    if (!learning || learning.kind !== 'axis') {
      axisBaseline.current = null;
      return;
    }
    if (!Array.isArray(axes) || axes.length === 0) return;
    if (!axisBaseline.current) {
      axisBaseline.current = axes.slice();
      return;
    }

    const base = axisBaseline.current;
    let bestIndex = -1;
    let bestDelta = AXIS_LEARN_THRESHOLD;
    for (let i = 0; i < axes.length; i += 1) {
      const delta = Math.abs(axes[i] - (base[i] ?? 0));
      if (delta > bestDelta) {
        bestDelta = delta;
        bestIndex = i;
      }
    }
    if (bestIndex < 0) return;

    // Which way the operator pushed decides the sign: the arm document defines
    // each axis by the direction it should move, not by the wiring.
    const pushedNegative = axes[bestIndex] - (base[bestIndex] ?? 0) < 0;
    capture(learning.key, { source: 'joy_axis', index: bestIndex, invert: pushedNegative });
  }, [axes, learning, capture]);

  const toggleLearn = useCallback((slot, kind) => {
    setStatus(null);
    setLearning((prev) => (prev?.key === slot.key ? null : { key: slot.key, kind }));
  }, []);

  const clear = useCallback((key) => {
    setLearning(null);
    setBindings((prev) => ({ ...prev, [key]: '' }));
  }, []);

  const toggleInvert = useCallback((key) => {
    setBindings((prev) => {
      const bind = parseBind(prev[key]);
      if (!bind) return prev;
      return { ...prev, [key]: formatBind({ ...bind, invert: !bind.invert }) };
    });
  }, []);

  // ── writing to the node ────────────────────────────────────────────────────

  const apply = useCallback(async () => {
    setBusy(true);
    setStatus(null);
    try {
      const changed = ALL_KEYS.filter((key) => (bindings[key] || '') !== (applied[key] || ''));
      const response = await callService(
        `${config.armNode}/set_parameters`,
        'rcl_interfaces/srv/SetParameters',
        parameterRequest(changed.map((key) => [BIND_PREFIX + key, bindings[key] || '', 'string'])),
      );
      const rejected = (response?.results || []).filter((r) => !r.successful);
      if (rejected.length) {
        setStatus({ tone: 'crit', text: rejected.map((r) => r.reason).join('; ') || 'Rejected' });
        return;
      }
      setApplied({ ...bindings });
      setStatus({
        tone: 'ok',
        text: `Applied — ${config.topics.armJoy} is using the new mapping.`,
      });
    } catch (err) {
      setStatus({ tone: 'crit', text: String(err.message || err) });
    } finally {
      setBusy(false);
    }
  }, [callService, bindings, applied, config.armNode, config.topics.armJoy]);

  const persist = useCallback(async () => {
    setBusy(true);
    setStatus(null);
    try {
      const response = await callService(
        `${config.armNode}/save_bindings`,
        'std_srvs/srv/Trigger',
        {},
      );
      setStatus({
        tone: response?.success ? 'ok' : 'crit',
        text: response?.message || (response?.success ? 'Saved.' : 'Save failed.'),
      });
    } catch (err) {
      setStatus({ tone: 'crit', text: String(err.message || err) });
    } finally {
      setBusy(false);
    }
  }, [callService, config.armNode]);

  // ── rendering ──────────────────────────────────────────────────────────────

  const live = armJoy.receivedAt > 0;
  const armButtons = armJoy.message?.buttons || [];
  const armAxes = armJoy.message?.axes || [];

  const renderRow = (slot, kind) => {
    const bind = parseBind(bindings[slot.key]);
    const isLearning = learning?.key === slot.key;
    const pending = (bindings[slot.key] || '') !== (applied[slot.key] || '');
    const value = kind === 'button' ? armButtons[slot.index] : armAxes[slot.index];
    const active = kind === 'button' ? value === 1 : Math.abs(value || 0) > 0.15;

    return (
      <tr key={slot.key} className={isLearning ? 'is-learning' : ''}>
        <td>
          <span className={`arm-dot ${active ? 'is-on' : ''}`} title="live from the arm frame" />
          <span className="mono">{slot.sdl}</span>
        </td>
        <td className="muted">{slot.action}</td>
        <td className="mono">
          {isLearning ? (
            <span className="chip is-warn">
              {kind === 'button' ? 'press a control…' : 'move a stick…'}
            </span>
          ) : (
            <>
              {bindLabel(bind)}
              {pending && <span className="chip is-warn"> not applied</span>}
            </>
          )}
        </td>
        <td className="arm-actions">
          <button
            type="button"
            className={`btn btn-sm ${isLearning ? 'is-active' : ''}`}
            onClick={() => toggleLearn(slot, kind)}
          >
            {isLearning ? 'Cancel' : 'Bind'}
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={!bind}
            onClick={() => toggleInvert(slot.key)}
            title="This control reads backwards"
          >
            Invert
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={!bind}
            onClick={() => clear(slot.key)}
          >
            Clear
          </button>
        </td>
      </tr>
    );
  };

  return (
    <Panel
      icon={Gamepad2}
      title="Arm mapping"
      bodyClassName="stack"
      actions={
        <span className="mono muted">
          {live ? `${config.topics.armJoy} live` : `no data on ${config.topics.armJoy}`}
        </span>
      }
    >
      {!live && (
        <p className="muted">
          Nothing is publishing <span className="mono">{config.topics.armJoy}</span>. Check that{' '}
          <span className="mono">arm_gamepad</span> is running — until it is, the arm sees no
          controller at all and stops itself after 0.2 s.
        </p>
      )}

      <p className="muted">
        Press <strong>Bind</strong>, then press the switch or move the stick you want. The arm reads
        a standard gamepad layout; these rows say which console control fills each of its controls.
        The dot lights when the arm is seeing that control right now.
      </p>

      {conflicts.length > 0 && (
        <p className="chip is-warn">
          {conflicts.map(([a, b]) => `${a} and ${b} share one control`).join('; ')}
        </p>
      )}

      <table className="arm-map">
        <thead>
          <tr>
            <th>Arm control</th>
            <th>Does what</th>
            <th>Bound to</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr className="arm-group">
            <td colSpan={4}>Sticks</td>
          </tr>
          {AXIS_SLOTS.map((slot) => renderRow(slot, 'axis'))}
          <tr className="arm-group">
            <td colSpan={4}>Buttons</td>
          </tr>
          {BUTTON_SLOTS.map((slot) => renderRow(slot, 'button'))}
        </tbody>
      </table>

      {status && <p className={`chip is-${status.tone}`}>{status.text}</p>}

      <div className="row-actions">
        <button type="button" className="btn" onClick={apply} disabled={busy || !dirty}>
          Apply to node
        </button>
        <button type="button" className="btn" onClick={persist} disabled={busy || dirty}>
          Save on console
        </button>
        <button type="button" className="btn" onClick={reload} disabled={busy}>
          Reload
        </button>
      </div>
      <p className="muted">
        <strong>Apply</strong> takes effect immediately, no restart. <strong>Save</strong> writes it
        to the console so it survives one — apply first, so what gets saved is what was tested.
      </p>
    </Panel>
  );
}
