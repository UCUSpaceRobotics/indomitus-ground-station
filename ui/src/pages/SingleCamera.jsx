import { Link, useParams } from 'react-router-dom';
import MonitorLayout from '../components/MonitorLayout';
import CameraFeed from '../components/CameraFeed';
import { useConfig } from '../config';

/** One camera, full screen — for a third display or a dedicated arm monitor. */
export default function SingleCamera() {
  const { cameraId } = useParams();
  const config = useConfig();
  const camera = config.cameras.find((cam) => cam.id === cameraId);

  if (!camera) {
    return (
      <MonitorLayout title="Camera not found" subtitle={cameraId}>
        <div className="camera-empty">
          <h3>No camera with id “{cameraId}”</h3>
          <p>It may have been renamed or removed in settings.</p>
          <Link className="btn" to="/">
            Back to monitors
          </Link>
        </div>
      </MonitorLayout>
    );
  }

  return (
    <MonitorLayout title={camera.name} subtitle={camera.topic} className="screen-single">
      <div className="single-feed">
        <CameraFeed camera={camera} variant="main" />
      </div>
    </MonitorLayout>
  );
}
