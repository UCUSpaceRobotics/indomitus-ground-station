import { useState } from 'react';
import TopBar from './TopBar';
import SettingsDialog from './SettingsDialog';

/** Page frame shared by every screen: top bar, body, settings dialog. */
export default function MonitorLayout({ title, subtitle, showBack = true, className = '', children }) {
  const [settingsOpen, setSettingsOpen] = useState(false);
  return (
    <div className={`screen ${className}`.trim()}>
      <TopBar
        title={title}
        subtitle={subtitle}
        showBack={showBack}
        onOpenSettings={() => setSettingsOpen(true)}
      />
      <main className="screen-body">{children}</main>
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
