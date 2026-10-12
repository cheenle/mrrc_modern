// One platform stream: /WSft8 carries JSON frames (hello/decodes/mode).
//
// Replaces the upstream three-socket design (state/decodes/waterfall): the
// platform's WS handshake carries the auth cookie, so no URL token is used.

import { applyDecodeBatch, applySnapshot, patch } from "./state.js";

const BACKOFF_MS = [500, 1000, 2000, 5000];

let socket = null;
let attempt = 0;
let closed = false;
let handlers = {};

function setConnected(value) {
  patch({ connected: { stream: value } });
}

function dispatch(message) {
  if (!message || typeof message !== "object") return;
  switch (message.type) {
    case "hello":
      patch({ workMode: message.mode });
      if (Array.isArray(message.decodes) && message.decodes.length) {
        applyDecodeBatch({ items: message.decodes });
      }
      break;
    case "decodes":
      applyDecodeBatch({ items: message.items || [] });
      break;
    case "mode":
      patch({ workMode: message.mode });
      handlers.onMode?.(message.mode);
      break;
    default:
      break;
  }
}

function open() {
  if (closed) return;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/WSft8`);
  socket.onopen = () => {
    attempt = 0;
    setConnected(true);
  };
  socket.onmessage = (event) => {
    try {
      dispatch(JSON.parse(event.data));
    } catch {
      /* binary or malformed frame: ignored */
    }
  };
  socket.onclose = (event) => {
    setConnected(false);
    if (closed) return;
    if (event.code === 4401) {
      // Platform session gone — reload lands on the platform login.
      location.reload();
      return;
    }
    const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    attempt += 1;
    setTimeout(open, delay);
  };
  socket.onerror = () => {
    /* onclose follows */
  };
}

export function onModeChange(fn) {
  handlers.onMode = fn;
}

export function startStreams() {
  closed = false;
  open();
}

export function stopStreams() {
  closed = true;
  if (socket) {
    socket.onclose = null;
    socket.close();
    socket = null;
  }
}
