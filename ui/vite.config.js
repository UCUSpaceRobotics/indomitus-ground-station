import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],

  // Relative asset URLs, so a built `dist` works from any path on any static
  // host — including opened straight off disk. Routing is hash-based to match.
  base: './',

  server: {
    // Bind every interface: the UI normally runs on the ground station laptop
    // and is opened from a second machine on the rover network.
    host: true,
    port: 5173,
  },

  preview: {
    host: true,
    port: 4173,
  },

  build: {
    target: 'es2022',
    sourcemap: true,
    chunkSizeWarningLimit: 900,
  },
});
