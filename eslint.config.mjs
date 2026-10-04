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
];

export default eslintConfig;
