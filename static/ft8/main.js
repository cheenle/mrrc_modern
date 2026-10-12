// FT8 cockpit bootstrap (platform-hosted): stream, views, slot clock.

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.getRegistrations().then((regs) => regs.forEach((r) => r.unregister()));
}

import { api } from "./api.js";
import { applySnapshot, subscribe } from "./state.js";
import { onModeChange, startStreams } from "./streams.js";
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

  const back = document.getElementById("btn-back-ssb");
  if (back) back.addEventListener("click", (event) => {
    event.preventDefault();
    backToSsb();
  });

  subscribe(({ station, safety, workMode }) => {
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
