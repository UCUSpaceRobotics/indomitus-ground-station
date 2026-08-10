import { Link } from 'react-router-dom';
import { Camera, LayoutDashboard, Monitor, SlidersHorizontal } from 'lucide-react';
import MonitorLayout from '../components/MonitorLayout';
import { useConfig } from '../config';
import { useRos } from '../ros/context';

const SHORTCUTS = [
  ['1 – 9', 'Select camera'],
  ['← / →', 'Previous / next camera'],
  ['G', 'Toggle focus / grid'],
  ['F', 'Fullscreen the camera pane'],
  ['S', 'Bypass the switch box'],
];

/** Launcher for the multi-monitor setup, plus a pre-run check of the link. */
export default function Home() {
  const config = useConfig();
  const { status, url } = useRos();

  const mainCameras = config.cameras.filter((cam) => cam.group === 'main');
  const auxCameras = config.cameras.filter((cam) => cam.group === 'aux');

  return (
    <MonitorLayout title="Indomitus Ground Station" subtitle="Monitor selection" showBack={false}>
      <div className="home">
        <div className="home-cards">
          <Link to="/left" className="home-card">
            <Monitor size={30} />
            <h2>Left monitor</h2>
            <p>Telemetry, command path, control box and rover log</p>
            <span className="muted">{auxCameras.length} cameras</span>
          </Link>
          <Link to="/right" className="home-card">
            <LayoutDashboard size={30} />
            <h2>Right monitor</h2>
            <p>Primary camera wall</p>
            <span className="muted">{mainCameras.length} cameras</span>
          </Link>
        </div>

        <section className="home-section">
          <h3>Single feed</h3>
          <p className="muted">Open one camera full-screen on a dedicated display.</p>
          <div className="home-chips">
            {config.cameras.map((cam) => (
              <Link key={cam.id} to={`/cam/${cam.id}`} className="chip-link">
                <Camera size={13} />
                {cam.name}
              </Link>
            ))}
          </div>
        </section>

        <section className="home-section">
          <h3>Setup</h3>
          <p className="muted">Capture the travel of each panel stick and set the centre deadzone.</p>
          <div className="home-chips">
            <Link to="/calibrate" className="chip-link">
              <SlidersHorizontal size={13} />
              Stick calibration
            </Link>
          </div>
        </section>

        <section className="home-section">
          <h3>Keyboard</h3>
          <dl className="shortcut-list">
            {SHORTCUTS.map(([keys, description]) => (
              <div key={keys}>
                <dt>
                  <kbd>{keys}</kbd>
                </dt>
                <dd>{description}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="home-section">
          <h3>Endpoints</h3>
          <dl className="endpoint-list mono">
            <div>
              <dt>rosbridge</dt>
              <dd>
                {url} <span className={`chip is-${status}`}>{status}</span>
              </dd>
            </div>
            <div>
              <dt>video</dt>
              <dd>{config.videoServerUrl}</dd>
            </div>
            <div>
              <dt>transport</dt>
              <dd>{config.videoMode}</dd>
            </div>
          </dl>
        </section>
      </div>
    </MonitorLayout>
  );
}
