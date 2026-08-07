import { useState } from "react";
import { adoptKey, devLogin } from "../api/client";

/**
 * The PoC sign-in the brief allows, and no more than that.
 *
 * It exists as a screen rather than an implicit call on first load for one
 * reason: **"a brand-new user searches before connecting anything" is a state
 * the product has to be able to show**, and it was the state that hid a defect
 * through three phases because every test and every click-through ran as a user
 * who already had connections. An address nobody has used yet *is* a new user,
 * so reaching that state costs one field.
 *
 * The key lives in `sessionStorage` — a known, accepted trade (`risks.md` R14,
 * design D10). One credential path is what makes "the console is a pure
 * consumer of the documented API" structurally true rather than an assertion
 * somebody has to audit.
 */
/** Matches `SEED_EMAIL` in `scripts/seed.py` — the identity `make seed` fills. */
const SEEDED_EMAIL = "seed@example.test";

export function SignInPage({ onSignedIn }: { onSignedIn: () => void }) {
  const [email, setEmail] = useState("console@example.test");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onSignedIn();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page signin">
      <header className="page-head">
        <h2>Sign in</h2>
        <p className="page-sub">
          No password: an address is an account. Three states are worth seeing, so each
          is one click away — <strong>this address</strong> has the live Gmail and Slack
          connections, <strong>seeded</strong> has history in every status, and{" "}
          <strong>brand-new</strong> has nothing connected at all.
        </p>
      </header>

      <form
        className="compose"
        onSubmit={(event) => {
          event.preventDefault();
          void run(() => devLogin(email.trim()));
        }}
      >
        <label className="field">
          <span className="field-label">Email</span>
          <input
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            type="email"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            required
          />
        </label>
        <button type="submit" className="button button-primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <button
          type="button"
          className="button"
          disabled={busy}
          onClick={() => {
            setEmail(SEEDED_EMAIL);
            void run(() => devLogin(SEEDED_EMAIL));
          }}
        >
          Sign in as the seeded demo account
        </button>
        <button
          type="button"
          className="button"
          disabled={busy}
          onClick={() => {
            const fresh = `new-${Date.now().toString(36)}@example.test`;
            setEmail(fresh);
            void run(() => devLogin(fresh));
          }}
        >
          Sign in as a brand-new user
        </button>
      </form>

      <details className="rationale">
        <summary>Use a key minted somewhere else</summary>
        <form
          className="compose"
          onSubmit={(event) => {
            event.preventDefault();
            void run(() => adoptKey(key.trim(), "pasted key"));
          }}
        >
          <label className="field">
            <span className="field-label">API key</span>
            <input
              value={key}
              onChange={(event) => setKey(event.target.value)}
              placeholder="sk_live_…"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
          </label>
          <button type="submit" className="button" disabled={busy || !key.trim()}>
            Use this key
          </button>
        </form>
      </details>

      {error ? (
        <div className="notice notice-bad">
          <div className="notice-body">
            <p className="notice-title">That did not work</p>
            <p className="notice-detail">{error}</p>
          </div>
        </div>
      ) : null}
    </section>
  );
}
