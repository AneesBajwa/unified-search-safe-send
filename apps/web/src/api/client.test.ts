import { describe, expect, it } from "vitest";
import { deriveApiBaseUrl } from "./client";

/**
 * Where the API is, given where the page came from.
 *
 * 🔴 This exists because the Codespaces case **cannot be reached from a
 * laptop**, and the naive derivation was wrong there in a way nothing local
 * would ever surface: GitHub forwards a port into the *hostname*
 * (`https://<name>-5173.app.github.dev`), not after a colon, so appending
 * `:8080` produces a host that resolves to nothing. DL3 says the project must
 * run in Codespaces; with the old form the console could not reach its own API
 * there at all, and the only symptom would have been every request failing.
 *
 * So the function takes a location rather than reading `window`, and the URL
 * Codespaces actually serves is fed to it here.
 */
describe("deriveApiBaseUrl", () => {
  it("keeps the host and swaps the port locally", () => {
    expect(
      deriveApiBaseUrl({ protocol: "http:", hostname: "localhost" }),
    ).toBe("http://localhost:8080");
  });

  it("follows the page onto the LAN rather than calling localhost", () => {
    // A phone loading this over the LAN and calling `localhost` is calling
    // itself, which presents as every request failing for no visible reason.
    expect(
      deriveApiBaseUrl({ protocol: "http:", hostname: "10.0.0.230" }),
    ).toBe("http://10.0.0.230:8080");
  });

  it("rewrites the forwarded port label in a Codespace, and appends nothing", () => {
    const derived = deriveApiBaseUrl({
      protocol: "https:",
      hostname: "fluffy-guacamole-abc123-5173.app.github.dev",
    });
    expect(derived).toBe("https://fluffy-guacamole-abc123-8080.app.github.dev");
    // The failure this replaces, named so it cannot come back by accident.
    expect(derived).not.toContain(":8080");
    expect(derived).not.toContain("-5173");
  });

  it("leaves a codespace name containing digits alone", () => {
    // Only the *port* label is rewritten. A name that happens to end in digits
    // must not be mangled, which is why the pattern is anchored on `-5173.`.
    expect(
      deriveApiBaseUrl({
        protocol: "https:",
        hostname: "repo-2024-5173.app.github.dev",
      }),
    ).toBe("https://repo-2024-8080.app.github.dev");
  });
});
