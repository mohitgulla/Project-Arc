/** Clipboard copy that also works on the plain-http tailnet origin (no `navigator.clipboard`). */
export function copyText(text: string): boolean {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      void navigator.clipboard.writeText(text);
      return true;
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}
