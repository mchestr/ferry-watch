import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    rollupOptions: {
      output: {
        // Libraries change less often than the app: in their own file, a phone keeps them cached
        // across deploys and only downloads the app again.
        manualChunks: (id) => (id.includes("/node_modules/") ? "vendor" : undefined),
      },
    },
  },
  server: {
    // `npm run dev` talks to a watcher running locally (./run.sh). Leave `changeOrigin` off:
    // the server refuses cross-origin requests, so the proxied Host must stay the dev server's.
    proxy: { "/api": { target: "http://localhost:8765", ws: true } },
  },
});
