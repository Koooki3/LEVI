/**
 * Runs in <head> before the first paint: puts the theme preference on <html>
 * so the frame never flashes the other theme. A stored "light" or "dark" is
 * applied; a stored "system" leaves `data-theme` off (the tokens follow the
 * media query); no stored value applies THEME_BOOT_DEFAULT. Storage may
 * throw (blocked site data); then the default is applied too.
 *
 * The root layout is a server component and cannot read constants from the
 * client module `@/lib/design/theme`, so the key and the default are repeated
 * here; a test checks that they equal THEME_STORAGE_KEY and
 * THEME_DEFAULT_PREFERENCE. Transitional default: "dark" until design stage 5,
 * then back to "system".
 */
export const THEME_BOOT_KEY = "levi-theme";
export const THEME_BOOT_DEFAULT: "system" | "light" | "dark" = "dark";

export const THEME_BOOT_SCRIPT = `(function(){var d=${JSON.stringify(
  THEME_BOOT_DEFAULT,
)},v=null;try{v=localStorage.getItem(${JSON.stringify(
  THEME_BOOT_KEY,
)});}catch(e){}if(v!=="light"&&v!=="dark"&&v!=="system")v=d;if(v!=="system")document.documentElement.setAttribute("data-theme",v);})();`;
