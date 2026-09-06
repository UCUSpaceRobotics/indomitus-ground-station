import { useCallback, useState } from 'react';
import { Beaker, Crosshair, RefreshCw } from 'lucide-react';
import Panel from './Panel';
import Readout from './Readout';
import { useConfig } from '../config';
import { isStale, useTick, useTopic } from '../ros/useTopic';
import { useServiceCaller } from '../ros/useService';
import { fmtNumber } from '../lib/format';

/** Longer than the node's own 3 s CAN timeout, so a slow bus reads as a slow
 *  bus rather than as a dead bridge. Tare adds a settle delay on top. */
const READ_TIMEOUT_MS = 5000;
const TARE_TIMEOUT_MS = 8000;

/**
 * The science container's two load cells, and everything the board can be told
 * to do about them.
 *
 * The container ESP32-C3 answers exactly three commands — read both scales,
 * zero the left, zero the right — so this panel is those three plus the value
 * they act on. There is no lid on the board and therefore no open/close here.
 *
 * The readout follows `/container/weights`, which container_can_node polls on
 * a timer, rather than calling the service on an interval from the browser:
 * one poller on the rover keeps the CAN bus quiet no matter how many screens
 * are open. Every control is also in the rover-function catalogue, so the same
 * actions can be bound to console switches.
 */
export default function ContainerPanel() {
  const config = useConfig();
  const now = useTick(500);
  const callService = useServiceCaller();

  const [busy, setBusy] = useState(null);
  const [status, setStatus] = useState(null);

  const weights = useTopic(config.topics.containerWeights, 'std_msgs/Float32MultiArray', {
    throttleMs: 0,
    renderMs: 200,
  });

  // The node polls at 2 Hz by default; three missed cycles is a real outage,
  // not a hiccup. Also covers the stream being switched off deliberately.
  const live = !isStale(weights.receivedAt, now, 2000);
  const data = weights.message?.data;
  const left = data?.[0];
  const right = data?.[1];
  const total = live && data?.length >= 2 ? left + right : null;

  const run = useCallback(
    async (action, service, timeoutMs) => {
      setBusy(action);
      setStatus(null);
      try {
        const response = await callService(service, 'std_srvs/Trigger', {}, timeoutMs);
        setStatus({
          tone: response?.success ? 'ok' : 'crit',
          text: response?.message || (response?.success ? 'OK' : 'failed'),
        });
      } catch (err) {
        setStatus({ tone: 'crit', text: String(err.message || err) });
      } finally {
        setBusy(null);
      }
    },
    [callService],
  );

  const readWeight = () => run('read', '/container/read_weight', READ_TIMEOUT_MS);
  const tareLeft = () => run('tare-left', '/container/tare_left', TARE_TIMEOUT_MS);
  const tareRight = () => run('tare-right', '/container/tare_right', TARE_TIMEOUT_MS);

  return (
    <Panel
      icon={Beaker}
      title="Container"
      bodyClassName="stack"
      actions={
        <span className={`chip ${live ? 'is-ok' : 'is-idle'}`}>
          {live ? `${fmtNumber(weights.hz, 0)} Hz` : 'no stream'}
        </span>
      }
    >
      <div className="readout-group">
        <Readout
          label="Left"
          value={fmtNumber(left, 1)}
          unit="g"
          noData={!live || left === undefined}
        />
        <Readout
          label="Right"
          value={fmtNumber(right, 1)}
          unit="g"
          noData={!live || right === undefined}
        />
        <Readout
          label="Total"
          value={fmtNumber(total, 1)}
          unit="g"
          noData={total === null}
        />
      </div>

      <div className="btn-group">
        <button type="button" className="btn btn-sm" onClick={readWeight} disabled={busy !== null}>
          <RefreshCw size={14} /> {busy === 'read' ? 'Reading…' : 'Read'}
        </button>
        <button type="button" className="btn btn-sm" onClick={tareLeft} disabled={busy !== null}>
          <Crosshair size={14} /> {busy === 'tare-left' ? 'Taring…' : 'Tare left'}
        </button>
        <button type="button" className="btn btn-sm" onClick={tareRight} disabled={busy !== null}>
          <Crosshair size={14} /> {busy === 'tare-right' ? 'Taring…' : 'Tare right'}
        </button>
      </div>

      {status && <p className={`panel-note is-${status.tone}`}>{status.text}</p>}
    </Panel>
  );
}
