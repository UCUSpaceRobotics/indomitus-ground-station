import { useCallback, useEffect, useRef, useState } from 'react';
import { Plus, RotateCcw, Trash2, Search } from 'lucide-react';
import {
  DEFAULT_CAMERAS,
  DEFAULT_TOPICS,
  VIDEO_MODES,
  defaultConfig,
  updateConfig,
  useConfig,
} from '../config';
import { useRos } from '../ros/context';
import { parameterRequest, useServiceCaller } from '../ros/useService';
import {
  CUSTOM_KEY,
  FUNCTION_GROUPS,
  SOURCE_JOY,
  SOURCE_LIMITS,
  SOURCE_SWITCHES,
  resolveCall,
} from '../lib/roverFunctions';

/** An index below zero is a bind whose control was taken by something else. */
const UNBOUND = -1;

/**
 * How long the camera table sits still before it is written back to the config.
 * Long enough that typing a topic does not tear the stream down on every
 * keystroke, short enough that a click-and-close still lands.
 */
const CAMERA_SAVE_DELAY = 600;

const sameCameras = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const TOPIC_LABELS = {
  switches: 'Camera switches',
  joy: 'Joystick',
  cmdVel: 'Drive command',
  servoTwist: 'Servo delta twist',
  odom: 'Odometry',
  battery: 'Battery state',
  imu: 'IMU',
  gps: 'GNSS fix',
  rosout: 'Log',
};

/**
 * Field-editable configuration.
 *
 * Everything the previous build hard-coded — bridge URL, camera names, camera
 * topics, which monitor a camera belongs to — is editable here and persisted
 * locally, so retargeting the UI at a different rover does not need a rebuild.
 */
export default function SettingsDialog({ open, onClose }) {
  const config = useConfig();
  const { ros, connected } = useRos();
  const dialogRef = useRef(null);
  const [draft, setDraft] = useState(config);
  const [topicNames, setTopicNames] = useState([]);
  const [scanState, setScanState] = useState('idle');
  const [bindStatus, setBindStatus] = useState(null);
  const [cameraSave, setCameraSave] = useState('idle');
  const callService = useServiceCaller();

  /** Last camera list handed to the config, so a no-op edit is not re-saved. */
  const savedCamerasRef = useRef(config.cameras);

  // Read through a ref so the draft is seeded only when the dialog opens: an
  // unrelated config update must not clobber edits in progress.
  const configRef = useRef(config);
  configRef.current = config;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setDraft(configRef.current);
      savedCamerasRef.current = configRef.current.cameras;
      setCameraSave('idle');
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  // The camera table saves itself. Retargeting a feed is done *while looking at
  // the feed*, so the tiles behind the dialog have to follow the edit, and an
  // operator who closes the dialog to check one must not lose the change. The
  // rest of the dialog still commits on Apply.
  //
  // Deliberately not keyed on `open`: closing mid-debounce must not cancel the
  // pending write. Once the write lands, draft and saved agree, so a closed
  // dialog is simply idle.
  useEffect(() => {
    if (sameCameras(draft.cameras, savedCamerasRef.current)) return undefined;
    setCameraSave('pending');
    const timer = setTimeout(() => {
      savedCamerasRef.current = draft.cameras;
      updateConfig({ cameras: draft.cameras });
      setCameraSave('saved');
    }, CAMERA_SAVE_DELAY);
    return () => clearTimeout(timer);
  }, [draft.cameras]);

  const scanTopics = useCallback(() => {
    if (!ros) return;
    setScanState('scanning');
    ros.getTopics(
      (result) => {
        const names = Array.isArray(result?.topics) ? [...result.topics].sort() : [];
        setTopicNames(names);
        setScanState(names.length ? 'done' : 'empty');
      },
      () => setScanState('failed'),
    );
  }, [ros]);

  const patchCamera = (index, patch) =>
    setDraft((prev) => ({
      ...prev,
      cameras: prev.cameras.map((cam, i) => (i === index ? { ...cam, ...patch } : cam)),
    }));

  const removeCamera = (index) =>
    setDraft((prev) => ({ ...prev, cameras: prev.cameras.filter((_, i) => i !== index) }));

  const addCamera = () =>
    setDraft((prev) => ({
      ...prev,
      cameras: [
        ...prev.cameras,
        {
          id: `cam${Date.now().toString(36)}`,
          name: 'New camera',
          topic: '/camera/new/image_raw',
          switchIndex: prev.cameras.length,
          group: 'main',
        },
      ],
    }));

  // ── rover function binds ──────────────────────────────────────────────
  //
  // Claiming a control releases it everywhere else, rather than refusing the
  // claim. A console gets rewired mid-competition and the operator wants the
  // switch they just picked; making them hunt for the old owner first is how
  // you end up with two things on one switch when the node finally accepts it.
  // Whatever loses its control is left visible and unbound, never deleted.
  const claimControl = (source, index, exceptBind) =>
    setDraft((prev) => ({
      ...prev,
      cameras: prev.cameras.map((cam) =>
        source === SOURCE_SWITCHES && cam.switchIndex === index
          ? { ...cam, switchIndex: UNBOUND }
          : cam,
      ),
      functionBinds: prev.functionBinds.map((bind, i) =>
        i !== exceptBind && bind.source === source && bind.index === index
          ? { ...bind, index: UNBOUND }
          : bind,
      ),
    }));

  const patchBind = (index, patch) => {
    if (patch.index !== undefined || patch.source !== undefined) {
      const current = draft.functionBinds[index];
      const source = patch.source ?? current.source;
      const next = patch.index ?? current.index;
      if (next >= 0) claimControl(source, next, index);
    }
    setDraft((prev) => ({
      ...prev,
      functionBinds: prev.functionBinds.map((bind, i) =>
        i === index ? { ...bind, ...patch } : bind,
      ),
    }));
  };

  const removeBind = (index) =>
    setDraft((prev) => ({
      ...prev,
      functionBinds: prev.functionBinds.filter((_, i) => i !== index),
    }));

  const addBind = () =>
    setDraft((prev) => ({
      ...prev,
      functionBinds: [
        ...prev.functionBinds,
        {
          id: `bind${Date.now().toString(36)}`,
          function: 'drive_power',
          source: SOURCE_SWITCHES,
          index: UNBOUND,
          invert: false,
        },
      ],
    }));

  /** Binds that are actually wired and complete enough to send. */
  const liveBinds = draft.functionBinds.filter(
    (b) => b.index >= 0 && (b.function !== CUSTOM_KEY || b.service),
  );

  const applyBinds = useCallback(async () => {
    setBindStatus({ tone: 'muted', text: 'Applying…' });
    const binds = draft.functionBinds.filter(
      (b) => b.index >= 0 && (b.function !== CUSTOM_KEY || b.service),
    );
    const cameraSwitches = draft.cameras
      .map((cam) => cam.switchIndex)
      .filter((i) => i >= 0);
    try {
      // Both in one batch: the node validates binds against the camera bits,
      // so sending them separately would briefly compare the new binds with
      // the old camera claims and refuse a set that is actually fine.
      const response = await callService(
        `${draft.interpreterNode}/set_parameters`,
        'rcl_interfaces/srv/SetParameters',
        parameterRequest([
          ['binds', JSON.stringify(binds), 'string'],
          ['camera_switches', cameraSwitches.length ? cameraSwitches : [UNBOUND], 'int_array'],
        ]),
      );
      const rejected = (response?.results || []).filter((r) => !r.successful);
      if (rejected.length) {
        setBindStatus({
          tone: 'crit',
          text: rejected.map((r) => r.reason).join('; ') || 'Rejected by the node',
        });
        return;
      }
      const saved = await callService(
        `${draft.interpreterNode}/save_bindings`,
        'std_srvs/srv/Trigger',
      );
      setBindStatus(
        saved?.success
          ? { tone: 'ok', text: `${binds.length} binds applied and saved.` }
          : { tone: 'warn', text: `Applied, but not saved: ${saved?.message || 'unknown error'}` },
      );
    } catch (err) {
      setBindStatus({ tone: 'crit', text: String(err.message || err) });
    }
  }, [callService, draft.functionBinds, draft.cameras, draft.interpreterNode]);

  const save = () => {
    savedCamerasRef.current = draft.cameras;
    updateConfig(draft);
    onClose();
  };

  // Loads defaults into the draft, so Cancel still backs the rest of the dialog
  // out. The camera table is the exception: it autosaves, so restoring defaults
  // does replace the cameras for real.
  const restoreDefaults = () => setDraft(defaultConfig());

  return (
    <dialog ref={dialogRef} className="settings" onClose={onClose}>
      <form method="dialog" onSubmit={(event) => event.preventDefault()}>
        <header className="settings-head">
          <h2>Ground station settings</h2>
          <button type="button" className="btn btn-sm" onClick={onClose}>
            Close
          </button>
        </header>

        <div className="settings-body">
          <section>
            <h3>Connection</h3>
            <label className="field">
              <span>rosbridge websocket</span>
              <input
                className="mono"
                value={draft.rosbridgeUrl}
                onChange={(event) => setDraft({ ...draft, rosbridgeUrl: event.target.value })}
                placeholder="ws://rover.local:9090"
              />
            </label>
            <label className="field">
              <span>web_video_server</span>
              <input
                className="mono"
                value={draft.videoServerUrl}
                onChange={(event) => setDraft({ ...draft, videoServerUrl: event.target.value })}
                placeholder="http://rover.local:8080"
              />
            </label>
            <p className="field-hint">
              Defaults follow the host this page was served from, so opening the UI from another
              laptop on the rover network works without editing anything. Overridable per window with{' '}
              <span className="mono">?ros=…&amp;video=…</span>.
            </p>
          </section>

          <section>
            <h3>Video</h3>
            <div className="settings-row">
              <label className="field">
                <span>Transport</span>
                <select
                  value={draft.videoMode}
                  onChange={(event) => setDraft({ ...draft, videoMode: event.target.value })}
                >
                  <option value={VIDEO_MODES.mjpeg}>MJPEG via web_video_server</option>
                  <option value={VIDEO_MODES.ros}>CompressedImage via rosbridge</option>
                </select>
              </label>
              <label className="field">
                <span>Quality {draft.videoQuality}</span>
                <input
                  type="range"
                  min="10"
                  max="95"
                  value={draft.videoQuality}
                  onChange={(event) =>
                    setDraft({ ...draft, videoQuality: Number(event.target.value) })
                  }
                />
              </label>
              <label className="field">
                <span>Width (0 = native)</span>
                <input
                  type="number"
                  min="0"
                  step="80"
                  value={draft.videoWidth}
                  onChange={(event) => setDraft({ ...draft, videoWidth: Number(event.target.value) })}
                />
              </label>
            </div>
            <p className="field-hint">
              MJPEG is lighter on the link. The rosbridge transport needs no extra rover-side node
              and reports true frame rate and frame age, which is what lets a frozen feed be
              distinguished from a live one.
            </p>
          </section>

          <section>
            <div className="settings-subhead">
              <h3>Cameras</h3>
              <div className="settings-subhead-actions">
                <span className={`save-state is-${cameraSave}`}>
                  {cameraSave === 'pending' ? 'Saving…' : 'Saved'}
                </span>
                <button type="button" className="btn btn-sm" onClick={scanTopics} disabled={!connected}>
                  <Search size={13} />
                  {scanState === 'scanning' ? 'Scanning…' : 'Scan topics'}
                </button>
                <button type="button" className="btn btn-sm" onClick={addCamera}>
                  <Plus size={13} /> Add
                </button>
              </div>
            </div>
            {scanState === 'failed' && <p className="field-hint is-warn">Topic scan failed.</p>}
            {scanState === 'done' && (
              <p className="field-hint">{topicNames.length} topics available for autocomplete.</p>
            )}
            <datalist id="ros-topic-names">
              {topicNames.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>

            <div className="camera-table">
              <div className="camera-table-head">
                <span>Name</span>
                <span>Image topic</span>
                <span>Switch</span>
                <span>Monitor</span>
                <span />
              </div>
              {draft.cameras.map((cam, index) => (
                <div className="camera-table-row" key={cam.id}>
                  <input
                    value={cam.name}
                    onChange={(event) => patchCamera(index, { name: event.target.value })}
                  />
                  <input
                    className="mono"
                    list="ros-topic-names"
                    value={cam.topic}
                    onChange={(event) => patchCamera(index, { topic: event.target.value })}
                  />
                  <input
                    type="number"
                    // -1 once a rover function has claimed this bit: the feed
                    // is then never selected, and the row shows why.
                    min={UNBOUND}
                    max="31"
                    value={cam.switchIndex}
                    onChange={(event) =>
                      patchCamera(index, { switchIndex: Number(event.target.value) })
                    }
                  />
                  <select
                    value={cam.group}
                    onChange={(event) => patchCamera(index, { group: event.target.value })}
                  >
                    <option value="main">Right</option>
                    <option value="aux">Left</option>
                  </select>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => removeCamera(index)}
                    title="Remove"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              ))}
            </div>
            <p className="field-hint">
              Switch index is the bit in <span className="mono">/switches</span> that enables the
              feed, matching the toggle order in <span className="mono">console_boards</span>.
              Camera rows are saved as you edit them — Cancel does not undo them.
            </p>
          </section>

          <section>
            <div className="settings-subhead">
              <h3>Rover functions</h3>
              <div className="settings-subhead-actions">
                <button type="button" className="btn btn-sm" onClick={addBind}>
                  <Plus size={13} /> Add
                </button>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={applyBinds}
                  disabled={!connected}
                  title={connected ? '' : 'rosbridge is not connected'}
                >
                  Apply to rover
                </button>
              </div>
            </div>
            <p className="field-hint">
              A latching switch sends its position as <span className="mono">SetBool</span>; a
              joystick button has no position and fires the <span className="mono">Trigger</span>
              {' '}twin instead. Binding a control here releases it from whichever camera or
              function held it before.
            </p>
            {bindStatus && (
              <p className={`field-hint is-${bindStatus.tone}`}>{bindStatus.text}</p>
            )}

            <div className="bind-table">
              <div className="bind-table-head">
                <span>Function</span>
                <span>Source</span>
                <span>Index</span>
                <span>Calls</span>
                <span>Inverted</span>
                <span />
              </div>
              {draft.functionBinds.map((bind, index) => {
                const call = resolveCall(bind);
                const unbound = bind.index < 0;
                return (
                  <div
                    className={`bind-table-row ${unbound ? 'is-unbound' : ''}`}
                    key={bind.id || index}
                  >
                    <select
                      value={bind.function}
                      onChange={(event) => patchBind(index, { function: event.target.value })}
                    >
                      {FUNCTION_GROUPS.map((group) => (
                        <optgroup key={group.label} label={group.label}>
                          {group.items.map((fn) => (
                            <option key={fn.key} value={fn.key}>
                              {fn.label}
                            </option>
                          ))}
                        </optgroup>
                      ))}
                      <option value={CUSTOM_KEY}>Custom service…</option>
                    </select>

                    <select
                      value={bind.source}
                      onChange={(event) => patchBind(index, { source: event.target.value })}
                    >
                      <option value={SOURCE_SWITCHES}>Panel switch</option>
                      <option value={SOURCE_JOY}>Joystick button</option>
                    </select>

                    <input
                      type="number"
                      min={UNBOUND}
                      max={SOURCE_LIMITS[bind.source] - 1}
                      value={bind.index}
                      onChange={(event) =>
                        patchBind(index, { index: Number(event.target.value) })
                      }
                    />

                    {bind.function === CUSTOM_KEY ? (
                      <input
                        className="mono"
                        placeholder="/my/service"
                        value={bind.service || ''}
                        onChange={(event) => patchBind(index, { service: event.target.value })}
                      />
                    ) : (
                      <span className="mono muted bind-call">
                        {call.service || '—'}
                        {call.kind && <em className="bind-kind">{call.kind}</em>}
                      </span>
                    )}

                    <input
                      type="checkbox"
                      checked={Boolean(bind.invert)}
                      // A Trigger has no position, so there is nothing to invert.
                      disabled={call.kind === 'trigger'}
                      onChange={(event) => patchBind(index, { invert: event.target.checked })}
                    />

                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => removeBind(index)}
                      title="Remove"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                );
              })}
            </div>
            {draft.functionBinds.length === 0 && (
              <p className="field-hint">Nothing bound. The console cannot reach these services.</p>
            )}
            {draft.functionBinds.length > liveBinds.length && (
              <p className="field-hint is-warn">
                {draft.functionBinds.length - liveBinds.length} bind(s) have no control and will
                not be sent — give them an index, or remove them.
              </p>
            )}
          </section>

          <section>
            <h3>Telemetry topics</h3>
            <div className="topic-grid">
              {Object.keys(DEFAULT_TOPICS).map((key) => (
                <label className="field" key={key}>
                  <span>{TOPIC_LABELS[key] || key}</span>
                  <input
                    className="mono"
                    list="ros-topic-names"
                    value={draft.topics[key] ?? ''}
                    onChange={(event) =>
                      setDraft({ ...draft, topics: { ...draft.topics, [key]: event.target.value } })
                    }
                  />
                </label>
              ))}
            </div>
          </section>
        </div>

        <footer className="settings-foot">
          <button type="button" className="btn" onClick={restoreDefaults}>
            <RotateCcw size={14} /> Restore defaults
          </button>
          <div className="settings-foot-right">
            <span className="muted">
              {draft.cameras.length} cameras · {DEFAULT_CAMERAS.length} shipped
            </span>
            <button type="button" className="btn" onClick={onClose}>
              Cancel
            </button>
            <button type="button" className="btn is-primary" onClick={save}>
              Apply
            </button>
          </div>
        </footer>
      </form>
    </dialog>
  );
}
