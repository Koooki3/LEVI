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
const HEX = "/#[0-9a-fA-F]{3,8}\\b/";
const HEX_MESSAGE =
  "Use a --ds-* token or a ds-* class instead of a hex colour (docs/DESIGN.md).";

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
    files: FRAME_FILES,
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: `Literal[value=${HEX}]`,
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
