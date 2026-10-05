import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

/** Files of the global frame (design stage 2) that must not hard-code colours. */
const FRAME_FILES = [
  "src/app/layout.tsx",
  "src/components/levi-header.tsx",
  "src/components/loading-component.tsx",
  "src/components/agent-workbench.tsx",
  "src/components/live/live-nav.tsx",
  "src/components/shell/**/*.{ts,tsx}",
  // Design stage 5 (global layer): home, guide, report, the token hook.
  "src/app/page.tsx",
  "src/app/guide/**/*.{ts,tsx}",
  "src/app/report/**/*.{ts,tsx}",
  "src/components/home/**/*.{ts,tsx}",
  "src/components/report/**/*.{ts,tsx}",
  "src/lib/design/**/*.{ts,tsx}",
];
// The episode viewer (design stage 3). Its data colours live in
// src/components/viewer/data-palette.ts (the one viewer file with hex: canvas
// and the 3D scene cannot read CSS variables) and in viewer.css; the URDF
// viewer keeps the robot models' material colours.
const VIEWER_FILES = [
  "src/app/[[]org]/[[]dataset]/[[]episode]/*.tsx",
  "src/components/viewer/*.tsx",
  "src/components/action-insights-panel.tsx",
  "src/components/anchored-review-section.tsx",
  "src/components/annotation-recorder.tsx",
  "src/components/annotations-panel.tsx",
  "src/components/annotations-timeline.tsx",
  "src/components/data-recharts.tsx",
  "src/components/dataset-update-notice.tsx",
  "src/components/draggable-popup.tsx",
  "src/components/fast-segmentation-panel.tsx",
  "src/components/filtering-panel.tsx",
  "src/components/levi-doctor.tsx",
  "src/components/levi-review.tsx",
  "src/components/live-segmentation-canvas.tsx",
  "src/components/object-annotation-panel.tsx",
  "src/components/overview-panel.tsx",
  "src/components/playback-bar.tsx",
  "src/components/raw-capture-notice.tsx",
  "src/components/recap-value-section.tsx",
  "src/components/side-nav.tsx",
  "src/components/simple-videos-player.tsx",
  "src/components/stats-panel.tsx",
  "src/components/subtask-vocabulary.tsx",
  "src/components/urdf-playback-bar.tsx",
  "src/components/video-overlay-canvas.tsx",
];
// A colour: # and exactly 3, 4, 6 or 8 hex digits, at the start of the
// string or after a space, "(", "," or ":", and not followed by another
// word character. "#heading", "#12345" and "url#abc" are not colours.
const HEX =
  "/(^|[\\s(,:])#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})(?![\\w-])/";
// Links and ids are never colours, even "#add" or "#face": the value of an
// href/to/id/htmlFor attribute or property is not checked.
const LINK_ATTR = "/^(href|to|id|htmlFor|hash)$/";
const NOT_LINK = `:not(JSXAttribute[name.name=${LINK_ATTR}] > Literal):not(Property[key.name=${LINK_ATTR}] > Literal)`;
const HEX_MESSAGE =
  "Use a --ds-* token or a ds-* class instead of a hex colour (docs/DESIGN.md).";

// Stage 4 pages (Live evaluation, Conversion & review, Training pool,
// Explore) and the Agent Workbench content: the same hex rule as the frame.
const PAGE_FILES = [
  "src/app/live/**/*.tsx",
  "src/app/workbench/**/*.tsx",
  "src/app/pool/**/*.tsx",
  "src/app/explore/**/*.tsx",
  "src/components/live/**/*.tsx",
  "src/components/conversion/**/*.tsx",
  "src/components/pool/**/*.tsx",
  "src/components/pages-ui/**/*.tsx",
  "src/components/agent-*.tsx",
  "src/components/ollama-*.tsx",
  "src/components/chip-multi-select.tsx",
  "src/components/hf-auth-button.tsx",
  "src/components/dataset-format.tsx",
];

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      // Allow `any` type as warning - core types are implemented, peripheral areas still need typing
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
  {
    // Design stage 2: the global frame uses `--ds-*` tokens, never a
    // hard-coded colour. Older pages follow in stage 6 (docs/DESIGN.md).
    files: [...FRAME_FILES, ...PAGE_FILES],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: `Literal[value=${HEX}]${NOT_LINK}`,
          message: HEX_MESSAGE,
        },
        {
          selector: `TemplateElement[value.raw=${HEX}]`,
          message: HEX_MESSAGE,
        },
      ],
    },
  },
  {
    // Design stage 3: the episode viewer, the same rule.
    files: VIEWER_FILES,
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: `Literal[value=${HEX}]${NOT_LINK}`,
          message: HEX_MESSAGE,
        },
        {
          selector: `TemplateElement[value.raw=${HEX}]`,
          message: HEX_MESSAGE,
        },
      ],
    },
  },
];

export default eslintConfig;
