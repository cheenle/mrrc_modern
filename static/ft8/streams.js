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
  // One socket carries state/decodes/waterfall; waterfall.js keys its
  // "stale frames" dimming off connected.waterfall.
  patch({ connected: { stream: value, state: value, decodes: value, waterfall: value } });
}

function dispatch(message) {
  if (!message || typeof message !== "object") return;
  switch (message.type) {
    case "hello":
      patch({ workMode: message.mode });
      for (const batch of message.decodes || []) applyDecodeBatch(batch);
      break;
    case "decodes":
      applyDecodeBatch(message);
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
  // Waterfall lines are binary messages: without this they arrive as Blob
  // and every frame is silently dropped (found by the CDP canvas probe).
  socket.binaryType = "arraybuffer";
  socket.onopen = () => {
    attempt = 0;
    setConnected(true);
  };
  socket.addEventListener("message", (event) => {
    if (typeof event.data !== "string" && !window.__ft8WfSeen) {
      window.__ft8WfSeen = true;
      console.log("[ft8] waterfall stream live:", event.data && event.data.byteLength, "bytes/line");
    }
  });
  socket.onmessage = (event) => {
    if (typeof event.data !== "string") {
      // Binary = one WF01 waterfall line (compact spectrum frame).
      if (event.data instanceof ArrayBuffer) handlers.onWaterfall?.(event.data);
      return;
    }
    try {
      dispatch(JSON.parse(event.data));
    } catch {
      /* malformed frame: ignored */
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

export function onWaterfallFrame(fn) {
  handlers.onWaterfall = fn;
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
