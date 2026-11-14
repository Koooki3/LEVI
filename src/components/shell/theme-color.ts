// The browser chrome colour (<meta name="theme-color">): the page background
// of each theme, so the address bar of a phone does not flash a stranger's
// colour above the page. A <meta> tag cannot read CSS variables, which is why
// these two are spelled out; a test keeps them equal to --ds-bg in tokens.css.
export const THEME_COLOR = {
  light: "#f5f5f7",
  dark: "#0b0b0c",
} as const;
