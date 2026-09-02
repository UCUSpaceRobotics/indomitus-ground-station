import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, Crosshair, Lock, RotateCcw, Save, SlidersHorizontal, Upload } from 'lucide-react';
import Panel from './Panel';
import { useConfig } from '../config';
import { isStale, useTick, useTopic } from '../ros/useTopic';
import { parameterRequest, useServiceCaller } from '../ros/useService';
import { buttonLabel, sameButton, usePanelButtons } from '../hooks/usePanelButtons';
import { usePersistentState } from '../hooks/usePersistentState';
import { NO_VALUE } from '../lib/format';

const NUM_AXES = 6;
const STICKS = [1, 2, 3];

/** Movement below this (in raw 0..1000 units) is noise, not an intentional push. */
const MOVE_THRESHOLD = 60;

/**
 * One capture per step: both ends of X, both ends of Y, then release to centre.
 *
 * The axis a step belongs to is *logical*. Which physical channel it maps to is
 * discovered from whichever channel actually moved, so it does not matter which
 * ADC pin a pot is soldered to, nor which stick the operator decides is #1.
 */
const STEPS = [
  { axis: 'x', edge: 'max', label: 'Hold X at maximum', hint: 'Push the stick the way you want to read +1.' },
  { axis: 'x', edge: 'min', label: 'Hold X at minimum', hint: 'Push the same axis the opposite way.' },
  { axis: 'y', edge: 'max', label: 'Hold Y at maximum', hint: 'Push the other axis the way you want to read +1.' },
  { axis: 'y', edge: 'min', label: 'Hold Y at minimum', hint: 'Push that axis the opposite way.' },
  { axis: 'both', edge: 'center', label: 'Release to centre', hint: 'Let the stick spring back, hands off.' },
];

const TOTAL_STEPS = STICKS.length * STEPS.length;
const ACTIONS = [
  ['next', 'Confirm / next'],
  ['restart', 'Restart wizard'],
];

/** Logical slot for stick 1-3. Physical channel is resolved by discovery. */
function slotIndex(stick, axis) {
  return (stick - 1) * 2 + (axis === 'y' ? 1 : 0);
}

function emptySlots() {
  return Array.from({ length: NUM_AXES }, () => ({
    physical: null,
    min: null,
    center: null,
    max: null,
  }));
}

function Cell({ value }) {
  if (value === null || value === undefined) return <span className="muted">{NO_VALUE}</span>;
  return <span>{Math.round(value)}</span>;
}

/**
 * Guided calibration for the three panel sticks.
 *
 * Reads `/joy/raw` rather than `/joy`: `/joy` is the output of the mapping being
 * calibrated, both remapped and normalized, so capturing through it would fold
 * the old calibration into the new one.
 */
export default function CalibrationPanel() {
  const config = useConfig();
  const now = useTick(500);
  const callService = useServiceCaller();

  const raw = useTopic(config.topics.joyRaw, 'std_msgs/Int32MultiArray', {
    throttleMs: 0,
    renderMs: 80,
  });

  const [step, setStep] = useState(0);
  const [slots, setSlots] = useState(emptySlots);
  const [deadzone, setDeadzone] = useState(0.05);
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [learning, setLearning] = useState(null);
  const [bindings, setBindings] = usePersistentState('indomitus.calib.bindings', {
    next: null,
    restart: null,
  });

  const live = !isStale(raw.receivedAt, now, 1500);
  const values = useMemo(() => raw.message?.data || [], [raw.message]);

  const done = step >= TOTAL_STEPS;
  const stick = done ? STICKS[STICKS.length - 1] : STICKS[Math.floor(step / STEPS.length)];
  const current = done ? null : STEPS[step % STEPS.length];

  const xSlot = slotIndex(stick, 'x');
  const ySlot = slotIndex(stick, 'y');
  const activeSlots = useMemo(
    () => (current?.axis === 'y' ? [ySlot] : current?.axis === 'x' ? [xSlot] : [xSlot, ySlot]),
    [current, xSlot, ySlot],
  );

  // Sampled at the instant of confirmation, not at the last React flush: the raw
  // topic runs at 50 Hz while this panel renders at ~12 Hz.
  const latest = useRef([]);
  latest.current = values;

  // Baseline is re-taken whenever the step changes; deviation from it is what
  // identifies the channel the operator is pushing.
  const [baseline, setBaseline] = useState([]);
  const [override, setOverride] = useState(null);

  const rezero = useCallback(() => {
    setBaseline(latest.current.slice());
    setOverride(null);
  }, []);

  useEffect(() => {
    rezero();
  }, [step, rezero]);

  // The effect above runs before the first message arrives, which would leave
  // the baseline empty; with an empty baseline every deviation computes as
  // |v - v| = 0, nothing ever passes the threshold, and the wizard deadlocks
  // because advancing the step needs a detection. Seed it as soon as data
  // exists, and re-seed if the frame width ever changes.
  useEffect(() => {
    if (values.length && baseline.length !== values.length) setBaseline(values.slice());
  }, [values, baseline.length]);

  // Deviation is instantaneous, not a running maximum. A running max never
  // decays, so one accidental nudge of another stick would win the step and
  // keep winning; this way letting go of it hands the step straight back.
  const deviation = useMemo(
    () => values.map((v, i) => Math.abs(v - (baseline[i] ?? v))),
    [values, baseline],
  );

  /**
   * A slot's channel is discovered once, on its first ("max") step, and is then
   * fixed for the "min" and centre steps — otherwise min and max could be
   * captured from two different pots.
   */
  const lockedChannel =
    current && current.edge !== 'center' ? slots[activeSlots[0]].physical : null;

  /** Channels already claimed by another axis, which must not be stolen. */
  const claimed = useMemo(() => {
    const own = activeSlots[0];
    return new Set(
      slots.map((s, i) => (i === own ? null : s.physical)).filter((p) => p !== null),
    );
  }, [slots, activeSlots]);

  /** Channel this step will capture from: locked, manually chosen, or detected. */
  const detected = useMemo(() => {
    if (lockedChannel !== null) return lockedChannel;
    if (override !== null) return override;

    let best = null;
    let bestValue = MOVE_THRESHOLD;
    deviation.forEach((d, i) => {
      if (claimed.has(i)) return;
      if (d > bestValue) {
        bestValue = d;
        best = i;
      }
    });
    return best;
  }, [deviation, claimed, lockedChannel, override]);

  const complete = useMemo(
    () =>
      slots.every(
        (s) => s.physical !== null && s.min !== null && s.center !== null && s.max !== null,
      ),
    [slots],
  );

  const duplicate = useMemo(() => {
    const used = slots.map((s) => s.physical).filter((p) => p !== null);
    return used.length !== new Set(used).size;
  }, [slots]);

  const advance = useCallback(() => {
    setStep((s) => s + 1);
    setStatus(null);
  }, []);

  const capture = useCallback(() => {
    if (!current || !live) return;

    // The centre step writes to channels already discovered; every other step
    // needs a channel to have actually moved, or there is nothing to assign.
    if (current.edge !== 'center' && detected === null) {
      setStatus({ tone: 'warn', text: 'Move the axis further — no channel moved enough to identify it.' });
      return;
    }

    setSlots((prev) => {
      const next = prev.map((s) => ({ ...s }));
      if (current.edge === 'center') {
        for (const slot of activeSlots) {
          const physical = next[slot].physical;
          if (physical === null) continue;
          const value = latest.current[physical];
          if (typeof value === 'number') next[slot].center = value;
        }
      } else {
        const slot = activeSlots[0];
        // Claims the channel on the "max" step; on "min" this is a no-op
        // because detected is already pinned to the locked channel.
        next[slot].physical = detected;
        const value = latest.current[detected];
        if (typeof value === 'number') next[slot][current.edge] = value;
      }
      return next;
    });
    advance();
  }, [current, live, detected, activeSlots, advance]);

  const restart = useCallback(() => {
    setSlots(emptySlots());
    setStep(0);
    setStatus(null);
  }, []);

  // A physical button either gets bound (learn mode) or fires its action, never
  // both — otherwise binding a button would also trigger it.
  const onPanelButton = useCallback(
    (button) => {
      if (learning) {
        setBindings((prev) => ({ ...prev, [learning]: button }));
        setLearning(null);
        return;
      }
      if (sameButton(bindings.next, button)) capture();
      else if (sameButton(bindings.restart, button)) restart();
    },
    [learning, bindings, capture, restart, setBindings],
  );

  usePanelButtons(onPanelButton);

  const apply = useCallback(async () => {
    setBusy(true);
    setStatus(null);
    try {
      const response = await callService(
        `${config.joyNode}/set_parameters`,
        'rcl_interfaces/srv/SetParameters',
        parameterRequest([
          ['axis_map', slots.map((s, i) => s.physical ?? i), 'int_array'],
          ['axis_min', slots.map((s) => s.min ?? 0)],
          ['axis_center', slots.map((s) => s.center ?? 500)],
          ['axis_max', slots.map((s) => s.max ?? 1000)],
          ['deadzone', deadzone],
        ]),
      );
      const rejected = (response?.results || []).filter((r) => !r.successful);
      if (rejected.length) {
        setStatus({ tone: 'crit', text: rejected.map((r) => r.reason).join('; ') || 'Rejected' });
      } else {
        setStatus({ tone: 'ok', text: 'Applied — /joy is using the new mapping.' });
      }
    } catch (err) {
      setStatus({ tone: 'crit', text: String(err.message || err) });
    } finally {
      setBusy(false);
    }
  }, [callService, slots, config.joyNode, deadzone]);

  const persist = useCallback(async () => {
    setBusy(true);
    setStatus(null);
    try {
      const response = await callService(
        `${config.joyNode}/save_calibration`,
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
  }, [callService, config.joyNode]);

  return (
    <Panel
      icon={SlidersHorizontal}
      title="Stick calibration"
      bodyClassName="stack"
      actions={
        <span className="mono muted">
          {live ? `${config.topics.joyRaw} live` : `no data on ${config.topics.joyRaw}`}
        </span>
      }
    >
      {!live && (
        <p className="panel-note is-warn">
          Nothing arriving on <span className="mono">{config.topics.joyRaw}</span>. Start{' '}
          <span className="mono">console_boards</span> — calibration needs the raw values.
        </p>
      )}

      <div className="calib-step">
        <div className="calib-step-head">
          <span className="chip">
            {done ? 'All sticks captured' : `Stick ${stick} of ${STICKS.length}`}
          </span>
          <span className="mono muted">
            step {Math.min(step + 1, TOTAL_STEPS)} / {TOTAL_STEPS}
          </span>
        </div>

        {done ? (
          <p className="calib-instruction">
            Every logical axis has a channel, both endpoints and a centre. Apply, then save.
          </p>
        ) : (
          <>
            <p className="calib-instruction">
              Stick {stick} · {current.label}
            </p>
            <p className="muted">{current.hint}</p>
          </>
        )}

        {!done && current.edge !== 'center' && (
          <div className={`calib-detect ${detected === null ? 'is-idle' : 'is-found'}`}>
            <Crosshair size={14} />
            {lockedChannel !== null ? (
              <span>
                Locked to channel <strong>{lockedChannel}</strong> — the same pot that gave this
                axis its maximum.
              </span>
            ) : detected === null ? (
              <>
                <span>Waiting for movement… (claimed channels are ignored)</span>
                <button type="button" className="btn btn-sm" onClick={rezero}>
                  Re-zero
                </button>
              </>
            ) : (
              <span>
                Channel <strong>{detected}</strong> moved{' '}
                <span className="mono">{Math.round(deviation[detected] ?? 0)}</span> — it will
                become stick {stick} {current.axis.toUpperCase()}
                {override !== null && ' (chosen manually)'}
              </span>
            )}
          </div>
        )}

        <div className="calib-live mono">
          {values.map((v, i) => {
            const isClaimed = claimed.has(i);
            const selectable = !done && current.edge !== 'center' && lockedChannel === null && !isClaimed;
            return (
              <button
                key={i}
                type="button"
                className={`calib-live-axis ${detected === i ? 'is-detected' : ''} ${
                  isClaimed ? 'is-claimed' : ''
                }`}
                disabled={!selectable}
                title={
                  isClaimed
                    ? 'Already assigned to another axis'
                    : selectable
                      ? 'Click to assign this channel manually'
                      : undefined
                }
                onClick={() => setOverride(i)}
              >
                <span className="muted">
                  ch {i}
                  {isClaimed && <Lock size={9} />}
                </span>
                <strong>{live ? v : NO_VALUE}</strong>
              </button>
            );
          })}
        </div>

        <div className="btn-group">
          <button type="button" className="btn is-primary" onClick={capture} disabled={done || !live}>
            <Check size={14} /> Confirm
            {bindings.next && <span className="muted"> · {buttonLabel(bindings.next)}</span>}
          </button>
          <button type="button" className="btn" onClick={restart}>
            <RotateCcw size={14} /> Restart
          </button>
        </div>
      </div>

      <div className="subhead">
        <span>Panel buttons</span>
        {learning && <span className="chip is-warn">press a button…</span>}
      </div>
      <p className="field-hint">
        Bind the panel's own buttons so calibration can be driven with both hands on the sticks. Any
        switch on either board works.
      </p>
      <div className="calib-bindings">
        {ACTIONS.map(([action, label]) => (
          <div className="calib-binding" key={action}>
            <span>{label}</span>
            <span className="mono muted">{buttonLabel(bindings[action]) || 'unbound'}</span>
            <button
              type="button"
              className={`btn btn-sm ${learning === action ? 'is-active' : ''}`}
              onClick={() => setLearning(learning === action ? null : action)}
            >
              {learning === action ? 'Cancel' : 'Bind'}
            </button>
          </div>
        ))}
      </div>

      <label className="field">
        <span>Centre deadzone {(deadzone * 100).toFixed(0)}%</span>
        <input
          type="range"
          min="0"
          max="40"
          step="1"
          value={Math.round(deadzone * 100)}
          onChange={(event) => setDeadzone(Number(event.target.value) / 100)}
        />
      </label>
      <p className="field-hint">
        Travel inside the deadzone reads as zero; the rest is rescaled to full range, so there is no
        jump at the edge.
      </p>

      <div className="subhead">
        <span>Result</span>
      </div>
      <div className="calib-table mono">
        <div className="calib-table-head">
          <span>Axis</span>
          <span>Ch</span>
          <span>Min</span>
          <span>Centre</span>
          <span>Max</span>
        </div>
        {slots.map((s, index) => (
          <div className="calib-table-row" key={index}>
            <span className="muted">
              stick {Math.floor(index / 2) + 1} {index % 2 === 0 ? 'X' : 'Y'}
            </span>
            <span>{s.physical === null ? <span className="muted">{NO_VALUE}</span> : s.physical}</span>
            <Cell value={s.min} />
            <Cell value={s.center} />
            <Cell value={s.max} />
          </div>
        ))}
      </div>

      {duplicate && (
        <p className="panel-note is-warn">
          The same channel is assigned to more than one axis. Restart and make sure each movement
          drives a different pot.
        </p>
      )}

      <div className="btn-group">
        <button
          type="button"
          className="btn is-primary"
          onClick={apply}
          disabled={!complete || busy}
        >
          <Upload size={14} /> Apply to node
        </button>
        <button type="button" className="btn" onClick={persist} disabled={busy}>
          <Save size={14} /> Save on rover
        </button>
      </div>

      {!complete && (
        <p className="field-hint">
          Finish all {TOTAL_STEPS} captures before applying — a missing endpoint would map that axis
          from a default it never measured.
        </p>
      )}
      {status && <p className={`panel-note is-${status.tone}`}>{status.text}</p>}
    </Panel>
  );
}
