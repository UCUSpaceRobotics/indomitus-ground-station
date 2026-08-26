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

  // Read through a ref so the draft is seeded only when the dialog opens: an
  // unrelated config update must not clobber edits in progress.
  const configRef = useRef(config);
  configRef.current = config;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setDraft(configRef.current);
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

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

  const save = () => {
    updateConfig(draft);
    onClose();
  };

  // Loads defaults into the draft only — Cancel still backs out, like every
  // other edit in this dialog.
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
                    min="0"
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
              feed, matching the toggle order in <span className="mono">switch_reader_node</span>.
            </p>
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
