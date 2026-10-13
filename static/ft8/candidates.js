// Band Activity list: compact WSJT-X-style decode rows.

import { api } from "./api.js";
import { getState, patch, subscribe } from "./state.js";
import { showToast } from "./toast.js";

function slotUtc(slotId) {
	return new Date(slotId * 15_000).toISOString().slice(11, 19);
}

// Wall-clock instant the decode actually finished, to 0.1 s.  Displaying
// this (instead of the slot start) makes the end-to-end latency visible:
// compare it against the slot start shown in the band separator.
function finishedUtc(epoch) {
	const d = new Date(epoch * 1000);
	return `${d.toISOString().slice(11, 19)}.${Math.floor(d.getUTCMilliseconds() / 100)}`;
}

function latencyTag(seconds) {
	if (seconds === null || seconds === undefined) return "";
	const value = Number(seconds);
	const sign = value >= 0 ? "+" : "";
	return ` ${sign}${value.toFixed(1)}s`;
}

function rowText(c) {
	const snr = `${c.snr > 0 ? "+" : ""}${c.snr}`.padStart(4);
	const dt = `${c.dt >= 0 ? "+" : ""}${Number(c.dt).toFixed(1)}`.padStart(5);
	const freq = String(Math.round(c.freq)).padStart(5);
	// Entity (DXCC country name from the server) trails the message text;
	// empty for own echoes and unknown calls (no trailing space then).
	const entity = c.entity ? ` ${c.entity}` : "";
	// When the decode finished (wall clock, 0.1 s) + how far past the slot end
	// it landed; falls back to the slot start when timing is unavailable.
	const when =
		typeof c.finished_epoch === "number"
			? `${finishedUtc(c.finished_epoch)}${latencyTag(c.finished_after_slot_end_s)}`
			: slotUtc(c.slot_id);
	return `${when} ${snr} ${dt} ${freq} ${c.text}${entity}`;
}

export function createCandidates(listElement) {
	const STALE_AFTER_MS = 10 * 60_000;
	// Native dblclick is unreliable here: every state patch re-renders this
	// list (replaceChildren), and the first click's select() round trip
	// usually lands between the two clicks — the rebuilt row node swallows
	// the dblclick and Reply silently needs several attempts. Detect the
	// double click on the click sequence keyed by row identity instead, so
	// DOM rebuilds between the clicks cannot lose it.
	const DBLCLICK_MS = 600;
	let lastClick = { key: null, t: 0 };

	function separatorText(slotId, freqHz) {
		const utc = slotUtc(slotId);
		return freqHz
			? `── ${(freqHz / 1e6).toFixed(3)} MHz ─ ${utc} UTC ──`
			: `── ${utc} UTC ──`;
	}

	// FT8 display filters (settings drawer): hide non-CQ rows, hide calls
	// already in the log (new-DXCC focus), hide own echoes.
	function visible(candidate, settings, workedCalls) {
		if (settings.showOnlyCQ && !candidate.is_cq) return false;
		if (settings.hideMine && candidate.mine) return false;
		if (settings.hideWorked && candidate.call) {
			// WS decode messages carry the originator under ``call`` (see
			// decode_message_view); ``from_call`` was the parser's internal name
			// and never reached the wire — the filter silently matched nothing.
			const base = String(candidate.call).split("/")[0].toUpperCase();
			if (workedCalls.includes(base)) return false;
		}
		return true;
	}

	function render() {
		const { candidates, selected, radio, settings, station } = getState();
		const now = Date.now();
		const freqHz = radio && radio.freq_hz;
		const workedCalls = (station && station.worked_calls) || [];
		const items = [];
		let previousSlot = null;
		const hidden = settings ? settings : {};
		for (const candidate of candidates) {
			if (!visible(candidate, hidden, workedCalls)) continue;
			if (candidate.slot_id !== previousSlot) {
				previousSlot = candidate.slot_id;
				const separator = document.createElement("li");
				separator.className = "separator";
				separator.textContent = separatorText(candidate.slot_id, freqHz);
				items.push(separator);
			}
			const item = document.createElement("li");
			item.className = "candidate";
			if (candidate.is_cq) item.classList.add("cq");
			if (candidate.is_new_dxcc) item.classList.add("new-dxcc");
			if (candidate.to_me) item.classList.add("to-me");
			if (candidate.mine) item.classList.add("mine");
			// JTDX-style whole-row highlight (server-computed category); the
			// background palette is scheme-independent, text colours still
			// follow the selected scheme.
			if (candidate.highlight) item.classList.add(`hl-${candidate.highlight}`);
			if (candidate.late) item.classList.add("late");
			if (now - (candidate._t || 0) > STALE_AFTER_MS)
				item.classList.add("stale");
			if (selected && selected.call === candidate.call)
				item.classList.add("selected");
			if (hidden.colorScheme === "contrast")
				item.classList.add("scheme-contrast");
			else if (hidden.colorScheme === "minimal")
				item.classList.add("scheme-minimal");
			item.textContent = rowText(candidate);
			item.addEventListener("click", () => {
				const key = `${candidate.slot_id}:${candidate.freq}:${candidate.text}`;
				const now = Date.now();
				if (lastClick.key === key && now - lastClick.t <= DBLCLICK_MS) {
					lastClick = { key: null, t: 0 };
					void reply(candidate);
					return;
				}
				lastClick = { key, t: now };
				void select(candidate);
			});
			items.push(item);
		}
		listElement.replaceChildren(...items);
	}

	async function select(candidate) {
		// Selecting never arms or transmits; it only enables the Reply button.
		let result = await api.select(candidate);
		if (
			!result.ok &&
			result.reason === "lease_required" &&
			!getState().lease.held
		) {
			// WSJT-X-style single tap: a free control lease is taken implicitly so
			// the tap just works. A lease held by another session still rejects —
			// exactly one controller at a time (§10.3, UC-002).
			const acquired = await api.acquireLease();
			result = acquired.ok ? await api.select(candidate) : acquired;
		}
		if (result.ok) {
			patch({ selected: { call: candidate.call, grid: candidate.grid || "" } });
			return true;
		}
		showToast(
			result.reason === "lease_required"
				? "Control is held by another session"
				: `Select rejected: ${result.reason || result.status}`,
		);
		return false;
	}

	async function reply(candidate) {
		// Second click of a double-click = Reply in ONE round trip: the fit deadline is
		// only ~2.2 s into the slot, so the arm must race it, not waste RTTs on
		// a separate select (the lease-less case mirrors select() below).
		let result = await api.reply(candidate);
		if (
			!result.ok &&
			result.reason === "lease_required" &&
			!getState().lease.held
		) {
			const acquired = await api.acquireLease();
			result = acquired.ok ? await api.reply(candidate) : acquired;
		}
		if (result.ok) {
			patch({ selected: { call: candidate.call, grid: candidate.grid || "" } });
			const tx = result.scheduled_tx;
			if (tx && tx.deferred) {
				const utc = tx.utc;
				showToast(
					`Reply armed → TX at ${utc.slice(0, 2)}:${utc.slice(2, 4)}:${utc.slice(4, 6)} UTC`,
				);
			}
			return true;
		}
		showToast(
			result.reason === "lease_required"
				? "Control is held by another session"
				: `Reply rejected: ${result.reason || result.status}`,
		);
		return false;
	}

	subscribe(render);
	// Re-render on a slow tick so quiet-band rows visibly age into "stale".
	setInterval(render, 30_000);
	render();
}
