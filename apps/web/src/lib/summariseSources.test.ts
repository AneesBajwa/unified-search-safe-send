import { describe, expect, it } from "vitest";
import type { SourceView } from "../api/types";
import { summariseSources } from "./summariseSources";

/**
 * 🔴 Every payload below was copied off a live API response, not written from
 * the type — the same rule the chip tests follow, for the same reason.
 *
 * The regression this pins was found by recording the demo rather than by
 * running the suite: a brand-new user with nothing connected saw "3 results
 * from 3 sources" when the web adapter was the only source that returned
 * anything and the other two had no grant at all.
 */

const source = (over: Partial<SourceView>): SourceView =>
  ({
    source: "web",
    status: "done",
    mode: "mock",
    result_count: 0,
    connection_id: null,
    display_name: null,
    ...over,
  }) as SourceView;

describe("summariseSources", () => {
  it("does not credit sources that returned nothing", () => {
    // The live first-run payload: Gmail and Slack unconnected, web returned 3.
    const summary = summariseSources(3, [
      source({ source: "gmail", status: "needs_reconnect", mode: "live", result_count: 0 }),
      source({ source: "slack", status: "needs_reconnect", mode: "live", result_count: 0 }),
      source({ source: "web", result_count: 3 }),
    ]);
    expect(summary).toBe("3 results from 1 of 3 sources");
  });

  it("drops the qualifier when every source contributed", () => {
    const summary = summariseSources(7, [
      source({ source: "gmail", mode: "live", result_count: 1 }),
      source({ source: "slack", mode: "live", result_count: 3 }),
      source({ source: "web", result_count: 3 }),
    ]);
    expect(summary).toBe("7 results from 3 sources");
  });

  it("counts a source that completed with no matches as not contributing", () => {
    // `done` with zero results is a real state and distinct from a failure —
    // but it still did not put anything in the list.
    const summary = summariseSources(3, [
      source({ source: "gmail", mode: "live", result_count: 0 }),
      source({ source: "web", result_count: 3 }),
    ]);
    expect(summary).toBe("3 results from 1 of 2 sources");
  });

  it("singularises one result and one source", () => {
    expect(summariseSources(1, [source({ result_count: 1 })])).toBe("1 result from 1 source");
  });
});
