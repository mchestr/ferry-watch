import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/** The release this bundle belongs to: FW_VERSION if set, else pyproject.toml's version, else "dev". */
function appVersion(): string {
  if (process.env.FW_VERSION) return process.env.FW_VERSION;
  try {
    // The repo's root in a checkout; /pyproject.toml in the image's web stage.
    const pyproject = readFileSync(new URL("../pyproject.toml", import.meta.url), "utf8");
    return /^version\s*=\s*"([^"]+)"/m.exec(pyproject)?.[1] ?? "dev";
  } catch {
    return "dev";
  }
}

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  define: { __APP_VERSION__: JSON.stringify(appVersion()) },
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
