import { useMemo } from 'react';
import MonitorLayout from '../components/MonitorLayout';
import CameraGrid from '../components/CameraGrid';
import TelemetryPanel from '../components/TelemetryPanel';
import DrivePanel from '../components/DrivePanel';
import SwitchPanel from '../components/SwitchPanel';
import ConsoleLog from '../components/ConsoleLog';
import { useConfig } from '../config';
import { usePersistentState } from '../hooks/usePersistentState';

/** Operator's situational-awareness screen: secondary feeds, live telemetry,
 *  the joystick command path and the rover's own log. */
export default function LeftMonitor() {
  const config = useConfig();
  const [logsCollapsed, setLogsCollapsed] = usePersistentState('gs.left.logsCollapsed', false);
  const cameras = useMemo(
    () => config.cameras.filter((cam) => cam.group === 'aux'),
    [config.cameras],
  );

  return (
    <MonitorLayout title="Left monitor" subtitle="Telemetry & diagnostics" className="screen-left">
      <div className={`left-layout ${logsCollapsed ? 'is-logs-collapsed' : ''}`.trim()}>
        <div className="left-cameras">
          <CameraGrid cameras={cameras} storageKey="left" />
        </div>
        <aside className="left-rail">
          <TelemetryPanel />
          <DrivePanel />
          <SwitchPanel />
        </aside>
        <div className="left-console">
          <ConsoleLog
            collapsible
            collapsed={logsCollapsed}
            onToggleCollapse={() => setLogsCollapsed((value) => !value)}
          />
        </div>
      </div>
    </MonitorLayout>
  );
}
