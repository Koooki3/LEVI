/**
 * Runs in <head> before the first paint: puts a stored "light" or "dark"
 * preference on <html> so the frame never flashes the other theme. "system"
 * (no stored value) leaves `data-theme` off and the tokens follow the
 * media query. Storage may throw (blocked site data); then nothing happens.
 *
 * The root layout is a server component and cannot read constants from the
 * client module `@/lib/design/theme`, so the key is repeated here; a test
 * checks that it equals THEME_STORAGE_KEY.
 */
export const THEME_BOOT_KEY = "levi-theme";

export const THEME_BOOT_SCRIPT = `(function(){try{var v=localStorage.getItem(${JSON.stringify(
  THEME_BOOT_KEY,
)});if(v==="light"||v==="dark")document.documentElement.setAttribute("data-theme",v);}catch(e){}})();`;
