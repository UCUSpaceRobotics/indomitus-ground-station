import MonitorLayout from '../components/MonitorLayout';
import CalibrationPanel from '../components/CalibrationPanel';
import DrivePanel from '../components/DrivePanel';

/**
 * Bench page for calibrating the panel sticks.
 *
 * The command path sits next to the wizard on purpose: after applying, the
 * operator can watch `/joy` and `/cmd_vel` react without switching screens,
 * which is the only real confirmation the calibration took.
 */
export default function Calibration() {
  return (
    <MonitorLayout title="Stick calibration" subtitle="Joystick setup">
      <div className="calib-page">
        <CalibrationPanel />
        <DrivePanel />
      </div>
    </MonitorLayout>
  );
}
