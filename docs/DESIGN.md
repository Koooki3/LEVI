# Design system

[中文](DESIGN.zh-CN.md)

LEVI's interface is being redesigned in stages: graphite accent, light and dark themes that follow the system, system fonts only, Lucide icons, Motion for drag and list reordering only. Stage 1 added the design tokens, the theme preference and a set of base components. Stage 2 (see [Global frame](#global-frame-stage-2)) moved the frame onto them: the top bar, toasts, the confirmation dialog, the command palette, the shortcut list and the Agent Workbench drawer. Stage 5's global layer (see [Brand](#brand) and [Global page base](#global-page-base-stage-5)) gave the site one mark, put the page base and every older page style on the tokens, and rebuilt the home page, the guide and the report. The episode viewer and the other pages move their own markup to `ds-*` components in their stages; until then they follow the themes through the older names mapped onto the tokens.

| What | Where |
| --- | --- |
| Tokens (CSS variables, `--ds-*`) | `src/styles/tokens.css` |
| Component styles (classes `ds-*`) | `src/styles/ds.css` |
| Components | `src/components/ds/` (`index.ts` exports all but `ReorderList`) |
| Theme preference | `src/lib/design/theme.ts` |
| Reduced-motion helpers, durations, breakpoints | `src/lib/design/motion.ts` |
| Specimen switch | `src/lib/design/gate.ts`, `src/middleware.ts` |
| Specimen page (development only) | `/design` (`src/app/design/`) |
| Global frame (stage 2) | `src/components/shell/`, `src/components/levi-header.tsx`, `src/styles/shell.css` |
| Mark (one definition) | `src/components/shell/brand.tsx`; tab icons drawn from it by `scripts/brand_icons.py` (`src/app/icon.svg`, `apple-icon.png`, `favicon.ico`) |
| Page base and older names (stage 5) | `src/app/globals.css`, `src/app/levi.css` |
| Home, guide, report (stage 5) | `src/components/home/`, `src/styles/home.css`; `src/app/guide/`, `src/styles/reading.css`; `src/components/report/`, `src/app/report/report.css` |
| Token values in code (canvas, WebGL) | `src/lib/design/css-tokens.ts` (`useCssTokens`) |
| Tests | `src/components/ds/__tests__/`, `src/lib/design/__tests__/`, `src/components/shell/__tests__/` |

## Rules for new code

- **No hard-coded colours.** Use a semantic token (`var(--ds-text-secondary)`, `var(--ds-surface-1)`) or a `ds-*` class. No hex, `rgb()` or `hsl()` in new CSS or TSX; a test fails if `ds.css`, `shell.css` or the specimen's CSS has one, and ESLint (`no-restricted-syntax`, list `FRAME_FILES` in `eslint.config.mjs`) rejects a hex colour in the frame's TSX (`#` and 3, 4, 6 or 8 hex digits at the start of a string or after a space, `(`, `,` or `:`; `href`, `to`, `id` and `htmlFor` values are links, not colours; `src/__tests__/eslint-hex.test.ts`). Older pages are cleaned up in stage 6. Data colours (time segments, masks, chart series) are a separate palette that stage 4 defines.
- **Semantic tokens only in components.** The raw steps `--ds-gray-l-*` / `--ds-gray-d-*` exist only to define the semantic tokens.
- **One primary button per screen.** Accent A ("graphite") is the darkest grey: it is used only for the primary button, the focus ring, selection and progress. Links in running text are primary text with an underline.
- **Status never by colour alone.** Status colours (success, warning, danger, info) appear only on badges, status dots, toasts and notes, always with an icon shape and words (`Badge`, `StatusDot`).
- **Smallest text 12 px** (`--ds-text-caption-size`); weights 400, 500, 600.
- **Icons**: Lucide only, through `Icon` (16 px with stroke 1.75; 20 or 24 px with 1.5). Decorative icons are `aria-hidden`; an icon that carries meaning gets a `label`.
- **No `title=` tooltips.** Use `Tooltip` (hover and keyboard focus); an icon-only button is an `IconButton`, which requires `label` (its `aria-label` and tooltip).
- **No `window.confirm`.** Ask through `useConfirmAction()` (`src/components/shell/confirm.tsx`; a test fails on any `window.confirm` in `src/`), and only for an irreversible action; for an expected, undoable deletion, delete and offer Undo in a toast.
- **Text** goes through the locale catalogs (`useLocale().t`, both `en.json` and `zh.json`). Component defaults (Close, Cancel, Loading, In progress, Dismiss notification, Notifications, Move, Theme, System, Light, Dark) are already there.

## Tokens

All tokens are CSS custom properties on `:root`, named `--ds-*`, so they never clash with the older `--bg`, `--accent` or `--surface-*` variables.

| Group | Tokens |
| --- | --- |
| Neutral scales | light `--ds-gray-l-0…11`, dark `--ds-gray-d-0…12` (cool neutral greys, proposal §4.1) |
| Surfaces | `--ds-bg`, `--ds-bg-reading`, `--ds-surface-sunken`, `--ds-surface-1` (card), `--ds-surface-2` (raised: menus, dialogs), `--ds-surface-hover`, `--ds-surface-hover-on-raised`, `--ds-surface-selected`, `--ds-field-bg`, `--ds-skeleton`, `--ds-media-bg` (black in both themes), `--ds-scrim` |
| Lines | `--ds-separator`, `--ds-separator-strong`, `--ds-border-control`, `--ds-border-control-on-raised` |
| Text | `--ds-text-primary`, `--ds-text-secondary`, `--ds-text-tertiary`, `--ds-text-tertiary-on-sunken`, `-on-hover`, `-on-selected`, `-on-raised`, `--ds-text-disabled`, `--ds-icon` |
| Accent A | `--ds-accent`, `--ds-accent-hover`, `--ds-on-accent`, `--ds-focus-ring`, `--ds-selected-indicator`, `--ds-progress`, `--ds-progress-track` |
| Status | `--ds-success`, `--ds-warning`, `--ds-danger`, `--ds-info`, each with `-bg`; `--ds-on-danger` |
| Data | `--ds-data-1…8` (blue, orange, aqua, yellow, magenta, green, violet, red; fixed order): series, time-segment and category colours, light and dark steps. Checked with the dataviz palette checks: neighbours stay apart for colour-vision deficiencies (ΔE ≥ 8.4) and normal vision (ΔE ≥ 19.3); every dark step is at least 3:1 on the dark page and card (tested). Light aqua, yellow and magenta are under 3:1 on white, so a coloured mark always has words beside it (legend, lane name). Never for text or interface state |
| Media | `--ds-media-bg` (black), `--ds-on-media`, `--ds-on-media-secondary` (white text over video and images, both themes; Tailwind `text-on-media`), `--ds-media-scrim` |
| Depth and material | `--ds-shadow-1…3`, `--ds-ring-raised` (dark), `--ds-material-bar`, `--ds-material-filter` |
| Type | `--ds-font-sans`, `--ds-font-mono`; `--ds-text-{display,title-1,title-2,title-3,body,reading,callout,caption}-{size,line}`; `--ds-weight-{regular,medium,semibold}`; `--ds-tracking-{title,display}` |
| Shape and space | `--ds-radius-{xs,sm,md,lg,full}` (4, 6, 10, 14, 999 px); `--ds-space-{0-5,1,2,3,4,5,6,8,10,12,16}` (4 px base); `--ds-control-{sm,md,lg}` (28, 32, 40 px); `--ds-hit-min`; `--ds-icon-{sm,md,lg}` |
| Motion | `--ds-dur-{instant,fast,base,base-exit,slow,slow-exit}` (0, 120, 200, 140, 320, 220 ms); `--ds-ease-{standard,exit,spring}`; `--ds-motion-{shift,toast-shift,dialog-scale,press-scale,drawer-shift}` |
| Layers | `--ds-z-{base,sticky,dock,popover,overlay,dialog,toast}` (0–60) |

Breakpoints (640, 900, 1200, 1440 px) are constants in `src/lib/design/motion.ts` (`BREAKPOINT`), since media queries cannot read variables.

**Contrast.** Text pairs reach 4.5:1 and control boundaries, focus ring, selection bar and progress reach 3:1 in both themes; `tokens-contrast.test.ts` computes them from `tokens.css` with the WCAG formula. Pairs the proposal rules out are replaced by dedicated tokens: tertiary text on sunken, hover and selected surfaces and on raised surfaces, control borders on raised surfaces (dark), and the hover fill on raised surfaces (dark: gray-6, lighter than the raised gray-5). Two status colours differ from the proposal so that they also pass on hover fills: light info `#3a6693` (was `#3d6a99`, 4.41:1 on hover) and dark danger `#ee8f80` (was `#e8806f`, 4.19:1 on the raised hover). Status colours are checked against card, raised, field, hover and raised-hover surfaces, and the hovered danger button against its text. Inside a `Card variant="sunken"`, a menu, dialog, sheet, tooltip or toast these swaps happen by themselves (`ds-on-sunken`, `ds-on-raised`); in a hovered or selected table row too. Elsewhere, add the class `ds-on-sunken` or `ds-on-raised` to a container drawn on those surfaces.

**Deliberate deviation: three light data colours under 3:1.** In the light theme `--ds-data-3`, `--ds-data-4` and `--ds-data-5` (aqua, yellow, magenta) are under 3:1 on white (about 2.8, 2.2 and 2.7), below the 3:1 the proposal asks of graphics. This is on purpose: darkening them to 3:1 brings neighbours too close for colour-vision deficiencies, and telling series apart for everyone comes first (adjacent CVD ΔE ≥ 8.4). The condition: a data colour never carries meaning alone. Every coloured mark has its words beside it (a legend, a lane name or a label in a text colour), data colours are never used for text, and the test allows these three, and only these, down to 2:1 in light. The dark steps all reach 3:1.

**Layers.** Neighbouring surfaces differ by one or two grey steps; cards always have a 1 px separator border (cards are only 1.09:1 against the light background). Shadows are for light mode; dark mode uses lighter surfaces and an inner hairline. The glass material (`ds-material`) is only for layers floating over content (top bar, floating toolbars); under `prefers-reduced-transparency` or `prefers-contrast: more` it becomes opaque.

## Themes

Light is the default. Dark applies under `prefers-color-scheme: dark` unless an ancestor has `data-theme="light"`, and always under `data-theme="dark"`. `data-theme` works on any element, so a subtree can be themed alone (the specimen shows both themes side by side this way). `prefers-contrast: more` raises separators, control borders and secondary text one step.

`useThemePreference()` returns `{ preference, resolved, setPreference }`: `"system" | "light" | "dark"`, stored in `localStorage` under `levi-theme`. Every explicit choice is stored, the default included (so it still holds when the default changes); only someone who never chose has no stored value, and then (or without storage) the preference is `THEME_DEFAULT_PREFERENCE`. **Transitional default: dark.** Until the pages use the tokens (stage 5) a light frame over the dark pages looks broken, so the default is `"dark"`; stage 5 sets it back to `"system"` (one constant in `src/lib/design/theme.ts`, repeated in `theme-boot.ts`, a test keeps them equal). Reads and writes go through `browserStorage`, which never throws. The hook follows system changes and other tabs. The hook itself does not touch `<html>`; `applyTheme(element, preference)` does. Since stage 2 the frame (`ShellProvider`) holds the one preference and applies it to `<html>`, and a small script in the root layout's `<head>` applies the same preference before the first paint (`theme-boot.ts`: a stored light/dark choice, nothing for a stored "system", the default when nothing is stored), so the bar never flashes the other theme. Native controls follow the theme: `color-scheme` is `light dark` on `<html>` and set by `data-theme` when there is one (stage 5; before it stayed dark). `ThemePicker` is the System / Light / Dark control; the top bar uses a menu with the same three choices.

Add `ds-root` to a container to give it the design-system font, text colour and background, and `color-scheme` that follows the theme.

## Components

Import from `@/components/ds`. The root layout imports `@/styles/tokens.css`, `@/styles/ds.css` and `@/styles/shell.css` once for every page.

| Component | Notes |
| --- | --- |
| `Button` | `variant` primary / secondary (default) / ghost / danger; `size` sm / md / lg (28 / 32 / 40 px); `loading` (spinner, `aria-busy`, clicks ignored, label kept); `icon`, `iconEnd`; `type="button"` by default |
| `IconButton` | Required `label` (accessible name and tooltip); `shortcut`; `pressed` for toggles (`aria-pressed`) |
| `Icon` | Lucide icon at `size` sm / md / lg; `label` makes it `role="img"` |
| `Tooltip` | Opens on hover after 0.5 s and at once on keyboard focus (not on click); Escape, pointer leave and focus loss close it; sets `aria-describedby` on the trigger |
| `Field`, `Input`, `Textarea`, `Select` | `Field` wires label, hint and error (`aria-describedby`, `aria-invalid`, `required`) to the control inside; `Select` is a native select |
| `Checkbox`, `Radio`, `RadioGroup`, `Switch` | Native inputs; `Checkbox indeterminate`; `RadioGroup` is a fieldset with a legend; `Switch` is a checkbox with `role="switch"` |
| `Badge`, `StatusDot`, `Tag` | Status as icon shape + colour + words (`tone` neutral, success, warning, danger, info); `StatusDot live` breathes once every 2 s (still under reduced motion); `Tag onRemove` gets a "Remove …" button with a 24 × 24 px target (16 px drawn); `removeLabel` is required (type-checked) when the tag is not plain text |
| `Card`, `Divider`, `Kbd` | `Card` `variant` default / sunken / raised, `padding` compact / regular, optional `title`, `description`, `actions` |
| `Dialog`, `Sheet` | `Sheet modal={false}` is a drawer beside the page: no scrim, no focus trap, Escape closes it only while focus is inside, focus still moves in and back; `width` sets its width in px. Otherwise modal (`aria-modal`), labelled by the title; focus moves in, Tab wraps, focus that lands outside (a press on the scrim, a stray `focus()`) is pulled back, Escape and the scrim close (`closeOnScrim={false}` keeps it open), focus returns to the opener; with nested modals only the innermost handles Tab and Escape, and a menu inside handles its own Escape first; `container` renders into another element (portal); `Sheet side` right / left / bottom |
| `ConfirmDialog`, `useConfirm` | `role="alertdialog"`; the title names action and object, `confirmLabel` is a verb; `tone="danger"` puts focus on Cancel; `const { confirm, dialog } = useConfirm()` then `await confirm({...})`; a second question while one is open cancels the first (it answers false). App code uses the frame's `useConfirmAction()` instead |
| `ToastProvider`, `useToast` | Bottom right; `show({ title, description, tone, action, duration })`; notes in a polite region, at most three, hidden after 4 s (paused while hovered or focused); a toast with an action stays until closed unless `duration` is given; errors (`danger`) in an assertive region, never hidden by newer toasts, stay until closed; outside a provider `show` does nothing |
| `Skeleton`, `SkeletonText` | Static blocks (no shimmer), hidden from assistive technology; mark the loading region `aria-busy="true"` |
| `Progress`, `Spinner` | `Progress value={n}` is determinate; `value={null}` indeterminate (a moving bar, or still with "In progress" under reduced motion); `Spinner` is `role="status"`; neither is a dialog. Limitation: a `Spinner` is its own live region, and a live region inserted together with its text is not announced by every screen reader; for a result that must be heard, keep one status region mounted and change its text, or use a toast |
| `EmptyState` | Icon, one sentence, the next step as a button (`action`, `secondaryAction`) |
| `Tabs` | ARIA tabs: one tab stop, ←/→, Home/End, disabled tabs skipped; switching is instant; only the selected tab has `aria-controls`; a `value` that matches no enabled tab selects the first enabled one |
| `SegmentedControl` | Radio group drawn as joined buttons; arrows move and select |
| `Menu` | Menu button: Enter/Space/↓ open on the first item, ↑ on the last; ↑/↓/Home/End; Enter/Space choose; Escape closes and returns focus; Tab and a click outside close. `variant` secondary / ghost; `iconOnly` (the label stays as the visually hidden name; give `tooltip`); `badge` (a count); an item with `checked` is a `menuitemradio` with a check mark |
| `Table`, `TableRow` | Sunken header, row separators, hover; `TableRow selected` (grey fill, weight 500, 2 px bar, `aria-current`); `ds-num` for right-aligned tabular numbers; the wrapper scrolls sideways |
| `ThemePicker` | System / Light / Dark |
| `ReorderList` | Import from `@/components/ds/ReorderList` (lazily with `next/dynamic` where possible): drag by the handle, or focus the handle and press ↑/↓/Home/End; each move is announced; `moveItem`, `DS_SPRING`, `dsLayoutTransition` are exported |

## Global frame (stage 2)

`src/app/layout.tsx` mounts `AppFrame` (`src/components/shell/app-frame.tsx`) around every page:

| Part | What it does |
| --- | --- |
| `ShellProvider` (`shell-context.tsx`) | The theme preference (one instance, applied to `<html>`); whether the palette or the shortcut list is open; the frame's keys (`global-keys.ts`) |
| `ToastProvider` | The toast region, bottom right, one polite and one assertive live region. Any component below calls `useToast().show({...})` from `@/components/ds` |
| `ConfirmProvider` (`confirm.tsx`) | One confirmation dialog at the root. `const confirm = useConfirmAction(); if (!(await confirm({ title, confirmLabel, tone }))) return;`. Cancel, Escape and the scrim answer false, as Cancel did in `window.confirm`; outside the provider the answer is always false. While a native modal `<dialog>` is open (the training pool's push dialog) the question is rendered inside it, because the rest of the page is inert |
| Top bar (`levi-header.tsx`) | 56 px, opaque `--ds-bg` with a separator (it does not stay on top while the page scrolls yet, so a translucent material would only show the older pages' dark background; it becomes `ds-material` once it is sticky). Wordmark; the pages (Live evaluation when shown, Explore, Conversion & review, Training pool when offered, Guide, Report), the current one with `aria-current="page"`, weight 600 and a 2 px bar; on the right Search (opens the palette), Jobs, the Agent Workbench toggle, Settings (Accounts & connections, Command palette, Keyboard shortcuts), Theme and Language. Below 900 px the pages move to their own scrolling row (`--levi-header-height` becomes 100 px; `.h-screen` pages subtract it) |
| Jobs (`jobs-menu.tsx`, `jobs.ts`) | The number of running training pool jobs and conversions, read from the existing `/api/levi/pool/jobs` and `/api/levi/jobs` on first load, when the menu opens, when the tab becomes visible again and every 60 s while it is visible (nothing while hidden, never two requests at once); the menu leads to the training pool and to Conversion & review |
| Command palette (`command-palette.tsx`, `commands.ts`) | ⌘K on macOS, Ctrl+K elsewhere, or Search. A combobox over a list box: go to a page, open the Agent Workbench, Accounts & connections or the shortcut list, choose the theme or the language. Words match the label in either language and Chinese and English keywords |
| Shortcut list (`shortcuts-dialog.tsx`) | `?` (not while typing). Lists the frame's keys and the pages' existing ones (episode viewer, annotations, review queue) |
| Agent Workbench drawer | `agent-workbench.tsx` renders its unchanged content in a non-modal right `Sheet` below the bar (`levi-agent-sheet`); the page stays usable beside it, its left edge still resizes it (the width is kept per browser). The bar's toggle, the palette and the old window events `levi-agent-toggle` / `levi-agent-connections` open it; it reports its state with `levi-agent-state` |

**Keys.** The frame binds only ⌘K / Ctrl+K and `?` (also the full-width `？`, and with AltGr). Neither fires during IME composition, `?` not in a text field, and neither while another modal (a confirmation, a page's own dialog, a native `showModal()` dialog) is open. Keys typed inside a modal layer (except Tab and Escape, which the layer handles) do not reach listeners on `window`, so page shortcuts never act behind a dialog, as with a native `confirm()`; a confirmation still open when the page changes answers false. The pages keep theirs: Space, ↑/↓, J/K, Escape, Ctrl/⌘+S/Z/Y.

**Loading overlay.** `loading-component.tsx` is `role="status"` with `aria-busy="true"`, not a dialog: it takes no focus and traps nothing.

**Tailwind** does not scan `docs/` (`@source not "../../docs"` in `globals.css`): the Markdown is not UI code and its words must not add utilities.

## Brand

One mark everywhere: a graphite tile with a geometric "L" and a square point, on a 32-unit grid. `LEVI_MARK` in `src/components/shell/brand.tsx` is its only definition; `<LeviMark size>` draws it in the page (tile `--ds-accent`, glyph `--ds-on-accent`, so it is black on light and white on dark, like the primary button) and `<LeviWordmark>` adds the name. The top bar, the home page, the guide and the report use it. The browser tab icon `src/app/icon.svg` (black tile, white tile under a dark browser theme), `apple-icon.png` (180 px, full bleed) and `favicon.ico` (16, 32, 48 px) are drawn from the same numbers: after changing the mark run `uv run --with pillow python scripts/brand_icons.py`; `brand.test.ts` fails when `icon.svg` no longer matches. No page draws a logo of its own, and the lime colour is gone.

## Global page base (stage 5)

`globals.css` sets the page from the tokens: background `--ds-bg`, primary text, the system font at body size, `color-scheme: light dark` (so scrollbars, select lists and date pickers follow the theme), quiet scrollbars, selection, `scroll-padding-top` for the bar, and one graphite focus ring for every element (`levi.css`, `outline: 2px` offset 2 px; `Highlight` in forced colours). In Chinese, older uppercase and wide-tracked labels are set normally.

**Older names.** Pages that have not moved yet keep their classes; their colours now come from the tokens, so every page follows light and dark and none shows the old green, parchment, lime or cyan. Map them when you migrate:

| Older name | Now | Use instead |
| --- | --- | --- |
| `--bg`, `--surface-0/1/2` | `--ds-bg`, `--ds-surface-sunken`, `--ds-surface-1`, `--ds-surface-2` | the same `--ds-*` |
| `--text-primary/muted/faint` | `--ds-text-primary/secondary/tertiary` | the same |
| `--accent`, `--accent-soft`, `--accent-ring` | `--ds-accent`, 12 % accent, `--ds-focus-ring` | `--ds-accent` only for the primary button, selection and progress; `--ds-surface-selected` for a selected fill |
| `--border-subtle`, `--border-strong` | `--ds-separator`, `--ds-separator-strong` | the same; `--ds-border-control` for inputs |
| Tailwind `white` (`text-white`, `border-white/10`, `bg-white/5`) | `--ds-text-primary` (a faint line or fill in both themes) | `--ds-separator` / `--ds-surface-hover`; over video `text-on-media` |
| Tailwind `slate-100…200` / `300…500` / `600` | primary / secondary / tertiary text (`500` is secondary: older pages put it on sunken and raised surfaces, where tertiary text falls under 4.5:1) | the text tokens |
| Tailwind `slate-700` / `800` / `900` / `950` | `--ds-separator-strong` / `--ds-separator` / `--ds-surface-1` / `--ds-bg` | the same |
| Tailwind `cyan-*`, `lime-*` | `--ds-accent` (`cyan-200`, `600`: `--ds-accent-hover`) | `Button variant="primary"`, `--ds-surface-selected` |
| Tailwind `red-*`, `orange/amber/yellow-*`, `green/emerald-*`, `blue-*` | `--ds-danger`, `--ds-warning`, `--ds-success`, `--ds-info` | `Badge`, `StatusDot` with a tone; chart series use `--ds-data-*` |
| `.levi-workbench`, `.levi-box` | page frame, card | `Card`, the page's own layout |
| `.levi-primary`, `.levi-secondary` | drawn like the ds buttons | `Button variant="primary"` / `"secondary"` |
| `.levi-input` | drawn like a ds field | `Input`, `Select`, `Textarea` in a `Field` |
| `.levi-table`, `.levi-status`, `.levi-error`, `.levi-code`, `.levi-metrics`, `.levi-eyebrow` | ds table, neutral or status badge, error note, code block, metric cards, section label | `Table`, `Badge`, an error in three parts (what, why, what to do), `<pre>` in `ds-*` styles |
| `.panel`, `.panel-raised` | card, raised card | `Card`, `Card variant="raised"` |

`levi.css` still holds the page styles of the episode viewer, Live, the training pool, conversion and the Agent Workbench; each literal colour in it was mapped by its role (surface, text, line, accent, status). A class is removed from `levi.css` once no page uses it. A test (`global-styles.test.ts`) fails on a colour literal in `globals.css`, `home.css`, `reading.css` and `report.css`, on one in `levi.css` other than black, and on the old palette anywhere in them.

**Home** (`/`): the work entrance. Continue (the last episode or dataset opened in this browser; the frame records visits in `localStorage` under `levi-recent`, nothing is sent), Needs you (agent tasks whose `waiting_for` is a person's approval, review or commit, from `/api/levi/agent/v1/activity/tasks`), Running (conversions and training pool jobs with their progress), the live evaluation card when a live service is shown, and recent datasets (visited first, then the other registered ones). Each card loads on its own with a skeleton and gives up after 10 s; it refreshes every 15 s while the tab is visible. The Hugging Face search and the older `/?path=` and `/?dataset=` links stay. The introduction moved to the guide.

**Reading layout** (`reading.css`, the guide and the report): one column up to 760 px, contents on the left that stay in view and mark the current section (`aria-current="location"`), reading text 16/26 (`--ds-text-reading-*`), Markdown headings, lists, links (primary text, underlined), quotes, code and tables in `.levi-prose`. The report's charts use `--ds-data-*` (read through `useCssTokens`; Recharts also accepts `var(--ds-…)` directly in current browsers, the hook is needed only for canvas and WebGL), running states are shown by colour and words with no looping animation, and "Updated" is a frame toast.

## Motion

| Interaction | Motion | Reduced motion |
| --- | --- | --- |
| Hover, press | colour change 120 ms; press `scale(0.98)` | colour only |
| Tabs, segments, frame stepping | none, instant | — |
| Menu, tooltip | fade + 4 px rise, 200 ms | none |
| Dialog | scrim fade, dialog `scale(0.97→1)` + fade, 200 ms | none |
| Sheet | slide from its side, 320 ms spring | none |
| Toast | 8 px rise + fade, 200 ms | none |
| Progress | width eases 200 ms; indeterminate bar travels | still, "In progress" |
| Running status | opacity breath every 2 s | still |
| Drag and reorder (Motion) | lift `scale(1.02)` + deeper shadow, spring into place | items snap |

Under `prefers-reduced-motion: reduce` the tokens `--ds-dur-base`, `--ds-dur-slow` (and their exit values) become 0, `--ds-ease-spring` becomes `linear` and all movement tokens become none; spinners, the indeterminate bar and the breathing dot stop. `data-motion="reduce"` on an ancestor does the same (for previews and tests). Components that render differently use `usePrefersReducedMotion()`; `ReorderList` uses Motion's `useReducedMotion` and `MotionConfig reducedMotion="user"`. `ReducedMotionScope reduce` forces the reduced path for a subtree (the specimen's preview switch; put `data-motion="reduce"` on an element too so the CSS follows). Exits are instant in stage 1 (no exit animation).

No infinite decorative animation, parallax, scroll hijacking, animation longer than 400 ms or animation that blocks input.

**Motion** (the `motion` package, formerly Framer Motion, MIT) is used only in `ReorderList`. It is about 45 KB gzipped and is loaded only by pages that import that component.

## Accessibility

- Focus ring: 2 px `--ds-focus-ring`, offset 2 px (class `ds-focus` on every interactive component); in Windows high contrast it uses `Highlight`.
- Every overlay traps focus (listeners on `document`, so it holds wherever focus went), closes on Escape and returns focus; menus and tabs follow the ARIA patterns; toasts use one polite and one assertive live region.
- Hit targets are at least 24 × 24 px.

## Specimen page

`/design` shows every token and component in light and dark side by side (`?only=light` or `?only=dark` shows one; `?motion=reduce` starts with reduced motion). It is served by `next dev`; in a production server (`next start`, `levi serve`) `src/middleware.ts` answers a plain 404 before routing (so neither the page's metadata nor its styles are sent) unless `LEVI_DESIGN_PAGE=1` is set in its environment (`src/lib/design/gate.ts`). It is not linked from the navigation.

## Testing

`global-styles.test.ts` checks the page base and the older styles (above), `brand.test.ts` the mark, `recent.test.ts` and `home-data.test.ts` the home page's data; ESLint's hex rule also covers the home page, guide, report and `src/lib/design/`.

`bun test` runs the component tests in a DOM (happy-dom, a dev dependency). `tokens-contrast.test.ts` also fails on a colour literal (hex, `rgb()`, `hsl()`) or a Tailwind arbitrary value in the components' or the specimen's TSX. A component test imports `./dom` first and calls `setupDom()`; `render`, `press`, `click`, `focus`, `dropFocus`, `fire`, `flush` and `mockMatchMedia` are there. The DOM globals are removed after each such file, so other tests run without a DOM.
