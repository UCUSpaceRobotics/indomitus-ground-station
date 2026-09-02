import { Link } from 'react-router-dom';
import { ArrowLeft, Moon, RefreshCw, Settings, Sun } from 'lucide-react';
import { updateConfig, useConfig } from '../config';
import { useRos } from '../ros/context';
import { fmtAge, NO_VALUE } from '../lib/format';

const STATUS_TEXT = {
  connected: 'Connected',
  connecting: 'Connecting',
  reconnecting: 'Reconnecting',
  idle: 'Idle',
};

/** Link health: state, measured round-trip time and rover clock offset. */
function ConnectionBadge() {
  const { status, latencyMs, clockSkewMs, attempt, url, reconnect } = useRos();
  const skewWarn = clockSkewMs !== null && Math.abs(clockSkewMs) > 2000;

  return (
    <div className={`link-badge is-${status}`} title={url}>
      <span className="link-dot" />
      <span className="link-text">{STATUS_TEXT[status] ?? status}</span>
      {status === 'connected' ? (
        <>
          <span className="link-stat mono">{latencyMs === null ? NO_VALUE : `${latencyMs} ms`}</span>
          {skewWarn && (
            <span className="link-stat mono is-warn" title="Rover clock differs from this machine">
              skew {fmtAge(Math.abs(clockSkewMs))}
            </span>
          )}
        </>
      ) : (
        <span className="link-stat mono">{attempt > 0 ? `retry ${attempt}` : '…'}</span>
      )}
      <button type="button" className="btn btn-icon" onClick={reconnect} title="Reconnect now">
        <RefreshCw size={13} />
      </button>
    </div>
  );
}

export default function TopBar({ title, subtitle, onOpenSettings, showBack = true }) {
  const config = useConfig();
  const nextTheme = config.theme === 'dark' ? 'light' : 'dark';

  return (
    <header className="topbar">
      <div className="topbar-left">
        {showBack && (
          <Link to="/" className="btn btn-icon" title="Monitor selection">
            <ArrowLeft size={15} />
          </Link>
        )}
        <div className="topbar-title">
          <strong>{title}</strong>
          {subtitle && <span className="muted">{subtitle}</span>}
        </div>
      </div>

      <div className="topbar-right">
        <ConnectionBadge />
        <button
          type="button"
          className="btn btn-icon"
          onClick={() => updateConfig({ theme: nextTheme })}
          title={`Switch to ${nextTheme} theme`}
        >
          {config.theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
        </button>
        <button type="button" className="btn btn-icon" onClick={onOpenSettings} title="Settings">
          <Settings size={15} />
        </button>
      </div>
    </header>
  );
}
