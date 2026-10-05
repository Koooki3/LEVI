import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

/**
 * Files that may hold a colour literal, each for a reason. Everything else
 * under `src` uses a `--ds-*` token or a `ds-*` class (docs/DESIGN.md).
 * Keep every entry commented; a new entry needs a reason a token cannot
 * serve (canvas, WebGL and SVG attributes that cannot read CSS variables).
 */
const HEX_EXCEPTIONS = [
  // The viewer's categorical data colours as numbers for canvas and the 3D
  // scene, and the robot models' material and light colours (WebGL takes
  // numbers, and a robot's paint is not a theme colour): the one viewer
  // module with hex; viewer.css holds the CSS side.
  "src/components/viewer/data-palette.ts",
  // Tests build colour strings to assert on them.
  "src/**/__tests__/**",
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
// A colour function with literal numbers: rgb(0 131 0), hsla(120, 50%, 40%, 1).
// Computed ones (`rgb(${r} ${g} ${b})` from a palette) are not matched.
const COLOR_FUNCTION = "/\\b(rgba?|hsla?)\\(\\s*\\d/";
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
    // Every interface source uses `--ds-*` tokens, never a hard-coded
    // colour; the exceptions above say why they differ.
    files: ["src/**/*.{ts,tsx}"],
    ignores: HEX_EXCEPTIONS,
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
        {
          selector: `Literal[value=${COLOR_FUNCTION}]`,
          message: HEX_MESSAGE,
        },
        {
          selector: `TemplateElement[value.raw=${COLOR_FUNCTION}]`,
          message: HEX_MESSAGE,
        },
      ],
    },
  },
];

export default eslintConfig;
