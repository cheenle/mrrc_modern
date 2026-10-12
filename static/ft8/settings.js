// Left settings drawer: Radio (rig levels/passband), FT8 (decode/display),
// Station (call & grid), Log (QSO history + ADIF).  FT8 preferences persist
// in localStorage and drive the candidate list filters/colours.

import { api } from "./api.js";
import { getState, patch, subscribe } from "./state.js";
import { showToast } from "./toast.js";

const STORAGE_KEY = "mrrc-ft8.settings";

// Defaults mirroring WSJT-X common values.
export const DEFAULTS = {
	decodeDepth: "fast", // fast | deep
	colorScheme: "classic", // classic | contrast | minimal
	showOnlyCQ: false, // hide non-CQ rows
	hideWorked: false, // hide calls already in the log (new-DXCC focus)
	hideMine: false, // hide own echoes
};

export function loadSettings() {
	let stored = {};
	try {
		stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
	} catch {
		/* corrupt storage -> defaults */
	}
	return { ...DEFAULTS, ...stored };
}

export function saveSettings(partial) {
	const next = { ...loadSettings(), ...partial };
	localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
	patch({ settings: next });
	return next;
}

// Rig mutations need the control lease; take it implicitly like candidate
// taps do (UC-002).  A lease held by another session stays rejected.
async function ensureLease() {
	if (getState().lease.mine) return true;
	if (getState().lease.held) return false;
	const acquired = await api.acquireLease();
	return Boolean(acquired.ok);
}

export function createSettingsDrawer() {
	const backdrop = document.getElementById("drawer-backdrop");
	const drawer = document.getElementById("settings-drawer");
	const content = document.getElementById("drawer-content");
	const tabs = Array.from(document.querySelectorAll("#drawer-tabs .tab"));
	const btnMenu = document.getElementById("btn-menu");
	const btnClose = document.getElementById("btn-drawer-close");
	let activeTab = "radio";

	// Seed the settings into state so the candidate list can filter.
	if (!getState().settings) patch({ settings: loadSettings() });

	// 后端持久化设置覆盖 localStorage 默认（auto_call_new_dxcc 权威在后端）。
	api.settings().then((res) => {
		if (res.ok && res.settings) {
			const merged = { ...loadSettings(), ...res.settings };
			localStorage.setItem(STORAGE_KEY, JSON.stringify(merged));
			patch({ settings: merged });
		}
	});

	function open() {
		backdrop.hidden = false;
		drawer.hidden = false;
		document.body.classList.add("drawer-open");
		renderTab(activeTab);
	}
	function close() {
		backdrop.hidden = true;
		drawer.hidden = true;
		document.body.classList.remove("drawer-open");
	}

	function switchTab(tab) {
		if (tab === "log") {
			close(); // leave the settings drawer
			openLogView(); // full-screen QSO log overlay
			return;
		}
		if (tab === "dxcc") {
			close(); // leave the settings drawer
			openDxccView(); // full-screen DXCC stats overlay
			return;
		}
		activeTab = tab;
		for (const t of tabs) t.classList.toggle("active", t.dataset.tab === tab);
		renderTab(tab);
	}

	// ---- tab renderers ---------------------------------------------------

	async function renderRadio() {
		content.innerHTML = "<p class='drawer-hint'>Loading rig settings…</p>";
		let mode = null;
		let rigUp = true;
		const levelRes = await api.rigLevels();
		// Sequential: both hit the same RigClient; concurrent requests interleave
		// with the level-query timeout/drop and corrupt the mode read (502).
		const modeRes = await api.rigMode();
		const levels = levelRes.ok ? levelRes.levels || {} : {};
		if (modeRes.ok) mode = modeRes;
		else rigUp = modeRes.status === 503 ? false : rigUp;

		// FT-710's hamlib model does not answer the ``L <name>`` query, so level
		// reads come back None — fall back to typical defaults; writes still work.
		const fallback = (key, def) => (levels[key] == null ? def : levels[key]);
		const agcMap = [
			[0, "OFF"],
			[2, "FAST"],
			[5, "MED"],
			[3, "SLOW"],
			[6, "AUTO"],
		];
		const agcValue = fallback("AGC", 6);

		// FT-710 USB/LSB filter bandwidths (Hamlib rig model 1049).
		const passbands = [1800, 2400, 3000];
		const currentPassband = mode && mode.passband_hz;
		const currentMode = (mode && mode.mode) || "USB";
		const html = [];
		html.push("<h3>Radio</h3>");
		if (!rigUp) {
			html.push(
				"<p class='drawer-hint dim'>rigctld unreachable — controls unavailable</p>",
			);
		}

		// Filter bandwidth selector (raw CAT SH via /radio/filter; hamlib's
		// M-passband path is broken on the FT-710 with hamlib 4.6.2).
		html.push(`<label class="setting-row">
      <span>Mode</span>
      <b>${currentMode}</b>
    </label>`);
		html.push(`<label class="setting-row">
      <span>Filter bandwidth</span>
      <select data-mode-passband ${rigUp ? "" : "disabled"}>
        ${passbands
					.map(
						(hz) =>
							`<option value="${hz}" ${hz === currentPassband ? "selected" : ""}>
            ${(hz / 1000).toFixed(1)} kHz</option>`,
					)
					.join("")}
      </select>
    </label>`);

		// ATT: FT-710 attenuator is 6/12/18 dB (not a plain on/off).
		const attValue = fallback("ATT", 0);
		html.push(`<label class="setting-row">
      <span>Attenuator</span>
      <select data-level="ATT" ${rigUp ? "" : "disabled"}>
        ${[0, 6, 12, 18]
					.map(
						(db) =>
							`<option value="${db}" ${db === attValue ? "selected" : ""}>${db ? `${db} dB` : "Off"}</option>`,
					)
					.join("")}
      </select>
    </label>`);

		// PREAMP: 10/20 dB (0 = off).
		const preampValue = fallback("PREAMP", 0);
		html.push(`<label class="setting-row">
      <span>Preamp</span>
      <select data-level="PREAMP" ${rigUp ? "" : "disabled"}>
        ${[0, 10, 20]
					.map(
						(db) =>
							`<option value="${db}" ${db === preampValue ? "selected" : ""}>${db ? `${db} dB` : "Off"}</option>`,
					)
					.join("")}
      </select>
    </label>`);

		// AGC: FT-710 discrete modes (0=OFF 2=FAST 5=MED 3=SLOW 6=AUTO).
		html.push(`<label class="setting-row">
      <span>AGC</span>
      <select data-level="AGC" ${rigUp ? "" : "disabled"}>
        ${agcMap
					.map(
						([v, label]) =>
							`<option value="${v}" ${v === agcValue ? "selected" : ""}>${label}</option>`,
					)
					.join("")}
      </select>
    </label>`);

		// RF Gain: 0..1.0.
		const rfValue = fallback("RF", 1.0);
		html.push(`<label class="setting-row">
      <span>RF Gain <b class="val">${rfValue == null ? "—" : Math.round(rfValue * 100)}%</b></span>
      <input type="range" data-level="RF" min="0" max="1" step="0.01"
        value="${rfValue ?? 1}" ${rigUp ? "" : "disabled"}>
    </label>`);

		html.push(`<div class="drawer-hint dim">Changes apply immediately.
      TX must be off. Unsupported items stay greyed out.</div>`);
		content.innerHTML = html.join("");

		const passbandSelect = content.querySelector("[data-mode-passband]");
		if (passbandSelect) {
			passbandSelect.addEventListener("change", async () => {
				const ok = await ensureLease();
				if (!ok) {
					showToast("Control is held by another session");
					return;
				}
				const hz = Number(passbandSelect.value);
				const result = await api.rigFilter(hz);
				if (!result.ok) {
					// Fallback: the mode-set path applies the width too (best effort).
					const fallback = await api.rigModeSet(currentMode, hz);
					if (!fallback.ok) {
						showToast(
							`Filter: ${result.status} — ${result.reason || "unavailable"}`,
						);
					} else {
						showToast(`Filter → ${(hz / 1000).toFixed(1)} kHz`);
					}
				} else {
					showToast(`Filter → ${(hz / 1000).toFixed(1)} kHz`);
				}
				// Read back so the drawer reflects the rig's actual passband.
				const modeRes = await api.rigMode();
				if (modeRes.ok && modeRes.passband_hz) {
					passbandSelect.value = String(modeRes.passband_hz);
				}
			});
		}
		for (const input of content.querySelectorAll(
			"select[data-level], input[data-level]",
		)) {
			const level = input.dataset.level;
			input.addEventListener("change", async () => {
				const ok = await ensureLease();
				if (!ok) {
					showToast("Control is held by another session");
					return;
				}
				const value =
					input.type === "checkbox"
						? input.checked
							? 1
							: 0
						: Number(input.value);
				const result = await api.rigLevel(level, value);
				if (!result.ok)
					showToast(`Rig ${level}: ${result.reason || result.status}`);
				else showToast(`${level} → ${value}`);
			});
		}
	}

	function renderFt8() {
		const s = loadSettings();
		content.innerHTML = `
      <h3>FT8</h3>
      <label class="setting-row">
        <span>Decode depth</span>
        <select data-setting="decodeDepth">
          <option value="fast" ${s.decodeDepth === "fast" ? "selected" : ""}>Fast</option>
          <option value="deep" ${s.decodeDepth === "deep" ? "selected" : ""}>Deep</option>
        </select>
      </label>
      <label class="setting-row">
        <span>Colour scheme</span>
        <select data-setting="colorScheme">
          <option value="classic" ${s.colorScheme === "classic" ? "selected" : ""}>Classic</option>
          <option value="contrast" ${s.colorScheme === "contrast" ? "selected" : ""}>High contrast</option>
          <option value="minimal" ${s.colorScheme === "minimal" ? "selected" : ""}>Minimal</option>
        </select>
      </label>
      <label class="setting-row toggle">
        <span>Show only CQ calls</span>
        <input type="checkbox" data-setting="showOnlyCQ" ${s.showOnlyCQ ? "checked" : ""}>
      </label>
      <label class="setting-row toggle">
        <span>Hide already-worked calls</span>
        <input type="checkbox" data-setting="hideWorked" ${s.hideWorked ? "checked" : ""}>
      </label>
      <label class="setting-row toggle">
        <span>Hide my own echoes</span>
        <input type="checkbox" data-setting="hideMine" ${s.hideMine ? "checked" : ""}>
      </label>
      <label class="setting-row toggle">
        <span>Auto-call new DXCC</span>
        <input type="checkbox" data-setting="auto_call_new_dxcc" ${s.auto_call_new_dxcc ? "checked" : ""}>
      </label>
      <label class="setting-row toggle">
        <span>Auto band hunt (DXCC)</span>
        <input type="checkbox" data-setting="auto_band_hunt" ${s.auto_band_hunt ? "checked" : ""}>
      </label>
      <a href="#" class="setting-row" data-bandhunt-link>
        <span>New-DXCC spots dashboard</span><span class="qso-meta">→</span>
      </a>
      <div class="drawer-hint dim">These display preferences are stored in this
        browser and apply immediately to the Band Activity list.</div>`;
		content
			.querySelector("[data-bandhunt-link]")
			?.addEventListener("click", (event) => {
				event.preventDefault();
				openBandHuntView();
			});
		for (const input of content.querySelectorAll("[data-setting]")) {
			input.addEventListener("change", () => {
				const value = input.type === "checkbox" ? input.checked : input.value;
				saveSettings({ [input.dataset.setting]: value });
				// auto_call_new_dxcc / auto_band_hunt 的权威状态在后端
				// （无人值守自动呼叫/自动切频读它们）。
				if (input.dataset.setting === "auto_call_new_dxcc") {
					api.putSetting("auto_call_new_dxcc", Boolean(value)).then((res) => {
						if (!res.ok)
							showToast(`Auto-call setting: ${res.reason || res.status}`);
					});
				} else if (input.dataset.setting === "auto_band_hunt") {
					api.putSetting("auto_band_hunt", Boolean(value)).then((res) => {
						if (!res.ok)
							showToast(`Band hunt setting: ${res.reason || res.status}`);
					});
				}
			});
		}
	}

	function renderStation() {
		const { station } = getState();
		const call = station?.my_call || "—";
		const grid = station?.my_grid || "—";
		const worked = station?.worked_calls || [];
		content.innerHTML = `
      <h3>Station</h3>
      <div class="setting-row"><span>Call sign</span><b>${call}</b></div>
      <div class="setting-row"><span>Grid square</span><b>${grid}</b></div>
      <div class="setting-row"><span>Worked calls</span><b>${worked.length}</b></div>
      <div class="drawer-hint dim">Call &amp; grid are configured on the server
        (MRRC_FT8_MY_CALL / MRRC_FT8_MY_GRID). Worked calls come from the QSO
        log and drive the “hide already-worked” filter.</div>`;
	}

	// Devices: station hardware (hamlib rig + audio, spec 2026-08-10).
	// Save persists server-side without restart; Apply & Restart relaunches
	// rigctld + the server (~20 s disconnect).
	async function renderDevices() {
		content.innerHTML = "<p class='drawer-hint'>Loading device settings…</p>";
		const res = await api.devices();
		if (!res.ok) {
			content.innerHTML =
				"<p class='drawer-hint'>Device settings unavailable.</p>";
			return;
		}
		const {
			config = {},
			source = {},
			audio_devices = [],
			serial_devices = [],
			curated_rig_models = [],
			baud_rates = [],
		} = res;
		const form = {
			rig_model: Number(config.rig_model ?? 1049),
			rig_device: String(config.rig_device ?? ""),
			rig_baud: Number(config.rig_baud ?? 38400),
			rig_stop_bits: Number(config.rig_stop_bits ?? 1),
			rig_mode: String(config.rig_mode ?? "USB"),
			rigctld_port: Number(config.rigctld_port ?? 4532),
			audio_device: config.audio_device ?? null,
			audio_in_device: config.audio_in_device ?? config.audio_device ?? null,
			audio_out_device: config.audio_out_device ?? config.audio_device ?? null,
			audio_in_channel: Number(config.audio_in_channel ?? 0),
		};
		const customModel = !curated_rig_models.some(
			(m) => m.model === form.rig_model,
		);
		const modelOptions =
			curated_rig_models
				.map(
					(m) =>
						`<option value="${m.model}" ${m.model === form.rig_model ? "selected" : ""}>${m.name} (${m.model})</option>`,
				)
				.join("") +
			`<option value="custom" ${customModel ? "selected" : ""}>Custom…</option>`;
		// A device not in the enumeration (e.g. /dev/cu.SLAB_USBtoUART) is
		// entered manually via the Custom… select option.
		const serialCustom =
			form.rig_device && !serial_devices.includes(form.rig_device);
		const serialOptions = serial_devices
			.map(
				(d) =>
					`<option value="${d}" ${d === form.rig_device ? "selected" : ""}>${d}</option>`,
			)
			.join("");
		const audioOption = (d, current) =>
			`<option value="${d.index}" ${String(d.index) === String(current ?? "") ? "selected" : ""}>${d.name}</option>`;
		const audioInOptions =
			`<option value="">System default</option>` +
			audio_devices
				.filter((d) => d.max_input > 0)
				.map((d) => audioOption(d, form.audio_in_device))
				.join("");
		const audioOutOptions =
			`<option value="">System default</option>` +
			audio_devices
				.filter((d) => d.max_output > 0)
				.map((d) => audioOption(d, form.audio_out_device))
				.join("");
		const sourceLine = (key) => `${key}: ${source[key] || "default"}`;
		content.innerHTML = `
      <h3>Devices</h3>
      <label class="setting-row">
        <span>Rig model (hamlib)</span>
        <select data-device-model>${modelOptions}</select>
      </label>
      <div class="device-custom" ${customModel ? "" : "hidden"}>
        <label class="setting-row">
          <span>Custom model number</span>
          <input data-device-model-custom type="number" min="1" value="${form.rig_model}">
        </label>
      </div>
      <label class="setting-row">
        <span>CAT serial device</span>
        <select data-device-serial>
          <option value="">—</option>${serialOptions}<option value="custom" ${serialCustom ? "selected" : ""}>Custom…</option>
        </select>
      </label>
      <div class="device-custom-serial" ${serialCustom ? "" : "hidden"}>
        <label class="setting-row">
          <span>Custom serial path</span>
          <input data-device-serial-custom type="text" value="${form.rig_device}">
        </label>
      </div>
      <label class="setting-row">
        <span>Baud rate</span>
        <select data-device-baud>
          ${baud_rates.map((b) => `<option value="${b}" ${b === form.rig_baud ? "selected" : ""}>${b}</option>`).join("")}
        </select>
      </label>
      <label class="setting-row">
        <span>Stop bits</span>
        <select data-device-stop>
          <option value="1" ${form.rig_stop_bits === 1 ? "selected" : ""}>1</option>
          <option value="2" ${form.rig_stop_bits === 2 ? "selected" : ""}>2</option>
        </select>
      </label>
      <label class="setting-row">
        <span>Rig mode</span>
        <select data-device-mode>
          ${["USB", "LSB", "AM", "FM", "CW", "RTTY"]
						.map(
							(m) =>
								`<option value="${m}" ${m === form.rig_mode ? "selected" : ""}>${m}</option>`,
						)
						.join("")}
        </select>
      </label>
      <label class="setting-row">
        <span>rigctld port</span>
        <input data-device-port type="number" min="1024" max="65535" value="${form.rigctld_port}">
      </label>
      <label class="setting-row">
        <span>Audio input (RX)</span>
        <select data-device-audio-in>${audioInOptions}</select>
      </label>
      <label class="setting-row">
        <span>Input channel</span>
        <select data-device-audio-channel>
          <option value="0" ${form.audio_in_channel === 0 ? "selected" : ""}>Left (0)</option>
          <option value="1" ${form.audio_in_channel === 1 ? "selected" : ""}>Right (1)</option>
        </select>
      </label>
      <label class="setting-row">
        <span>Audio output (TX)</span>
        <select data-device-audio-out>${audioOutOptions}</select>
      </label>
      <p class="drawer-hint dim">Source — ${sourceLine("rig_model")} · ${sourceLine("rig_device")} · ${sourceLine("rig_baud")} · ${sourceLine("rig_stop_bits")} · ${sourceLine("rigctld_port")} · ${sourceLine("audio_in_device")} · ${sourceLine("audio_out_device")}</p>
      <div class="device-actions" style="display:flex;gap:8px;margin-top:8px">
        <button data-device-save class="cmd">Save</button>
        <button data-device-apply class="cmd">Apply &amp; Restart</button>
      </div>
      <p class="drawer-hint dim">Apply &amp; Restart relaunches rigctld and the
        server — about 20 seconds of disconnect, then log in again.</p>`;

		const customWrap = content.querySelector(".device-custom");
		content
			.querySelector("[data-device-model]")
			?.addEventListener("change", (e) => {
				const custom = e.target.value === "custom";
				if (customWrap) customWrap.hidden = !custom;
			});
		const serialCustomWrap = content.querySelector(".device-custom-serial");
		content
			.querySelector("[data-device-serial]")
			?.addEventListener("change", (e) => {
				const custom = e.target.value === "custom";
				if (serialCustomWrap) serialCustomWrap.hidden = !custom;
			});
		const readForm = () => ({
			rig_model: Number(
				(content.querySelector("[data-device-model]")?.value === "custom"
					? content.querySelector("[data-device-model-custom]")?.value || 1049
					: content.querySelector("[data-device-model]")?.value) || 1049,
			),
			rig_device: String(
				content.querySelector("[data-device-serial]")?.value === "custom"
					? (content.querySelector("[data-device-serial-custom]")?.value ?? "")
					: (content.querySelector("[data-device-serial]")?.value ?? ""),
			),
			rig_baud: Number(
				content.querySelector("[data-device-baud]")?.value ?? 38400,
			),
			rig_stop_bits: Number(
				content.querySelector("[data-device-stop]")?.value ?? 1,
			),
			rig_mode: String(
				content.querySelector("[data-device-mode]")?.value ?? "USB",
			),
			rigctld_port: Number(
				content.querySelector("[data-device-port]")?.value ?? 4532,
			),
			audio_in_device:
				Number(content.querySelector("[data-device-audio-in]")?.value) || null,
			audio_out_device:
				Number(content.querySelector("[data-device-audio-out]")?.value) || null,
			audio_in_channel: Number(
				content.querySelector("[data-device-audio-channel]")?.value ?? 0,
			),
		});
		content
			.querySelector("[data-device-save]")
			?.addEventListener("click", async () => {
				const result = await api.saveDevices(readForm());
				if (!result.ok) {
					showToast(
						result.reason === "tx_active"
							? "Devices locked during TX"
							: `Save: ${result.reason || result.status}`,
					);
				} else {
					showToast("Device settings saved (apply to restart)");
					renderDevices(); // refresh source labels
				}
			});
		content
			.querySelector("[data-device-apply]")
			?.addEventListener("click", async () => {
				if (
					!window.confirm(
						"Apply will restart rigctld and the server.\n~20 s disconnect, then log in again. Continue?",
					)
				)
					return;
				const result = await api.applyDevices();
				if (!result.ok) {
					showToast(
						result.reason === "tx_active"
							? "Devices locked during TX"
							: `Apply: ${result.reason || result.status}`,
					);
				} else {
					showToast("Restarting… reconnecting shortly");
				}
			});
	}

	// ---- full-screen QSO log -------------------------------------------

	const logOverlay = document.getElementById("log-overlay");
	const logList = document.getElementById("log-list");
	const logCount = document.getElementById("log-count");

	async function openLogView() {
		logOverlay.hidden = false;
		document.body.classList.add("log-open");
		logList.innerHTML = "<p class='drawer-hint'>Loading QSO log…</p>";
		const res = await api.qsos();
		if (!res.ok) {
			logList.innerHTML = `<p class='drawer-hint dim'>Could not load log: ${res.reason || res.status}</p>`;
			return;
		}
		const qsos = res.qsos || [];
		if (logCount) logCount.textContent = String(qsos.length);
		const rows = qsos
			.map((q) => {
				const done = q.completed_epoch
					? new Date(q.completed_epoch * 1000)
							.toISOString()
							.slice(0, 16)
							.replace("T", " ")
					: "—";
				return `<div class="qso-row">
        <span class="qso-call">${q.dx_call}</span>
        <span class="qso-meta">${q.mode || ""} ${q.band || ""} ${done}</span>
      </div>`;
			})
			.join("");
		logList.innerHTML = rows || "<p class='drawer-hint dim'>No QSOs yet.</p>";
	}

	function closeLogView() {
		logOverlay.hidden = true;
		document.body.classList.remove("log-open");
	}

	// ---- full-screen DXCC stats ------------------------------------------

	const dxccOverlay = document.getElementById("dxcc-overlay");
	const dxccContent = document.getElementById("dxcc-content");
	const dxccCount = document.getElementById("dxcc-count");

	async function openDxccView() {
		dxccOverlay.hidden = false;
		document.body.classList.add("log-open");
		dxccContent.innerHTML = "<p class='drawer-hint'>Loading DXCC stats…</p>";
		const res = await api.dxcc();
		if (!res.ok) {
			dxccContent.innerHTML = `<p class='drawer-hint dim'>Could not load DXCC: ${res.reason || res.status}</p>`;
			return;
		}
		if (dxccCount) dxccCount.textContent = String(res.total);
		const html = [];
		html.push(
			`<div class="drawer-hint">已通联 <b>${res.total}</b> 个 DXCC 实体` +
				(res.unmatched ? `（${res.unmatched} 条未识别呼号）` : "") +
				"</div>",
		);
		// 波段矩阵（by_band 降序）
		const bands = Object.entries(res.by_band || {}).sort((a, b) => b[1] - a[1]);
		if (bands.length) {
			html.push("<h3>By band</h3>");
			html.push(
				bands
					.map(
						([band, n]) =>
							`<div class="qso-row"><span class="qso-call">${band}</span>` +
							`<span class="qso-meta">${n} DXCC</span></div>`,
					)
					.join(""),
			);
		}
		// 实体列表
		html.push("<h3>Entities</h3>");
		const rows = (res.entities || [])
			.map(
				(e) =>
					`<div class="qso-row"><span class="qso-call">${e.name}</span>` +
					`<span class="qso-meta">${e.continent} · ${e.first_utc} · ${e.band_count} band(s)</span></div>`,
			)
			.join("");
		html.push(rows || "<p class='drawer-hint dim'>No DXCC yet.</p>");
		dxccContent.innerHTML = html.join("");
	}

	function closeDxccView() {
		dxccOverlay.hidden = true;
		document.body.classList.remove("log-open");
	}

	// ---- full-screen New-DXCC band-hunt dashboard --------------------------

	const bandHuntOverlay = document.getElementById("bandhunt-overlay");
	const bandHuntContent = document.getElementById("bandhunt-content");
	const BAND_HUNT_WINDOWS = [
		[10, "10 min"],
		[30, "30 min"],
		[60, "1 hour"],
		[240, "4 hours"],
		[1440, "1 day"],
	];
	const BAND_HUNT_SPOT_CAP = 200; // rows per window; newest first

	function escapeHtml(value) {
		return String(value ?? "").replace(
			/[&<>"']/g,
			(c) =>
				({
					"&": "&amp;",
					"<": "&lt;",
					">": "&gt;",
					'"': "&quot;",
					"'": "&#39;",
				})[c],
		);
	}

	function buildWindowHtml(label, res) {
		const html = [];
		html.push(`<h3>${label}</h3>`);
		if (!res.ok) {
			html.push(
				`<p class='drawer-hint dim'>${escapeHtml(res.reason || res.status)}</p>`,
			);
			return html.join("");
		}
		const bands = res.bands || [];
		const allSpots = bands.flatMap((b) => b.spots || []);
		const workedTotal = bands.reduce(
			(n, b) => n + (b.worked_spot_count || 0),
			0,
		);
		if (!allSpots.length) {
			html.push(
				`<p class='drawer-hint dim'>0 new-DXCC spots` +
					`${workedTotal ? ` · ${workedTotal} already-worked nearby` : ""}.</p>`,
			);
			return html.join("");
		}
		const fresh = allSpots.slice(0, BAND_HUNT_SPOT_CAP);
		html.push(
			`<div class="qso-row"><span class="qso-call"></span>` +
				`<span class="qso-meta">${allSpots.length} new-DXCC spot(s)` +
				`${workedTotal ? ` · ${workedTotal} worked nearby` : ""}` +
				`${allSpots.length > fresh.length ? ` (showing ${fresh.length})` : ""}</span></div>`,
		);
		for (const s of fresh) {
			const name = s.entity || s.callsign || "?";
			const band = s.band || "?";
			const snr = s.snr == null ? "" : ` ${s.snr}dB`;
			const t = s.qso_time ? s.qso_time.replace("T", " ").slice(5, 16) : "";
			html.push(
				`<div class="qso-row"><span class="qso-call">${escapeHtml(name)}</span>` +
					`<span class="qso-meta">${escapeHtml(s.callsign)} · ${escapeHtml(band)}` +
					`${snr} · ${escapeHtml(t)}</span></div>`,
			);
		}
		return html.join("");
	}

	async function openBandHuntView() {
		bandHuntOverlay.hidden = false;
		document.body.classList.add("log-open");
		bandHuntContent.innerHTML =
			"<p class='drawer-hint'>Loading new-DXCC spots…</p>";

		// Fire every window fetch in parallel; each window renders the moment its
		// own fetch resolves (re-render keeps the final innerHTML in stable window
		// ORDER, so the 10-min window is visible in ~1 s instead of waiting for
		// the 1-day window). Promise.allSettled means one hard-failing fetch no
		// longer aborts the others — buildWindowHtml already renders an error hint
		// for {ok:false} responses.
		const rendered = new Map();
		const tasks = BAND_HUNT_WINDOWS.map(async ([windowMin, label]) => {
			const res = await api.bandHunt({
				window_min: windowMin,
				detail: 1,
				min_spots: 1,
			});
			rendered.set(label, buildWindowHtml(label, res));
			bandHuntContent.innerHTML = BAND_HUNT_WINDOWS.map(
				([, l]) => rendered.get(l) ?? "",
			).join("");
		});
		await Promise.allSettled(tasks);
	}

	function closeBandHuntView() {
		bandHuntOverlay.hidden = true;
		document.body.classList.remove("log-open");
	}

	function renderTab(tab) {
		if (tab === "radio") renderRadio();
		else if (tab === "ft8") renderFt8();
		else if (tab === "station") renderStation();
		else if (tab === "devices") renderDevices();
	}

	btnMenu.addEventListener("click", open);
	btnClose.addEventListener("click", close);
	backdrop.addEventListener("click", close);
	for (const t of tabs)
		t.addEventListener("click", () => switchTab(t.dataset.tab));

	document
		.getElementById("btn-log-close")
		.addEventListener("click", closeLogView);
	logOverlay.addEventListener("click", (event) => {
		// Clicking the dimmed backdrop (outside the panel) closes the overlay.
		if (event.target === logOverlay) closeLogView();
	});

	document
		.getElementById("btn-dxcc-close")
		.addEventListener("click", closeDxccView);
	dxccOverlay.addEventListener("click", (event) => {
		// Clicking the dimmed backdrop (outside the panel) closes the overlay.
		if (event.target === dxccOverlay) closeDxccView();
	});

	document
		.getElementById("btn-bandhunt-close")
		.addEventListener("click", closeBandHuntView);
	bandHuntOverlay.addEventListener("click", (event) => {
		// Clicking the dimmed backdrop (outside the panel) closes the overlay.
		if (event.target === bandHuntOverlay) closeBandHuntView();
	});

	// Reflect station info from fresh snapshots.
	subscribe(() => {
		if (activeTab === "station") renderStation();
	});
}
