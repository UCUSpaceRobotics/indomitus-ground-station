import { useEffect } from 'react';
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom';
import RosProvider from './ros/RosProvider';
import Home from './pages/Home';
import LeftMonitor from './pages/LeftMonitor';
import RightMonitor from './pages/RightMonitor';
import SingleCamera from './pages/SingleCamera';
import Calibration from './pages/Calibration';
import ArmMapping from './pages/ArmMapping';
import { useConfig } from './config';

export default function App() {
  const config = useConfig();

  useEffect(() => {
    document.documentElement.dataset.theme = config.theme;
  }, [config.theme]);

  return (
    // Hash routing so /left and /right resolve from any static host — including
    // a `dist` folder opened directly — without server rewrite rules.
    <HashRouter>
      <RosProvider url={config.rosbridgeUrl}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/left" element={<LeftMonitor />} />
          <Route path="/right" element={<RightMonitor />} />
          <Route path="/cam/:cameraId" element={<SingleCamera />} />
          <Route path="/calibrate" element={<Calibration />} />
          <Route path="/arm-mapping" element={<ArmMapping />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </RosProvider>
    </HashRouter>
  );
}
