import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.{ts,tsx}"],
      setupFiles: ["./src/test/setup.ts"],
      // The ferries (and the owner) are on Pacific time, which also has DST transitions to test against.
      env: { TZ: "America/Vancouver" },
      restoreMocks: true,
      unstubGlobals: true,
    },
  }),
);
