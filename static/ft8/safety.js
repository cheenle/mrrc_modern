// Bottom state bar and safety controls (NFR-063: never hidden).

import { api } from "./api.js";
import { getState, subscribe } from "./state.js";

const HEARTBEAT_MS = 5000; // §15.4: every 5 s while holding the lease
const COUNTDOWN_TICK_MS = 1000;

// The watchdog ticks every second without broadcasting, so the CQ LOOP
// countdown must run locally between state snapshots.  Module-level
// singletons: the bar is created once and never stacks a second interval.
let countdownTimer = null;
let countdownRemaining = 0;

function formatMmSs(seconds) {
  const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
  const ss = String(seconds % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

export function createSafetyBar() {
  const lamps = {
    cat: document.getElementById("lamp-cat"),
    audio: document.getElementById("lamp-audio"),
    dsp: document.getElementById("lamp-dsp"),
    clock: document.getElementById("lamp-clock"),
    ptt: document.getElementById("ptt-state"),
  };
  const leaseState = document.getElementById("lease-state");
  const sequencerState = document.getElementById("sequencer-state");
  const buttons = {
    cq: document.getElementById("btn-cq"),
    reply: document.getElementById("btn-reply"),
    txOff: document.getElementById("btn-tx-off"),
    clearFault: document.getElementById("btn-clear-fault"),
    lease: document.getElementById("btn-lease"),
    stop: document.getElementById("btn-stop"),
  };

  let heartbeat = null;
  let lastSequencer = null;

  function stopCountdown() {
    if (countdownTimer !== null) {
      clearInterval(countdownTimer);
      countdownTimer = null;
    }
  }

  function startCountdown() {
    if (countdownTimer !== null) return;
    countdownTimer = setInterval(() => {
      if (countdownRemaining > 0) countdownRemaining -= 1;
      sequencerState.textContent = `CQ LOOP ${formatMmSs(countdownRemaining)}`;
      if (countdownRemaining === 0) stopCountdown(); // hold at 0 until a snapshot re-syncs
    }, COUNTDOWN_TICK_MS);
  }

  function render() {
    const { lease, safety, sequencer, selected } = getState();
    const faults = new Set(safety.faults || []);
    for (const [name, lamp] of Object.entries({ cat: lamps.cat, audio: lamps.audio, dsp: lamps.dsp, clock: lamps.clock })) {
      lamp.className = `lamp ${faults.has(name) ? "bad" : "ok"}`;
    }
    lamps.ptt.className = `lamp ${safety.ptt_on ? "on" : ""}`;
    leaseState.textContent = lease.mine ? "CONTROL" : lease.held ? "OBSERVER (lease held)" : "OBSERVER";
    const cqLoop = sequencer.cq_loop;
    if (cqLoop && cqLoop.active) {
      // Only a new state snapshot replaces the sequencer object; decode
      // patches reuse it and must not reset the locally ticking countdown.
      if (sequencer !== lastSequencer) {
        lastSequencer = sequencer;
        countdownRemaining = Math.max(0, Math.floor(cqLoop.idle_remaining_s || 0));
      }
      sequencerState.textContent = `CQ LOOP ${formatMmSs(countdownRemaining)}`;
      startCountdown();
    } else {
      lastSequencer = null;
      stopCountdown();
      if (sequencer.dx_call) {
        sequencerState.innerHTML = `${sequencer.state} <span class="seq-target">→ ${escapeHtml(sequencer.dx_call)}</span>`;
      } else {
        sequencerState.textContent = sequencer.state;
      }
    }
    buttons.cq.disabled = !lease.mine || safety.armed;
    buttons.reply.disabled = !lease.mine || !selected;
    buttons.txOff.disabled = !lease.mine;
    // Recovery path (§15.5): visible only while a fault is latched; the
    // mutation broadcasts a fresh snapshot, so the bar re-renders itself.
    buttons.clearFault.hidden = faults.size === 0;
    buttons.clearFault.disabled = !lease.mine;
    buttons.lease.textContent = lease.mine ? "Release control" : "Take control";
    buttons.lease.disabled = lease.held && !lease.mine;

    if (lease.mine && heartbeat === null) {
      heartbeat = setInterval(() => api.heartbeat(), HEARTBEAT_MS);
    } else if (!lease.mine && heartbeat !== null) {
      clearInterval(heartbeat);
      heartbeat = null;
    }
  }

  buttons.cq.addEventListener("click", () => api.cq(true));
  buttons.reply.addEventListener("click", () => api.reply());
  buttons.txOff.addEventListener("click", () => api.txOff());
  buttons.clearFault.addEventListener("click", () => api.clearFault());
  buttons.lease.addEventListener("click", () => {
    const { lease } = getState();
    return lease.mine ? api.releaseLease() : api.acquireLease();
  });
  // STOP is unconditional and needs no lease (NFR-038).
  buttons.stop.addEventListener("click", () => api.stop());

  subscribe(render);
  render();
}
