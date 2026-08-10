import { Activity, BatteryMedium, Compass, Gauge, MapPin, Move, Zap } from 'lucide-react';
import Panel from './Panel';
import Readout, { Bar } from './Readout';
import { useConfig } from '../config';
import { isStale, useTick, useTopic } from '../ros/useTopic';
import {
  compassPoint,
  fmtNumber,
  quaternionToRollPitchDeg,
  quaternionToYawDeg,
  twistSpeed,
} from '../lib/format';

const FIX_STATUS = {
  '-1': 'no fix',
  0: 'fix',
  1: 'SBAS',
  2: 'GBAS',
};

function batteryTone(percent) {
  if (percent === null) return 'default';
  if (percent < 15) return 'crit';
  if (percent < 30) return 'warn';
  return 'ok';
}

/**
 * Live rover telemetry.
 *
 * Every value here comes off a topic. Where a topic is absent or has gone quiet
 * the row shows "—" and dims, instead of the randomised placeholder numbers this
 * panel used to display — a ground station that invents a battery percentage is
 * worse than one that admits it does not know.
 */
export default function TelemetryPanel() {
  const config = useConfig();
  const now = useTick(500);

  const odom = useTopic(config.topics.odom, 'nav_msgs/Odometry', { throttleMs: 200, renderMs: 200 });
  const battery = useTopic(config.topics.battery, 'sensor_msgs/BatteryState', {
    throttleMs: 500,
    renderMs: 500,
  });
  const imu = useTopic(config.topics.imu, 'sensor_msgs/Imu', { throttleMs: 200, renderMs: 200 });
  const gps = useTopic(config.topics.gps, 'sensor_msgs/NavSatFix', { throttleMs: 500, renderMs: 500 });

  const odomStale = isStale(odom.receivedAt, now, 2000);
  const batteryStale = isStale(battery.receivedAt, now, 10_000);
  const imuStale = isStale(imu.receivedAt, now, 2000);
  const gpsStale = isStale(gps.receivedAt, now, 10_000);

  const pose = odom.message?.pose?.pose;
  const speed = twistSpeed(odom.message?.twist?.twist);
  const yaw = quaternionToYawDeg(pose?.orientation);
  const attitude = quaternionToRollPitchDeg(imu.message?.orientation);

  // BatteryState.percentage is 0-1 per the message spec, but plenty of drivers
  // publish 0-100. Accept both rather than showing a 1% battery on a full pack.
  const rawPercent = battery.message?.percentage;
  const percent =
    typeof rawPercent === 'number' && Number.isFinite(rawPercent)
      ? rawPercent > 1.5
        ? rawPercent
        : rawPercent * 100
      : null;
  const voltage = battery.message?.voltage;
  const current = battery.message?.current;

  const fix = gps.message?.status?.status;

  return (
    <Panel icon={Activity} title="Telemetry" bodyClassName="stack">
      <div className="readout-group">
        <Readout
          icon={Gauge}
          label="Ground speed"
          value={fmtNumber(speed, 2)}
          unit="m/s"
          noData={odomStale || speed === null}
          title={config.topics.odom}
        />
        <Readout
          icon={Compass}
          label="Heading"
          value={yaw === null ? '' : `${fmtNumber(yaw, 1)}° ${compassPoint(yaw)}`}
          noData={odomStale || yaw === null}
          title={config.topics.odom}
        />
        <Readout
          icon={MapPin}
          label="Odom X / Y"
          value={pose ? `${fmtNumber(pose.position?.x, 2)} / ${fmtNumber(pose.position?.y, 2)}` : ''}
          unit="m"
          noData={odomStale || !pose}
          title={config.topics.odom}
        />
      </div>

      <div className="readout-group">
        <Readout
          icon={Move}
          label="Roll / Pitch"
          value={attitude ? `${fmtNumber(attitude.roll, 1)}° / ${fmtNumber(attitude.pitch, 1)}°` : ''}
          tone={attitude && Math.max(Math.abs(attitude.roll), Math.abs(attitude.pitch)) > 25 ? 'crit' : 'default'}
          noData={imuStale || !attitude}
          title={config.topics.imu}
        />
      </div>

      <div className="readout-group">
        <Readout
          icon={BatteryMedium}
          label="Battery"
          value={percent === null ? '' : `${fmtNumber(percent, 0)}%`}
          tone={batteryTone(percent)}
          noData={batteryStale || percent === null}
          title={config.topics.battery}
        />
        {!batteryStale && percent !== null && (
          <Bar value={percent} min={0} max={100} tone={batteryTone(percent)} />
        )}
        <Readout
          icon={Zap}
          label="Pack voltage"
          value={`${fmtNumber(voltage, 1)} V${
            typeof current === 'number' && Number.isFinite(current) ? ` · ${fmtNumber(current, 1)} A` : ''
          }`}
          noData={batteryStale || typeof voltage !== 'number' || !Number.isFinite(voltage)}
          title={config.topics.battery}
        />
      </div>

      <div className="readout-group">
        <Readout
          icon={MapPin}
          label="GNSS"
          value={
            gps.message
              ? `${fmtNumber(gps.message.latitude, 5)}, ${fmtNumber(gps.message.longitude, 5)}`
              : ''
          }
          tone={fix === -1 ? 'warn' : 'default'}
          noData={gpsStale || !gps.message}
          title={config.topics.gps}
        />
        <Readout
          label="Fix"
          value={FIX_STATUS[String(fix)] ?? 'unknown'}
          tone={fix === -1 ? 'warn' : fix === undefined ? 'default' : 'ok'}
          noData={gpsStale || fix === undefined}
          title={config.topics.gps}
        />
      </div>
    </Panel>
  );
}
