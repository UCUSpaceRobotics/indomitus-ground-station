import { useCallback, useEffect, useMemo, useState } from 'react';
import { Camera, Circle } from 'lucide-react';
import { useConfig } from '../config';
import { captureAll, datasetServers, fetchDatasetState, toggleRecording } from '../lib/dataset';

const POLL_MS = 2000;
const NOTE_MS = 3000;

/**
 * Dataset capture on the rover: one frame from every camera now, or one a
 * second while recording. Frames are written on the rover's disk at full
 * quality by the camera servers (see lib/dataset.js); the DualSense's 14 and
 * 15 do the same, and the REC state shown here is polled back from the
 * servers, so a press on the gamepad shows up here within a poll.
 */
export default function DatasetControls() {
  const config = useConfig();
  const servers = useMemo(() => datasetServers(config.cameras), [config.cameras]);
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null);

  const refresh = useCallback(async () => {
    setState(await fetchDatasetState(servers));
  }, [servers]);

  useEffect(() => {
    if (servers.length === 0) return undefined;
    let cancelled = false;
    const tick = async () => {
      const next = await fetchDatasetState(servers);
      if (!cancelled) setState(next);
    };
    tick();
    const timer = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [servers]);

  useEffect(() => {
    if (!note) return undefined;
    const timer = setTimeout(() => setNote(null), NOTE_MS);
    return () => clearTimeout(timer);
  }, [note]);

  const run = useCallback(
    async (work) => {
      setBusy(true);
      try {
        setNote(await work());
      } finally {
        setBusy(false);
        refresh();
      }
    },
    [refresh],
  );

  const onCapture = () =>
    run(async () => {
      const count = await captureAll(servers);
      return count > 0
        ? { tone: 'ok', text: `Saved ${count} frame${count === 1 ? '' : 's'}` }
        : { tone: 'crit', text: 'No camera answered' };
    });

  const onRecord = () =>
    run(async () => {
      const { target, count } = await toggleRecording(servers);
      if (target === null) return { tone: 'crit', text: 'No camera answered' };
      return { tone: 'ok', text: `Recording ${target ? 'on' : 'off'} · ${count} cam` };
    });

  if (servers.length === 0) return null;

  const live = state?.live ?? 0;
  const recording = Boolean(state?.recording);

  return (
    <div className={`dataset-controls${recording ? ' is-recording' : ''}`}>
      {note && <span className={`dataset-note is-${note.tone}`}>{note.text}</span>}
      <button
        type="button"
        className="btn btn-sm"
        onClick={onCapture}
        disabled={busy || live === 0}
        title="Save one frame from every running camera on the rover (DualSense 14)"
      >
        <Camera size={13} />
        Capture
      </button>
      <button
        type="button"
        className={`btn btn-sm${recording ? ' is-rec' : ''}`}
        onClick={onRecord}
        disabled={busy || live === 0}
        title={`${recording ? 'Stop' : 'Start'} saving one frame per second from every running camera (DualSense 15)`}
      >
        <Circle size={11} className="dataset-rec-dot" />
        {recording ? 'REC' : 'Record'}
        {state && <span className="mono dataset-saved" title="Frames saved on the rover since the camera servers started">{state.saved}</span>}
      </button>
    </div>
  );
}
