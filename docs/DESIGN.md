# Design system

[中文](DESIGN.zh-CN.md)

LEVI's interface is being redesigned in stages: graphite accent, light and dark themes that follow the system, system fonts only, Lucide icons, Motion for drag and list reordering only. Stage 1 (this document) adds the design tokens, the theme preference and a set of base components. **No existing page uses them yet**: `globals.css`, `levi.css`, `annotations-skin.css` and `report.css` are unchanged, and the existing pages render exactly as before. Pages move over in later stages.

| What | Where |
| --- | --- |
| Tokens (CSS variables, `--ds-*`) | `src/styles/tokens.css` |
| Component styles (classes `ds-*`) | `src/styles/ds.css` |
| Components | `src/components/ds/` (`index.ts` exports all but `ReorderList`) |
| Theme preference | `src/lib/design/theme.ts` |
| Reduced-motion helpers, durations, breakpoints | `src/lib/design/motion.ts` |
| Specimen page (development only) | `/design` (`src/app/design/`) |
| Tests | `src/components/ds/__tests__/`, `src/lib/design/__tests__/` |

## Rules for new code

- **No hard-coded colours.** Use a semantic token (`var(--ds-text-secondary)`, `var(--ds-surface-1)`) or a `ds-*` class. No hex, `rgb()` or `hsl()` in new CSS or TSX; a test fails if `ds.css` or the specimen's CSS has one. Data colours (time segments, masks, chart series) are a separate palette that stage 4 defines.
- **Semantic tokens only in components.** The raw steps `--ds-gray-l-*` / `--ds-gray-d-*` exist only to define the semantic tokens.
- **One primary button per screen.** Accent A ("graphite") is the darkest grey: it is used only for the primary button, the focus ring, selection and progress. Links in running text are primary text with an underline.
- **Status never by colour alone.** Status colours (success, warning, danger, info) appear only on badges, status dots, toasts and notes, always with an icon shape and words (`Badge`, `StatusDot`).
- **Smallest text 12 px** (`--ds-text-caption-size`); weights 400, 500, 600.
- **Icons**: Lucide only, through `Icon` (16 px with stroke 1.75; 20 or 24 px with 1.5). Decorative icons are `aria-hidden`; an icon that carries meaning gets a `label`.
- **No `title=` tooltips.** Use `Tooltip` (hover and keyboard focus); an icon-only button is an `IconButton`, which requires `label` (its `aria-label` and tooltip).
- **No `window.confirm`.** Use `ConfirmDialog` or `useConfirm()` for an irreversible action only; for an expected, undoable deletion, delete and offer Undo in a toast.
- **Text** goes through the locale catalogs (`useLocale().t`, both `en.json` and `zh.json`). Component defaults (Close, Cancel, Loading, In progress, Dismiss notification, Notifications, Move, Theme, System, Light, Dark) are already there.

## Tokens

All tokens are CSS custom properties on `:root`, named `--ds-*`, so they never clash with the older `--bg`, `--accent` or `--surface-*` variables.

| Group | Tokens |
| --- | --- |
| Neutral scales | light `--ds-gray-l-0…11`, dark `--ds-gray-d-0…12` (cool neutral greys, proposal §4.1) |
| Surfaces | `--ds-bg`, `--ds-bg-reading`, `--ds-surface-sunken`, `--ds-surface-1` (card), `--ds-surface-2` (raised: menus, dialogs), `--ds-surface-hover`, `--ds-surface-selected`, `--ds-field-bg`, `--ds-skeleton`, `--ds-media-bg` (black in both themes), `--ds-scrim` |
| Lines | `--ds-separator`, `--ds-separator-strong`, `--ds-border-control`, `--ds-border-control-on-raised` |
| Text | `--ds-text-primary`, `--ds-text-secondary`, `--ds-text-tertiary`, `--ds-text-tertiary-on-sunken`, `-on-hover`, `-on-selected`, `-on-raised`, `--ds-text-disabled`, `--ds-icon` |
| Accent A | `--ds-accent`, `--ds-accent-hover`, `--ds-on-accent`, `--ds-focus-ring`, `--ds-selected-indicator`, `--ds-progress`, `--ds-progress-track` |
| Status | `--ds-success`, `--ds-warning`, `--ds-danger`, `--ds-info`, each with `-bg`; `--ds-on-danger` |
| Depth and material | `--ds-shadow-1…3`, `--ds-ring-raised` (dark), `--ds-material-bar`, `--ds-material-filter` |
| Type | `--ds-font-sans`, `--ds-font-mono`; `--ds-text-{display,title-1,title-2,title-3,body,reading,callout,caption}-{size,line}`; `--ds-weight-{regular,medium,semibold}`; `--ds-tracking-{title,display}` |
| Shape and space | `--ds-radius-{xs,sm,md,lg,full}` (4, 6, 10, 14, 999 px); `--ds-space-{0-5,1,2,3,4,5,6,8,10,12,16}` (4 px base); `--ds-control-{sm,md,lg}` (28, 32, 40 px); `--ds-hit-min`; `--ds-icon-{sm,md,lg}` |
| Motion | `--ds-dur-{instant,fast,base,base-exit,slow,slow-exit}` (0, 120, 200, 140, 320, 220 ms); `--ds-ease-{standard,exit,spring}`; `--ds-motion-{shift,toast-shift,dialog-scale,press-scale,drawer-shift}` |
| Layers | `--ds-z-{base,sticky,dock,popover,overlay,dialog,toast}` (0–60) |

Breakpoints (640, 900, 1200, 1440 px) are constants in `src/lib/design/motion.ts` (`BREAKPOINT`), since media queries cannot read variables.

**Contrast.** Text pairs reach 4.5:1 and control boundaries, focus ring, selection bar and progress reach 3:1 in both themes; `tokens-contrast.test.ts` computes them from `tokens.css` with the WCAG formula. Three pairs the proposal rules out are replaced by dedicated tokens: tertiary text on sunken and hover surfaces (light) and on raised surfaces (dark), and control borders on raised surfaces (dark). Inside a `Card variant="sunken"`, a menu, dialog, sheet, tooltip or toast these swaps happen by themselves (`ds-on-sunken`, `ds-on-raised`); in a hovered or selected table row too. Elsewhere, add the class `ds-on-sunken` or `ds-on-raised` to a container drawn on those surfaces.

**Layers.** Neighbouring surfaces differ by one or two grey steps; cards always have a 1 px separator border (cards are only 1.09:1 against the light background). Shadows are for light mode; dark mode uses lighter surfaces and an inner hairline. The glass material (`ds-material`) is only for layers floating over content (top bar, floating toolbars); under `prefers-reduced-transparency` or `prefers-contrast: more` it becomes opaque.

## Themes

Light is the default. Dark applies under `prefers-color-scheme: dark` unless an ancestor has `data-theme="light"`, and always under `data-theme="dark"`. `data-theme` works on any element, so a subtree can be themed alone (the specimen shows both themes side by side this way). `prefers-contrast: more` raises separators, control borders and secondary text one step.

`useThemePreference()` returns `{ preference, resolved, setPreference }`: `"system" | "light" | "dark"`, stored in `localStorage` under `levi-theme` (`"system"` removes the key). Reads and writes go through `browserStorage`, which never throws; without storage the preference is "system". The hook follows system changes and other tabs. It does **not** touch `<html>`: call `applyTheme(element, preference)` where the theme should apply. Stage 2 applies it to `<html>` once the pages use the tokens; until then the existing pages stay dark. `ThemePicker` is the System / Light / Dark control.

Add `ds-root` to a container to give it the design-system font, text colour and background, and `color-scheme` that follows the theme.

## Components

Import from `@/components/ds`; the styles need `@/styles/tokens.css` and `@/styles/ds.css` imported once (the specimen page does; stage 2 moves them into the root layout).

| Component | Notes |
| --- | --- |
| `Button` | `variant` primary / secondary (default) / ghost / danger; `size` sm / md / lg (28 / 32 / 40 px); `loading` (spinner, `aria-busy`, clicks ignored, label kept); `icon`, `iconEnd`; `type="button"` by default |
| `IconButton` | Required `label` (accessible name and tooltip); `shortcut`; `pressed` for toggles (`aria-pressed`) |
| `Icon` | Lucide icon at `size` sm / md / lg; `label` makes it `role="img"` |
| `Tooltip` | Opens on hover after 0.5 s and at once on keyboard focus (not on click); Escape, pointer leave and focus loss close it; sets `aria-describedby` on the trigger |
| `Field`, `Input`, `Textarea`, `Select` | `Field` wires label, hint and error (`aria-describedby`, `aria-invalid`, `required`) to the control inside; `Select` is a native select |
| `Checkbox`, `Radio`, `RadioGroup`, `Switch` | Native inputs; `Checkbox indeterminate`; `RadioGroup` is a fieldset with a legend; `Switch` is a checkbox with `role="switch"` |
| `Badge`, `StatusDot`, `Tag` | Status as icon shape + colour + words (`tone` neutral, success, warning, danger, info); `StatusDot live` breathes once every 2 s (still under reduced motion); `Tag onRemove` gets a "Remove …" button |
| `Card`, `Divider`, `Kbd` | `Card` `variant` default / sunken / raised, `padding` compact / regular, optional `title`, `description`, `actions` |
| `Dialog`, `Sheet` | Modal (`aria-modal`), labelled by the title; focus moves in, Tab wraps, Escape and the scrim close, focus returns to the opener; `container` renders into another element (portal); `Sheet side` right / left / bottom |
| `ConfirmDialog`, `useConfirm` | `role="alertdialog"`; the title names action and object, `confirmLabel` is a verb; `tone="danger"` puts focus on Cancel; `const { confirm, dialog } = useConfirm()` then `await confirm({...})` replaces `window.confirm` |
| `ToastProvider`, `useToast` | Bottom right, three at a time; `show({ title, description, tone, action, duration })`; polite region, errors (`danger`) in an assertive region and they stay; others hide after 4 s, paused while hovered or focused; outside a provider `show` does nothing |
| `Skeleton`, `SkeletonText` | Static blocks (no shimmer), hidden from assistive technology; mark the loading region `aria-busy="true"` |
| `Progress`, `Spinner` | `Progress value={n}` is determinate; `value={null}` indeterminate (a moving bar, or still with "In progress" under reduced motion); `Spinner` is `role="status"`; neither is a dialog |
| `EmptyState` | Icon, one sentence, the next step as a button (`action`, `secondaryAction`) |
| `Tabs` | ARIA tabs: one tab stop, ←/→, Home/End, disabled tabs skipped; switching is instant |
| `SegmentedControl` | Radio group drawn as joined buttons; arrows move and select |
| `Menu` | Menu button: Enter/Space/↓ open on the first item, ↑ on the last; ↑/↓/Home/End; Enter/Space choose; Escape closes and returns focus; Tab and a click outside close |
| `Table`, `TableRow` | Sunken header, row separators, hover; `TableRow selected` (grey fill, weight 500, 2 px bar, `aria-current`); `ds-num` for right-aligned tabular numbers; the wrapper scrolls sideways |
| `ThemePicker` | System / Light / Dark |
| `ReorderList` | Import from `@/components/ds/ReorderList` (lazily with `next/dynamic` where possible): drag by the handle, or focus the handle and press ↑/↓/Home/End; each move is announced; `moveItem`, `DS_SPRING`, `dsLayoutTransition` are exported |

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

Under `prefers-reduced-motion: reduce` the tokens `--ds-dur-base`, `--ds-dur-slow` (and their exit values) become 0, `--ds-ease-spring` becomes `linear` and all movement tokens become none; spinners, the indeterminate bar and the breathing dot stop. `data-motion="reduce"` on an ancestor does the same (for previews and tests). Components that render differently use `usePrefersReducedMotion()`; `ReorderList` uses Motion's `useReducedMotion` and `MotionConfig reducedMotion="user"`. Exits are instant in stage 1 (no exit animation).

No infinite decorative animation, parallax, scroll hijacking, animation longer than 400 ms or animation that blocks input.

**Motion** (the `motion` package, formerly Framer Motion, MIT) is used only in `ReorderList`. It is about 45 KB gzipped and is loaded only by pages that import that component.

## Accessibility

- Focus ring: 2 px `--ds-focus-ring`, offset 2 px (class `ds-focus` on every interactive component); in Windows high contrast it uses `Highlight`.
- Every overlay traps focus, closes on Escape and returns focus; menus and tabs follow the ARIA patterns; toasts use one polite and one assertive live region.
- Hit targets are at least 24 × 24 px.

## Specimen page

`/design` shows every token and component in light and dark side by side (`?only=light` or `?only=dark` shows one; `?motion=reduce` starts with reduced motion). It is served by `next dev`; a production server (`next start`, `levi serve`) answers 404 unless `LEVI_DESIGN_PAGE=1` is set in its environment. It is not linked from the navigation.

## Testing

`bun test` runs the component tests in a DOM (happy-dom, a dev dependency). A component test imports `./dom` first and calls `setupDom()`; `render`, `press`, `click`, `focus`, `fire`, `flush` and `mockMatchMedia` are there. The DOM globals are removed after each such file, so other tests run without a DOM.
