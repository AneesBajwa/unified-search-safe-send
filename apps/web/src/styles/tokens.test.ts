import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * The palette's accessibility claim, computed rather than asserted.
 *
 * The spec says every text colour clears 4.5:1 on `--surface` and every
 * *interactive* border clears 3:1. Prose is not a guarantee — this reads
 * `tokens.css` and does the arithmetic, in the same spirit as
 * `tests/test_web_boundary.py` on the Python side.
 *
 * `--line` is deliberately exempt: it draws dividers and card edges, where the
 * boundary is not the only thing identifying a control. `--line-2` is not
 * exempt, because on a white secondary button sitting on a white card the
 * border *is* the affordance.
 */
const HERE = dirname(fileURLToPath(import.meta.url));
const TOKENS = readFileSync(join(HERE, "tokens.css"), "utf8");

/**
 * Parsed from the first `:root` block, with comments stripped first.
 *
 * Both of those matter. The map is last-write-wins, so a superseded value left
 * in a trailing comment — or a colour redefined inside a later `@media` block —
 * would silently become the value under test, and the suite would go green over
 * a palette that fails. That is the exact failure this file exists to rule out.
 */
function tokens(): Record<string, string> {
  const withoutComments = TOKENS.replace(/\/\*[\s\S]*?\*\//g, "");
  const root = withoutComments.match(/:root\s*\{([^}]*)\}/);
  if (!root) throw new Error("tokens.css has no :root block");

  const found: Record<string, string> = {};
  for (const match of root[1].matchAll(/(--[a-z0-9-]+)\s*:\s*(#[0-9a-f]{6})\s*;/gi)) {
    found[match[1]] = match[2].toLowerCase();
  }
  return found;
}

function luminance(hex: string): number {
  const value = Number.parseInt(hex.slice(1), 16);
  const [r, g, b] = [(value >> 16) & 255, (value >> 8) & 255, value & 255].map((raw) => {
    const channel = raw / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

/** Every colour a word is ever set in. */
const TEXT = ["--ink", "--ink-2", "--muted", "--faint", "--accent", "--ok", "--warn", "--bad"];

/** Each state's text against the tint it sits on inside a chip, badge or notice. */
const ON_TINT: Array<[string, string]> = [
  ["--accent", "--accent-weak"],
  ["--ok", "--ok-weak"],
  ["--warn", "--warn-weak"],
  ["--bad", "--bad-weak"],
];

describe("tokens", () => {
  it("defines every token the rest of the system references", () => {
    const found = tokens();
    for (const name of [
      ...TEXT,
      ...ON_TINT.map(([, tint]) => tint),
      "--surface",
      "--canvas",
      "--line",
      "--line-2",
      "--primary",
      "--primary-fg",
    ]) {
      expect(found[name], `${name} is missing from tokens.css`).toBeDefined();
    }
  });

  it.each(TEXT)("%s clears AA on --surface", (name) => {
    const found = tokens();
    expect(contrast(found[name], found["--surface"])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(ON_TINT)("%s clears AA on %s", (ink, tint) => {
    const found = tokens();
    expect(contrast(found[ink], found[tint])).toBeGreaterThanOrEqual(4.5);
  });

  it("--primary-fg clears AA on --primary", () => {
    const found = tokens();
    expect(contrast(found["--primary-fg"], found["--primary"])).toBeGreaterThanOrEqual(4.5);
  });

  it("--line-2 clears 3:1 on --surface, because it is the only thing identifying a control", () => {
    const found = tokens();
    expect(contrast(found["--line-2"], found["--surface"])).toBeGreaterThanOrEqual(3);
  });

  it("pins light, and no stylesheet in this directory ships a dark scheme", () => {
    expect(TOKENS).toMatch(/color-scheme:\s*light\s*;/);

    // Every stylesheet, not just this one — five more land in later tasks and
    // an assertion that only ever reads its own file is not a guarantee.
    for (const file of readdirSync(HERE).filter((name) => name.endsWith(".css"))) {
      expect(readFileSync(join(HERE, file), "utf8"), file).not.toMatch(/prefers-color-scheme/);
    }
  });

  it("has retired App.css rather than leaving it as dead weight", () => {
    expect(existsSync(join(HERE, "..", "App.css"))).toBe(false);
  });
});
