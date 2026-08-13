# Console redesign — "Instrument"

**Date:** 2026-08-12
**Scope:** `apps/web` only. No API, worker, schema or core change.
**Status:** approved design, ready for an implementation plan.

---

## 1. Why

The console works and its structure is sound, but it reads as generated rather
than built. Three causes, and only one of them is colour.

**Periwinkle on near-black.** The dark palette's accent is `#7d9cff` on
`#0f1216`. That pairing is the most recognisable signature of a generated app
there is, and it is the palette every reviewer on a dark-mode machine sees —
including the author, who has therefore never looked at the light palette the
CSS already ships.

**Phone sizing applied to desktop.** `--tap: 44px` is a *touch* floor and the
right one, but today it is the floor on every device. A 1500px browser gets a
54px primary button, a 48px secondary, and 44px disclosure triangles. This, not
the hue, is why the confirm dialog's Send button looked oversized.

**Everything is a card.** History rows, connection rows and provider rows are
each an individually bordered, individually shadowed card in a vertical stack.
Three separate lists, three separate treatments, none of them dense.

On top of that there are two real layout defects, described in §7.

## 2. What is not changing

Named explicitly, because the temptation in a redesign is to churn things that
are already right.

- **The gate's structure.** Destination pinned and never scrolling away, the
  body as the only scrolling region, actions fixed at the bottom, Escape
  cancels and never sends, confirm disables on press and shows the returned
  send state rather than a spinner. All correct; all kept.
- **The writing voice.** "Recorded. It is now yours to follow." /
  "A failure would mean we know nothing was sent." The copy is the most human
  thing in the app. No microcopy rewrites.
- **Routes, data flow, hooks, API client.** Untouched.
- **Mobile-first as the base case.** 375px remains the default and every
  desktop rule is added in a media query. Nothing is authored desktop-first and
  walked back.
- **The four distinct source states.** `done` / `empty` / `failed` / `retrying`
  must never collapse into each other, and `uncertain` stays amber rather than
  red. `apps/web/src/components/SourceStatusChip.test.tsx:107` asserts four
  distinct `className` values; the redesign preserves that and the test stays
  green unmodified.
- **The console holds no business rules.** `tests/test_web_boundary.py` greps
  this tree for locally-derived decisions. This work is pure presentation and
  introduces none.

## 3. Colour scheme: light only

`color-scheme: light` is pinned on `:root` and the entire
`@media (prefers-color-scheme: dark)` block is **deleted**, not left in place.

Rationale: one palette can be tuned properly and verified for contrast; two
cannot, on this budget. Pinning also means the app renders identically on a
dark-mode Mac and a light-mode reviewer's machine, which removes the failure
mode where the author never sees what is shipped. The existing comment in
`index.css` about both schemes being first-class is removed along with the
block it justifies — a stale rationale is worse than none.

## 4. Tokens

All of these live in `src/styles/tokens.css`.

### Surfaces and text

| Token | Value | Use |
|---|---|---|
| `--canvas` | `#F6F7F8` | page background behind panels |
| `--surface` | `#FFFFFF` | cards, panels, bars, inputs |
| `--surface-2` | `#F1F3F5` | inset wells, panel header strips, segmented track, code |
| `--surface-3` | `#E8EBEE` | hover/pressed on quiet controls |
| `--ink` | `#15181C` | primary text — 17.8:1 on white |
| `--ink-2` | `#3C434E` | secondary body copy |
| `--muted` | `#5B6472` | labels, meta — 5.98:1 |
| `--faint` | `#676F7D` | timestamps, least-important meta — 5.07:1 |

Note `--faint` is deliberately darker than today's `#7d8694`, which fails AA.
There is no text colour in this system that fails AA at any size.

### Lines

Two tokens, and the distinction is load-bearing.

| Token | Value | Use | Contrast rule |
|---|---|---|---|
| `--line` | `#E4E7EB` | dividers between rows, card edges | decorative — the boundary is not the only affordance, so 3:1 does not apply |
| `--line-2` | `#8A939E` | input borders, secondary-button borders, segmented track border | 3.11:1 on white — meets WCAG 1.4.11, because here the border **is** the only thing identifying the control |

`--line-2` is noticeably darker than the shipped `#c3c9d3` (1.9:1). This is a
deliberate trade: a slightly heavier input outline in exchange for controls
that are identifiable to a low-vision user. It is also the kind of thing that
is visible to a reviewer who checks.

### Action and state

| Token | Value | Contrast on white |
|---|---|---|
| `--primary` / `--primary-hover` / `--primary-fg` | `#15181C` / `#000000` / `#FFFFFF` | 17.8:1 |
| `--accent` / `--accent-weak` / `--accent-line` | `#0B5CD5` / `#EAF1FD` / `#B9D0F5` | 5.99:1 |
| `--ok` / `--ok-weak` / `--ok-line` | `#0E6B3F` / `#F1F9F4` / `#BFE0CD` | 6.58:1 |
| `--warn` / `--warn-weak` / `--warn-line` | `#8A5A0B` / `#FDF7EA` / `#EFD7A8` | 5.92:1 |
| `--bad` / `--bad-weak` / `--bad-line` | `#A4192C` / `#FDF3F4` / `#F0C6CB` | 7.63:1 (7.01:1 on its own weak tint) |

`--primary` is ink, not a hue. The one irreversible action on any screen is the
darkest thing on it, and saturation is spent exclusively on state — so on the
Search page a failed source is the loudest element, which is exactly the
priority this product claims to have.

`--accent` is reserved for links, the focus ring, text selection, and the
`in_flight` state. It is never a button fill.

### Type

System stack: `system-ui, -apple-system, "Segoe UI", sans-serif`. No webfont —
no network request, no FOUT, and SF/Segoe at these sizes need no help. Mono
stays `ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace`.

| Role | Size / line-height / weight / tracking |
|---|---|
| gate destination | 26px / 1.15 / 700 / −0.02em |
| page title (`h2`) | 20px → 22px ≥40rem / 1.25 / 640 / −0.015em |
| section head | 11px / 1.3 / 700 / +0.08em, uppercase, `--faint` |
| body-lg (message body, sign-in lede) | 15px / 1.55 / 400 |
| body | 14px / 1.55 / 400 |
| meta | 12.5px / 1.45 / 400 |
| micro (chips, badges, timestamps) | 11.5px / 1.35 / 600 |
| input | **16px below 40rem**, 14px above |

The 16px input floor below 40rem is not cosmetic: below it, iOS Safari zooms
the page on focus and the layout jumps. It stays.

### Space, radius, elevation, motion

Space on a 4px base: `4 8 12 16 20 24 32 40 56`.

Radii tighten from today's 6/10/14 to `--r-sm 6`, `--r 8`, `--r-lg 10`,
`--r-xl 14`, `--pill 999`.

Elevation is two steps and no more:

```
--shadow-1: 0 1px 2px rgb(21 24 28 / 6%);                       resting surfaces
--shadow-2: 0 1px 2px rgb(21 24 28 / 6%),
            0 12px 32px rgb(21 24 28 / 12%);                    the gate sheet only
```

Depth comes from hairlines, not blur. The current `backdrop-filter` blur on the
top bar and tab bar is removed — it is a phone-OS idiom that reads as
decoration on a desktop console, and it costs a compositing layer.

Motion: 120ms ease on background/border transitions, 160ms on the gate's
entrance. The existing `prefers-reduced-motion` block is kept verbatim.

### Touch targets

`--tap: 44px` is retained and remains the floor **on touch**. From `40rem` up,
controls drop to pointer-appropriate sizes (§5). This is expressed as a
min-width media query so the mobile case is still the un-qualified default.

### Breakpoints

Exactly two, and they do different jobs. Both are `min-width`; nothing in this
system is authored desktop-first.

| Breakpoint | What changes |
|---|---|
| `40rem` (640px) | **Layout only** — the gate footer becomes a right-aligned row. |
| `40rem` **and** `pointer: fine` | **Control density.** Buttons and inputs shed 8px and inputs go 16px → 14px. Pointer-gated, so a touch tablet keeps both the 44px floor and the 16px anti-zoom input size. |
| `64rem` (1024px) | **Shell.** Top bar and tab bar give way to the left rail; the Search page gains its source panel. |

A control-density change earlier than the shell change is deliberate: a 700px
tablet window wants pointer-sized controls but not a nav rail.

## 5. Components

### Buttons

Two orthogonal axes, replacing today's single `.button` with ad-hoc overrides.

**Size** — authored mobile-first, so the touch sizes are the un-qualified
default and the density block subtracts from them:

| | base (touch) | `≥40rem` **and** `pointer: fine` |
|---|---|---|
| `sm` | 44px | 30px |
| `md` (default) | 44px | 36px |
| `lg` | 52px | 44px |

**Every base size is at or above `--tap`, including `sm`.** An earlier draft had
`sm` at 38px, which contradicted the touch-target rule below and would have put
the History tabs, the seed filter and Sign out under the floor on a phone. Small
means small on a pointer, not on a thumb.

The density block is gated on `pointer: fine` as well as width. Width alone is a
proxy for input device, and it is wrong on precisely the devices the floor
exists for: an iPad in portrait is 768px and touch-only, and a width-only rule
handed it 30px tab targets.

**Variant** — `primary` (ink fill, white text), `secondary` (white fill,
`--line-2` border, `--surface-2` on hover), `quiet` (transparent, accent text,
no border), `danger` (`--bad-weak` fill, `--bad` text, `--bad-line` border).

Two rules the current CSS has no way to express:

1. **One primary per view.** Enforced by review, not by code.
2. **Paired actions share a size.** Enforced by an `.actions` cluster class:
   `display: flex; gap: 12px; align-items: stretch`, with `min-height` set on
   the cluster rather than on each button, so siblings cannot disagree about
   height. Defect 1 (§7) cannot recur inside `.actions`.

### Inputs

36px on pointer / 44px on touch, `--line-2` border, `--r` radius, `--surface`
fill. Focus is `outline: 2px solid var(--accent); outline-offset: 2px` — the
existing `:focus-visible` rule, kept.

`select` gets an explicit appearance reset and a chevron so it stops rendering
as a platform control among custom ones.

### Panel

New primitive. A `--surface` container with `--line` border, `--r-lg` radius,
`--shadow-1`, an optional `--surface-2` header strip in section-head type, and
hairline-divided rows.

This one primitive replaces four ad-hoc treatments: the send-detail `.facts`
list, the history `.rows-cards` stack, the connections `.rows-cards` stack, and
the new Sources panel. Consolidating them is most of the density win.

### Chips and badges

Pill, `micro` type, `--surface` + `--line` when neutral, `<state>-weak` fill +
`<state>-line` border + `<state>` text when carrying a state. Chips keep four
visually distinct terminal states.

### Notices

Restructured from a vertical stack into a row: a 7px state dot, then title and
detail in a column, then the action button pushed right with `margin-left:
auto`. Today the action is a full-width block under the text, which is why
"Reconnect" reads as heavier than the problem it solves.

Below 40rem the action wraps to its own full-width row.

### Empty state

Dashed `--line-2` border, centred, title in 15px/640, body in 14px `--muted`,
max 34rem measure. Structurally as today, restyled.

### Navigation icons

Four inline SVGs written by hand — search, compose, history, accounts — no
icon-library dependency. They matter most in the mobile tab bar, which is
text-only today and is the least finished surface in the app. Each is a 20px
`currentColor` stroke path, `aria-hidden`, with the existing text label kept
beneath.

### Brand mark

The `linear-gradient(135deg, accent, mix(accent, good))` square is replaced
with a solid `--ink` rounded square. A gradient logo mark is the second most
recognisable generated-app signature after the periwinkle.

## 6. Shell

### Below 64rem — unchanged behaviour, restyled

Top bar (brand, identity, sign out), single column, bottom tab bar with icons.
`.app[data-gate="open"]` still hides both — while the gate is open there is
nowhere else to be. Content measure and 16px gutters as today.

### From 64rem — rail and canvas

A 214px sticky left rail: brand at the top, the four destinations as rows with
icon + label (`--surface-2` fill and a 2px inset ink bar when active), then
identity and sign out pushed to the foot with `margin-top: auto`. Canvas is
`--canvas` at 24px padding, content measure 58rem.

The top bar and bottom tab bar are both `display: none` at this width.

### Search page from 64rem — the source panel

Two columns: results at `1fr`, and a 250px right column holding the **Sources
panel** (one row per source: name, and status in the state colour) plus any
source notices.

The justification is specific rather than aesthetic. "Which source failed" is
the one thing this product must never let a customer miss — a silently failed
source makes an incomplete answer look complete. In a single column that
information is a chip row that leaves the viewport as soon as you scroll the
results. As a standing panel it does not.

Below 64rem the panel collapses back to the existing chip row plus inline
notices, unchanged.

## 7. The two defects

### Defect 1 — the gate's actions are different sizes

`.gate-confirm` is `min-height: 54px; min-width: 11rem`; `.button-cancel` is
`min-height: 48px; min-width: 8rem`. At `≥40rem` they sit on one row at two
different sizes.

**Fix.** `.gate-actions` becomes `display: flex; gap: 12px; justify-content:
flex-end`. Both buttons are `lg`, equal height, with matching `min-width`.
Cancel is `secondary`, Send is `primary`. The `.gate-gap` spacer div is deleted
on desktop — `gap` does that job.

**Mobile is preserved exactly.** Below 40rem the footer stays a grid: Send
full-width on top, a 24px gap, Cancel full-width at the bottom where the thumb
rests. The gap is the anti-mistap defence and is not negotiable.

### Defect 2 — the settled footer collapses into a right-pinned column

`.gate-settled` is `display: grid` and sits inside `.gate-actions`, which at
`≥40rem` is `grid-auto-flow: column; justify-content: flex-end`. The settled
block therefore becomes a single narrow grid column pinned to the right, with
its children stacked and dead canvas beside them. The `grid-auto-flow: row`
rule intended to fix this operates on the wrong axis.

**Fix — structural, not a CSS patch.** The settled block leaves
`.gate-actions` entirely. `ConfirmDialog` renders it as its own region between
`.gate-scroll` and the footer:

- a full-width row with the state badge, the short send id, and the note,
  left-aligned;
- then a normal `.gate-actions` footer with Done (`secondary`) and Open the
  send (`primary`), same sizes, same right alignment as the unsettled case.

Because the settled content is no longer a grid item inside a column-flow
parent, the failure cannot recur. This is the one JSX change the redesign
requires in `ConfirmDialog.tsx`; the component's logic is untouched.

### Defect 3 — the palette

Addressed by §3 and §4.

## 8. Per-screen

**Sign in.** Centred card at 26rem on canvas, brand above it, lede in body-lg.
Three sign-in paths as one `primary` and two `secondary`, full-width and
stacked. Key-paste stays in the disclosure.

**Search.** As §6. Search bar loses its sticky offset hack (`top: 56px`,
brittle against the bar's real height) and becomes a normal flow element on
desktop, sticky under the top bar only below 64rem.

**Compose.** Field order is correct and unchanged — it is deliberately the
plainest surface in the app and stays that way. 44rem measure, textarea
`min-height: 200px`, submit right-aligned in an `.actions` row rather than
full-width.

**History.** Toolbar keeps the segmented control and the "Only what I did"
switch. The row stack becomes one `panel`: hairline-divided rows carrying state
badge, recipient, subject, and relative time right-aligned. "Run it again"
becomes a `quiet` `sm` action on the row.

**Send detail.** `.facts` becomes a `panel` with a `--surface-2` header. The
body preview stays a `--surface-2` well, untruncated, as today. The
`uncertain` block becomes a warn `panel`: lede, reason, the "look for yourself
at the provider" link as a full-width `secondary`, then the resolutions as
equal-width buttons in an `.actions` row. The two-tap arm-then-confirm
behaviour is unchanged.

**Connections.** Connection rows become a `panel` — name, status chip, meta,
actions right-aligned. "Add a connection" becomes a second `panel` below it.
The scope-hint notice and the disconnect confirmation flow are unchanged.

## 9. File structure

`App.css` is 1253 lines doing six jobs. It is replaced by six focused files
under `src/styles/`, imported by `index.css`:

| File | Contents |
|---|---|
| `tokens.css` | colour, type, space, radius, elevation, motion |
| `base.css` | reset, element defaults, focus ring, reduced motion |
| `components.css` | buttons, inputs, card, panel, chip, badge, notice, empty |
| `shell.css` | top bar, rail, tab bar, canvas, page grid |
| `screens.css` | search, compose, history, send detail, connections |
| `gate.css` | the confirm gate |

The gate gets its own file because it is the highest-value component in the
app and should be editable without scrolling past history rows.

`index.css` is reduced to six `@import` lines and nothing else — its current
token and base-element contents move into `tokens.css` and `base.css`.
`App.css` is **deleted** and its import removed from `App.tsx`; none of its
rules are carried over verbatim. No rule from either file survives unreviewed.

## 10. Verification

Work is not done until all of the following have been run and their output
read:

1. `npm run typecheck` — clean.
2. `npm run lint` — clean.
3. `npm test` — green, including `SourceStatusChip.test.tsx` **unmodified**.
   If that test needs editing, the four states have collapsed and the change is
   wrong.
4. `uv run pytest tests/test_web_boundary.py` — green. No business rule has
   entered the console.
5. Every screen screenshotted at **375px** and **1440px**: sign-in, search
   (empty / partial / finished / a failed source), compose, the gate
   (unsettled, error, settled), history (both tabs), send detail (delivered,
   failed, uncertain), connections (connected, needs-reconnect, none
   configured).
6. Contrast spot-checked against the table in §4 for every pair actually
   shipped.
7. No horizontal scroll at 375px, in portrait or landscape.

## 11. Risks

**R1 — the gate regresses while being restyled.** It is the component the
product is judged on. Mitigation: `gate.css` is written last, after the token
and component layers are verified on lower-stakes screens, and the mobile
stacked-with-gap arrangement is screenshotted at 375px before and after.

**R2 — the rail eats width that results wanted.** Four destinations is a thin
rail. Mitigation: 214px is narrow by design and the canvas measure grows from
46rem to 58rem, so results are wider than today despite the rail.

**R3 — `--line-2` at `#8A939E` reads heavier than the mockup.** The mockup used
`#D5DAE0`, which fails 1.4.11. Accepted deliberately; if it looks wrong in
situ the fix is to give secondary controls a `--surface-2` fill *in addition
to* the border, never to lighten the border below 3:1.

**R4 — scope creep into copy.** The voice is a strength. No microcopy changes
without being asked.
