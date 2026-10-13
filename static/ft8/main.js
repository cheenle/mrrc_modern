// FT8 cockpit bootstrap (platform-hosted): stream, views, slot clock.

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations().then((regs) => regs.forEach((r) => r.unregister()));
}

import { api } from "./api.js";
import { applySnapshot, subscribe } from "./state.js";
import { onModeChange, onWaterfallFrame, startStreams } from "./streams.js";
import { createWaterfall, parseFrame } from "./waterfall.js";
import { createCandidates } from "./candidates.js";
import { createSafetyBar } from "./safety.js";
import { createBandSelect } from "./band.js";
import { createSettingsDrawer, loadSettings } from "./settings.js";

const cockpit = document.getElementById("cockpit");

async function backToSsb() {
  await api.mode("ssb");
  location.href = "/";
}

async function boot() {
  cockpit.hidden = false;

  const waterfall = createWaterfall(document.getElementById("waterfall-canvas"));
  onWaterfallFrame((buffer) => {
    const frame = parseFrame(buffer);
    if (frame) waterfall.push(frame);
  });
  createCandidates(document.getElementById("candidate-list"));
  createSafetyBar();
  createBandSelect(document.getElementById("band-select"));
  createSettingsDrawer();
  // Seed FT8 display prefs before the first candidate render.
  applySnapshot({ settings: loadSettings() });

  const snapshot = await api.state();
  if (snapshot.ok) applySnapshot(snapshot);
  startStreams();

  // If another session switches the station back to SSB, follow it.
  onModeChange((mode) => {
    if (mode === "ssb") location.href = "/";
  });

  const start = document.getElementById("btn-ft8-start");
  if (start) start.addEventListener("click", async () => {
    start.disabled = true;
    const result = await api.mode("ft8");
    if (!result.ok) start.disabled = false;
  });

  const adif = document.getElementById("btn-adif-export");
  if (adif) adif.addEventListener("click", () => {
    window.location.href = "/api/ft8/logs/adif";
  });

  const back = document.getElementById("btn-back-ssb");
  if (back) back.addEventListener("click", (event) => {
    event.preventDefault();
    backToSsb();
  });

  subscribe(({ station, safety, workMode, engineRunning }) => {
    // The engine is runtime-only (a restart returns the station to SSB), so
    // say so instead of showing an empty decode list.
    const hint = document.getElementById("ft8-engine-hint");
    if (hint) hint.hidden = engineRunning !== false;
    document.getElementById("station-id").textContent =
      `${(station && station.my_call) || "MRRC FT8"}${safety.armed ? " — TX ARMED" : ""}`;
    const badge = document.getElementById("mode-badge");
    if (badge) badge.textContent = (workMode || "ft8").toUpperCase();
  });

  setInterval(() => {
    const now = new Date();
    document.getElementById("slot-clock").textContent =
      now.toISOString().slice(11, 19) + "Z";
  }, 250);
}

boot();
