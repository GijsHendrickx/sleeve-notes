# Design system

Version 0.1. Read this before touching UI. The tokens live in `webapp/templates/base.html` (`:root`); this file says what they mean and how to use them. `design/design-system.html` is the visual reference (open it in a browser, components are live). `design/type-candidates.html` records the typeface comparison that led to the current choice.

The product name is not final. Never hardcode it: use `{{ APP_NAME }}`.

## The idea

A piece of audio hardware, not a SaaS dashboard. Light grey is the housing, near-black is the display, and **one orange LED** is the only chroma. References: Technics/Pioneer faceplates for colour and numerals, modern Apple-style rounded corners and calm spacing for everything else. Deliberately not skeuomorphic: no metal, screws, wood, glow or photos of a turntable.

The sticker preview is the only element that represents the printed object. It keeps its own paper colour (`--paper`) and sits on a dark slipmat (`.stage`).

## Colour

| Token | Value | Use |
|---|---|---|
| `--g0` | #F3F4F4 | Inputs, hover surface (`--s2`) |
| `--g1` | #E6E8E9 | Panels, cards (`--s1`) |
| `--g2` | #D5D8DA | Page background (`--app-bg`), recessed wells |
| `--g3` | #BFC3C6 | Tracks and bars (`--s3`), light text on dark |
| `--g4` | #8E9498 | Decorative only, dim text on dark |
| `--g5` | #4E5457 | Secondary text (`--dim`) |
| `--g6` | #2A2E30 | Raised dark, slipmat |
| `--g7` | #17191A | Ink (`--ink`), display, primary button (`--accent`) |
| `--led` | #FF5A14 | The LED |
| `--good` / `--warn` / `--bad` | #246338 / #745600 / #A8322A | Status. Warn is ochre on purpose, never orange |

Contrast: ink on housing 12.3, `--dim` on housing 5.4, LED on `--g7` 5.7, LED on light grey only 2.2–2.5.

### The LED rule

Orange means **active or look here**: the current flow step, the primary button, running progress, the "now" row, a just-synced cell. Never decoration.

- Orange is a fill only. **Never orange text on a light surface.** On `--g7` it may be text (the BPM readout).
- On light grey the LED always sits in a dark bezel (`.led` does this with a 2px `--g7` ring). Orange alone fails 3:1 contrast.
- Orange is never the only signal. Pair it with text or shape.
- One primary button per screen. `.btn-primary` carries the LED automatically.

## Type

- **Schibsted Grotesk** for headings and all interface text. Headings 700–800, tracking −0.02 to −0.04em, line-height ~1.0–1.1.
- **Red Hat Mono** for anything numeric or positional: BPM, key, track position, duration, the readout, the sticker. Always `tabular-nums`.
- Min size 12px. No italics on song or release titles. Full column headers; BPM is the only abbreviation.
- Chosen over Inter/Geist-style defaults on purpose: the owner found those read as AI-generated.

## Shape and surface

Radii: `--r-sm` 6 (chips, pills, small inputs) · `--r-md` 10 (buttons, fields, presets) · `--r-lg` 14 (cards, panels, modals, toast) · `--r-xl` 22 (landing hero). Bigger object, rounder corners. No fully round pills.

Surfaces: **panel** (`.card`: `--g1`, hairline ring, 1px white top edge), **well** (inset, e.g. `.seg`, fields), **display** (`--g7`: readout, toast, hero, slipmat). Dark surfaces re-point `--ink/--dim/--mute` locally (see `.run-toast`, `.stage`) so templates keep using `text-app-*` and need no changes.

## Motifs (vinyl / deck vocabulary)

- **LED** (`.led`): described above.
- **Grooves**: faint concentric rings, only on dark surfaces, never behind text that must be read. Used in the landing hero.
- **Strobe dots**: a ring of small dots with one lit orange, travelling. For short waits with no measurable progress (per-row sync). Not built into the app yet.
- **Pitch fader**: a ruled-track range input with a dark cap and an orange centre line. For choosing a value within a range. Not built into the app yet.
- **33/45-style segmented control** (`.seg`): one option always on.
- **Tonearm**: tried and dropped. Do not add it to buttons, progress or tables. If it returns it is brand/background only, drawn as a straight arm with a hard L-bend at the headshell.

## Do / don't

- Do keep the LED rare. If three things on a screen are orange, two are wrong.
- Do use existing helpers: `.card`, `.btn-primary/-ghost/-quiet`, `.field`, `.pill`, `.seg`, `.t-good/.t-warn/.t-bad`, `.link`, `.row-hover`, `.row-selected`.
- Don't use raw Tailwind palette colours, glow, gradients, glass, or text-based icons.
- Don't set a second accent hue for warnings or highlights.
- Don't reintroduce the old teal (`#1F5A63`). `--accent` is now ink; the colour is in the LED.

## Status

Done: tokens, typography, `.card/.btn/.field/.pill/.seg/.rail/.stage/.run-toast/.start` restyle, landing page.
To do, in this order: Home, flow step 1 (Records), step 2 (BPM readout in mono), step 3 (Design), step 4 (Get them), `/collection`, `/tracks`, modals. Add the strobe and the pitch fader where a real need appears (per-row sync; sticker size).
