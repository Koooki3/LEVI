// Utility to post a message to the parent window with custom URLSearchParams.
// Standalone browser tabs do not have a parent host to notify; that is a normal
// deployment, not an error. Cross-origin or detached frames are best-effort too.
export function postParentMessageWithParams(
  setParams: (params: URLSearchParams) => void,
): boolean {
  if (typeof window === "undefined" || window.parent === window) return false;

  try {
    const parentOrigin = "https://huggingface.co";
    const searchParams = new URLSearchParams();
    setParams(searchParams);
    window.parent.postMessage(
      { queryString: searchParams.toString() },
      parentOrigin,
    );
    return true;
  } catch {
    return false;
  }
}
