/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// `npm run build` writes the SPA into arc/tower/static/, which FastAPI serves at `/`
// (arc/tower/api.py). `npm run dev` proxies /api to a local `arc tower serve --v2 --local`.
const outDir = fileURLToPath(new URL("../arc/tower/static", import.meta.url));

export default defineConfig({
  plugins: [react()],
  build: {
    outDir,
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    host: "127.0.0.1",
    proxy: { "/api": "http://127.0.0.1:4174" },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
