# Console Redesign ("Instrument") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the console's visual system with a single light "Instrument" palette, fix two real layout defects in the confirm gate, and give the app a left-rail shell with a standing per-source status panel on desktop.

**Architecture:** `apps/web/src/App.css` (1253 lines, six responsibilities) is deleted and replaced by six focused stylesheets under `src/styles/`, imported by `index.css`. Dark mode is removed and `color-scheme: light` is pinned. Two structural JSX changes are required — the gate's settled block moves out of the collapsing action grid, and `App.tsx` gains a desktop rail. Everything else is presentation-only: no hook, API-client, route or business-logic change.

**Tech Stack:** React 19, Vite 8, TypeScript, plain CSS custom properties (no framework, no webfont, no icon library), Vitest + Testing Library, oxlint.

**Spec:** `docs/superpowers/specs/2026-08-12-ui-redesign-design.md`

---

## Ground rules for the executor

- **Read the spec first.** Every value in this plan traces to a section there.
- **Verify with the whole gate, every task**, not just the one test file you
  touched: `npm run typecheck && npm run lint && npm test && npm run build`.
  Task 2 ran `vitest` alone on the file it had just written, and left
  `typecheck` and `build` red for an entire task before anyone noticed —
  `tokens.test.ts` imports `node:fs` and `tsconfig.app.json` did not list
  `node` in `types`. A green subset is not a green suite.
- **Mobile-first.** Base rules target 375px; every desktop rule is a
  `min-width` media query. Never author desktop-first and walk it back.
- **Two breakpoints only.** `40rem` changes control density; `64rem` changes
  the shell. Do not invent a third.
- **`SourceStatusChip.test.tsx` must stay green unmodified.** It asserts the
  four terminal source states have four distinct `className` values. If you
  find yourself editing it, the states have collapsed and the change is wrong.
- **Expect a rough patch.** `App.css` is deleted in Task 3. Between Task 3 and
  the end of Task 5 the app is deliberately half-styled. Do not judge
  intermediate screenshots; Task 5 is the first point the app is coherent again.
- **The class vocabulary is preserved.** `.button`, `.button-primary`,
  `.button-quiet`, `.button-danger`, `.badge-*`, `.chip-*`, `.notice-*` keep
  their names so most JSX needs no edit. `.button` alone renders as the
  *secondary* variant, exactly as today.

---

## File structure

**Created**

| Path | Responsibility |
|---|---|
| `apps/web/src/styles/tokens.css` | colour, radius, elevation, control heights |
| `apps/web/src/styles/base.css` | reset, element defaults, focus ring, reduced motion |
| `apps/web/src/styles/components.css` | button, actions cluster, input, card, panel, chip, badge, notice, empty, countdown |
| `apps/web/src/styles/shell.css` | top bar, rail, tab bar, canvas, page grid |
| `apps/web/src/styles/screens.css` | search, compose, history, send detail, connections |
| `apps/web/src/styles/gate.css` | the confirm gate |
| `apps/web/src/styles/tokens.test.ts` | computes WCAG contrast from `tokens.css`; asserts the scheme is pinned |
| `apps/web/src/components/NavIcon.tsx` | four hand-written inline SVGs |
| `apps/web/src/components/SourcePanel.tsx` | desktop standing source-status panel |
| `apps/web/src/routes/ConfirmDialog.test.tsx` | regression tests for the two gate defects |

**Modified**

| Path | Change |
|---|---|
| `apps/web/src/index.css` | reduced to six `@import` lines |
| `apps/web/src/App.tsx` | drop `App.css` import; add rail shell; nav icons |
| `apps/web/src/routes/ConfirmDialog.tsx` | settled block leaves `.gate-actions`; both actions `button-lg` |
| `apps/web/src/routes/SearchPage.tsx` | two-column layout with `SourcePanel` from 64rem |
| `apps/web/src/routes/HistoryPage.tsx` | row stack becomes one panel |
| `apps/web/src/routes/ConnectionsPage.tsx` | row stacks become panels |
| `apps/web/src/routes/SendDetailPage.tsx` | facts and uncertainty become panels |
| `apps/web/src/routes/ComposePage.tsx` | submit into an `.actions` row |
| `apps/web/src/routes/SignInPage.tsx` | centred card |
| `apps/web/vite.config.ts` | the "one component is unit-tested" comment is no longer true |

**Deleted**

- `apps/web/src/App.css`

---

## Task 1: Baseline

**Files:** none modified.

- [ ] **Step 1: Bring the stack up**

```bash
cd /Users/aneesbajwa/www/unified-search-safe-send
make up
make seed
```

Expected: compose starts db, api, worker and web. The console is on
`http://localhost:5173`, the API on `http://localhost:8080`.

- [ ] **Step 2: Confirm the suite is green before you touch anything**

```bash
cd apps/web && npm run typecheck && npm run lint && npm test
```

Expected: typecheck clean, lint clean, vitest reports all files passed
(`SourceStatusChip.test.tsx`, `client.test.ts`, `summariseSources.test.ts`).

```bash
cd ../.. && uv run pytest tests/test_web_boundary.py -q
```

Expected: passed.

- [ ] **Step 3: Capture "before" screenshots**

Sign in at `http://localhost:5173` as `seed@example.test` (the "seeded demo
account" button). Screenshot at **375px** and **1440px** viewport widths:
sign-in, search with results, compose, the confirm gate, history (both tabs),
a send detail page, connections.

Save under `docs/images/before/` — these are the comparison set for Task 12.

- [ ] **Step 4: Commit the baseline images**

```bash
git add docs/images/before
git commit -m "docs(ui): capture the console as it stands before the redesign"
```

---

## Task 2: Tokens, with the contrast check written first

**Files:**
- Create: `apps/web/src/styles/tokens.test.ts`
- Create: `apps/web/src/styles/tokens.css`

- [ ] **Step 1: Write the failing test**

Create `apps/web/src/styles/tokens.test.ts`:

```ts
import { readFileSync } from "node:fs";
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

function tokens(): Record<string, string> {
  const found: Record<string, string> = {};
  for (const match of TOKENS.matchAll(/(--[a-z0-9-]+)\s*:\s*(#[0-9a-f]{6})\s*;/gi)) {
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
    for (const name of [...TEXT, "--surface", "--canvas", "--line", "--line-2", "--primary", "--primary-fg"]) {
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

  it("pins light and ships no dark scheme", () => {
    expect(TOKENS).toMatch(/color-scheme:\s*light\s*;/);
    expect(TOKENS).not.toMatch(/prefers-color-scheme/);
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd apps/web && npx vitest run src/styles/tokens.test.ts
```

Expected: FAIL — `ENOENT: no such file or directory, open '.../src/styles/tokens.css'`.

- [ ] **Step 3: Write the tokens**

Create `apps/web/src/styles/tokens.css`:

```css
/*
 * Design tokens — "Instrument".
 *
 * One colour scheme, pinned. Two schemes cannot both be tuned and
 * contrast-verified on this budget, and following the OS meant the author only
 * ever saw one of them — the dark one, whose #7d9cff accent on near-black is
 * the most recognisable generated-app signature there is.
 * See docs/superpowers/specs/2026-08-12-ui-redesign-design.md §3.
 *
 * Every text colour here clears 4.5:1 on --surface and every interactive
 * border clears 3:1. That is not a claim: src/styles/tokens.test.ts reads this
 * file and computes it.
 */

:root {
  color-scheme: light;

  /* surfaces */
  --canvas: #f6f7f8;
  --surface: #ffffff;
  --surface-2: #f1f3f5;
  --surface-3: #e8ebee;

  /* text */
  --ink: #15181c;
  --ink-2: #3c434e;
  --muted: #5b6472;
  --faint: #676f7d;

  /* Two line tokens, and the distinction is load-bearing.
     --line is decorative: dividers and card edges.
     --line-2 identifies a control, so it clears 3:1 and is visibly darker
     than a hairline. Never lighten it below that to taste — give the control
     a --surface-2 fill instead. */
  --line: #e4e7eb;
  --line-2: #8a939e;

  /* The one irreversible action on a screen is the darkest thing on it.
     Saturation is spent on state, not on buttons. */
  --primary: #15181c;
  --primary-hover: #000000;
  --primary-fg: #ffffff;

  /* Links, focus, selection, in-flight. Never a button fill. */
  --accent: #0b5cd5;
  --accent-weak: #eaf1fd;
  --accent-line: #b9d0f5;

  --ok: #0e6b3f;
  --ok-weak: #f1f9f4;
  --ok-line: #bfe0cd;

  /* Uncertain is amber and never red: failed means we know nothing was sent,
     uncertain means we do not know, and conflating them invites a resend. */
  --warn: #8a5a0b;
  --warn-weak: #fdf7ea;
  --warn-line: #efd7a8;

  --bad: #a4192c;
  --bad-weak: #fdf3f4;
  --bad-line: #f0c6cb;

  --r-sm: 6px;
  --r: 8px;
  --r-lg: 10px;
  --r-xl: 14px;
  --pill: 999px;

  /* Two elevation steps, and the second belongs to the gate alone. Depth
     everywhere else comes from hairlines. */
  --shadow-1: 0 1px 2px rgb(21 24 28 / 6%);
  --shadow-2:
    0 1px 2px rgb(21 24 28 / 6%),
    0 12px 32px rgb(21 24 28 / 12%);

  /* Control heights. Touch is the un-qualified default; >=40rem subtracts 8px.
     --h-md at base is exactly --tap, so the 44px touch floor holds without a
     separate rule. */
  --tap: 44px;
  --h-sm: 38px;
  --h-md: 44px;
  --h-lg: 52px;

  --safe-bottom: env(safe-area-inset-bottom, 0px);
}

@media (min-width: 40rem) {
  :root {
    --h-sm: 30px;
    --h-md: 36px;
    --h-lg: 44px;
  }
}
```

- [ ] **Step 4: Run the test again**

```bash
cd apps/web && npx vitest run src/styles/tokens.test.ts
```

Expected: all pass. (The assertion that `App.css` is gone belongs to Task 3,
which is what deletes it — this commit stays green.)

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/styles/tokens.css apps/web/src/styles/tokens.test.ts
git commit -m "feat(ui): one light palette, with its contrast claim computed rather than asserted"
```

---

## Task 3: Base layer, rewire index.css, delete App.css

**Files:**
- Create: `apps/web/src/styles/base.css`
- Modify: `apps/web/src/index.css` (whole file)
- Modify: `apps/web/src/App.tsx:12` (remove the `App.css` import)
- Delete: `apps/web/src/App.css`

- [ ] **Step 1: Write the base layer**

Create `apps/web/src/styles/base.css`:

```css
/*
 * Reset and element defaults.
 *
 * Mobile-first: everything here targets a 375px phone and the media queries
 * *add* desktop.
 */

*,
*::before,
*::after {
  box-sizing: border-box;
}

html {
  /* No horizontal scroll anywhere — checked at 375px and on rotation. */
  overflow-x: hidden;
}

body {
  margin: 0;
  background: var(--canvas);
  color: var(--ink);
  font:
    14px/1.55 system-ui,
    -apple-system,
    "Segoe UI",
    sans-serif;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
  /* Stops iOS Safari inflating text in landscape, which silently breaks a
     layout that was verified in portrait. */
  -webkit-text-size-adjust: 100%;
}

h1,
h2,
h3 {
  margin: 0;
  line-height: 1.25;
  font-weight: 640;
  letter-spacing: -0.015em;
}

h2 {
  font-size: 20px;
}

h3 {
  font-size: 16px;
}

p {
  margin: 0;
}

a {
  color: var(--accent);
  text-underline-offset: 2px;
}

code,
pre,
kbd {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  font-size: 0.9em;
}

/* Long provider ids, URLs and error payloads wrap rather than widening the
   page. A horizontal scrollbar on a phone is a layout bug, not a feature. */
pre,
code {
  overflow-wrap: anywhere;
  word-break: break-word;
}

::selection {
  background: var(--accent-weak);
}

:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--r-sm);
}

@media (min-width: 40rem) {
  h2 {
    font-size: 22px;
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

- [ ] **Step 2: Reduce index.css to imports**

Replace the **entire contents** of `apps/web/src/index.css` with:

```css
/*
 * The console's stylesheet, in six files.
 *
 * Order matters: tokens define the variables everything else reads, base sets
 * element defaults, and the later files are progressively more specific.
 * gate.css is last and on its own — it is the highest-value component in the
 * app and should be editable without scrolling past history rows.
 */
@import "./styles/tokens.css";
@import "./styles/base.css";
@import "./styles/components.css";
@import "./styles/shell.css";
@import "./styles/screens.css";
@import "./styles/gate.css";
```

- [ ] **Step 3: Create the three files that do not exist yet as empty placeholders**

CSS `@import` of a missing file fails the Vite build, so create them now with
just a header comment. They are filled in Tasks 4, 5, 7–11 and 6.

```bash
cd apps/web/src/styles
printf '/* Reusable components. Filled in Task 4. */\n' > components.css
printf '/* App shell: bars, rail, canvas. Filled in Task 5. */\n' > shell.css
printf '/* Per-screen layout. Filled in Tasks 7-11. */\n' > screens.css
printf '/* The confirm gate. Filled in Task 6. */\n' > gate.css
```

- [ ] **Step 4: Drop the App.css import**

In `apps/web/src/App.tsx`, delete line 12:

```diff
-import "./App.css";
```

- [ ] **Step 5: Delete App.css**

```bash
cd /Users/aneesbajwa/www/unified-search-safe-send && git rm apps/web/src/App.css
```

- [ ] **Step 6: Assert the old stylesheet is actually gone**

Add to `apps/web/src/styles/tokens.test.ts`, inside the `describe("tokens")`
block. Change the import on line 1 from `import { readFileSync }` to
`import { existsSync, readFileSync }`.

```ts
  it("has retired App.css rather than leaving it as dead weight", () => {
    expect(existsSync(join(HERE, "..", "App.css"))).toBe(false);
  });
```

- [ ] **Step 7: Verify**

```bash
cd apps/web && npx vitest run src/styles/tokens.test.ts && npm run typecheck && npm run build
```

Expected: all token tests pass, including the new `has retired App.css`.
Typecheck clean. Build succeeds — this is what proves the six `@import`s
resolve.

The running app will look unstyled. That is expected until Task 5.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/index.css apps/web/src/styles apps/web/src/App.tsx
git commit -m "refactor(ui): split the stylesheet into six files and retire App.css"
```

---

## Task 4: Components

**Files:**
- Modify: `apps/web/src/styles/components.css` (replace the placeholder)

- [ ] **Step 1: Write the component layer**

Replace the whole of `apps/web/src/styles/components.css`:

```css
/*
 * Reusable components.
 *
 * Two rules this layer exists to make structural rather than aspirational:
 *   1. One primary action per view.
 *   2. Paired actions share a size — see .actions.
 * The second is why the gate's Send was 54px tall next to a 48px Cancel.
 */

/* ------------------------------------------------------------------ text */

.page-sub,
.muted {
  color: var(--muted);
  font-size: 13px;
}

.bad {
  color: var(--bad);
  font-size: 13px;
}

.section-head {
  margin: 4px 0 0;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--faint);
}

/* --------------------------------------------------------------- buttons */

/* .button on its own is the *secondary* variant, as it has always been. */
button,
.button {
  -webkit-appearance: none;
  appearance: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: var(--h-md);
  padding: 0 14px;
  border: 1px solid var(--line-2);
  border-radius: var(--r);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 13.5px;
  font-weight: 600;
  white-space: nowrap;
  text-decoration: none;
  cursor: pointer;
  transition:
    background 120ms ease,
    border-color 120ms ease;
}

button:not(:disabled):hover,
.button:not(:disabled):hover {
  background: var(--surface-2);
}

button:disabled,
.button:disabled {
  opacity: 0.5;
  cursor: default;
}

.button-sm {
  min-height: var(--h-sm);
  padding: 0 11px;
  font-size: 12.5px;
}

.button-lg {
  min-height: var(--h-lg);
  padding: 0 20px;
  font-size: 15px;
}

.button-primary {
  background: var(--primary);
  border-color: var(--primary);
  color: var(--primary-fg);
}

.button-primary:not(:disabled):hover {
  background: var(--primary-hover);
  border-color: var(--primary-hover);
}

.button-quiet {
  border-color: transparent;
  background: transparent;
  color: var(--accent);
}

.button-quiet:not(:disabled):hover {
  background: var(--accent-weak);
}

.button-danger {
  background: var(--bad-weak);
  border-color: var(--bad-line);
  color: var(--bad);
}

.button-danger:not(:disabled):hover {
  background: color-mix(in srgb, var(--bad) 12%, var(--surface));
}

/* Paired actions share a size. The cluster owns the height and the children
   are told not to fight it, so two siblings cannot disagree — which is the
   defect this class exists to make impossible.

   The guarantee holds per flex line. Anywhere the pair must stack (the gate on
   a phone), do not use .actions — stack them explicitly instead. */
.actions {
  display: flex;
  align-items: stretch;
  gap: 12px;
  flex-wrap: wrap;
  min-height: var(--h-md);
}

.actions-lg {
  min-height: var(--h-lg);
}

.actions-end {
  justify-content: flex-end;
}

.actions > button,
.actions > .button {
  min-height: 0;
}

/* ---------------------------------------------------------------- inputs */

input,
select,
textarea {
  width: 100%;
  min-height: var(--h-md);
  padding: 9px 12px;
  border: 1px solid var(--line-2);
  border-radius: var(--r);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  /* 16px or iOS zooms the page on focus and the layout jumps. */
  font-size: 16px;
}

textarea {
  min-height: 200px;
  line-height: 1.55;
  resize: vertical;
}

select {
  -webkit-appearance: none;
  appearance: none;
  padding-right: 34px;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2.5 4.5 6 8l3.5-3.5' fill='none' stroke='%235b6472' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/></svg>");
  background-repeat: no-repeat;
  background-position: right 12px center;
}

.field {
  display: grid;
  gap: 5px;
}

.field-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--faint);
}

@media (min-width: 40rem) {
  input,
  select,
  textarea {
    font-size: 14px;
  }
}

/* ----------------------------------------------------------- card, panel */

.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  padding: 14px;
  box-shadow: var(--shadow-1);
}

/* One primitive for four lists that each used to invent their own: the
   send-detail facts, history rows, connection rows and the source panel. */
.panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-1);
  overflow: hidden;
}

.panel-head {
  padding: 9px 14px;
  background: var(--surface-2);
  border-bottom: 1px solid var(--line);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--faint);
}

.panel-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.panel-item {
  border-bottom: 1px solid var(--line);
}

.panel-item:last-child {
  border-bottom: 0;
}

.panel-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 14px;
  font-size: 13px;
}

.panel-link {
  display: grid;
  gap: 4px;
  min-height: var(--tap);
  padding: 12px 14px;
  color: inherit;
  text-decoration: none;
}

.panel-link:hover {
  background: var(--surface-2);
}

.panel-body {
  padding: 14px;
}

.panel-foot {
  padding: 10px 14px;
  border-top: 1px solid var(--line);
  background: var(--surface-2);
}

/* --------------------------------------------------------- chip, badge */

.badge,
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--line);
  border-radius: var(--pill);
  background: var(--surface);
  color: var(--muted);
  font-size: 11.5px;
  font-weight: 600;
  line-height: 1.35;
  white-space: nowrap;
}

.badge {
  padding: 2px 9px;
}

.chip {
  padding: 5px 11px;
}

.badge-ok {
  color: var(--ok);
  background: var(--ok-weak);
  border-color: var(--ok-line);
}

.badge-bad {
  color: var(--bad);
  background: var(--bad-weak);
  border-color: var(--bad-line);
}

.badge-flight {
  color: var(--accent);
  background: var(--accent-weak);
  border-color: var(--accent-line);
}

/* Amber, not red. Failed means we know nothing was sent; uncertain means we do
   not know, and conflating them invites exactly the wrong action. */
.badge-doubt {
  color: var(--warn);
  background: var(--warn-weak);
  border-color: var(--warn-line);
}

.badge-seed {
  color: var(--faint);
  background: var(--surface-2);
  border-color: var(--line);
  font-weight: 500;
}

.chip-source {
  font-weight: 700;
  color: var(--ink);
}

.chip-value {
  color: var(--muted);
}

/* --muted, not --faint. This is the one place a text token sits on
   --surface-3, and --faint measures 4.23:1 there — below AA, and at 10.5px
   that is the least forgiving size in the system. --muted clears it. */
.chip-mode {
  padding: 1px 6px;
  border-radius: var(--pill);
  background: var(--surface-3);
  color: var(--muted);
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

/* The four terminal states must never collapse into each other.
   SourceStatusChip.test.tsx asserts four distinct className values. */
.chip-done {
  border-color: var(--ok-line);
  background: var(--ok-weak);
}

.chip-done .chip-source,
.chip-done .chip-value {
  color: var(--ok);
}

.chip-empty {
  border-color: var(--line);
  background: var(--surface-2);
}

.chip-failed {
  border-color: var(--bad-line);
  background: var(--bad-weak);
}

.chip-failed .chip-source,
.chip-failed .chip-value {
  color: var(--bad);
}

.chip-retrying {
  border-color: var(--warn-line);
  background: var(--warn-weak);
}

.chip-retrying .chip-source,
.chip-retrying .chip-value {
  color: var(--warn);
}

.chip-running {
  border-color: var(--accent-line);
  background: var(--accent-weak);
  animation: pulse 1.4s ease-in-out infinite;
}

@keyframes pulse {
  50% {
    opacity: 0.55;
  }
}

.ellipsis::after {
  content: "…";
}

/* An action reads as an action: a real touch target and a verb. */
.chip-action {
  min-height: var(--h-md);
  padding: 0 14px;
  border-color: var(--accent-line);
  background: var(--accent-weak);
  color: var(--ink);
  font-size: 12.5px;
}

.chip-action .chip-verb {
  color: var(--accent);
  font-weight: 700;
}

/* -------------------------------------------------------------- notices */

.notices {
  display: grid;
  gap: 10px;
}

/* A row, not a stack: dot, then the words, then the action pushed right. The
   old layout put a full-width button under the text, which made "Reconnect"
   read as heavier than the problem it solves. */
.notice {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  flex-wrap: wrap;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  background: var(--surface);
}

.notice::before {
  content: "";
  flex: none;
  width: 7px;
  height: 7px;
  margin-top: 6px;
  border-radius: 50%;
  background: var(--muted);
}

.notice-body {
  flex: 1 1 16rem;
  min-width: 0;
  display: grid;
  gap: 3px;
}

.notice-title {
  font-size: 13.5px;
  font-weight: 650;
}

.notice-detail {
  font-size: 12.5px;
  color: var(--muted);
  overflow-wrap: anywhere;
}

.notice-meta {
  font-size: 12px;
  color: var(--faint);
}

.notice-cta,
.notice > button,
.notice > .button {
  flex: none;
  margin-left: auto;
}

.notice-bad {
  border-color: var(--bad-line);
  background: var(--bad-weak);
}

.notice-bad::before {
  background: var(--bad);
}

.notice-bad .notice-title {
  color: var(--bad);
}

.notice-warn {
  border-color: var(--warn-line);
  background: var(--warn-weak);
}

.notice-warn::before {
  background: var(--warn);
}

.notice-warn .notice-title {
  color: var(--warn);
}

.notice-info {
  border-color: var(--accent-line);
  background: var(--accent-weak);
}

.notice-info::before {
  background: var(--accent);
}

/* An action, not an error. It gets the accent, not the alarm. */
.notice-action {
  border-color: var(--accent-line);
  background: var(--accent-weak);
}

.notice-action::before {
  background: var(--accent);
}

/* Below 40rem the action wraps to its own full-width row. */
@media (max-width: 39.99rem) {
  .notice-cta,
  .notice > button,
  .notice > .button {
    width: 100%;
    margin-left: 0;
  }
}

/* -------------------------------------------------- empty, list, misc */

.empty {
  display: grid;
  gap: 6px;
  padding: 22px 16px;
  border: 1px dashed var(--line-2);
  border-radius: var(--r-lg);
  text-align: center;
}

.empty-title {
  font-size: 15px;
  font-weight: 640;
}

.empty-body {
  max-width: 34rem;
  margin: 0 auto;
  font-size: 13.5px;
  color: var(--muted);
}

.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 10px;
}

.plain-list {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 13px;
  color: var(--muted);
}

.load-more {
  display: flex;
  justify-content: center;
  padding: 4px 0 8px;
}

/* A disclosure triangle is a control, so it is measured against the same touch
   floor as everything else — the browser default is about 24px. */
.rationale {
  padding: 4px 12px;
  border: 1px solid var(--line);
  border-radius: var(--r);
  background: var(--surface-2);
  font-size: 13px;
}

.rationale summary {
  display: flex;
  align-items: center;
  min-height: var(--tap);
  color: var(--muted);
  font-weight: 600;
  cursor: pointer;
}

.rationale p {
  margin-bottom: 12px;
  color: var(--muted);
  line-height: 1.55;
}

.countdown {
  font-size: 12px;
  color: var(--muted);
}

.countdown-waiting {
  color: var(--warn);
  font-weight: 600;
}

.countdown-checking {
  color: var(--accent);
  font-weight: 600;
}

.countdown-backoff,
.countdown-sep {
  color: var(--faint);
  font-weight: 400;
}

/* The full body, scrollable, never truncated: a customer cannot confirm — or
   audit — what they cannot read. */
.body-preview {
  max-height: 40vh;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: var(--r);
  background: var(--surface-2);
  font-size: 15px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}

/* Untruncated on purpose: a generic message is what makes an operator guess. */
.error-detail {
  max-height: 50vh;
  margin: 0;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: var(--r);
  background: var(--surface-2);
  font-size: 12.5px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  overflow: auto;
}
```

- [ ] **Step 2: Verify it builds and nothing regressed**

```bash
cd apps/web && npm run build && npm test
```

Expected: build succeeds; all tests pass.

- [ ] **Step 3: Commit**

```bash
git add apps/web/src/styles/components.css
git commit -m "feat(ui): the component layer, with paired actions that cannot disagree about size"
```

---

## Task 5: Shell — bars, rail, nav icons

**Files:**
- Create: `apps/web/src/components/NavIcon.tsx`
- Modify: `apps/web/src/styles/shell.css` (replace the placeholder)
- Modify: `apps/web/src/App.tsx` (Brand, Nav, and the shell markup)

- [ ] **Step 1: Write the icons**

Create `apps/web/src/components/NavIcon.tsx`:

```tsx
/**
 * Four icons, written by hand rather than pulled from a library.
 *
 * They earn their place in the bottom tab bar, which is text-only today and is
 * the least finished surface in the app. `aria-hidden` throughout: the text
 * label beside them is the accessible name, and an icon that also announces
 * itself just says everything twice.
 */
export type NavIconName = "search" | "compose" | "history" | "accounts";

const PATHS: Record<NavIconName, string> = {
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14ZM20 20l-4.1-4.1",
  compose: "M4 20h16M6 16.5 16.8 5.7a2 2 0 0 1 2.8 2.8L8.8 19.3 4.5 20l.7-4.3Z",
  history: "M12 7v5l3.2 2M3.5 12a8.5 8.5 0 1 0 2.6-6.1M5.5 3.5v3h3",
  accounts: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM4.5 20a7.5 7.5 0 0 1 15 0",
};

export function NavIcon({ name }: { name: NavIconName }) {
  return (
    <svg
      className="navicon"
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
```

- [ ] **Step 2: Write the shell layer**

Replace the whole of `apps/web/src/styles/shell.css`:

```css
/*
 * The app shell.
 *
 * Below 64rem: top bar, single column, bottom tab bar — every primary
 * destination inside a thumb's reach, which is a one-handed decision rather
 * than a fashion.
 *
 * From 64rem: a left rail, and the width that buys is spent on the Search
 * page's source panel (screens.css) rather than on wider margins.
 */

.app {
  min-height: 100dvh;
  display: flex;
  flex-direction: column;
}

/* ------------------------------------------------------ top bar (mobile) */

.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 56px;
  padding: 0 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

/* A solid mark. The gradient it replaces is the second most recognisable
   generated-app signature after periwinkle-on-black. */
.brand-mark {
  flex: none;
  width: 22px;
  height: 22px;
  border-radius: var(--r-sm);
  background: var(--ink);
}

.brand-name {
  font-size: 13.5px;
  font-weight: 650;
  letter-spacing: -0.01em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  min-width: 0;
}

.identity {
  max-width: 44vw;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ------------------------------------------------------------- tab bar */

.tabbar {
  position: fixed;
  inset: auto 0 0 0;
  z-index: 20;
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: 1fr;
  background: var(--surface);
  border-top: 1px solid var(--line);
  padding-bottom: var(--safe-bottom);
}

.navlink {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  min-height: 54px;
  padding: 6px 8px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--muted);
  text-decoration: none;
}

.navlink-on {
  color: var(--ink);
}

.navicon {
  flex: none;
}

/* While the gate is open there is nowhere else to be. */
.app[data-gate="open"] .topbar,
.app[data-gate="open"] .tabbar,
.app[data-gate="open"] .rail {
  display: none;
}

/* ------------------------------------------------------------- content */

.app-main {
  flex: 1;
  width: 100%;
  padding: 16px 16px calc(76px + var(--safe-bottom));
}

.page {
  display: grid;
  gap: 16px;
  align-content: start;
  width: 100%;
  max-width: 58rem;
  margin: 0 auto;
}

.page-head {
  display: grid;
  gap: 6px;
}

/* The rail does not exist below 64rem. */
.rail {
  display: none;
}

/* Signed out: no rail, no tab bar, so nothing to leave room for. */
.app-bare .app-main {
  padding-bottom: 24px;
}

/* --------------------------------------------------- 40rem: density only */

@media (min-width: 40rem) {
  .app-main {
    padding: 20px 24px 48px;
  }
}

/* ----------------------------------------------------- 64rem: the shell */

@media (min-width: 64rem) {
  .app {
    flex-direction: row;
  }

  .topbar,
  .tabbar {
    display: none;
  }

  .rail {
    position: sticky;
    top: 0;
    align-self: flex-start;
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: none;
    width: 214px;
    height: 100dvh;
    padding: 14px 12px;
    background: var(--surface);
    border-right: 1px solid var(--line);
  }

  .rail .brand {
    padding: 4px 8px 16px;
  }

  .rail .navlink {
    flex-direction: row;
    justify-content: flex-start;
    gap: 10px;
    min-height: 38px;
    padding: 8px 10px;
    border-radius: var(--r);
    font-size: 13.5px;
  }

  .rail .navlink-on {
    background: var(--surface-2);
    color: var(--ink);
    font-weight: 650;
    box-shadow: inset 2px 0 0 0 var(--ink);
  }

  .rail-foot {
    display: grid;
    gap: 4px;
    margin-top: auto;
    padding: 12px 10px 0;
    border-top: 1px solid var(--line);
  }

  .rail-foot .identity {
    max-width: 100%;
  }

  .rail-foot .button {
    justify-content: flex-start;
    padding: 0;
  }

  .app-main {
    flex: 1;
    min-width: 0;
    padding: 24px;
  }
}
```

- [ ] **Step 3: Rewrite App.tsx's shell**

Replace `apps/web/src/App.tsx` in full. The doc comment, the auth gate and the
route table are unchanged; the chrome around them is not.

```tsx
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { clearKey, storedIdentity, storedKey } from "./api/client";
import { NavIcon, type NavIconName } from "./components/NavIcon";
import { ComposePage } from "./routes/ComposePage";
import { ConfirmDialog } from "./routes/ConfirmDialog";
import { ConnectionsPage } from "./routes/ConnectionsPage";
import { HistoryPage } from "./routes/HistoryPage";
import { SearchPage } from "./routes/SearchPage";
import { SendDetailPage } from "./routes/SendDetailPage";
import { SignInPage } from "./routes/SignInPage";

/**
 * The console. A **pure consumer** of the documented API: it holds no business
 * rules and has no privileged path.
 *
 * A grep of this directory for ranking, error classification or any local
 * `canSend` derivation returns nothing, and that is not a promise —
 * `tests/test_web_boundary.py` greps this tree and fails on a hit, mirroring the
 * source-agnosticism test on the backend. Whether a send may proceed, whether an
 * error is retryable and how results rank are all read from API responses.
 *
 * Navigation is a bottom bar on a phone and a left rail from 64rem up. That is
 * a thumb decision rather than a fashion: every primary destination has to be
 * reachable one-handed, and the confirm sheet deliberately covers the bar —
 * while the gate is open, there is nowhere else to be.
 */
export default function App() {
  const [signedIn, setSignedIn] = useState(() => Boolean(storedKey()));
  const queryClient = useQueryClient();
  const location = useLocation();

  if (!signedIn) {
    return (
      <div className="app app-bare">
        <main className="app-main">
          <SignInPage onSignedIn={() => setSignedIn(true)} />
        </main>
      </div>
    );
  }

  // The gate takes the whole screen on a phone, so the chrome around it would
  // only be somewhere else to tap.
  const gateOpen = location.pathname.startsWith("/confirm/");

  const signOut = () => {
    clearKey();
    queryClient.clear();
    setSignedIn(false);
  };

  return (
    <div className="app" data-gate={gateOpen ? "open" : "closed"}>
      <nav className="rail" aria-label="Primary">
        <Brand />
        <Nav />
        <div className="rail-foot">
          <span className="identity" title="Signed in as">
            {storedIdentity() ?? "signed in"}
          </span>
          <button type="button" className="button button-quiet button-sm" onClick={signOut}>
            Sign out
          </button>
        </div>
      </nav>

      <header className="topbar">
        <Brand />
        <div className="topbar-right">
          <span className="identity" title="Signed in as">
            {storedIdentity() ?? "signed in"}
          </span>
          <button type="button" className="button button-quiet button-sm" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>

      <main className="app-main">
        <Routes>
          <Route path="/" element={<SearchPage />} />
          <Route path="/search/:searchId" element={<SearchPage />} />
          <Route path="/compose" element={<ComposePage />} />
          <Route path="/confirm/:draftId" element={<ConfirmDialog />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/history/sends/:sendId" element={<SendDetailPage />} />
          <Route path="/connections" element={<ConnectionsPage />} />
        </Routes>
      </main>

      <nav className="tabbar" aria-label="Primary">
        <Nav />
      </nav>
    </div>
  );
}

function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark" aria-hidden="true" />
      <span className="brand-name">Unified Search</span>
    </div>
  );
}

const LINKS: Array<{ to: string; label: string; end: boolean; icon: NavIconName }> = [
  { to: "/", label: "Search", end: true, icon: "search" },
  { to: "/compose", label: "Compose", end: false, icon: "compose" },
  { to: "/history", label: "History", end: false, icon: "history" },
  { to: "/connections", label: "Accounts", end: false, icon: "accounts" },
];

function Nav() {
  return (
    <>
      {LINKS.map((link) => (
        <NavLink
          key={link.to}
          to={link.to}
          end={link.end}
          className={({ isActive }) => (isActive ? "navlink navlink-on" : "navlink")}
        >
          <NavIcon name={link.icon} />
          {link.label}
        </NavLink>
      ))}
    </>
  );
}
```

Note the rail and the top bar both render, and CSS decides which is visible.
Both `<nav>` elements carry `aria-label="Primary"`, which is intentional —
only one is in the accessibility tree at a time because the other is
`display: none`.

- [ ] **Step 4: Verify**

```bash
cd apps/web && npm run typecheck && npm run lint && npm test && npm run build
```

Expected: all clean.

- [ ] **Step 5: Look at it**

With `make up` running, open `http://localhost:5173`. At 1440px you should see
the rail; at 375px the top bar and the icon tab bar. The app is coherent again
from here on.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/App.tsx apps/web/src/components/NavIcon.tsx apps/web/src/styles/shell.css
git commit -m "feat(ui): a left rail on desktop, a real icon tab bar on a phone"
```

---

## Task 6: The gate — both defects, tests first

**Files:**
- Create: `apps/web/src/routes/ConfirmDialog.test.tsx`
- Modify: `apps/web/src/styles/gate.css` (replace the placeholder)
- Modify: `apps/web/src/routes/ConfirmDialog.tsx:166-225` (the footer)
- Modify: `apps/web/vite.config.ts` (the comment about one unit-tested component)

- [ ] **Step 1: Write the failing regression tests**

Create `apps/web/src/routes/ConfirmDialog.test.tsx`:

```tsx
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
      [...buttons].map((button) => (button.className.match(/\bbutton-(sm|lg)\b/) ?? [, "md"])[1]),
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
      [...buttons].map((button) => (button.className.match(/\bbutton-(sm|lg)\b/) ?? [, "md"])[1]),
    );
    expect(sizes.size).toBe(1);
  });
});
```

- [ ] **Step 2: Run it and watch two of the three fail**

```bash
cd apps/web && npx vitest run src/routes/ConfirmDialog.test.tsx
```

Expected: `gives Cancel and Send the same size class` FAILS (today Send has
`gate-confirm` and Cancel has `button-cancel`, with no shared size class), and
`puts the settled block outside the action cluster` FAILS (`.gate-settled` is
inside `.gate-actions` today).

- [ ] **Step 3: Restructure the footer**

In `apps/web/src/routes/ConfirmDialog.tsx`, replace the whole `<footer>` block
(lines 164–225) with:

```tsx
        {/* 3 and 4. Distinct, separated, disabled on press, and the returned
            send state shown immediately rather than a spinner.

            The settled block is its own region rather than a child of the
            action row. As a grid item inside a column-flow parent it collapsed
            into a narrow right-pinned column with dead canvas beside it; out
            here it cannot. */}
        {settled ? (
          <>
            <div className="gate-settled">
              <p className="gate-settled-line">
                <StateBadge state={settled.send.state} />
                <span className="gate-settled-id">
                  <code>{settled.send.send_id.slice(0, 8)}</code>
                </span>
              </p>
              <p className="gate-settled-note">
                {settled.replayed
                  ? "This was a duplicate — the same message, not a second one."
                  : "Recorded. It is now yours to follow."}
              </p>
            </div>
            <footer className="gate-actions">
              <button
                type="button"
                className="button button-lg"
                onClick={() => navigate("/history")}
              >
                Done
              </button>
              <button
                type="button"
                className="button button-primary button-lg"
                onClick={() => navigate(`/history/sends/${settled.send.send_id}`)}
              >
                Open the send
              </button>
            </footer>
          </>
        ) : (
          <footer className="gate-actions">
            <button
              type="button"
              className="button button-lg"
              onClick={() => navigate(-1)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="button button-primary button-lg gate-confirm"
              disabled={pressed || send.isPending}
              onClick={() => {
                if (inFlight.current) return;
                inFlight.current = true;
                setPressed(true);
                send.mutate({
                  draftId: draft.data.draft.id,
                  confirmedSha256: confirmation.confirm_sha256,
                });
              }}
            >
              {pressed ? "Sending…" : "Send it"}
            </button>
          </footer>
        )}
```

Note the DOM order is now **Cancel first, Send second**. On desktop that reads
left-to-right as cancel-then-confirm; on a phone CSS reverses it with `order`
so Send is on top and Cancel sits under the thumb. The `.gate-gap` spacer div
is gone — `gap` and `order` do that job.

- [ ] **Step 4: Run the tests again**

```bash
cd apps/web && npx vitest run src/routes/ConfirmDialog.test.tsx
```

Expected: 3 passed.

- [ ] **Step 5: Write the gate's stylesheet**

Replace the whole of `apps/web/src/styles/gate.css`:

```css
/* ============================================================== THE GATE ==

   The highest-value component in the app. A cramped or ambiguous confirm step
   *is* the misfire this product exists to prevent, so it gets the whole screen
   on a phone and the layout is a fixed three-row grid: the destination never
   scrolls away, the body owns the only scrolling region, and the actions are
   always where the thumb expects them.
   ========================================================================= */

.gate {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: grid;
  background: var(--canvas);
}

.gate-sheet {
  display: grid;
  grid-template-rows: auto 1fr auto;
  min-height: 0;
  background: var(--surface);
}

/* Settled adds a fourth row between the body and the actions. */
.gate-sheet:has(.gate-settled) {
  grid-template-rows: auto 1fr auto auto;
}

.gate-loading {
  display: grid;
  gap: 12px;
  align-content: center;
  justify-items: center;
  padding: 24px;
  text-align: center;
}

.gate-head {
  display: grid;
  gap: 3px;
  padding: 16px 18px 14px;
  border-bottom: 1px solid var(--line);
}

.gate-eyebrow {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--faint);
}

/* 1. The destination is the most prominent element on the screen. Verbatim —
      never a friendly alias that could hide the real recipient. */
.gate-destination {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.15;
  letter-spacing: -0.02em;
  overflow-wrap: anywhere;
}

.gate-warning {
  margin-top: 3px;
  font-size: 13.5px;
  line-height: 1.5;
  color: var(--ink-2);
  overflow-wrap: anywhere;
}

.gate-via {
  font-size: 12.5px;
  color: var(--muted);
  overflow-wrap: anywhere;
}

.gate-channel {
  font-weight: 650;
  text-transform: capitalize;
}

.gate-subject {
  font-size: 12.5px;
  overflow-wrap: anywhere;
}

.gate-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--faint);
}

/* 2. The only scrolling region. */
.gate-scroll {
  min-height: 0;
  display: grid;
  gap: 10px;
  align-content: start;
  padding: 14px 18px 18px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}

.gate-body-label {
  margin-bottom: -4px;
}

.gate-scroll .body-preview {
  max-height: none;
  overflow: visible;
}

.gate-digest {
  font-size: 12px;
  color: var(--faint);
  overflow-wrap: anywhere;
}

.gate-error {
  margin-top: 2px;
}

/* The settled block: its own region, full width, left-aligned. */
.gate-settled {
  padding: 14px 18px 0;
}

.gate-settled-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.gate-settled-id {
  font-size: 12px;
  color: var(--muted);
}

.gate-settled-note {
  font-size: 13px;
  color: var(--muted);
}

/* 3 and 4. Distinct, separated, and never adjacent on a phone.

   Base case (touch): stacked full-width, the primary on top, a 24px gap, and
   the secondary at the bottom where a thumb rests. The gap *is* the defence —
   a thumb reaching for Cancel must not be able to land on Send. */
.gate-actions {
  display: grid;
  gap: 24px;
  padding: 16px 18px calc(18px + var(--safe-bottom));
  border-top: 1px solid var(--line);
  background: var(--surface);
}

.gate-actions > button {
  width: 100%;
  min-height: var(--h-lg);
}

/* DOM order is Cancel then Send. On a phone the primary goes on top. */
.gate-actions > .button-primary {
  order: -1;
}

@media (min-width: 40rem) {
  /* One row, right-aligned, and the two actions are the same size — which the
     old 54px-vs-48px pair was not.

     The height lives on the buttons, not on this row. Putting `min-height` here
     instead does not work: box-sizing is border-box, so the row's own 16/18px
     padding is *inside* that height, and stretched children with `min-height: 0`
     collapse to whatever is left — 25px, measured. Equal sizing comes from both
     buttons sharing `--h-lg`, and `stretch` then keeps them level if one wraps
     to two lines. */
  .gate-actions {
    display: flex;
    align-items: stretch;
    justify-content: flex-end;
    gap: 12px;
  }

  .gate-actions > button {
    width: auto;
    min-width: 9rem;
  }

  .gate-actions > .button-primary {
    order: 0;
  }

  .gate {
    place-items: center;
    padding: 24px;
    background: rgb(21 24 28 / 45%);
  }

  .gate-sheet {
    width: min(34rem, 100%);
    max-height: min(46rem, 100%);
    border: 1px solid var(--line);
    border-radius: var(--r-xl);
    box-shadow: var(--shadow-2);
    overflow: hidden;
  }
}
```

- [ ] **Step 6: Update the now-untrue comment in vite.config.ts**

In `apps/web/vite.config.ts`, replace the `test.include` comment:

```diff
-    // One component is unit-tested and it is `SourceStatusChip` — see its test
-    // file for why that one and not the others.
+    // Unit tests are deliberately few and each one earns its place: the source
+    // chip (four states that must never collapse), the gate's footer structure
+    // (two shipped layout defects, locked), and the palette's contrast claim.
```

- [ ] **Step 7: Verify, and look at all three states**

```bash
cd apps/web && npm run typecheck && npm run lint && npm test
```

Expected: all pass.

With the stack up: compose a message, reach the gate, and check at **375px**
that Send is on top, Cancel is at the bottom, and there is a clear 24px gap
between them. At **1440px** check both buttons are the same height on one
right-aligned row. Send it, and check the settled state fills the width with no
dead canvas.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/routes/ConfirmDialog.tsx apps/web/src/routes/ConfirmDialog.test.tsx apps/web/src/styles/gate.css apps/web/vite.config.ts
git commit -m "fix(gate): equal-size actions, and a settled block that cannot collapse into a column"
```

---

## Task 7: Search — the standing source panel

**Files:**
- Create: `apps/web/src/components/SourcePanel.tsx`
- Modify: `apps/web/src/styles/screens.css` (append)
- Modify: `apps/web/src/routes/SearchPage.tsx:105-157`

- [ ] **Step 1: Write the panel**

Create `apps/web/src/components/SourcePanel.tsx`:

```tsx
import type { SourceView } from "../api/types";
import { sourceLabel } from "../lib/format";

/**
 * Per-source status as a standing panel rather than a chip row that scrolls
 * away.
 *
 * The reason is specific rather than aesthetic: a source that failed silently
 * makes an incomplete answer look complete, and in a single column that
 * information leaves the viewport the moment you scroll the results. This is
 * what the desktop width is spent on.
 *
 * The chip row still renders below 64rem — CSS decides which is visible, and
 * neither derives anything. `status`, `error.classification` and
 * `result_count` are read from the payload exactly as the chips read them.
 */
export function SourcePanel({
  sources,
  reported,
}: {
  sources: SourceView[];
  reported: number;
}) {
  return (
    <aside className="panel source-panel" aria-label="Source status">
      <p className="panel-head">
        Sources · {reported} of {sources.length} reported
      </p>
      <ul className="panel-list">
        {sources.map((source) => (
          <li
            key={`${source.source}:${source.connection_id ?? "none"}`}
            className="panel-item"
          >
            <div className="panel-row">
              <span className="source-panel-name">
                {source.display_name
                  ? `${sourceLabel(source.source)} · ${source.display_name}`
                  : sourceLabel(source.source)}
              </span>
              <SourceState source={source} />
            </div>
          </li>
        ))}
      </ul>
    </aside>
  );
}

/** The same four terminal states the chips render, in the same colours. */
function SourceState({ source }: { source: SourceView }) {
  if (source.status === "pending" || source.status === "running") {
    return (
      <span className="source-panel-state is-running">
        searching<span className="ellipsis" aria-hidden="true" />
      </span>
    );
  }
  if (source.status === "failed" || source.error) {
    const retrying = source.error?.classification === "transient";
    return (
      <span className={retrying ? "source-panel-state is-retrying" : "source-panel-state is-failed"}>
        {retrying ? "retrying" : "unavailable"}
      </span>
    );
  }
  if (source.result_count > 0) {
    return <span className="source-panel-state is-done">{source.result_count}</span>;
  }
  return <span className="source-panel-state">no matches</span>;
}
```

- [ ] **Step 2: Append the search styles**

Append to `apps/web/src/styles/screens.css`:

```css
/* ----------------------------------------------------------------- search */

.searchbar {
  display: flex;
  gap: 8px;
}

.searchbar input {
  flex: 1;
  min-width: 0;
}

.search-layout {
  display: grid;
  gap: 16px;
  align-items: start;
}

.search-main {
  display: grid;
  gap: 14px;
  min-width: 0;
}

.search-side {
  display: none;
}

.search-summary {
  display: grid;
  gap: 3px;
}

.search-query {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 19px;
  overflow-wrap: anywhere;
}

.search-progress {
  font-size: 12.5px;
  color: var(--muted);
}

.search-footer {
  display: grid;
  gap: 8px;
  justify-items: start;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.results {
  display: grid;
  gap: 9px;
}

.result-head {
  display: flex;
  align-items: center;
  gap: 10px;
  justify-content: space-between;
}

.result-source {
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: var(--faint);
}

.result-time {
  font-size: 11.5px;
  color: var(--faint);
}

.result-title {
  margin: 6px 0 3px;
  font-size: 14.5px;
  overflow-wrap: anywhere;
}

.result-snippet {
  font-size: 13px;
  color: var(--muted);
  overflow-wrap: anywhere;
}

.result-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
}

.result-author {
  min-width: 0;
  font-size: 11.5px;
  color: var(--faint);
  overflow-wrap: anywhere;
}

.result-actions {
  display: flex;
  gap: 4px;
  margin-left: auto;
}

.source-panel-name {
  font-weight: 650;
  color: var(--ink);
  overflow-wrap: anywhere;
  min-width: 0;
}

.source-panel-state {
  margin-left: auto;
  font-size: 11.5px;
  font-weight: 650;
  color: var(--muted);
  white-space: nowrap;
}

.source-panel-state.is-done {
  color: var(--ok);
}

.source-panel-state.is-failed {
  color: var(--bad);
}

.source-panel-state.is-retrying {
  color: var(--warn);
}

.source-panel-state.is-running {
  color: var(--accent);
}

/* From 64rem the width buys a standing source panel, and the chip row and
   inline notices step aside for it. */
@media (min-width: 64rem) {
  .search-layout {
    grid-template-columns: minmax(0, 1fr) 250px;
    gap: 20px;
  }

  .search-side {
    display: grid;
    gap: 12px;
    position: sticky;
    top: 24px;
  }

  .search-main > .chips,
  .search-main > .notices {
    display: none;
  }
}
```

- [ ] **Step 3: Rewire SearchPage**

In `apps/web/src/routes/SearchPage.tsx`, add the import:

```tsx
import { SourcePanel } from "../components/SourcePanel";
```

Then replace the `{data ? ( ... ) : null}` block (lines 105–179) with:

```tsx
      {data ? (
        <div className="search-layout">
          <div className="search-main">
            <div className="search-summary">
              <h2 className="search-query">
                {data.query} <SeedBadge isSeed={data.is_seed} />
              </h2>
              <p className="search-progress" aria-live="polite">
                {data.finished ? (
                  summariseSources(data.results.length, sources)
                ) : (
                  <>
                    {reported} of {sources.length} sources reported · showing partial results
                  </>
                )}
              </p>
            </div>

            <div className="chips">
              {sources.map((source) => (
                <SourceStatusChip
                  key={keyOf(source)}
                  source={source}
                  searchFinished={data.finished}
                  disambiguate={(perSource.get(source.source) ?? 0) > 1}
                  onAction={connect.start}
                />
              ))}
            </div>

            {problems.length > 0 ? (
              <div className="notices">
                {problems.map((source) => (
                  <SourceNotice key={keyOf(source)} source={source} onAction={connect.start} />
                ))}
              </div>
            ) : null}

            {data.results.length === 0 ? (
              {/* Copy unchanged — "the source chips above" is still accurate on
                  a phone, which is where this is most often read. */}
              <EmptyState title={data.finished ? "No matches" : "Nothing has landed yet"}>
                {data.finished
                  ? "Check the source chips above. A source that could not be reached is not the same as a source with nothing in it."
                  : "Sources report as they finish, so results appear here one source at a time."}
              </EmptyState>
            ) : (
              <div className="results">
                {data.results.map((result) => (
                  <ResultCard key={`${result.source}:${result.id}`} result={result} />
                ))}
              </div>
            )}

            {data.finished ? (
              <div className="search-footer">
                <button
                  type="button"
                  className="button"
                  disabled={rerun.isPending}
                  onClick={() =>
                    rerun.mutate(data.search_id, {
                      onSuccess: (created) => navigate(`/search/${created.search_id}`),
                    })
                  }
                >
                  {rerun.isPending ? "Running…" : "Run it again"}
                </button>
                <p className="muted">
                  A rerun is a new search. This one stays exactly as it is — including what
                  failed, which is the part worth keeping.
                </p>
              </div>
            ) : null}
          </div>

          <div className="search-side">
            <SourcePanel sources={sources} reported={reported} />
            {problems.length > 0 ? (
              <div className="notices">
                {problems.map((source) => (
                  <SourceNotice key={keyOf(source)} source={source} onAction={connect.start} />
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
```

The notices render twice in the DOM and CSS hides one — the same trade the
shell makes with the rail and top bar. Both are `display: none` at the width
where the other is shown, so neither is announced twice.

- [ ] **Step 4: Verify**

```bash
cd apps/web && npm run typecheck && npm run lint && npm test && npm run build
```

Expected: all clean. `SourceStatusChip.test.tsx` unmodified and green.

- [ ] **Step 5: Look at it**

Sign in as `console@example.test`, run a search. At 1440px the source panel
should be beside the results with the chip row hidden. At 375px the chip row is
back and the panel is gone. Disconnect Slack from Accounts and re-run to check
a failed source reads red in both.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/components/SourcePanel.tsx apps/web/src/routes/SearchPage.tsx apps/web/src/styles/screens.css
git commit -m "feat(search): per-source status as a standing panel, not a chip row that scrolls away"
```

---

## Task 8: History as one panel

**Files:**
- Modify: `apps/web/src/styles/screens.css` (append)
- Modify: `apps/web/src/routes/HistoryPage.tsx:86-171`

- [ ] **Step 1: Append the history styles**

Append to `apps/web/src/styles/screens.css`:

```css
/* ---------------------------------------------------------------- history */

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.segmented {
  display: inline-flex;
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--line);
  border-radius: var(--r);
  background: var(--surface-2);
}

.segment {
  min-height: var(--h-sm);
  padding: 0 14px;
  border: 0;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--muted);
  font-size: 13px;
}

.segment:not(:disabled):hover {
  background: transparent;
  color: var(--ink);
}

.segment-on {
  background: var(--surface);
  color: var(--ink);
  font-weight: 650;
  box-shadow: var(--shadow-1);
}

.switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: var(--h-sm);
  font-size: 12.5px;
  color: var(--muted);
  cursor: pointer;
}

.switch input {
  width: 18px;
  height: 18px;
  min-height: 0;
  padding: 0;
  accent-color: var(--accent);
}

.row-top {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.row-time {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--faint);
}

.row-title {
  font-weight: 640;
  overflow-wrap: anywhere;
}

.row-sub {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 12.5px;
  color: var(--muted);
}

.row-meta {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.row-actions {
  display: flex;
  justify-content: flex-end;
  padding: 0 10px 10px;
}
```

- [ ] **Step 2: Turn both lists into panels**

In `apps/web/src/routes/HistoryPage.tsx`, in `SendList`, replace the `<ul>`:

```tsx
      <ul className="panel panel-list">
        {rows.map((send) => (
          <li key={send.send_id} className="panel-item">
            <Link className="panel-link" to={`/history/sends/${send.send_id}`}>
              <span className="row-top">
                <StateBadge state={send.state} />
                <SeedBadge isSeed={send.is_seed} />
                <span className="row-time">{relativeTime(send.created_at)}</span>
              </span>
              <span className="row-title">{send.recipient_display}</span>
              <span className="row-sub">{send.subject || send.body || ""}</span>
              <span className="row-meta">
                <RetryCountdown send={send} />
                {send.state !== "in_flight" && send.attempts > 1 ? (
                  <span className="countdown">{send.attempts} attempts</span>
                ) : null}
              </span>
            </Link>
          </li>
        ))}
      </ul>
```

And in `SearchList`:

```tsx
      <ul className="panel panel-list">
        {rows.map((search) => (
          <li key={search.search_id} className="panel-item">
            <Link className="panel-link" to={`/search/${search.search_id}`}>
              <span className="row-top">
                <SeedBadge isSeed={search.is_seed} />
                <span className="row-time">{relativeTime(search.created_at)}</span>
              </span>
              <span className="row-title">{search.query}</span>
              <span className="row-sub">
                {search.result_count} result{search.result_count === 1 ? "" : "s"} ·{" "}
                {search.finished ? "finished" : "still running"}
              </span>
            </Link>
            <div className="row-actions">
              <button
                type="button"
                className="button button-quiet button-sm"
                disabled={rerun.isPending}
                onClick={() =>
                  rerun.mutate(search.search_id, {
                    onSuccess: (created) => navigate(`/search/${created.search_id}`),
                  })
                }
              >
                Run it again
              </button>
            </div>
          </li>
        ))}
      </ul>
```

- [ ] **Step 3: Verify**

```bash
cd apps/web && npm run typecheck && npm run lint && npm run build
```

Expected: clean.

- [ ] **Step 4: Look at it**

Signed in as `seed@example.test`, open History. Both tabs should be one bordered
list with hairline dividers, not a stack of shadowed cards. Check the
`uncertain` row still reads amber.

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/routes/HistoryPage.tsx apps/web/src/styles/screens.css
git commit -m "feat(history): one list with hairlines instead of a stack of cards"
```

---

## Task 9: Connections as panels

**Files:**
- Modify: `apps/web/src/styles/screens.css` (append)
- Modify: `apps/web/src/routes/ConnectionsPage.tsx:89-140, 197-279`

- [ ] **Step 1: Append the connection styles**

Append to `apps/web/src/styles/screens.css`:

```css
/* ------------------------------------------------------------ connections */

.connection {
  display: grid;
  gap: 8px;
  padding: 14px;
}

.connection-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.connection-name {
  flex: 1;
  min-width: 0;
  font-size: 14.5px;
  font-weight: 650;
  overflow-wrap: anywhere;
}

.connection-meta {
  font-size: 12px;
  color: var(--muted);
}

.connection-detail {
  font-size: 12.5px;
  color: var(--muted);
  overflow-wrap: anywhere;
}

.connection-notice {
  margin-top: 2px;
}

.connection-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
  margin-top: 2px;
}
```

- [ ] **Step 2: Swap the two `rows-cards` lists for panels**

In `apps/web/src/routes/ConnectionsPage.tsx`, change the connected list
(line 89) from:

```tsx
      <ul className="rows rows-cards">
```

to:

```tsx
      <ul className="panel panel-list">
```

Change the "Add a connection" list (line 101) the same way, and change its
`<li>` (line 105) from `className="card connection"` to
`className="panel-item connection"`.

In `ConnectionRow`, change the `<li>` (line 198) from
`className="card connection"` to `className="panel-item connection"`.

- [ ] **Step 3: Verify**

```bash
cd apps/web && npm run typecheck && npm run lint && npm run build
```

Expected: clean.

- [ ] **Step 4: Look at it**

Open Accounts as `console@example.test`. Both lists should read as panels.
Check a `needs_reconnect` row shows its red badge and a primary Reconnect,
and a healthy row shows a secondary Re-authorize — primary only when something
is actually broken.

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/routes/ConnectionsPage.tsx apps/web/src/styles/screens.css
git commit -m "feat(connections): rows in panels, actions right-aligned"
```

---

## Task 10: Send detail

**Files:**
- Modify: `apps/web/src/styles/screens.css` (append)
- Modify: `apps/web/src/routes/SendDetailPage.tsx:63-84, 122-136, 173-235`

- [ ] **Step 1: Append the detail styles**

Append to `apps/web/src/styles/screens.css`:

```css
/* ------------------------------------------------------------ send detail */

.detail-head {
  gap: 4px;
}

.detail-state {
  display: flex;
  align-items: center;
  gap: 8px;
}

.detail-recipient {
  font-size: 22px;
  overflow-wrap: anywhere;
}

.detail-subject {
  font-size: 13.5px;
  font-weight: 600;
}

.facts {
  margin: 0;
}

.fact {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
  font-size: 12.5px;
}

.fact:last-child {
  border-bottom: 0;
}

.fact dt {
  color: var(--muted);
}

.fact dd {
  min-width: 0;
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
}

.detail-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

/* Amber, not red. This is the one state whose copy has to do real work. */
.doubt {
  border-color: var(--warn-line);
}

.doubt .panel-head {
  background: var(--warn-weak);
  color: var(--warn);
}

.doubt-body {
  display: grid;
  gap: 10px;
  padding: 14px;
}

.doubt-lede {
  font-size: 13.5px;
  line-height: 1.55;
}

.doubt-reason,
.doubt-help {
  font-size: 12.5px;
  color: var(--muted);
  overflow-wrap: anywhere;
}

.doubt-verify {
  width: 100%;
  border-color: var(--warn-line);
  color: var(--warn);
}

.doubt-actions {
  display: grid;
  gap: 12px;
}

.doubt-choice {
  display: grid;
  gap: 4px;
}

.doubt-choice .button {
  width: 100%;
}

@media (min-width: 40rem) {
  .doubt-actions {
    grid-auto-flow: column;
    grid-auto-columns: 1fr;
    align-items: start;
  }
}
```

- [ ] **Step 2: Wrap the facts in a panel**

In `apps/web/src/routes/SendDetailPage.tsx`, replace the `<dl className="facts">`
opening (line 63) and its close (line 84) with:

```tsx
      <div className="panel">
        <p className="panel-head">Delivery</p>
        <dl className="facts">
          <div className="fact">
            <dt>Attempts</dt>
            <dd>
              {detail.attempts} of {detail.max_attempts ?? 6} <RetryCountdown send={detail} />
            </dd>
          </div>
          <div className="fact">
            <dt>Dispatched</dt>
            <dd>{absoluteTime(detail.dispatched_at)}</dd>
          </div>
          <div className="fact">
            <dt>Delivered</dt>
            <dd>{absoluteTime(detail.delivered_at)}</dd>
          </div>
          <div className="fact">
            <dt>Provider message id</dt>
            <dd>
              <code>{detail.provider_message_id ?? "—"}</code>
            </dd>
          </div>
        </dl>
      </div>
```

- [ ] **Step 3: Turn the uncertainty block into a warn panel**

In `UncertaintyPanel`, replace the returned `<div className="doubt">` wrapper
(lines 173–238) with:

```tsx
    <div className="panel doubt">
      <p className="panel-head">We do not know whether this arrived</p>
      <div className="doubt-body">
        <p className="doubt-lede">
          It was dispatched at {absoluteTime(uncertainty.dispatched_at)} and we asked the
          provider {uncertainty.reconcile_attempts} times without getting a usable answer.
          This is not a failure — a failure would mean we know nothing was sent.
        </p>
        {uncertainty.reason ? <p className="doubt-reason">{uncertainty.reason}</p> : null}

        {/* Its own control rather than a link buried in a sentence: it is the
            action that actually settles this, and an inline link is a 15px-tall
            target on a phone. */}
        <a
          className="button doubt-verify"
          href={uncertainty.verify_url}
          target="_blank"
          rel="noreferrer"
        >
          Look for yourself at the provider
        </a>
        <p className="doubt-help">
          It takes about three seconds, and you can answer this in a way we cannot.
        </p>

        <label className="field">
          <span className="field-label">Note (optional)</span>
          <input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="what you saw"
          />
        </label>

        <div className="doubt-actions">
          {uncertainty.resolutions.map((resolution) => {
            const copy = COPY[resolution] ?? { label: resolution, help: "" };
            const armed = chosen === resolution;
            return (
              <div key={resolution} className="doubt-choice">
                <button
                  type="button"
                  className={armed ? "button button-primary" : "button"}
                  disabled={resolve.isPending}
                  onClick={() => {
                    if (!armed) {
                      setChosen(resolution);
                      return;
                    }
                    resolve.mutate({ sendId, resolution, note });
                  }}
                >
                  {armed
                    ? resolve.isPending
                      ? "Recording…"
                      : "Tap again to confirm"
                    : copy.label}
                </button>
                <p className="doubt-help">{copy.help}</p>
              </div>
            );
          })}
        </div>

        {resolve.isError ? <p className="bad">{String(resolve.error)}</p> : null}
      </div>
    </div>
```

The `<h3 className="doubt-title">` is replaced by the panel head, so delete it.

- [ ] **Step 4: Verify**

```bash
cd apps/web && npm run typecheck && npm run lint && npm run build
```

Expected: clean.

- [ ] **Step 5: Look at it**

As `seed@example.test`, open History → the `uncertain` send. Check the panel is
amber, the two resolutions sit side by side at 1440px and stack at 375px, and
the two-tap arm-then-confirm still works. Then open a `failed` send and check
the untruncated provider error still renders.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/routes/SendDetailPage.tsx apps/web/src/styles/screens.css
git commit -m "feat(detail): facts and the uncertain state as panels"
```

---

## Task 11: Compose and sign-in

**Files:**
- Modify: `apps/web/src/styles/screens.css` (append)
- Modify: `apps/web/src/routes/ComposePage.tsx:133-135`
- Modify: `apps/web/src/routes/SignInPage.tsx:42, 72-97`

- [ ] **Step 1: Append the styles**

Append to `apps/web/src/styles/screens.css`:

```css
/* -------------------------------------------------------- compose, signin */

.compose {
  display: grid;
  gap: 12px;
  max-width: 44rem;
}

.signin {
  max-width: 26rem;
  margin: 0 auto;
  padding-top: 8vh;
}

.signin-card {
  display: grid;
  gap: 14px;
  padding: 20px;
}

.signin-brand {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 2px;
}

.signin .compose {
  max-width: none;
}

.signin .compose .button {
  width: 100%;
}
```

- [ ] **Step 2: Put Compose's submit in an actions row**

In `apps/web/src/routes/ComposePage.tsx`, replace the submit button (lines
133–135) with:

```tsx
        <div className="actions actions-end">
          <button type="submit" className="button button-primary" disabled={createDraft.isPending}>
            {createDraft.isPending ? "Preparing…" : "Review before sending"}
          </button>
        </div>
```

- [ ] **Step 3: Give sign-in a card**

In `apps/web/src/routes/SignInPage.tsx`, change the section (line 42) from:

```tsx
    <section className="page signin">
```

to:

```tsx
    <section className="page signin">
      <div className="card signin-card">
        <div className="signin-brand">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">Unified Search &amp; Safe Send</span>
        </div>
```

and close the two new `<div>`s immediately before the closing `</section>` at
the end of the component, after the error notice block:

```tsx
      </div>
    </section>
```

The two new `<div>`s must wrap **everything** from the `<header>` through the
error notice — the `<form>`, the `<details>` and the error block all sit inside
the card. The buttons themselves need no change: `.signin .compose .button`
makes all three full-width, and the primary is already marked.

- [ ] **Step 4: Verify**

```bash
cd apps/web && npm run typecheck && npm run lint && npm run build
```

Expected: clean. If typecheck complains about unbalanced JSX, the closing tags
in Step 3 are in the wrong place — the two new `<div>`s must wrap everything
from the header through the error notice.

- [ ] **Step 5: Look at it**

Sign out and check the sign-in screen at 375px and 1440px: a single centred
card, three full-width buttons, one of them primary.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/routes/ComposePage.tsx apps/web/src/routes/SignInPage.tsx apps/web/src/styles/screens.css
git commit -m "feat(ui): compose actions on a row, sign-in on a card"
```

---

## Task 12: Full verification

**Files:** none modified unless a check fails.

- [ ] **Step 1: The whole suite**

```bash
cd apps/web && npm run typecheck && npm run lint && npm test && npm run build
```

Expected: all clean. `SourceStatusChip.test.tsx` **unmodified** and green — if
it needed editing, the four source states have collapsed and the work is wrong.

- [ ] **Step 2: The boundary still holds**

```bash
cd /Users/aneesbajwa/www/unified-search-safe-send && uv run pytest tests/test_web_boundary.py -q
```

Expected: passed. No business rule entered the console.

- [ ] **Step 3: Confirm no stylesheet was left behind**

```bash
cd /Users/aneesbajwa/www/unified-search-safe-send
test ! -f apps/web/src/App.css && echo "App.css gone"
grep -rn "prefers-color-scheme" apps/web/src || echo "no dark scheme"
grep -rn "backdrop-filter" apps/web/src || echo "no backdrop blur"
```

Expected: all three confirmations print. `grep` printing nothing but the
fallback message is the pass condition.

- [ ] **Step 4: Screenshot every screen at both widths**

At **375px** and **1440px**, capture: sign-in; search empty; search with
partial results; search finished with a failed source; compose; the gate
unsettled; the gate showing a refusal; the gate settled; history sends;
history searches; send detail delivered; send detail failed; send detail
uncertain; connections with an account; connections needing reconnect.

Save under `docs/images/after/` and compare against `docs/images/before/`.

- [ ] **Step 5: Check the two defects by eye**

- At 1440px the gate's Cancel and Send are the **same height and shape**.
- At 375px Send is on top, Cancel at the bottom, with a clear gap between them.
- The settled footer fills the sheet's width with **no dead canvas** to its left.

- [ ] **Step 6: No horizontal scroll at 375px**

Rotate to landscape on every screen. `document.documentElement.scrollWidth`
must equal `clientWidth` throughout.

- [ ] **Step 7: Commit the after-shots**

```bash
git add docs/images/after
git commit -m "docs(ui): the console after the redesign"
```

---

## Self-review notes

**Spec coverage.** §3 → Task 2 (`color-scheme: light`, no dark block, asserted
by test). §4 → Task 2 (tokens) and Task 3 (base). §5 → Task 4. §6 → Task 5
(shell) and Task 7 (source panel). §7 defect 1 → Task 6 Step 3/5; defect 2 →
Task 6 Step 3; defect 3 → Tasks 2–3. §8 → Tasks 7–11, one screen per task. §9
→ Task 3. §10 → Task 12. §11 R1 is honoured by ordering the gate after the
token and component layers are proven on lower-stakes screens.

**Naming.** `--h-sm/--h-md/--h-lg` are used consistently in `components.css`,
`shell.css` and `gate.css`. `.button-sm` / `.button-lg` are the only size
classes and `md` is the unmarked default — which is why
`ConfirmDialog.test.tsx` falls back to `"md"` when neither matches.
`.panel` / `.panel-list` / `.panel-item` / `.panel-link` / `.panel-row` /
`.panel-head` are used with the same meaning in Tasks 7, 8, 9 and 10.

**Known intentional duplication.** The notices render in both columns on
Search (Task 7) and the nav renders in both the rail and the tab bar (Task 5).
In each case exactly one is `display: none`, so neither is announced twice.
