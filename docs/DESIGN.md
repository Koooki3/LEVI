# Design system

[中文](DESIGN.zh-CN.md)

This is how LEVI's interface is built today: graphite accent, light and dark themes that follow the system unless a person chose one, system fonts only, Lucide icons, Motion for drag and list reordering only. Every route reads its colours from the `--ds-*` tokens and the sheets that define them; the older stylesheet `levi.css`, the older CSS variables (`--bg`, `--accent`, `--surface-*`) and the remapped Tailwind colour scales are gone. The document keeps the headings of the stages the work was done in (the changelog links to them); [Migration status](#migration-status) says what is on `ds-*` components and what still composes Tailwind utilities over the tokens.

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
| Page base (stage 5) | `src/app/globals.css` (the only global stylesheet besides the three above) |
| Tab titles | `src/components/shell/route-title.tsx` |
| Home, guide, report (stage 5) | `src/components/home/`, `src/styles/home.css`; `src/app/guide/`, `src/styles/reading.css`; `src/components/report/`, `src/app/report/report.css` |
| Token values in code (canvas, WebGL) | `src/lib/design/css-tokens.ts` (`useCssTokens`) |
| Colour lint | `eslint.config.mjs` (`HEX_EXCEPTIONS`), `src/__tests__/eslint-hex.test.ts` |
| Language catalogues | `src/i18n/en.json`, `src/i18n/zh.json`, checked by `src/i18n/__tests__/catalog.test.ts` |
| Tests | `src/components/ds/__tests__/`, `src/lib/design/__tests__/`, `src/components/shell/__tests__/` |

## Rules for new code

- **No hard-coded colours.** Use a semantic token (`var(--ds-text-secondary)`, `var(--ds-surface-1)`) or a `ds-*` class. No hex, `rgb()` or `hsl()` in CSS or TSX, and no Tailwind arbitrary colour values. Three checks enforce it: ESLint (`no-restricted-syntax`) rejects a hex colour, or `rgb()`/`hsl()` with literal numbers, in **every** `src/**/*.{ts,tsx}` except the files listed in `HEX_EXCEPTIONS` in `eslint.config.mjs` (the URDF viewer's robot materials, `data-palette.ts` for canvas and WebGL, tests; each entry has its reason in a comment, and a new entry needs a reason a token cannot serve). A colour is `#` plus 3, 4, 6 or 8 hex digits at the start of a string or after a space, `(`, `,` or `:`; `href`, `to`, `id` and `htmlFor` values are links and are not checked. Tests (`global-styles.test.ts`, `tokens-contrast.test.ts`, the page style tests) fail on a colour literal in `globals.css`, `ds.css`, `shell.css`, `home.css`, `reading.css`, `report.css`, `pages.css`, `agent-content.css`, `viewer.css` and `annotations.css` (the data palette in `viewer.css` is a mirror of `tokens.css`, see [Data colours](#data-colours)), and on a Tailwind palette class (`text-white`, `bg-slate-800`, `border-cyan-400`…) anywhere in `src/`.
- **Semantic tokens only in components.** The raw steps `--ds-gray-l-*` / `--ds-gray-d-*` exist only to define the semantic tokens.
- **One primary button per screen.** Accent A ("graphite") is the darkest grey: it is used only for the primary button, the focus ring, selection and progress. Links in running text are primary text with an underline.
- **Status never by colour alone.** Status colours (success, warning, danger, info) appear only on badges, status dots, toasts and notes, always with an icon shape and words (`Badge`, `StatusDot`).
- **Smallest text 12 px** (`--ds-text-caption-size`); weights 400, 500, 600.
- **Icons**: Lucide only, through `Icon` (16 px with stroke 1.75; 20 or 24 px with 1.5). Decorative icons are `aria-hidden`; an icon that carries meaning gets a `label`.
- **`title=` is not an interface.** A control's name, an icon's meaning, a disabled button's reason and anything a person needs to act on are visible words, a `Tooltip` (hover and keyboard focus) or an `aria-describedby` pair; an icon-only button is an `IconButton`, which requires `label` (its `aria-label` and tooltip). A native `title` is allowed only as an extra on non-interactive text that already shows what matters: the full value of a cut-off table cell, an exact timestamp, an id or a path.
- **No `window.confirm`.** Ask through `useConfirmAction()` (`src/components/shell/confirm.tsx`; a test fails on any `window.confirm` in `src/`), and only for an irreversible action; for an expected, undoable deletion, delete and offer Undo in a toast.
- **Text** goes through the locale catalogs (`useLocale().t`, both `en.json` and `zh.json`). Component defaults (Close, Cancel, Loading, In progress, Dismiss notification, Notifications, Move, Theme, System, Light, Dark) are already there.

## Tokens

All tokens are CSS custom properties on `:root`, named `--ds-*`. (The older `--bg`, `--accent` and `--surface-*` variables no longer exist.)

| Group | Tokens |
| --- | --- |
| Neutral scales | light `--ds-gray-l-0…11`, dark `--ds-gray-d-0…12` (cool neutral greys, proposal §4.1) |
| Surfaces | `--ds-bg`, `--ds-bg-reading`, `--ds-surface-sunken`, `--ds-surface-1` (card), `--ds-surface-2` (raised: menus, dialogs), `--ds-surface-hover`, `--ds-surface-hover-on-raised`, `--ds-surface-selected`, `--ds-field-bg`, `--ds-skeleton`, `--ds-media-bg` (black in both themes), `--ds-scrim` |
| Lines | `--ds-separator`, `--ds-separator-strong`, `--ds-border-control`, `--ds-border-control-on-raised` |
| Text | `--ds-text-primary`, `--ds-text-secondary`, `--ds-text-tertiary`, `--ds-text-tertiary-on-sunken`, `-on-hover`, `-on-selected`, `-on-raised`, `--ds-text-disabled`, `--ds-icon` |
| Accent A | `--ds-accent`, `--ds-accent-hover`, `--ds-on-accent`, `--ds-focus-ring`, `--ds-selected-indicator`, `--ds-progress`, `--ds-progress-track` |
| Status | `--ds-success`, `--ds-warning`, `--ds-danger`, `--ds-info`, each with `-bg`; `--ds-on-danger` |
| Data | `--ds-data-1…8` (blue, orange, aqua, yellow, magenta, green, violet, red; fixed order): series, time-segment and category colours, light and dark steps; see [Data colours](#data-colours). Never for text or interface state |
| Media | `--ds-media-bg` (black), `--ds-on-media`, `--ds-on-media-secondary` (white text over video and images, both themes; Tailwind `text-on-media`), `--ds-media-scrim` |
| Depth and material | `--ds-shadow-1…3`, `--ds-ring-raised` (dark), `--ds-material-bar`, `--ds-material-filter` |
| Type | `--ds-font-sans`, `--ds-font-mono`; `--ds-text-{display,title-1,title-2,title-3,body,reading,callout,caption}-{size,line}`; `--ds-weight-{regular,medium,semibold}`; `--ds-tracking-{title,display}` |
| Shape and space | `--ds-radius-{xs,sm,md,lg,full}` (4, 6, 10, 14, 999 px); `--ds-space-{0-5,1,2,3,4,5,6,8,10,12,16}` (4 px base); `--ds-control-{sm,md,lg}` (28, 32, 40 px); `--ds-hit-min`; `--ds-icon-{sm,md,lg}` |
| Motion | `--ds-dur-{instant,fast,base,base-exit,slow,slow-exit}` (0, 120, 200, 140, 320, 220 ms); `--ds-ease-{standard,exit,spring}`; `--ds-motion-{shift,toast-shift,dialog-scale,press-scale,drawer-shift}` |
| Layers | `--ds-z-{base,sticky,dock,popover,overlay,dialog,toast}` (0–60) |

Breakpoints (640, 900, 1200, 1440 px) are constants in `src/lib/design/motion.ts` (`BREAKPOINT`), since media queries cannot read variables.

**Contrast.** Text pairs reach 4.5:1 and control boundaries, focus ring, selection bar and progress reach 3:1 in both themes; `tokens-contrast.test.ts` computes them from `tokens.css` with the WCAG formula. Pairs the proposal rules out are replaced by dedicated tokens: tertiary text on sunken, hover and selected surfaces and on raised surfaces, control borders on raised surfaces (dark), and the hover fill on raised surfaces (dark: gray-6, lighter than the raised gray-5). Two status colours differ from the proposal so that they also pass on hover fills: light info `#3a6693` (was `#3d6a99`, 4.41:1 on hover) and dark danger `#ee8f80` (was `#e8806f`, 4.19:1 on the raised hover). Status colours are checked against card, raised, field, hover and raised-hover surfaces, and the hovered danger button against its text. Inside a `Card variant="sunken"`, a menu, dialog, sheet, tooltip or toast these swaps happen by themselves (`ds-on-sunken`, `ds-on-raised`); in a hovered or selected table row too. Elsewhere, add the class `ds-on-sunken` or `ds-on-raised` to a container drawn on those surfaces.

**Layers.** Neighbouring surfaces differ by one or two grey steps; cards always have a 1 px separator border (cards are only 1.09:1 against the light background). Shadows are for light mode; dark mode uses lighter surfaces and an inner hairline. The glass material (`ds-material`) is only for layers floating over content (top bar, floating toolbars); under `prefers-reduced-transparency` or `prefers-contrast: more` it becomes opaque.

### Data colours

Data colours are not interface colours. `tokens.css` is their one source: eight categorical hues in a fixed order (`--ds-data-1…8`), each with a light and a dark step. They are checked with the dataviz palette checks: neighbours stay apart for colour-vision deficiencies (ΔE ≥ 8.4) and for normal vision (ΔE ≥ 19.3), and every dark step is at least 3:1 on the dark page and card (tested). Everything else takes them from there: chart series (`seriesColor(i)`, the report's charts through `useCssTokens`), annotation styles (`--style-subtask` … `--style-memory` in `annotations.css`), RECAP advantage (`--dv-positive`, `--dv-negative`), time segments and labels on video. `viewer.css` mirrors them as `--dv-1…8` (aliases of the tokens); canvas and the 3D scene cannot read CSS variables and use `DATA_ON_MEDIA` in `data-palette.ts` (the dark steps as numbers; a test keeps them equal to `viewer.css`), the one viewer file that holds hex values.

**Deliberate deviation: three light data colours under 3:1.** In the light theme `--ds-data-3`, `--ds-data-4` and `--ds-data-5` (aqua, yellow, magenta) are under 3:1 on white (about 2.8, 2.2 and 2.7), below the 3:1 the proposal asks of graphics. This is on purpose: darkening them to 3:1 brings neighbours too close for colour-vision deficiencies, and telling series apart for everyone comes first (adjacent CVD ΔE ≥ 8.4). The condition: a data colour never carries meaning alone. Every coloured mark has its words beside it (a legend, a lane name or a label in a text colour), data colours are never used for text, and the test allows these three, and only these, down to 2:1 in light. Status (pass/warn/fail, success/failure) uses the status tokens with an icon and words, not data colours.

## Themes

The stylesheet's base is light; dark applies under `prefers-color-scheme: dark` unless an ancestor has `data-theme="light"`, and always under `data-theme="dark"`. `data-theme` works on any element, so a subtree can be themed alone (the specimen shows both themes side by side this way). `prefers-contrast: more` raises separators, control borders and secondary text one step.

`useThemePreference()` returns `{ preference, resolved, setPreference }`: `"system" | "light" | "dark"`, stored in `localStorage` under `levi-theme`. **The default is `"system"`**: someone who never chose follows the system, and so does any visit where storage is unavailable (`THEME_DEFAULT_PREFERENCE` in `src/lib/design/theme.ts`, repeated in `theme-boot.ts`; a test keeps them equal). Every explicit choice, "system" included, is stored, so it still holds if the default ever changes. Reads and writes go through `browserStorage`, which never throws. The hook follows system changes and other tabs; it does not touch `<html>` itself, `applyTheme(element, preference)` does. The frame (`ShellProvider`) holds the one preference and applies it to `<html>`, and a small script in the root layout's `<head>` applies a stored light or dark choice before the first paint (`theme-boot.ts`; nothing for a stored "system" or no stored value), so the bar never flashes the other theme. Native controls follow the theme: `color-scheme` is `light dark` on `<html>` and set by `data-theme` when there is one. `ThemePicker` is the System / Light / Dark control; the top bar uses a menu with the same three choices.

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
| Top bar (`levi-header.tsx`) | 56 px and **sticky**: it stays at the top while the page scrolls, drawn with the bar material (`--ds-material-bar` with `--ds-material-filter`: translucent and blurred; opaque under `prefers-reduced-transparency` or `prefers-contrast: more`) and a separator. Wordmark (`LeviWordmark`, from `brand.tsx`); the pages (Live evaluation when shown, Explore, Conversion & review, Training pool when offered, Guide, Report), the current one with `aria-current="page"`, weight 600 and a 2 px bar; on the right Search (opens the palette), Jobs, the Agent Workbench toggle, Settings (Accounts & connections, Command palette, Keyboard shortcuts), Theme and Language. Below 900 px the pages move to their own scrolling row, the bar is 100 px (`--levi-header-height`) and scrolls away with the page instead of staying (it would cover too much of a phone) |
| Below the bar (`shell.css`, `globals.css`) | `--levi-header-height` is the bar's height (full-height pages subtract it); `--levi-sticky-top` is how much of the top the bar covers while scrolled (the same 56 px on wide screens, 0 below 900 px). `scroll-padding-top`, the reading column's anchors (`scroll-margin-top`) and contents (`top`) and the page parts that stick (the live banner, the training pool's facets, the specimen's headers) start below it. A page part that sticks to the top of the window uses `top: calc(var(--levi-sticky-top) + …)`; one that sticks inside its own scrolling box (table headers, the annotation list) does not |
| Tab title (`route-title.tsx`) | The root layout's metadata is the name alone ("LEVI", with a bilingual description); `RouteTitle` sets `document.title` to the page's name in the reader's language and the name: "Explore · LEVI", "训练池 · LEVI", "Episode viewer · lerobot/aloha_static_coffee · LEVI". Pages and names: `routePageName` (home, guide, report, explore, conversion & review, training pool, live evaluation, the episode viewer, the specimen) |
| Jobs (`jobs-menu.tsx`, `jobs.ts`) | The number of running training pool jobs and conversions, read from the existing `/api/levi/pool/jobs` and `/api/levi/jobs` on first load, when the menu opens, when the tab becomes visible again and every 60 s while it is visible (nothing while hidden, never two requests at once); the menu leads to the training pool and to Conversion & review |
| Command palette (`command-palette.tsx`, `commands.ts`) | ⌘K on macOS, Ctrl+K elsewhere, or Search. A combobox over a list box: go to a page, open the Agent Workbench, Accounts & connections or the shortcut list, choose the theme or the language. Words match the label in either language and Chinese and English keywords |
| Shortcut list (`shortcuts-dialog.tsx`) | `?` (not while typing). Lists the frame's keys and the pages' existing ones (episode viewer, annotations, review queue) |
| Agent Workbench drawer | `agent-workbench.tsx` renders its unchanged content in a non-modal right `Sheet` below the bar (`levi-agent-sheet`); the page stays usable beside it, its left edge still resizes it (drag, or focus it and press ←/→, Shift for larger steps, Home/End; it is a separator with `aria-valuemin`, `aria-valuenow`, `aria-valuemax` in px, `panel-width.ts`; the width is kept per browser). The bar's toggle, the palette and the old window events `levi-agent-toggle` / `levi-agent-connections` open it; it reports its state with `levi-agent-state` |

**Keys.** The frame binds only ⌘K / Ctrl+K and `?` (also the full-width `？`, and with AltGr). Neither fires during IME composition, `?` not in a text field, and neither while another modal (a confirmation, a page's own dialog, a native `showModal()` dialog) is open. Keys typed inside a modal layer (except Tab and Escape, which the layer handles) do not reach listeners on `window`, so page shortcuts never act behind a dialog, as with a native `confirm()`; a confirmation still open when the page changes answers false. The pages keep theirs: Space, ↑/↓, J/K, Escape, Ctrl/⌘+S/Z/Y.

**Loading overlay.** `loading-component.tsx` is `role="status"` with `aria-busy="true"`, not a dialog: it takes no focus and traps nothing.

**Tailwind** does not scan `docs/` (`@source not "../../docs"` in `globals.css`): the Markdown is not UI code and its words must not add utilities.

## Brand

One mark everywhere: a graphite tile with a geometric "L" and a square point, on a 32-unit grid. `LEVI_MARK` in `src/components/shell/brand.tsx` is its only definition; `<LeviMark size>` draws it in the page (tile `--ds-accent`, glyph `--ds-on-accent`, so it is black on light and white on dark, like the primary button) and `<LeviWordmark>` adds the name. The top bar draws the wordmark; the home page, the guide and the report draw the mark. The browser tab icon `src/app/icon.svg` (black tile, white tile under a dark browser theme), `apple-icon.png` (180 px, full bleed) and `favicon.ico` (16, 32, 48 px) are drawn from the same numbers: after changing the mark run `uv run --with pillow python scripts/brand_icons.py`; `brand.test.ts` fails when `icon.svg` no longer matches. No page draws a logo of its own, and the lime colour is gone.

## Global page base (stage 5)

`globals.css` is the one global stylesheet. It sets the page from the tokens: background `--ds-bg`, primary text, the system font at body size, `color-scheme: light dark` (so scrollbars, select lists and date pickers follow the theme), quiet scrollbars, selection, `scroll-padding-top` below the sticky bar, tabular figures (`.tabular`), one graphite focus ring for every element (`outline: 2px`, offset 2 px; `Highlight` in forced colours), the pointer and disabled look of buttons, and the accent colour of checkboxes, radios, ranges and `<progress>`. In Chinese, the few remaining uppercase and wide-tracked labels are set normally (`ds-eyebrow` too: `:lang(zh)`). It also maps Tailwind's `font-sans`, `font-mono` and `text-on-media` (white over video and images) to the tokens.

**Older names are gone.** `levi.css` (the pre-redesign page styles) and the mappings that pointed older code at the tokens were deleted: `--bg`, `--surface-*`, `--text-*`, `--accent*`, `--border-*` and Tailwind's remapped `white`, `slate`, `cyan`, `lime`, status hues. A Tailwind colour class is Tailwind's own colour again, so none may be left in the code (a test finds them). When porting code from an older branch:

| Older name | Use |
| --- | --- |
| `--bg`, `--surface-0/1/2` | `--ds-bg`, `--ds-surface-sunken`, `--ds-surface-1`, `--ds-surface-2` |
| `--text-primary/muted/faint` | `--ds-text-primary/secondary/tertiary` |
| `--accent`, `--accent-soft`, `--accent-ring` | `--ds-accent` only for the primary button, selection and progress; `--ds-surface-selected` for a selected fill; `--ds-focus-ring` |
| `--border-subtle`, `--border-strong` | `--ds-separator`, `--ds-separator-strong`; `--ds-border-control` for inputs |
| Tailwind `white` (`text-white`, `border-white/10`, `bg-white/5`) | `--ds-text-primary`, `--ds-separator`, `--ds-surface-hover`; over video `text-on-media` |
| Tailwind `slate-*`, `zinc-*` | the text tokens (primary, secondary, tertiary), `--ds-separator(-strong)`, `--ds-surface-1`, `--ds-bg` |
| Tailwind `cyan-*`, `lime-*` | `--ds-accent`; `Button variant="primary"`; `--ds-surface-selected` |
| Tailwind `red-*`, `orange/amber/yellow-*`, `green/emerald-*`, `blue-*` | `--ds-danger`, `--ds-warning`, `--ds-success`, `--ds-info` through `Badge` or `StatusDot`; chart series use `--ds-data-*` |
| `.levi-box`, `.levi-eyebrow`, `.levi-primary`, `.levi-secondary`, `.levi-input`, `.levi-table`, `.levi-status`, `.levi-error`, `.panel` | `Card` / `EmptyState`, `ds-eyebrow`, `Button`, `Input` / `Select` / `Textarea` in a `Field`, `Table`, `Badge`, an error in three parts (`Problem`), `Card` |

`global-styles.test.ts` fails on a colour literal in `globals.css`, `home.css`, `reading.css` and `report.css`, on the old palette, variable names or remaps, on `levi.css` coming back, and on a Tailwind palette class anywhere in `src/`.

**Home** (`/`): the work entrance. Continue (the last episode or dataset opened in this browser; the frame records visits in `localStorage` under `levi-recent`, nothing is sent), Needs you (agent tasks whose `waiting_for` is a person's approval, review or commit, from `/api/levi/agent/v1/activity/tasks`), Running (conversions and training pool jobs with their progress), the live evaluation card when a live service is shown, and recent datasets (visited first, then the other registered ones). Each card loads on its own with a skeleton and gives up after 10 s; it refreshes every 15 s while the tab is visible. The Hugging Face search and the older `/?path=` and `/?dataset=` links stay. The introduction moved to the guide.

**Reading layout** (`reading.css`, the guide and the report): one column up to 760 px, contents on the left that stay in view and mark the current section (`aria-current="location"`), reading text 16/26 (`--ds-text-reading-*`), Markdown headings, lists, links (primary text, underlined), quotes, code and tables in `.levi-prose`. The report's charts use `--ds-data-*` (read through `useCssTokens`; Recharts also accepts `var(--ds-…)` directly in current browsers, the hook is needed only for canvas and WebGL), running states are shown by colour and words with no looping animation, and "Updated" is a frame toast.

## Episode viewer (stage 3)

The episode viewer (`src/app/[org]/[dataset]/[episode]/`) uses the tokens in both themes. Its styles are in `src/components/viewer/`:

| What | Where |
| --- | --- |
| Frame, tab bar, episode list, media, playback, notes, metric tiles | `viewer.css` (classes `vw-*`) |
| Annotation panels, timeline, value-model and anchored-review lanes, segmentation | `annotations.css` (scoped under `.annotations-skin`; replaces `annotations-skin.css`) |
| Data palette | `viewer.css` (`--dv-1` … `--dv-8` = the tokens `--ds-data-1` … `--ds-data-8`, `--dv-positive`, `--dv-negative`, `--dv-neutral`) and `data-palette.ts` |
| Tabs and the Analysis tab | `viewer-tabs.ts`, `analysis-tab.tsx` |
| Error page | `load-error.tsx` |
| Tests | `src/components/viewer/__tests__/` |

**Tabs.** Episodes, Annotations, 3D Replay (when the robot is supported), Statistics, Frame gallery and **Analysis**. Analysis holds the former Action insights, Filtering and Doctor tabs as a segmented control; each view loads exactly what its tab loaded. A tab id stored by an older session (`insights`, `filtering`, `doctor`) opens Analysis on that view; the view is kept in `sessionStorage` (`analysisView`). The tab bar and the annotation sub-tabs are ds `Tabs` (←/→ between tabs).

**Inspector.** On the Annotations tab a right column (320 px, `inspector.tsx`) shows the selected time segment's form, or on Objects & Tracking the selected object (its facts and Accept / Reject). The panels still render their own forms with the same state and handlers; `InspectorPortal` only moves the form's DOM into the column, and without a column (`useInspectorSlot()` is null) it renders in place. The column collapses to a rail; below 1200 px it is a drawer along the bottom edge that starts collapsed. Escape, Ctrl/⌘+S/Z/Y keep working (they listen on the window).

**Data colours** (time segments, chart series, masks, labels on video) take the palette described under [Data colours](#data-colours), never the accent. Text never takes a data colour: pills, lane names and legends are text colours with a coloured dot or bar beside them.

**Media** is black in both themes (`--ds-media-bg`); each camera tile and the 3D viewport carry `data-theme="dark"` so the controls drawn on them are dark. Labels on video are near-white words on a dark plate with a coloured bar. The 3D background is black.

**Episode list.** Outcome is a shape and words (check circle, crossed circle, empty circle; a ring for a person's label) and stays a button that cycles the label; flag is a pressed toggle; Failures and Flagged are filter chips. The heading row has previous/next episode buttons that do what ↑/↓ do.

**Kept keys.** Space (play/pause), ↑/↓ (episode), Escape and Ctrl/⌘+S/Z/Y in the annotation editor, as before; none fires while typing.

**Feedback.** The loading overlay appears after 300 ms (none for a fast load) with a spinner that stops under reduced motion; spinners elsewhere are the Lucide spinner; errors on the page say what happened, why (technical details) and what to do (Try again, Back to Explore); the dataset-changed card sits bottom left so it never covers the toasts.

ESLint's colour rule covers the viewer's files like all of `src/`; the exceptions are `data-palette.ts` (the palette as numbers for canvas and WebGL) and the URDF viewer (the robot models' material and light colours).

## Pages (stage 4)

Live evaluation (`/live`), Conversion & review (`/workbench`), the training pool (`/pool`), Explore (`/explore`) and the content of the Agent Workbench drawer use the tokens and follow the theme. Their API calls, jobs and data are unchanged; only markup, classes and feedback changed.

| What | Where |
| --- | --- |
| Page styles (classes `pg-*`) | `src/components/pages-ui/pages.css`, imported by the four pages |
| Agent Workbench content styles | `src/components/pages-ui/agent-content.css` (rules under `.levi-agent-sheet`; the drawer itself is the frame's) |
| Feedback pieces | `src/components/pages-ui/feedback.tsx` |
| Tests | `src/components/pages-ui/__tests__/`, `src/components/pool/__tests__/composition-order.test.tsx` |

- **Classes.** The pages' former `levi-*` classes from `levi.css` are restated as `pg-*` with the same layout and token colours; buttons, fields and tables use `ds-btn`, `ds-input` and `ds-table` (or the components). The Agent Workbench content keeps its `levi-agent-*`, `levi-activity-*` and `levi-connection-*` names (they are shared with the drawer); `agent-content.css` restates them under `.levi-agent-sheet`, and its buttons and fields carry `ds-btn` / `ds-input`, with a pressed button (tabs, toggles) drawn as the selected state. Neither stylesheet has a colour literal (tested), and the colour lint rule covers these pages' TSX like all of `src/`. With `levi.css` gone these rules stand alone (no older rule sits behind them); their comments that mention it are historical.
- **Feedback** (candidates for `ds`): `Problem` is the three-part error (what happened, why — usually the server's sentence —, what to do, optional collapsed technical details; `role="alert"`, or `live={false}` for a standing error); `RequestProblem` names the failed action and shows the server's message as the reason; `Note` is an inline info/success/warning note; `JobCard` is the job card every page uses for a running or finished job (status badge, title, meta, then the page's progress and results); `EmptyLine` is a one-line empty state inside a card.
- **Status** is a `Badge` or `StatusDot` (shape, colour and words): pool job states, conversion requirement checks, live session and service states (a running session breathes), episode outcomes. Progress bars are `Progress`.
- **Loading and results.** Skeletons where a layout is known (pool preview, picked episodes, live statistics); results that land away from the action (a recipe saved, records cleared, space freed) are toasts; errors stay next to the action that failed.
- **Training pool task order** is a `ReorderList` (Motion): drag by the handle or press ↑/↓ on it; each task keeps its move-up, move-down and remove buttons.
- **One primary button per screen**: Register & browse (Conversion & review), Start export (training pool; Scan now while the pool was never scanned), Run conversion once a plan exists.
- **Page layouts.** The training pool has a step bar (① Select ② Compose ③ Export, `pool-steps.tsx`: the current step follows the composition and the export, each step jumps to its section) and **Export…** in the title row as the page's primary action (it brings the export form into view and focuses the name; *Scan now* is the primary only while the pool was never scanned); recent jobs start collapsed and point to the top bar's Jobs menu. Live evaluation has a summary row (running sessions, labelled of finished episodes, the most recent error; `live-summary.tsx`) and its "watch only" explanation is an info `Note`. In Conversion & review the wizard's current step owns the one primary button (Inspect, Review plan, Run conversion, then Review converted dataset; none while a conversion runs); the chosen export card is pressed and outlined.
- **Agent Workbench content.** Its sections are ds `Tabs`; Run pilot or Execute remaining is the primary of a run's actions; approving a plan, accepting a pilot and committing changes carry `HumanActionMark` ("Needs your confirmation") and are the primary of their step.
- **Shared components** `hf-auth-button.tsx` (ds buttons, menu and dialog) and `dataset-format.tsx` (a ds badge) bring their styles with them (`pages-ui/shared.css`), so they look the same on every page, the episode viewer included.
- **Helpers.** `pg-small`, `pg-mt-2…6`, `pg-my-2/3`, `pg-full`, `pg-block`, `pg-mono`, `pg-between`, `pg-stack` replace Tailwind spacing and text utilities on these pages: `ds-root` resets heading and paragraph margins outside any layer, so layered utilities lose there (a test keeps them out). `RequestProblem` drops an "Error:" prefix (`cleanMessage`) and takes `onRetry` (Try again).
- **Reduced motion.** Every moving rule of `pages.css` and `agent-content.css` also stops under `data-motion="reduce"` (the app's own switch), tested.
- **Data colour.** The object tool's mask overlay reads `--ds-data-6` from its canvas's CSS colour (canvas cannot read variables).
- **Known gaps**: table cells whose text is cut keep a native `title` with the full text (allowed by the `title=` rule: the cell already shows what matters); the live session list can warn about a duplicate React key when two sessions report the same model and task folder (data-dependent, not changed here).

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
- A disabled control says why in words next to it, tied with `aria-describedby` (the live episode list's removal is the model); the sticky bar never hides a focused element or an anchor (`scroll-padding-top`, `scroll-margin-top`).

## Specimen page

`/design` shows every token and component in light and dark side by side (`?only=light` or `?only=dark` shows one; `?motion=reduce` starts with reduced motion). It is served by `next dev`; in a production server (`next start`, `levi serve`) `src/middleware.ts` answers a plain 404 before routing (so neither the page's metadata nor its styles are sent) unless `LEVI_DESIGN_PAGE=1` is set in its environment (`src/lib/design/gate.ts`). It is not linked from the navigation.

## Migration status

| Area | State |
| --- | --- |
| Frame: top bar, toasts, confirmation, palette, shortcut list, Agent Workbench drawer | On `ds-*` components and tokens (`shell.css`) |
| Home, guide, report | Tokens, `ds-*` components, Lucide icons; the home page and the guide share `home.css` and `reading.css` |
| Live evaluation, Conversion & review, training pool, Explore | `pg-*` styles on tokens with ds buttons, fields, tables, badges and the feedback pieces (`pages-ui/`) |
| Agent Workbench content | Keeps its `levi-agent-*`, `levi-activity-*`, `levi-connection-*` class names (the drawer shares them); `agent-content.css` styles them under `.levi-agent-sheet`; its controls carry `ds-btn` / `ds-input` |
| Episode viewer | Frame, tabs, episode list, playback and inspector on `vw-*` styles and ds components (`viewer/`); the annotation panels, timeline and lanes on `annotations.css`; the analysis views (Action insights, filtering, statistics, overview, doctor) and the 3D viewer lay themselves out with Tailwind utilities whose colours are token values (`text-(--ds-text-secondary)`), not yet `ds-*` components |
| Colour | Tokens only, in CSS and TSX, checked on all of `src/` (two documented exceptions); `levi.css` and the older variables and Tailwind remaps are deleted |
| Icons | Lucide through `Icon`; remaining text arrows and check marks are in prose, keyboard hints and the 3D viewer's HUD |
| Language | Every `t("…")` literal has a key in both catalogues (tested); the catalogues have the same keys |

Gaps that are known and kept on purpose: the data-dependent duplicate React key warning in the live session list (see Pages), and the native `title` on cut-off table cells (see the `title=` rule).

## Testing

`global-styles.test.ts` checks the page base, the sticky bar, the wordmark, the absence of `levi.css`, the old names and Tailwind palette classes; `brand.test.ts` the mark; `route-title.test.ts` the tab titles; `lint-config.test.ts` and `src/__tests__/eslint-hex.test.ts` the colour rule (every source covered, exceptions listed and commented, `rgb()`/`hsl()` caught); `src/i18n/__tests__/catalog.test.ts` the catalogues; `recent.test.ts` and `home-data.test.ts` the home page's data; `report-blocks.test.tsx` the report's sort header (`aria-sort`, icon, tooltip), metric delta icons and state icons.

`bun test` runs the component tests in a DOM (happy-dom, a dev dependency). `tokens-contrast.test.ts` also fails on a colour literal (hex, `rgb()`, `hsl()`) or a Tailwind arbitrary value in the components' or the specimen's TSX. A component test imports `./dom` first and calls `setupDom()`; `render`, `press`, `click`, `focus`, `dropFocus`, `fire`, `flush` and `mockMatchMedia` are there. The DOM globals are removed after each such file, so other tests run without a DOM.
