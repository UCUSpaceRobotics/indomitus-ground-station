import MonitorLayout from '../components/MonitorLayout';
import ArmMappingPanel from '../components/ArmMappingPanel';
import SwitchPanel from '../components/SwitchPanel';

/**
 * Bench page for binding console controls to the arm.
 *
 * The raw switch view sits next to the mapping on purpose: it is the only way
 * to tell a switch that is bound to nothing from one whose board is not
 * reporting at all, and those look identical on the mapping table alone.
 */
export default function ArmMapping() {
  return (
    <MonitorLayout title="Arm mapping" subtitle="Console to arm controls">
      <div className="calib-page">
        <ArmMappingPanel />
        <SwitchPanel />
      </div>
    </MonitorLayout>
  );
}
