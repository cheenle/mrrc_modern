// FT8 band selector in the top bar: tunes the rig dial to the selected
// band's FT8 frequency via the lease-gated /radio/band mutation.

import { api } from "./api.js";
import { getState, subscribe } from "./state.js";
import { showToast } from "./toast.js";

// Dial frequencies for the FT8 sub-band on each HF band.
export const FT8_BANDS = [
  { label: "7M", freq_hz: 7_074_000 },
  { label: "10M", freq_hz: 10_136_000 },
  { label: "14M", freq_hz: 14_074_000 },
  { label: "18M", freq_hz: 18_104_000 },
  { label: "21M", freq_hz: 21_074_000 },
  { label: "24M", freq_hz: 24_915_000 },
  { label: "28M", freq_hz: 28_074_000 },
];

const MATCH_HZ = 50_000; // within ±50 kHz counts as the same band
const DEFAULT_INDEX = 2; // 14M

export function createBandSelect(select) {
  for (const { label, freq_hz } of FT8_BANDS) {
    const option = document.createElement("option");
    option.value = String(freq_hz);
    option.textContent = label;
    select.appendChild(option);
  }
  select.value = String(FT8_BANDS[DEFAULT_INDEX].freq_hz);
  select.hidden = false;

  // Reflect the rig's actual dial frequency when a poll lands on a band.
  subscribe(({ radio }) => {
    const f = radio.freq_hz;
    if (typeof f === "number" && f > 0) {
      const match = FT8_BANDS.find((b) => Math.abs(b.freq_hz - f) < MATCH_HZ);
      if (match) select.value = String(match.freq_hz);
    }
  });

  // ---- dial frequency: display, ±1 kHz nudge, click-to-type --------------
  const dial = document.getElementById("dial-freq");
  let dialHz = 0;

  function renderDial() {
    if (!dial || !dialHz) return;
    dial.textContent = (dialHz / 1e6).toFixed(3);
  }

  async function tuneTo(freq_hz) {
    const result = await api.band(Math.round(freq_hz));
    if (result.ok) {
      dialHz = result.freq_hz;
      renderDial();
    } else {
      showToast(`调谐失败：${result.reason || result.status}`);
    }
  }

  subscribe(({ radio }) => {
    const f = radio && radio.freq_hz;
    if (typeof f === "number" && f > 0) {
      dialHz = f;
      renderDial();
      const match = FT8_BANDS.find((b) => Math.abs(b.freq_hz - f) < MATCH_HZ);
      select.value = match ? String(match.freq_hz) : "";
    }
  });

  document.getElementById("tune-down")?.addEventListener("click", () => {
    if (dialHz) void tuneTo(dialHz - 1000);
  });
  document.getElementById("tune-up")?.addEventListener("click", () => {
    if (dialHz) void tuneTo(dialHz + 1000);
  });
  dial?.addEventListener("click", () => {
    const typed = window.prompt("输入频率（kHz）", dialHz ? String(dialHz / 1000) : "");
    if (!typed) return;
    const khz = Number(typed);
    if (!Number.isFinite(khz) || khz < 100 || khz > 60000) {
      showToast("频率需在 100–60000 kHz 之间");
      return;
    }
    void tuneTo(khz * 1000);
  });

  select.addEventListener("change", async () => {
    const freq_hz = Number(select.value);
    let result = await api.band(freq_hz);
    if (!result.ok && result.reason === "lease_required" && !getState().lease.held) {
      // A free control lease is taken implicitly, exactly like a candidate tap.
      const acquired = await api.acquireLease();
      result = acquired.ok ? await api.band(freq_hz) : acquired;
    }
    if (!result.ok) {
      showToast(
        result.reason === "lease_required"
          ? "Control is held by another session"
          : `Band rejected: ${result.reason || result.status}`,
      );
      // Revert to the last known rig frequency, else the default band.
      const f = getState().radio.freq_hz;
      const match =
        typeof f === "number" && f > 0
          ? FT8_BANDS.find((b) => Math.abs(b.freq_hz - f) < MATCH_HZ)
          : undefined;
      select.value = String(match ? match.freq_hz : FT8_BANDS[DEFAULT_INDEX].freq_hz);
    }
  });
}
