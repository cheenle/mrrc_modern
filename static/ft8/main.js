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
import { showToast } from "./toast.js";

const cockpit = document.getElementById("cockpit");

async function backToSsb() {
  await api.mode("ssb");
  location.href = "/";
}

// One-shot "deep decode this slot" (Task 14).  A transient runtime event, not a
// persisted setting, so it has its own small endpoint rather than PUT /station.
// api.js is outside this task's file set, so the POST goes out directly: the
// cookie rides same-origin and a dead session reloads, like api.js does.
async function requestDeepSlot() {
  const response = await fetch("/api/ft8/engine/deep-slot", {
    method: "POST",
    credentials: "same-origin",
  });
  if (response.status === 401) {
    location.reload();
    return { ok: false };
  }
  let payload = {};
  try { payload = await response.json(); } catch { /* empty body */ }
  return { status: response.status, ...payload };
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

  // One-shot "deep decode this slot".  Created here (rather than in ft8.html,
  // which is outside this task's file set) and only bites when "Allow «deep
  // decode this slot»" is enabled in the drawer (escalate_on_request).
  const actions = document.querySelector("#state-bar .actions");
  if (actions) {
    const deep = document.createElement("button");
    deep.id = "btn-deep-slot";
    deep.className = "cmd";
    deep.type = "button";
    deep.textContent = "Deep slot";
    deep.title = "对当前时隙多跑一次深度解码（一次性，需在设置里开启）";
    deep.addEventListener("click", async () => {
      deep.disabled = true;
      const result = await requestDeepSlot();
      deep.disabled = false;
      if (!result.ok) showToast(`Deep slot: ${result.reason || result.status}`);
      else if (!result.escalate_on_request)
        showToast("未生效：请在设置里开启「深度解码本槽」");
      else showToast("本时隙将执行深度解码");
    });
    actions.appendChild(deep);
  }

  // 升级率是慢变量：30 s 一次足够，而 15 s 一次会让请求数白翻一倍。
  // 401 由 api.js 的 request() 统一重载处理，这里不重复。
  const engineStatsEl = document.querySelector("#engine-stats");
  if (engineStatsEl) {
    const renderEngineStats = async () => {
      const { status, engine } = await api.engineStats();
      if (status !== 200 || !engine) {
        engineStatsEl.textContent = "—";
        return;
      }
      const pct = (engine.escalation_rate * 100).toFixed(1);
      engineStatsEl.textContent = `升级 ${pct}% · ${engine.escalations}/${engine.decodes}`;
      engineStatsEl.title =
        `时隙 ${engine.slots_started} · 跳过 ${engine.slots_skipped} · ` +
        `快路径 ${engine.fast_decodes} · 升级 ${engine.escalations} · ` +
        `超时 ${engine.deadline_misses} · 错误 ${engine.decode_errors}`;
    };
    renderEngineStats();
    setInterval(renderEngineStats, 30_000);
  }

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
