import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Camera,
  Expand,
  LayoutGrid,
  Minimize2,
  MonitorPlay,
  ToggleLeft,
  ToggleRight,
  VideoOff,
} from 'lucide-react';
import CameraFeed from './CameraFeed';
import { useConfig } from '../config';
import { usePersistentState } from '../hooks/usePersistentState';
import { useTopic } from '../ros/useTopic';

/** Square-ish tiling that fills the pane exactly, rather than leaving a ragged
 *  last row the way `auto-fit` + `minmax` does. */
function gridShape(count) {
  const cols = Math.max(1, Math.ceil(Math.sqrt(count)));
  const rows = Math.max(1, Math.ceil(count / cols));
  return {
    gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
    gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))`,
  };
}

function isTypingTarget(target) {
  const tag = target?.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target?.isContentEditable;
}

/**
 * Camera pane driven by the physical toggle switches on the control box
 * (`/switches`, published by console_boards).
 *
 * Switch handling is deliberately fail-open: until the switch box publishes
 * anything, every camera is shown. An operator can also ignore the switches
 * entirely, which matters when the control box is unplugged mid-run and its last
 * message left feeds "off".
 */
export default function CameraGrid({ cameras, storageKey = 'cameras' }) {
  const config = useConfig();
  const containerRef = useRef(null);

  const [viewMode, setViewMode] = usePersistentState(`indomitus.${storageKey}.view`, 'focus');
  const [ignoreSwitches, setIgnoreSwitches] = usePersistentState(
    `indomitus.${storageKey}.ignoreSwitches`,
    false,
  );
  const [selectedId, setSelectedId] = useState(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const switches = useTopic(config.topics.switches, 'std_msgs/Int32MultiArray', {
    throttleMs: 100,
    renderMs: 150,
  });

  // The control box publishes at 20 Hz but its values change rarely. Keying on
  // the contents rather than the array identity keeps the whole pane — and the
  // keyboard listener below — from being rebuilt several times a second.
  const rawStates = switches.message?.data;
  const switchKey = Array.isArray(rawStates) && rawStates.length ? rawStates.join(',') : '';
  const hasSwitchData = switchKey !== '';

  const visibleCameras = useMemo(() => {
    const list = cameras || [];
    if (switchKey === '' || ignoreSwitches) return list;
    const states = switchKey.split(',').map(Number);
    return list.filter((cam) => {
      // A camera whose switch index is outside the reported array stays visible:
      // a short message means "unknown", not "off".
      if (cam.switchIndex >= states.length) return true;
      return states[cam.switchIndex] === 1;
    });
  }, [cameras, switchKey, ignoreSwitches]);

  // Derived, not stored: the previous version kept this in an effect whose
  // dependency array contained a freshly-built array, so it re-ran every render.
  const mainCamera =
    visibleCameras.find((cam) => cam.id === selectedId) || visibleCameras[0] || null;

  const toggleFullscreen = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    if (document.fullscreenElement) document.exitFullscreen?.();
    else el.requestFullscreen?.().catch(() => {});
  }, []);

  useEffect(() => {
    const onChange = () => setIsFullscreen(document.fullscreenElement === containerRef.current);
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  // Operator shortcuts: driving with a joystick in both hands means the mouse is
  // a poor way to switch cameras.
  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTypingTarget(event.target)) return;

      const digit = Number(event.key);
      if (Number.isInteger(digit) && digit >= 1 && digit <= 9) {
        const cam = visibleCameras[digit - 1];
        if (cam) {
          setSelectedId(cam.id);
          setViewMode('focus');
          event.preventDefault();
        }
        return;
      }

      const step = (delta) => {
        if (visibleCameras.length === 0) return;
        const current = visibleCameras.findIndex((cam) => cam.id === mainCamera?.id);
        const next = (current + delta + visibleCameras.length) % visibleCameras.length;
        setSelectedId(visibleCameras[next].id);
      };

      switch (event.key.toLowerCase()) {
        case 'g':
          setViewMode((mode) => (mode === 'grid' ? 'focus' : 'grid'));
          break;
        case 'f':
          toggleFullscreen();
          break;
        case 's':
          setIgnoreSwitches((value) => !value);
          break;
        case 'arrowright':
          step(1);
          break;
        case 'arrowleft':
          step(-1);
          break;
        default:
          return;
      }
      event.preventDefault();
    };

    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [visibleCameras, mainCamera, setViewMode, setIgnoreSwitches, toggleFullscreen]);

  const total = (cameras || []).length;
  const hiddenCount = total - visibleCameras.length;

  return (
    <div className="camera-pane" ref={containerRef}>
      <div className="camera-toolbar">
        <div className="camera-toolbar-info">
          <Camera size={15} />
          <span>
            {visibleCameras.length}/{total} feeds
          </span>
          {hiddenCount > 0 && <span className="muted">· {hiddenCount} switched off</span>}
          {!hasSwitchData && <span className="muted">· no switch data</span>}
        </div>

        <div className="camera-toolbar-actions">
          <button
            type="button"
            className={`btn btn-sm ${ignoreSwitches ? 'is-warn' : ''}`}
            onClick={() => setIgnoreSwitches((value) => !value)}
            title="Ignore the physical switch box and show every camera (S)"
          >
            {ignoreSwitches ? <ToggleRight size={14} /> : <ToggleLeft size={14} />}
            {ignoreSwitches ? 'Switches bypassed' : 'Switches active'}
          </button>
          <div className="btn-group">
            <button
              type="button"
              className={`btn btn-sm ${viewMode === 'focus' ? 'is-active' : ''}`}
              onClick={() => setViewMode('focus')}
              title="Single large feed with a thumbnail strip (G)"
            >
              <MonitorPlay size={14} /> Focus
            </button>
            <button
              type="button"
              className={`btn btn-sm ${viewMode === 'grid' ? 'is-active' : ''}`}
              onClick={() => setViewMode('grid')}
              title="All feeds at equal size (G)"
            >
              <LayoutGrid size={14} /> Grid
            </button>
          </div>
          <button type="button" className="btn btn-sm" onClick={toggleFullscreen} title="Fullscreen (F)">
            {isFullscreen ? <Minimize2 size={14} /> : <Expand size={14} />}
          </button>
        </div>
      </div>

      {visibleCameras.length === 0 || !mainCamera ? (
        <div className="camera-empty">
          <VideoOff size={40} />
          <h3>No cameras enabled</h3>
          <p>
            {hasSwitchData
              ? 'The control box currently reports every feed on this monitor as switched off.'
              : 'No cameras are assigned to this monitor.'}
          </p>
          {hasSwitchData && (
            <button type="button" className="btn" onClick={() => setIgnoreSwitches(true)}>
              Bypass switches
            </button>
          )}
        </div>
      ) : viewMode === 'focus' ? (
        <div className="camera-focus">
          <div className="camera-main">
            <CameraFeed camera={mainCamera} variant="main" />
            <div className="camera-caption">
              <span className="camera-caption-name">{mainCamera.name}</span>
              <span className="mono muted">{mainCamera.topic}</span>
            </div>
          </div>

          {visibleCameras.length > 1 && (
            <div className="camera-strip">
              {visibleCameras.map((cam, index) => (
                <button
                  type="button"
                  key={cam.id}
                  className={`camera-thumb ${cam.id === mainCamera.id ? 'is-active' : ''}`}
                  onClick={() => setSelectedId(cam.id)}
                  title={`${cam.name} — ${cam.topic}`}
                >
                  <CameraFeed camera={cam} variant="thumb" />
                  <span className="camera-thumb-label">
                    <kbd>{index + 1}</kbd>
                    {cam.name}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="camera-mosaic" style={gridShape(visibleCameras.length)}>
          {visibleCameras.map((cam, index) => (
            <button
              type="button"
              key={cam.id}
              className="camera-tile"
              onClick={() => {
                setSelectedId(cam.id);
                setViewMode('focus');
              }}
              title={`${cam.name} — ${cam.topic}`}
            >
              <CameraFeed camera={cam} variant="tile" />
              <span className="camera-thumb-label">
                <kbd>{index + 1}</kbd>
                {cam.name}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
