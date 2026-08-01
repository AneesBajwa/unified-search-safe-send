import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useConnections, useCreateDraft } from "../api/hooks";

/**
 * Compose. It is a textarea, and plain is fine.
 *
 * Deliberately the least polished surface in the app (risks.md R13): ranked
 * fifth of five by what a weak version costs the customer. Effort went to
 * confirm instead, which is ranked first — a demo with a beautiful compose
 * screen and a janky confirm dialog reads as not understanding what was built.
 *
 * Creating a draft has **no external effect**. Nothing here can send.
 */
export function ComposePage() {
  const navigate = useNavigate();
  const connections = useConnections();
  const createDraft = useCreateDraft();

  const [channel, setChannel] = useState<"gmail" | "slack">("slack");
  const [to, setTo] = useState("C024BE91L");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("Confirming for Thursday.");

  return (
    <section>
      <h2>Compose</h2>
      <p className="muted">
        Creating a draft contacts no provider. The next screen is the gate.
      </p>

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
            },
            {
              onSuccess: (created) => navigate(`/confirm/${created.draft.id}`),
            },
          );
        }}
      >
        <label>
          Channel
          <select
            value={channel}
            onChange={(event) => {
              const next = event.target.value as "gmail" | "slack";
              setChannel(next);
              setTo(next === "gmail" ? "qa@example.test" : "C024BE91L");
            }}
          >
            <option value="slack">Slack</option>
            <option value="gmail">Gmail</option>
          </select>
        </label>

        <label>
          To
          <input value={to} onChange={(event) => setTo(event.target.value)} />
        </label>

        {channel === "gmail" ? (
          <label>
            Subject
            <input
              value={subject}
              onChange={(event) => setSubject(event.target.value)}
            />
          </label>
        ) : null}

        <label>
          Message
          <textarea
            rows={6}
            value={body}
            onChange={(event) => setBody(event.target.value)}
          />
        </label>

        <button type="submit" disabled={createDraft.isPending}>
          Review before sending
        </button>
      </form>

      {createDraft.isError ? (
        <p className="bad">{String(createDraft.error)}</p>
      ) : null}

      <details>
        <summary className="muted">Connections</summary>
        <ul>
          {(connections.data ?? []).map((connection) => (
            <li key={connection.id}>
              {connection.provider} · {connection.display_name} · {connection.status}
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}
