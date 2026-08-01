import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { useConnections, useCreateDraft } from "../api/hooks";
import { sourceLabel } from "../lib/format";
import { useConnectFlow } from "../lib/useConnectFlow";

/**
 * Compose. It is a textarea, and plain is fine.
 *
 * Deliberately the least polished surface in the app (`risks.md` R13): ranked
 * fifth of five by what a weak version costs the customer, and the cut order is
 * the inverse of the naive one. Effort went to confirm instead — a demo with a
 * beautiful compose screen and a janky confirm dialog reads as not understanding
 * what was built.
 *
 * Creating a draft has **no external effect**. There is no control on this page
 * that delivers anything; the only way out of here is the gate.
 */
export function ComposePage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const connections = useConnections();
  const createDraft = useCreateDraft();
  const connect = useConnectFlow();

  const [channel, setChannel] = useState<"gmail" | "slack">("gmail");
  // 🔴 Empty, not prefilled. Phase 2 defaulted this to a fixture Slack channel
  // id, which does not exist in any real workspace — so a reviewer's first send
  // failed with `channel_not_found` on a value they never typed, and the first
  // thing the product did was look broken. A placeholder that says what the
  // field wants is worth more than a default that is wrong.
  const [to, setTo] = useState("");
  const [subject, setSubject] = useState(params.get("subject") ?? "");
  const [body, setBody] = useState("Confirming for Thursday.");
  const [fromId, setFromId] = useState<number | null>(null);

  const refusal = createDraft.error instanceof ApiError ? createDraft.error : null;
  const rows = connections.data?.connections ?? [];
  // 🔴 Only offered when there is a choice to make. One account per provider is
  // the normal case and a picker there is noise; with two, letting the API pick
  // "the first active one" means the customer finds out which account sent it
  // on the confirm screen, after composing. Multi-account is a headline
  // capability — it has to be reachable from the surface that uses it.
  const sendable = rows.filter(
    (row) => row.provider === channel && row.status === "active",
  );

  return (
    <section className="page">
      <header className="page-head">
        <h2>Compose</h2>
        <p className="page-sub">
          Creating a draft contacts no provider. The next screen is the gate, and it is the
          only way anything leaves here.
        </p>
      </header>

      <form
        className="compose"
        onSubmit={(event) => {
          event.preventDefault();
          createDraft.mutate(
            {
              channel,
              to,
              body,
              ...(channel === "gmail" && subject ? { subject } : {}),
              ...(fromId === null ? {} : { connection_id: fromId }),
            },
            { onSuccess: (created) => navigate(`/confirm/${created.draft.id}`) },
          );
        }}
      >
        <label className="field">
          <span className="field-label">Channel</span>
          <select
            value={channel}
            onChange={(event) => {
              setChannel(event.target.value as "gmail" | "slack");
              setTo("");
              setFromId(null);
            }}
          >
            <option value="gmail">Gmail</option>
            <option value="slack">Slack</option>
          </select>
        </label>

        {sendable.length > 1 ? (
          <label className="field">
            <span className="field-label">From</span>
            <select
              value={fromId ?? sendable[0].id}
              onChange={(event) => setFromId(Number(event.target.value))}
            >
              {sendable.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.display_name}
                </option>
              ))}
            </select>
          </label>
        ) : null}

        <label className="field">
          <span className="field-label">To</span>
          <input
            value={to}
            onChange={(event) => setTo(event.target.value)}
            placeholder={
              channel === "gmail" ? "someone@example.com" : "a channel id, e.g. C024BE91L"
            }
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            required
          />
        </label>

        {channel === "gmail" ? (
          <label className="field">
            <span className="field-label">Subject</span>
            <input value={subject} onChange={(event) => setSubject(event.target.value)} />
          </label>
        ) : null}

        <label className="field">
          <span className="field-label">Message</span>
          <textarea rows={7} value={body} onChange={(event) => setBody(event.target.value)} />
        </label>

        <button type="submit" className="button button-primary" disabled={createDraft.isPending}>
          {createDraft.isPending ? "Preparing…" : "Review before sending"}
        </button>
      </form>

      {refusal ? (
        <div className={refusal.actionUrl ? "notice notice-action" : "notice notice-bad"}>
          <div className="notice-body">
            <p className="notice-title">
              {refusal.actionUrl ? "That account needs connecting" : "That draft was refused"}
            </p>
            <p className="notice-detail">{refusal.message}</p>
          </div>
          {refusal.actionUrl ? (
            <button
              type="button"
              className="button button-primary"
              disabled={connect.phase === "waiting"}
              onClick={() => connect.start(refusal.actionUrl as string)}
            >
              {connect.phase === "waiting" ? "Waiting…" : "Connect now"}
            </button>
          ) : null}
        </div>
      ) : null}

      {createDraft.isError && !refusal ? (
        <p className="bad">{String(createDraft.error)}</p>
      ) : null}

      <details className="rationale">
        <summary>Which account this would go out from</summary>
        {rows.length === 0 ? (
          <p>Nothing is connected, so the gate will refuse and offer you a connect.</p>
        ) : (
          <ul className="plain-list">
            {rows.map((row) => (
              <li key={row.id}>
                {sourceLabel(row.provider)} · {row.display_name} · {row.status}
              </li>
            ))}
          </ul>
        )}
      </details>
    </section>
  );
}
