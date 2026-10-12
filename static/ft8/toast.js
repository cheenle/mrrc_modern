// Transient operator feedback: REST rejections must never fail silently.

const TOAST_MS = 3000;
let timer = null;

export function showToast(message) {
  const element = document.getElementById("toast");
  if (!element) return;
  element.textContent = message;
  element.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => {
    element.hidden = true;
  }, TOAST_MS);
}
