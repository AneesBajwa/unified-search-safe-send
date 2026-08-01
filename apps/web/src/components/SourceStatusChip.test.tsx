import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SourceView } from "../api/types";
import { SourceStatusChip } from "./SourceStatusChip";

/**
 * The one component with unit tests, and the choice is deliberate.
 *
 * `ui-architecture.md` §Testing specifies Vitest for the chips and Playwright
 * for the confirm flow; group 13 owns test consolidation. This chip is pulled
 * forward because it is the only surface where being wrong is *actively
 * dangerous* rather than merely ugly: if "we looked and found nothing" and "we
 * could not look" collapse into the same rendering, the customer concludes
 * "nobody emailed me about this" when we never reached their mailbox. It is
 * also a pure function of one payload, so a test costs almost nothing.
 *
 * 🔴 **Every payload below was copied off a live API response**, not written
 * from the type. Three phases of green suites have now hidden defects that were
 * found by *using* the product, and every one was a test asserting the shape of
 * a value instead of reading the value that crossed the boundary. So: the
 * action test reads the URL that reaches the handler and compares it to the one
 * on the payload — it never checks that "an action was rendered".
 */

afterEach(cleanup);

/** Brand-new user, first search. `GET /v1/searches/{id}` — verified live. */
const NEVER_CONNECTED: SourceView = {
  source: "gmail",
  status: "needs_reconnect",
  mode: "live",
  result_count: 0,
  error: {
    code: "connection_not_connected",
    classification: "needs_reconnect",
    message: "this source has no connection, so it has no oauth token",
    action_url: "/v1/connections/gmail/authorize",
  },
};

/** Seeded revoked grant — the dataset a reviewer with no accounts explores. */
const REVOKED: SourceView = {
  source: "gmail",
  status: "needs_reconnect",
  mode: "live",
  result_count: 0,
  error: {
    code: "connection_needs_reconnect",
    classification: "needs_reconnect",
    message: '{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}',
    action_url: "/v1/connections/gmail/authorize?reconnect=3",
    reconnect_url: "/v1/connections/gmail/authorize?reconnect=3",
  },
};

const FOUND: SourceView = { source: "slack", status: "done", mode: "live", result_count: 3 };
const EMPTY: SourceView = { source: "gmail", status: "done", mode: "live", result_count: 0 };
const MOCKED: SourceView = { source: "web", status: "done", mode: "mock", result_count: 3 };
const RUNNING: SourceView = { source: "slack", status: "running", mode: "live", result_count: 0 };

/** `fault:permanent` — a source that will not come back. */
const PERMANENT: SourceView = {
  source: "gmail",
  status: "failed",
  mode: "live",
  result_count: 0,
  error: {
    code: "provider_unavailable",
    classification: "permanent",
    message: "fault:synthetic_bad_request injected permanent failure (fault adapter)",
  },
};

/** `fault:transient` — throttled or briefly down, and a retry is still coming. */
const TRANSIENT: SourceView = {
  source: "gmail",
  status: "failed",
  mode: "live",
  result_count: 0,
  error: {
    code: "provider_unavailable",
    classification: "transient",
    message: "fault:synthetic_unavailable injected transient failure (fault adapter)",
  },
};

function renderChip(source: SourceView, searchFinished = true) {
  const onAction = vi.fn();
  const { container } = render(
    <SourceStatusChip source={source} searchFinished={searchFinished} onAction={onAction} />,
  );
  const chip = container.firstElementChild as HTMLElement;
  return { chip, onAction, text: chip.textContent ?? "" };
}

describe("SourceStatusChip: the states must not collapse into each other", () => {
  it("distinguishes found, empty, retrying and permanently failed", () => {
    const states = [FOUND, EMPTY, TRANSIENT, PERMANENT].map((source) => {
      const { chip, text } = renderChip(source, source === TRANSIENT ? false : true);
      return { state: chip.dataset.state, className: chip.className, text };
    });

    // Four payloads, four distinct renderings — by state, by styling, and by the
    // words a customer actually reads. If any pair collapses, one of these sets
    // shrinks and the customer can draw a false conclusion.
    expect(new Set(states.map((s) => s.state)).size).toBe(4);
    expect(new Set(states.map((s) => s.className)).size).toBe(4);
    expect(new Set(states.map((s) => s.text)).size).toBe(4);
  });

  it("says 'no matches' when it looked and 'unavailable' when it could not", () => {
    expect(renderChip(EMPTY).text).toContain("no matches");
    expect(renderChip(PERMANENT).text).toContain("unavailable");
    expect(renderChip(PERMANENT).text).not.toContain("no matches");
  });

  it("separates a source still retrying from one that has given up", () => {
    // The same `status: failed`. Only `classification` and whether the search is
    // finished tell them apart, and the difference is "wait" versus "this is as
    // good as it gets".
    expect(renderChip(TRANSIENT, false).text).toContain("retrying");
    expect(renderChip(TRANSIENT, true).text).toContain("unavailable");
  });

  it("shows a count when there are results", () => {
    expect(renderChip(FOUND).text).toContain("3");
  });

  it("never renders a mocked source as if it were live", () => {
    expect(renderChip(MOCKED).text).toContain("mock");
    expect(renderChip(FOUND).text).not.toContain("mock");
  });

  it("shows a source still working as working", () => {
    expect(renderChip(RUNNING).text).toContain("searching");
  });
});

describe("SourceStatusChip: the actionable states", () => {
  it("offers 'Connect' for a source that was never connected", () => {
    // The state every new user meets first. Until phase 4 it rendered as a
    // permanent provider failure, about a provider they had simply not
    // connected — with nothing to click.
    const { chip, text } = renderChip(NEVER_CONNECTED);
    expect(text).toContain("Connect");
    expect(text).not.toContain("Reconnect");
    expect(chip.tagName).toBe("BUTTON");
  });

  it("offers 'Reconnect' for a grant that existed and was revoked", () => {
    expect(renderChip(REVOKED).text).toContain("Reconnect");
  });

  it("branches on error.code, not on status", () => {
    // Both payloads carry `status: "needs_reconnect"`. If this component read
    // the status it could not tell them apart, and it would offer to reconnect
    // an account nobody ever linked.
    expect(NEVER_CONNECTED.status).toBe(REVOKED.status);
    expect(renderChip(NEVER_CONNECTED).text).not.toBe(renderChip(REVOKED).text);
  });

  it("hands the handler the payload's own action_url, verbatim", () => {
    // 🔴 The whole point. This link has been broken on four separate surfaces
    // across three phases, every time because somebody checked that *an*
    // action rendered rather than reading where it went. Read the value.
    for (const source of [NEVER_CONNECTED, REVOKED]) {
      cleanup();
      const { chip, onAction } = renderChip(source);
      chip.click();
      expect(onAction).toHaveBeenCalledTimes(1);
      expect(onAction).toHaveBeenCalledWith(source.error?.action_url);
    }
  });

  it("is inert rather than misleading when there is no action to offer", () => {
    const stripped: SourceView = {
      ...NEVER_CONNECTED,
      error: { ...NEVER_CONNECTED.error!, action_url: null },
    };
    const { chip } = renderChip(stripped);
    expect((chip as HTMLButtonElement).disabled).toBe(true);
  });

  it("names the account when a provider has more than one grant", () => {
    // Two Gmail accounts produce two `gmail` entries that succeed or fail
    // independently. "Gmail · reconnect" beside "Gmail · 4" is unreadable
    // without saying which Gmail — `(source, connection_id)` is the key.
    const first: SourceView = {
      ...FOUND,
      source: "gmail",
      connection_id: 31,
      display_name: "work@example.test",
    };
    const second: SourceView = {
      ...REVOKED,
      connection_id: 42,
      display_name: "personal@example.test",
    };
    render(<SourceStatusChip source={first} searchFinished disambiguate />);
    render(<SourceStatusChip source={second} searchFinished disambiguate />);
    expect(screen.getByText(/work@example\.test/)).toBeTruthy();
    expect(screen.getByText(/personal@example\.test/)).toBeTruthy();
  });

  it("stays quiet about the account when there is only one", () => {
    const { text } = renderChip({
      ...FOUND,
      connection_id: 31,
      display_name: "work@example.test",
    });
    expect(text).not.toContain("work@example.test");
  });

  it("renders the source name as a label rather than branching on it", () => {
    // A fourth source must render without this file being edited. `fault:` names
    // are passed through as-is because they are diagnostic, not customer-facing.
    render(
      <SourceStatusChip
        source={{ source: "notion", status: "done", mode: "live", result_count: 2 }}
        searchFinished
      />,
    );
    expect(screen.getByText("Notion")).toBeTruthy();
  });
});
