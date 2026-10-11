import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: {
    // `npm run dev` talks to a watcher running locally (./run.sh).
    proxy: { "/api": { target: "http://localhost:8765", ws: true } },
  },
});
