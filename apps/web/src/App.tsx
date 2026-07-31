import { useCallback, useEffect, useState } from "react";
import "./App.css";

/**
 * Phase 0 walking skeleton.
 *
 * The entire feature set is: fetch the API's /health and render it. What it
 * proves is the part that matters — that a browser can reach the deployed
 * SPA, which reaches the deployed API, which reaches Postgres, with the
 * migration applied. Every later deploy is a re-run of this path.
 */

const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8080";

type Database =
  | { connected: true; server_version: string; migration: string | null; users: number }
  | { connected: false; error?: string };

type Health = {
  status: "ok" | "degraded";
  service: string;
  version: string;
  database: Database;
};

type State =
  | { phase: "loading" }
  | { phase: "loaded"; health: Health }
  | { phase: "error"; message: string };

export default function App() {
  const [state, setState] = useState<State>({ phase: "loading" });

  const load = useCallback(async () => {
    setState({ phase: "loading" });
    try {
      const res = await fetch(`${API_BASE_URL}/health`);
      // A 503 still carries a useful body — the API reports degraded rather
      // than throwing, so read the payload before deciding it failed.
      const health = (await res.json()) as Health;
      setState({ phase: "loaded", health });
    } catch (err) {
      setState({
        phase: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <main className="app">
      <header>
        <h1>Unified Search &amp; Safe Send</h1>
        <p className="subtitle">Phase 0 — walking skeleton</p>
      </header>

      <section className="card">
        <div className="card-head">
          <h2>API health</h2>
          <button type="button" onClick={() => void load()} disabled={state.phase === "loading"}>
            {state.phase === "loading" ? "Checking…" : "Re-check"}
          </button>
        </div>

        <p className="target">
          <code>GET {API_BASE_URL}/health</code>
        </p>

        {state.phase === "loading" && <p className="muted">Contacting the API…</p>}

        {state.phase === "error" && (
          <>
            <p className="pill pill-bad">unreachable</p>
            <p className="muted">
              The SPA could not reach the API at all — network, CORS, or the service is down.
            </p>
            <pre>{state.message}</pre>
          </>
        )}

        {state.phase === "loaded" && (
          <>
            <p className={state.health.status === "ok" ? "pill pill-good" : "pill pill-warn"}>
              {state.health.status}
            </p>
            <dl>
              <dt>Service</dt>
              <dd>
                {state.health.service} v{state.health.version}
              </dd>
              <dt>Database</dt>
              <dd>
                {state.health.database.connected ? (
                  <>
                    connected · PostgreSQL {state.health.database.server_version} · migration{" "}
                    <code>{state.health.database.migration ?? "none"}</code> ·{" "}
                    {state.health.database.users} user
                    {state.health.database.users === 1 ? "" : "s"}
                  </>
                ) : (
                  <span className="bad">
                    not connected
                    {state.health.database.error ? ` — ${state.health.database.error}` : ""}
                  </span>
                )}
              </dd>
            </dl>
          </>
        )}
      </section>

      <footer className="muted">
        SPA → API → Postgres. If this reads <code>ok</code> from the deployed URL, the deploy path
        is proven.
      </footer>
    </main>
  );
}
