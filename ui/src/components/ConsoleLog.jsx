import { useEffect, useMemo, useRef, useState } from 'react';
import { Pause, Play, Search, Terminal, Trash2 } from 'lucide-react';
import Panel from './Panel';
import { useConfig } from '../config';
import { useTopicBuffer } from '../ros/useTopic';
import { useRos } from '../ros/context';
import { fmtClock, stampToMs } from '../lib/format';

/** rcl_interfaces/msg/Log severity constants. */
const LEVELS = [
  { value: 10, key: 'debug', label: 'Debug' },
  { value: 20, key: 'info', label: 'Info' },
  { value: 30, key: 'warn', label: 'Warn' },
  { value: 40, key: 'error', label: 'Error' },
  { value: 50, key: 'fatal', label: 'Fatal' },
];

function levelKey(level) {
  if (level >= 50) return 'fatal';
  if (level >= 40) return 'error';
  if (level >= 30) return 'warn';
  if (level >= 20) return 'info';
  return 'debug';
}

/**
 * The rover's own log stream (`/rosout`).
 *
 * This replaces a hard-coded four-line "command log" that printed the same
 * fictional startup messages regardless of what the rover was doing.
 */
export default function ConsoleLog() {
  const config = useConfig();
  const { status } = useRos();
  const [minLevel, setMinLevel] = useState(20);
  const [query, setQuery] = useState('');
  const [paused, setPaused] = useState(false);
  const scrollRef = useRef(null);
  const pinnedToBottom = useRef(true);

  const { entries, clear } = useTopicBuffer(config.topics.rosout, 'rcl_interfaces/msg/Log', {
    limit: 500,
    renderMs: 250,
    paused,
  });

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return entries.filter(({ message }) => {
      if ((message?.level ?? 0) < minLevel) return false;
      if (!needle) return true;
      return (
        String(message?.msg || '').toLowerCase().includes(needle) ||
        String(message?.name || '').toLowerCase().includes(needle)
      );
    });
  }, [entries, minLevel, query]);

  // Follow the tail unless the operator has scrolled up to read something.
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedToBottom.current) el.scrollTop = el.scrollHeight;
  }, [filtered]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    pinnedToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
  };

  return (
    <Panel
      icon={Terminal}
      title="Rover log"
      className="panel-fill"
      bodyClassName="console"
      actions={
        <div className="console-controls">
          <label className="field-inline">
            <Search size={13} />
            <input
              type="search"
              value={query}
              placeholder="filter"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <select
            value={minLevel}
            onChange={(event) => setMinLevel(Number(event.target.value))}
            title="Minimum severity"
          >
            {LEVELS.map((level) => (
              <option key={level.value} value={level.value}>
                {level.label}+
              </option>
            ))}
          </select>
          <button
            type="button"
            className={`btn btn-sm ${paused ? 'is-warn' : ''}`}
            onClick={() => setPaused((value) => !value)}
            title={paused ? 'Resume' : 'Pause (messages keep buffering)'}
          >
            {paused ? <Play size={13} /> : <Pause size={13} />}
          </button>
          <button type="button" className="btn btn-sm" onClick={clear} title="Clear">
            <Trash2 size={13} />
          </button>
        </div>
      }
    >
      <div className="console-lines mono" ref={scrollRef} onScroll={onScroll}>
        {filtered.length === 0 ? (
          <p className="console-empty">
            {status === 'connected'
              ? `Nothing on ${config.topics.rosout} at this level yet.`
              : 'Waiting for the rosbridge connection…'}
          </p>
        ) : (
          filtered.map(({ seq, receivedAt, message }) => {
            const key = levelKey(message?.level ?? 20);
            const stamp = stampToMs(message?.stamp) ?? receivedAt;
            return (
              <div key={seq} className={`console-line is-${key}`}>
                <span className="console-time">{fmtClock(stamp)}</span>
                <span className="console-level">{key.toUpperCase()}</span>
                <span className="console-node">{message?.name || '?'}</span>
                <span className="console-msg">{message?.msg}</span>
              </div>
            );
          })
        )}
      </div>
      {paused && <div className="console-paused">Paused — {entries.length} buffered</div>}
    </Panel>
  );
}
