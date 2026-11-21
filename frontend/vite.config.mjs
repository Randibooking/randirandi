// SPDX-License-Identifier: AGPL-3.0-only
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  base: './',
  plugins: [react()],
  publicDir: false,
  build: {
    outDir: '../.local/styles/mui',
    emptyOutDir: true,
    sourcemap: false,
    assetsInlineLimit: 0,
    rollupOptions: {
      input: fileURLToPath(new URL('./src/main.jsx', import.meta.url)),
      output: {
        entryFileNames: 'app.js',
        assetFileNames: '[name][extname]',
        codeSplitting: false,
      },
    },
  },
});
