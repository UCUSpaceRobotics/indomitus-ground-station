import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import App from './App.jsx';
import { getConfig } from './config.js';

// Applied before the first paint so a saved light theme does not flash dark.
document.documentElement.dataset.theme = getConfig().theme;

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
