// Tiny DOM helpers shared by the app and the result renderers.
// Everything the server sends goes through textContent, never innerHTML.

export const SVG = "http://www.w3.org/2000/svg";

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export const svg = (tag, attrs = {}, text) => {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== undefined) el.textContent = text;
  return el;
};

export const notice = (kind, text) =>
  h("div", { class: `notice ${kind}`, role: kind === "error" ? "alert" : null }, text);

// A short message that fades out by itself (copy confirmations and the like).
export function toast(text) {
  let box = document.getElementById("toasts");
  if (!box) { box = h("div", { id: "toasts", role: "status", "aria-live": "polite" }); document.body.append(box); }
  const t = h("div", { class: "toast" }, text);
  box.append(t);
  setTimeout(() => t.remove(), 2600);
}
