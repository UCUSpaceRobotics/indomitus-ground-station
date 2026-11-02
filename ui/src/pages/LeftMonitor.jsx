import { useMemo } from 'react';
import MonitorLayout from '../components/MonitorLayout';
import CameraGrid from '../components/CameraGrid';
import { useConfig } from '../config';

/** Primary camera wall. Chrome is kept to a single bar so the feeds get the
 *  whole screen. */
export default function LeftMonitor() {
  const config = useConfig();
  const cameras = useMemo(
    () => config.cameras.filter((cam) => cam.group === 'main'),
    [config.cameras],
  );

  return (
    <MonitorLayout title="Left monitor" subtitle="Camera wall" className="screen-left">
      <CameraGrid cameras={cameras} storageKey="main" />
    </MonitorLayout>
  );
}
