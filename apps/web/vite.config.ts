import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    watch: {
      // 🔴 Polling, and it is load-bearing rather than paranoid.
      //
      // The dev server runs in Docker over a Colima bind mount, which does not
      // deliver inotify events to the container. Without this, a change is on
      // disk, passes `tsc`, and is *not served* — Vite is still handing out the
      // module it read at boot. Phase 2 lost two debugging cycles to a fix that
      // was already correct, and phase 5 lost another to a stylesheet that had
      // been saved and was not on the wire.
      //
      // The tell, if this ever regresses: `curl http://localhost:5173/src/<file>`
      // and read what the dev server actually has. `docker compose restart web`
      // clears it.
      usePolling: true,
      interval: 300,
    },
  },
  test: {
    // One component is unit-tested and it is `SourceStatusChip` — see its test
    // file for why that one and not the others.
    environment: "jsdom",
    // `.ts` as well as `.tsx` — a glob that quietly matched only components
    // meant a new test file for `client.ts` was collected by nobody and passed
    // by default.
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
