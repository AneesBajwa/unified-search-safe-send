import { useState } from "react";
import { useConnections, useDisconnect } from "../api/hooks";
import type { Connection } from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { absoluteTime, sourceLabel } from "../lib/format";
import { useConnectFlow } from "../lib/useConnectFlow";

/**
 * Trust rank 2, behind only the gate itself (`risks.md` R13).
 *
 * Trust does not recover from "it just stopped working", so this page never
 * shows a dead end: every state that can be repaired shows the repair, at the
 * row that needs it, and the repair is the payload's own `reconnect_url` rather
 * than a URL this file builds.
 *
 * Three states worth telling apart, and all three are on the wire:
 *
 * - **needs_reconnect** — the grant was revoked. Re-grant in place, keeping the
 *   same `connection.id`, because every draft and send references it.
 * - **active but `scopes_ok: false`** — a permission is missing. Surfaced *now*,
 *   as a hint, rather than later as `not_allowed_token_type`, which names
 *   nothing and reads like our bug. Reported, never enforced.
 * - **not configured** — there is no OAuth client for that provider. Our gap,
 *   not the customer's, and it says so instead of offering a button that leads
 *   to an error page.
 */
export function ConnectionsPage() {
  const connections = useConnections();
  const connect = useConnectFlow();

  const rows = connections.data?.connections ?? [];
  const available = connections.data?.available ?? [];
  const connectedProviders = new Set(rows.map((row) => row.provider));

  return (
    <section className="page">
      <header className="page-head">
        <h2>Connections</h2>
        <p className="page-sub">
          As many accounts per provider as you like — each is its own grant, searched
          independently and revocable on its own. Disconnecting deletes the credentials
          and keeps the history: the record of what was sent through an account outlives
          the account.
        </p>
      </header>

      {connect.usedSameTab ? (
        <div className="notice notice-info">
          <div className="notice-body">
            <p className="notice-title">The popup was blocked</p>
            <p className="notice-detail">
              The grant is happening in this tab instead. Come back here when the provider
              is done with you.
            </p>
          </div>
        </div>
      ) : null}

      {connect.phase === "waiting" ? (
        <div className="notice notice-info">
          <div className="notice-body">
            <p className="notice-title">Waiting for the provider</p>
            <p className="notice-detail">
              Finish in the window that just opened. This page updates itself the moment
              the grant lands.
            </p>
          </div>
        </div>
      ) : null}

      {connect.phase === "failed" && connect.error ? (
        <div className="notice notice-bad">
          <div className="notice-body">
            <p className="notice-title">That connection could not be started</p>
            <p className="notice-detail">{connect.error}</p>
          </div>
        </div>
      ) : null}

      {connections.isLoading ? <p className="muted">Loading…</p> : null}

      {rows.length === 0 && !connections.isLoading ? (
        <EmptyState title="Nothing connected yet">
          Search still works — the web source needs no account, and every result it finds
          is real. Connect a provider below to search it too.
        </EmptyState>
      ) : null}

      <ul className="rows rows-cards">
        {rows.map((row) => (
          <ConnectionRow
            key={row.id}
            connection={row}
            onConnect={connect.start}
            busy={connect.phase === "waiting"}
          />
        ))}
      </ul>

      <h3 className="section-head">Add a connection</h3>
      <ul className="rows rows-cards">
        {available.map((provider) => {
          const already = connectedProviders.has(provider.provider);
          return (
            <li key={provider.provider} className="card connection">
              <div className="connection-head">
                <span className="connection-name">{sourceLabel(provider.provider)}</span>
                {provider.configured ? null : (
                  <span className="badge badge-bad">not configured</span>
                )}
                {already ? <span className="badge badge-ok">connected</span> : null}
              </div>
              {provider.configured ? (
                <details className="rationale">
                  <summary>Why this app asks for these permissions</summary>
                  <p>{provider.rationale}</p>
                </details>
              ) : (
                <p className="connection-detail">
                  No OAuth client is configured for {sourceLabel(provider.provider)} in
                  this deployment. That is ours to fix, not yours — there is nothing here
                  you can do about it.
                </p>
              )}
              {provider.configured && provider.authorize_url ? (
                <div className="connection-actions">
                  <button
                    type="button"
                    className={already ? "button" : "button button-primary"}
                    disabled={connect.phase === "waiting"}
                    onClick={() => connect.start(provider.authorize_url as string)}
                  >
                    {already ? "Connect another account" : `Connect ${sourceLabel(provider.provider)}`}
                  </button>
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * Every status the API can report, and how it reads.
 *
 * `expired` and `errored` exist in the schema and are currently unreachable —
 * an access token that can be refreshed is refreshed silently, so the customer
 * never sees "expired", and a grant that cannot be repaired becomes
 * `needs_reconnect`, which is the state that carries an action. They are
 * rendered anyway rather than defaulted, because the alternative is a console
 * that calls an unfamiliar state healthy.
 */
const STATUS_BADGE: Record<string, string> = {
  active: "badge badge-ok",
  expired: "badge badge-doubt",
  errored: "badge badge-bad",
  needs_reconnect: "badge badge-bad",
};

const STATUS_LABEL: Record<string, string> = {
  active: "active",
  expired: "expired",
  errored: "errored",
  needs_reconnect: "needs reconnecting",
};

function ConnectionRow({
  connection,
  onConnect,
  busy,
}: {
  connection: Connection;
  onConnect: (actionUrl: string) => void;
  busy: boolean;
}) {
  const disconnect = useDisconnect();
  const [confirming, setConfirming] = useState(false);

  const revoked = connection.status === "needs_reconnect";
  // `scopes_ok` is a subset test, reported and never enforced: a literal
  // equality check condemns a healthy Google grant, because we request `email`
  // and Google hands back the fully-qualified scope. So this is a hint.
  //
  // 🔴 And a hint is only shown when there is evidence for it. A connection with
  // **no recorded scopes at all** — every seeded row, and any grant written
  // before we started storing them — comes back `scopes_ok: false` with the
  // entire required set listed as missing. That is "we do not know" rendered as
  // "this is broken", which is precisely the false conclusion the rest of this
  // console exists to prevent, aimed at ourselves.
  const missingScopes = connection.missing_scopes ?? [];
  const knownScopes = connection.granted_scopes ?? [];
  const needsScopes =
    connection.scopes_ok === false && missingScopes.length > 0 && knownScopes.length > 0;

  return (
    <li className="card connection" data-status={connection.status}>
      <div className="connection-head">
        <span className="connection-name">{connection.display_name}</span>
        {/* 🔴 Green is for `active` and nothing else. This used to be
            `revoked ? bad : ok`, which painted **every** other status green —
            so a connection the API reported as `errored` or `expired` would
            have rendered as healthy with the word "errored" inside a green
            badge. An unknown status is not a good one. */}
        <span className={STATUS_BADGE[connection.status] ?? "badge badge-bad"} data-state={connection.status}>
          {STATUS_LABEL[connection.status] ?? connection.status}
        </span>
      </div>

      <p className="connection-meta">
        {sourceLabel(connection.provider)}
        {connection.last_success_at ? (
          <>
            <span aria-hidden="true"> · </span>
            last worked {absoluteTime(connection.last_success_at)}
          </>
        ) : null}
      </p>

      {revoked && connection.last_error_detail ? (
        <p className="connection-detail">{connection.last_error_detail}</p>
      ) : null}

      {needsScopes ? (
        <div className="notice notice-warn connection-notice">
          <div className="notice-body">
            <p className="notice-title">A permission is missing</p>
            <p className="notice-detail">
              This grant is missing <code>{missingScopes.join(", ")}</code>. It will keep
              working until it hits a call that needs one — re-granting now avoids an
              error that names nothing.
            </p>
          </div>
        </div>
      ) : null}

      <div className="connection-actions">
        {connection.reconnect_url ? (
          // Primary only when something is actually broken. A re-grant is always
          // *available* — it is their account — but an accent button on a
          // healthy row reads as an instruction rather than an option.
          <button
            type="button"
            className={revoked || needsScopes ? "button button-primary" : "button"}
            disabled={busy}
            onClick={() => onConnect(connection.reconnect_url as string)}
          >
            {busy ? "Waiting…" : revoked ? "Reconnect" : "Re-authorize"}
          </button>
        ) : null}

        {confirming ? (
          <>
            <button
              type="button"
              className="button button-danger"
              disabled={disconnect.isPending}
              onClick={() => disconnect.mutate(connection.id)}
            >
              {disconnect.isPending ? "Disconnecting…" : "Yes, disconnect"}
            </button>
            <button type="button" className="button" onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </>
        ) : (
          <button type="button" className="button" onClick={() => setConfirming(true)}>
            Disconnect
          </button>
        )}
      </div>

      {disconnect.isError ? (
        <p className="connection-detail bad">{String(disconnect.error)}</p>
      ) : null}
    </li>
  );
}
