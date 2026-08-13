import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Two structural regressions, locked.
 *
 * Both were shipped, and neither was a taste problem: the gate's two actions
 * rendered at different sizes, and the settled block collapsed into a
 * right-pinned column with dead canvas beside it because it was a grid item
 * inside a column-flow parent. A screenshot catches those once; a test catches
 * them every time.
 *
 * These assert *structure*, never behaviour the API owns — nothing here
 * decides whether a send may proceed.
 */

const useDraft = vi.fn();
const useSendDraft = vi.fn();

vi.mock("../api/hooks", () => ({
  useDraft: () => useDraft(),
  useSendDraft: () => useSendDraft(),
}));

vi.mock("../lib/useConnectFlow", () => ({
  useConnectFlow: () => ({ phase: "idle", start: vi.fn(), usedSameTab: false, error: null }),
}));

const { ConfirmDialog } = await import("./ConfirmDialog");

const DRAFT = {
  draft: { id: "d1" },
  confirmation: {
    recipient_display: "someone@example.test",
    warning: "This will email someone@example.test from seed@example.test.",
    channel: "gmail",
    connection_display: "seed@example.test (seeded Gmail)",
    subject: "Thursday",
    body: "Confirming for Thursday.",
    confirm_sha256: "540ab11b92aa000000000000000000000000000000000000000000000000abcd",
  },
};

function renderGate() {
  return render(
    <MemoryRouter initialEntries={["/confirm/d1"]}>
      <Routes>
        <Route path="/confirm/:draftId" element={<ConfirmDialog />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useDraft.mockReturnValue({
    isLoading: false,
    isError: false,
    data: DRAFT,
    error: null,
    refetch: vi.fn(),
  });
  useSendDraft.mockReturnValue({
    data: undefined,
    error: null,
    isPending: false,
    mutate: vi.fn(),
    reset: vi.fn(),
  });
});

describe("the gate's footer", () => {
  it("gives Cancel and Send the same size class", () => {
    const { container } = renderGate();
    const buttons = container.querySelectorAll(".gate-actions button");

    expect(buttons).toHaveLength(2);
    const sizes = new Set(
      [...buttons].map((button) => button.className.match(/\bbutton-(sm|lg)\b/)?.[1] ?? "md"),
    );
    expect(sizes.size, "the two actions must not be different sizes").toBe(1);
  });

  it("puts the settled block outside the action cluster, so it cannot collapse into a column", () => {
    useSendDraft.mockReturnValue({
      data: { send: { send_id: "6c7718b9-0000-0000-0000-000000000000", state: "in_flight" }, replayed: false },
      error: null,
      isPending: false,
      mutate: vi.fn(),
      reset: vi.fn(),
    });

    const { container } = renderGate();
    const settled = container.querySelector(".gate-settled");

    expect(settled).not.toBeNull();
    expect(
      settled?.closest(".gate-actions"),
      "the settled block must not be a child of the action row",
    ).toBeNull();
  });

  it("still offers two equally sized actions once settled", () => {
    useSendDraft.mockReturnValue({
      data: { send: { send_id: "6c7718b9-0000-0000-0000-000000000000", state: "in_flight" }, replayed: false },
      error: null,
      isPending: false,
      mutate: vi.fn(),
      reset: vi.fn(),
    });

    const { container } = renderGate();
    const buttons = container.querySelectorAll(".gate-actions button");

    expect(buttons).toHaveLength(2);
    const sizes = new Set(
      [...buttons].map((button) => button.className.match(/\bbutton-(sm|lg)\b/)?.[1] ?? "md"),
    );
    expect(sizes.size).toBe(1);
  });
});
