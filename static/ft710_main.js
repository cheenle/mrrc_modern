/**
 * MRRC Modern Web Control — Main Controller
 * ==========================================
 * WebSocket connection, state management, message dispatch.
 * Connects to /WSradio endpoint with auth token.
 *
 * Depends on: settings_manager.js (loaded first) for getAuthToken()
 */

// ── Auth ────────────────────────────────────────────────────────────
function getAuthToken() {
	const m = document.cookie.match(/(?:^|;\s*)mrrc_auth=([^;]*)/);
	return m ? m[1] : "";
}

function handleAuthExpired() {
	if (window.__authRedirecting) return;
	window.__authRedirecting = true;
	try {
		if (wsRadio) wsRadio.close();
	} catch (e) {}
	window.location.replace(
		"/login?next=" + encodeURIComponent(window.location.pathname),
	);
}

// ── WebSocket ───────────────────────────────────────────────────────
let wsRadio = null;
let wsSpectrum = null;
let wsReconnectTimer = null;
let wsReconnectDelay = 1000;
const WS_RECONNECT_MAX = 30000;
let pingTimer = null;
let _intentionalClose = false; // set by power button to block auto-reconnect

function wsUrlWithAuth(path) {
	const token = getAuthToken();
	const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
	const host = window.location.host;
	return (
		proto +
		"//" +
		host +
		path +
		(token ? "?token=" + encodeURIComponent(token) : "")
	);
}

// Same-auth URL for static files (uses http/https, not ws/wss)
function staticUrlWithAuth(path) {
	const token = getAuthToken();
	const proto = window.location.protocol;
	const host = window.location.host;
	return (
		proto +
		"//" +
		host +
		path +
		(token ? "?token=" + encodeURIComponent(token) : "")
	);
}

function connectWebSocket() {
	if (wsRadio && wsRadio.readyState === WebSocket.OPEN) return;

	const url = wsUrlWithAuth("/WSradio");
	console.log("Connecting to", url);
	wsRadio = new WebSocket(url);
	wsRadio.binaryType = "arraybuffer";

	wsRadio.onopen = () => {
		console.log("WebSocket connected");
		wsReconnectDelay = 1000;
		updateConnectionStatus(true);
		startPing();
		// Also connect spectrum + audio
		connectSpectrumSocket();
		connectAudioRX();
		connectAudioTX();
	};

	wsRadio.onmessage = (event) => {
		try {
			const msg = JSON.parse(event.data);
			handleMessage(msg);
		} catch (e) {
			console.warn("[ws] message parse/handle error:", e,
				"raw head:", String(event.data).slice(0, 80));
		}
		if (!window.__netBytes)
			window.__netBytes = {
				rxCtrl: 0,
				rxSpec: 0,
				rxAudio: 0,
				txCtrl: 0,
				txAudio: 0,
			};
		window.__netBytes.rxCtrl += event.data.length;
	};

	wsRadio.onclose = (event) => {
		console.log("WebSocket closed:", event.code);
		updateConnectionStatus(false);
		stopPing();
		if (event.code === 4001) {
			handleAuthExpired();
			return;
		}
		if (_intentionalClose) {
			// User pressed power button — don't auto-reconnect
			_intentionalClose = false;
			return;
		}
		if (event.code === 1006) {
			// Handshake failed — possibly a stale token (the server replies
			// HTTP 403 to the WS upgrade, which the browser reports as 1006).
			// Verify the session before retrying forever.
			checkAuthThenReconnect();
			return;
		}
		scheduleReconnect();
	};

	wsRadio.onerror = (e) => {
		console.warn("WebSocket error");
	};
}

// ── Spectrum WebSocket ──────────────────────────────────────────────
function connectSpectrumSocket() {
	if (wsSpectrum && wsSpectrum.readyState === WebSocket.OPEN) return;
	const url = wsUrlWithAuth("/WSspectrum");
	wsSpectrum = new WebSocket(url);
	wsSpectrum.binaryType = "arraybuffer";
	wsSpectrum.onmessage = (event) => {
		if (event.data instanceof ArrayBuffer) {
			if (!window.__netBytes)
				window.__netBytes = {
					rxCtrl: 0,
					rxSpec: 0,
					rxAudio: 0,
					txCtrl: 0,
					txAudio: 0,
				};
			window.__netBytes.rxSpec += event.data.byteLength;
			handleSpectrumBinary(event.data);
		}
	};
	wsSpectrum.onclose = () => {
		wsSpectrum = null;
	};
	wsSpectrum.onerror = () => {};
}

function handleSpectrumBinary(buffer) {
	const data = new Uint8Array(buffer);
	// Minimum: 1 version byte + 850 wf1 = 851 bytes
	if (data.length < 851) {
		console.debug("Spectrum frame too short:", data.length);
		return;
	}

	const version = data[0];
	if (version < 1 || version > 2) {
		console.debug("Spectrum frame unknown version:", version);
		return;
	}

	const wf1 = Array.from(data.slice(1, 851));

	// If we have wf2 data (v2+), extract it for potential future use
	if (data.length >= 1701) {
		const wf2 = Array.from(data.slice(851, 1701));
		window._lastWf2 = wf2;
	}

	// Update waterfall
	if (typeof renderWaterfallRow === "function") {
		renderWaterfallRow(wf1);
	}

	// Update S-meter from scope data peak
	if (typeof radioState !== "undefined") {
		updateSMeterFromSpectrum(wf1);
	}
}

function updateSMeterFromSpectrum(wf1) {
	// Peak signal from spectrum as approximate S-meter
	if (!wf1 || wf1.length === 0) return;
	let maxVal = 0;
	let sum = 0;
	for (let i = 0; i < wf1.length; i++) {
		if (wf1[i] > maxVal) maxVal = wf1[i];
		sum += wf1[i];
	}
	// Store for UI / diagnostics
	window._specPeak = maxVal;
	window._specAvg = sum / wf1.length;
}

function scheduleReconnect() {
	if (wsReconnectTimer) return;
	console.log("Reconnecting in", wsReconnectDelay, "ms");
	wsReconnectTimer = setTimeout(() => {
		wsReconnectTimer = null;
		connectWebSocket();
		wsReconnectDelay = Math.min(wsReconnectDelay * 2, WS_RECONNECT_MAX);
	}, wsReconnectDelay);
}

// On WS handshake failure (1006), distinguish a stale auth token from a
// network outage before retrying: /api/auth/check answers 401 for a bad
// session, so we only redirect to login when the server itself says so.
let authCheckInFlight = false;
function checkAuthThenReconnect() {
	if (authCheckInFlight) return;
	authCheckInFlight = true;
	fetch("/api/auth/check", { cache: "no-store" })
		.then((r) => {
			if (r.status === 401) {
				handleAuthExpired();
			} else {
				scheduleReconnect();
			}
		})
		.catch(() => {
			scheduleReconnect(); // server unreachable — keep retrying
		})
		.finally(() => {
			authCheckInFlight = false;
		});
}

function startPing() {
	stopPing();
	pingTimer = setInterval(() => {
		window.__pingSent = performance.now();
		sendMsg({ type: "ping" });
	}, 15000);
}

function stopPing() {
	if (pingTimer) {
		clearInterval(pingTimer);
		pingTimer = null;
	}
}

function sendMsg(msg) {
	if (wsRadio && wsRadio.readyState === WebSocket.OPEN) {
		var raw = JSON.stringify(msg);
		wsRadio.send(raw);
		if (!window.__netBytes)
			window.__netBytes = {
				rxCtrl: 0,
				rxSpec: 0,
				rxAudio: 0,
				txCtrl: 0,
				txAudio: 0,
			};
		window.__netBytes.txCtrl += raw.length;
		return true;
	}
	return false;
}

function sendCommand(field, value) {
	return sendMsg({ type: "set", field: field, value: value });
}

// ── State ───────────────────────────────────────────────────────────
const radioState = {
	vfo_a_freq: 14200000,
	vfo_b_freq: 7050000,
	active_vfo: "A",
	mode: 1,
	mode_name: "USB",
	mode_display: "USB",
	band_name: "20m",
	tx_status: 0,
	is_transmitting: false,
	s_meter: 0,
	s_meter_dbm: -54,
	s_unit: "S0",
	comp_meter: 0,
	alc_meter: 0,
	power_meter: 0,
	swr_meter: 0,
	id_meter: 0,
	vd_meter: 0,
	af_gain: 128,
	rf_gain: 255,
	rf_power: 100,
	filter_width: 5,
	filter_hz: 2400,
	rx_audio_silent: false,
	preamp: 0,
	preamp_label: "OFF",
	attenuator: 0,
	attenuator_label: "OFF",
	noise_blanker: false,
	noise_reduction: false,
	auto_notch: false,
	compressor: false,
	compressor_level: 50,
	tuner_status: 0,
	power_on: true,
	squelch: 0,
	mic_gain: 50,
	split: false,
	scope_span: 6,
	scope_speed: 2,
	scope_mode: 0,
	scope_start_freq: 0,
	serial_connected: false,
	last_update: 0,
	ws_connected: false,
};

const bands = [];
const uiModes = [];
const memChannels = new Array(6).fill(null);

// Per-radio description from fullState (multi-model support).  null means
// "legacy server / FT-710" — every consumer in ft710_ui.js must treat null
// as "render exactly as before".
let radioCapabilities = null;
let radioModel = null;
let radioDisplayName = null;

// ── Message Handler ─────────────────────────────────────────────────
function handleMessage(msg) {
	switch (msg.type) {
		case "fullState":
			// Merge full state data
			if (msg.data) {
				Object.assign(radioState, msg.data);
				console.log("[fullState] serial_connected =", msg.data.serial_connected,
					"type =", typeof msg.data.serial_connected);
			} else {
				console.warn("[fullState] no msg.data");
			}
			if (msg.bands) {
				bands.length = 0;
				bands.push(...msg.bands);
			}
			if (msg.modes) {
				uiModes.length = 0;
				uiModes.push(...msg.modes);
			}
			if (msg.filterTables) {
				window.filterTables = msg.filterTables;
			}
			// Per-radio capabilities/model drive the adaptive UI in
			// ft710_ui.js (applyRadioCapabilities) and the audio boost below.
			radioCapabilities = msg.capabilities || null;
			radioModel = msg.radioModel || null;
			radioDisplayName = msg.radioDisplayName || null;
			_applyRadioBranding();
			// IC-7300 USB audio is much hotter than the FT-710's — skip the
			// 10x boost there. (Initial guess — tune after hardware test.)
			AUDIO_GAIN_BOOST = radioModel === "ic7300" ? 1.0 : 10.0;
			if (msg.memChannels) {
				memChannels.length = 0;
				memChannels.push(...msg.memChannels);
				// Persist to cookie
				FT710Settings.setCookie(
					"ft710_memChannels",
					JSON.stringify(memChannels),
				);
				renderMemoryChannels();
			}
			radioState.ws_connected = true;
			// Hide controls the radio lacks / rebuild model-specific selects
			// BEFORE the first render pass so labels come up correct.
			if (typeof applyRadioCapabilities === "function") {
				applyRadioCapabilities();
			}
			renderAll();
			_applyAfGainToAudioNode(); // sync gain node from radio state
			_applySavedMicGain(); // push persisted mic gain back to the radio
			// Optional ATR1000 tuner module — inert unless server enables it
			if (window.ATR1000 && typeof window.ATR1000.init === "function") {
				window.ATR1000.init(msg.atr1000Enabled === true);
			}
			// Also request memChannels explicitly (belt-and-suspenders with server push)
			if (!msg.memChannels) {
				sendMsg({ type: "memLoadAll" });
			}
			break;

		case "stateUpdate":
			if (msg.fields) {
				Object.assign(radioState, msg.fields);
			}
			{
				const changedFields = msg.fields ? Object.keys(msg.fields) : msg.dirty;
				if (changedFields) {
					renderUpdates(changedFields);
					// NOTE: server af_gain no longer drives the playback gain
					// node — the volume slider is browser-local (cookie).
				}
			}
			break;

		case "value":
			if (msg.field && msg.value !== undefined) {
				radioState[msg.field] = msg.value;
				renderField(msg.field);
			}
			break;

		case "memChannels":
			if (msg.channels) {
				memChannels.length = 0;
				memChannels.push(...msg.channels);
				// Persist to cookie so channels survive page refresh
				FT710Settings.setCookie(
					"ft710_memChannels",
					JSON.stringify(memChannels),
				);
				renderMemoryChannels();
			}
			break;

		case "pong":
			if (window.__pingSent) {
				window.__lastRtt = Math.round(performance.now() - window.__pingSent);
				window.__pingSent = 0;
			}
			break;

		case "error":
			console.warn("Server error:", msg.message);
			if (msg.message && typeof showToast === "function") {
				showToast(msg.message);
			}
			break;
	}
}

// ── Branding ────────────────────────────────────────────────────────
// Retitle the page/menu from fullState.radioDisplayName ("Yaesu FT-710",
// "Icom IC-7300", ...).  Fallback "MRRC Modern" is used only when an old
// server sends no radioDisplayName.
function _applyRadioBranding() {
	const name = radioDisplayName || "MRRC Modern";
	document.title = name + " Web Control";
	const menuTitle = document.getElementById("menu-title");
	if (menuTitle) menuTitle.textContent = name + " Menu";
	const version = document.getElementById("menu-version");
	if (version) version.textContent = name + " v1.0";
	const appleMeta = document.querySelector(
		'meta[name="apple-mobile-web-app-title"]',
	);
	if (appleMeta) appleMeta.setAttribute("content", name);
}

// ── Connection Status ───────────────────────────────────────────────
function updateConnectionStatus(connected) {
	radioState.ws_connected = connected;
	const dot = document.querySelector("#status-serial .status-dot");
	if (dot) {
		if (connected && radioState.serial_connected) {
			dot.classList.add("connected");
		} else {
			dot.classList.remove("connected");
		}
	}
	// Keep power button in sync (catches unexpected disconnects)
	var powerBtn = document.getElementById("btn-power");
	if (powerBtn) {
		if (connected) {
			powerBtn.classList.add("active");
		} else {
			powerBtn.classList.remove("active");
		}
	}
}

// ── Initialization ──────────────────────────────────────────────────
var _isMobile =
	/iPhone|iPad|iPod|Android/i.test(navigator.userAgent) ||
	(navigator.maxTouchPoints > 1 && !/Chrome/.test(navigator.userAgent));

function bodyload() {
	// Restore memory channels from cookie first (instant, before server responds)
	try {
		var savedMem = FT710Settings.getCookie("ft710_memChannels");
		if (savedMem) {
			var parsed = JSON.parse(savedMem);
			memChannels.length = 0;
			memChannels.push(...parsed);
			// Defer render until DOM is ready; renderMemoryChannels will be called
			// again after fullState arrives, but this gives instant display
		}
	} catch (e) {
		/* ignore */
	}

	// Wire power button — toggles web client connection on/off
	// (does NOT control radio hardware power)
	var powerBtn = document.getElementById("btn-power");
	if (powerBtn) {
		powerBtn.addEventListener("click", () => {
			if (wsRadio && wsRadio.readyState === WebSocket.OPEN) {
				// Disconnect: stop web client
				_intentionalClose = true; // block auto-reconnect
				wsRadio.close();
				if (wsAudioRX) wsAudioRX.close();
				if (wsAudioTX) wsAudioTX.close();
				updateConnectionStatus(false);
				powerBtn.classList.remove("active");
			} else {
				// Connect: start web client
				// Close any stale connections first to avoid zombie WS
				// accumulating TX ownership (non_owner_drops issue).
				if (wsRadio) {
					try {
						wsRadio.close();
					} catch (_) {}
					wsRadio = null;
				}
				if (wsAudioRX) {
					try {
						wsAudioRX.close();
					} catch (_) {}
					wsAudioRX = null;
				}
				if (wsAudioTX) {
					try {
						wsAudioTX.close();
					} catch (_) {}
					wsAudioTX = null;
				}
				// Call AudioRX_start() SYNCHRONOUSLY from user gesture
				// so AudioContext is created while iOS gesture context is active
				if (typeof connectAudioRX === "function") connectAudioRX();
				// iOS audio unlock: MUST play a sound during user gesture.
				// AudioContext.resume() alone is NOT enough on iOS — the Web Audio
				// clock stays frozen until an actual buffer is processed.
				try {
					if (typeof AudioRX_context !== "undefined" && AudioRX_context) {
						var _uO = AudioRX_context.createOscillator();
						var _uG = AudioRX_context.createGain();
						_uG.gain.value = 0.001;
						_uO.connect(_uG);
						_uG.connect(AudioRX_context.destination);
						_uO.frequency.value = 440;
						_uO.start(0);
						_uO.stop(AudioRX_context.currentTime + 0.01);
						console.log("iOS audio unlock: oscillator fired");
					}
				} catch (_) {}
				if (typeof connectAudioTX === "function") connectAudioTX();
				connectWebSocket();
				powerBtn.classList.add("active");
			}
		});
	}

	if (_isMobile) {
		console.log("Mobile detected — tap ⏻ to start web client");
		if (powerBtn) powerBtn.classList.remove("active");
	} else {
		if (powerBtn) powerBtn.classList.add("active");
		connectWebSocket();
	}
}

// ── Keyboard support ────────────────────────────────────────────────
document.addEventListener("keydown", (e) => {
	if (e.repeat) return; // ignore key auto-repeat (held key)
	if (e.target !== document.body) return; // don't steal keys from inputs
	// Space = PTT
	if (e.code === "Space") {
		e.preventDefault();
		if (!radioState.is_transmitting) {
			if (window.PTTManager) PTTManager.pttStart();
			else handlePTTStart();
		} else {
			if (window.PTTManager) PTTManager.pttEnd();
			else handlePTTEnd();
		}
	}
	// Arrow keys = tune
	if (e.code === "ArrowLeft") {
		tuneBy(-(currentTuneStep || 1000));
	}
	if (e.code === "ArrowRight") {
		tuneBy(currentTuneStep || 1000);
	}
	if (e.code === "ArrowUp") {
		tuneBy((currentTuneStep || 1000) * 5);
	}
	if (e.code === "ArrowDown") {
		tuneBy(-(currentTuneStep || 1000) * 5);
	}
});

window.addEventListener("load", bodyload);

// ═══════════════════════════════════════════════════════════════════════
// ── Audio RX (adapted from sunmrrc controls.js) ───────────────────────
// ═══════════════════════════════════════════════════════════════════════

const AudioRX_sampleRate = 48000;
// Codec tags (must match server's opus_rx.py)
const AUDIO_TAG_PCM = 0x00;
const AUDIO_TAG_OPUS = 0x01;

var wsAudioRX = null;
var AudioRX_context = null;
var AudioRX_source_node = null;
var AudioRX_gain_node = null;
var _rxOpusDecoder = null;

// ── Opus decoder (cached) ────────────────────────────────────────────
function getRxOpusDecoder() {
	if (_rxOpusDecoder) return _rxOpusDecoder;
	try {
		if (typeof OpusDecoder === "undefined") return null;
		_rxOpusDecoder = new OpusDecoder(48000, 1);
		console.log("RX Opus decoder ready (48kHz mono)");
	} catch (e) {
		console.warn("RX Opus decoder creation failed:", e);
		_rxOpusDecoder = null;
	}
	return _rxOpusDecoder;
}

// ── RX MP3 Recorder (lamejs → real MP3, works in all browsers) ────────
var RX_RECORDER_MIME = "audio/mpeg";
var RX_RECORDER_EXT = "mp3";
var RX_MP3_BITRATE = 128; // kbps, good balance of quality vs size

// lame.js (~500 KB) is lazy-loaded on first REC click — it is not needed
// for normal operation and would otherwise slow every page load.
var _lameLoadPromise = null;
function _loadLame() {
	if (
		typeof lamejs !== "undefined" &&
		typeof lamejs.Mp3Encoder === "function"
	) {
		return Promise.resolve(true);
	}
	if (_lameLoadPromise) return _lameLoadPromise;
	_lameLoadPromise = new Promise((resolve) => {
		var s = document.createElement("script");
		s.src = staticUrlWithAuth("/modules/lame.js?v=1");
		s.onload = () => {
			resolve(
				typeof lamejs !== "undefined" &&
					typeof lamejs.Mp3Encoder === "function",
			);
		};
		s.onerror = () => {
			resolve(false);
		};
		document.head.appendChild(s);
	});
	return _lameLoadPromise;
}

function _formatRecordingTimestamp(d) {
	function pad(n) {
		return String(n).padStart(2, "0");
	}
	return (
		d.getFullYear() +
		pad(d.getMonth() + 1) +
		pad(d.getDate()) +
		"-" +
		pad(d.getHours()) +
		pad(d.getMinutes()) +
		pad(d.getSeconds())
	);
}

function _downloadRecording(chunks) {
	if (!chunks || chunks.length === 0) return;
	var blob = new Blob(chunks, { type: "audio/mpeg" });
	var url = URL.createObjectURL(blob);
	var a = document.createElement("a");
	a.href = url;
	a.download = "mrrc-rx-" + _formatRecordingTimestamp(new Date()) + ".mp3";
	document.body.appendChild(a);
	a.click();
	a.remove();
	setTimeout(() => {
		URL.revokeObjectURL(url);
	}, 30000);
}

/** Convert float32 [-1,+1] samples → Int16Array for the MP3 encoder. */
function _f32ToInt16(f32) {
	var len = f32.length;
	var out = new Int16Array(len);
	for (var i = 0; i < len; i++) {
		var s = f32[i];
		// clamp to [-1, +1]
		if (s > 1.0) s = 1.0;
		else if (s < -1.0) s = -1.0;
		out[i] = s < 0 ? s * 0x8000 : s * 0x7fff; // -32768 .. 32767
	}
	return out;
}

window.RXRecorder = (() => {
	var encoder = null; // lamejs.Mp3Encoder
	var chunks = null; // Array<Uint8Array> of encoded MP3 data
	var active = false;
	var totalFrames = 0;

	function isSupported() {
		return (
			typeof lamejs !== "undefined" && typeof lamejs.Mp3Encoder === "function"
		);
	}

	function _notify() {
		if (typeof renderRecordingState === "function") renderRecordingState();
	}

	async function start() {
		if (active) return true;
		if (!isSupported()) {
			// First use: pull the MP3 encoder on demand
			var loaded = await _loadLame();
			if (!loaded || !isSupported()) {
				console.warn("MP3 recording not supported — lamejs unavailable");
				_notify();
				return false;
			}
		}
		if (!AudioRX_context || AudioRX_context.state === "closed") {
			if (typeof connectAudioRX === "function") connectAudioRX();
		}
		if (!AudioRX_context || AudioRX_context.state === "closed") {
			_notify();
			return false;
		}
		if (AudioRX_context.state === "suspended") {
			try {
				await AudioRX_context.resume();
			} catch (e) {
				/* continue anyway */
			}
		}

		encoder = new lamejs.Mp3Encoder(1, AudioRX_sampleRate, RX_MP3_BITRATE);
		chunks = [];
		totalFrames = 0;
		active = true;
		_notify();
		console.log(
			"MP3 recording started — " +
				RX_MP3_BITRATE +
				" kbps, " +
				AudioRX_sampleRate +
				" Hz mono",
		);
		return true;
	}

	function stop() {
		if (!active) return;
		active = false;
		if (encoder) {
			var tail = encoder.flush();
			if (tail && tail.length > 0) chunks.push(tail);
			console.log(
				"MP3 recording stopped — " +
					totalFrames +
					" frames, " +
					chunks.length +
					" mp3 chunks",
			);
		}
		_downloadRecording(chunks);
		chunks = [];
		encoder = null;
		totalFrames = 0;
		_notify();
	}

	/** Feed one chunk of decoded float32 RX audio into the MP3 encoder. */
	function feed(f32) {
		if (!active || !encoder || !f32 || f32.length === 0) return;
		var int16 = _f32ToInt16(f32);
		var mp3buf = encoder.encodeBuffer(int16);
		if (mp3buf && mp3buf.length > 0) chunks.push(mp3buf);
		totalFrames++;
	}

	async function toggle() {
		return active ? stop() : await start();
	}

	return {
		start: start,
		stop: stop,
		toggle: toggle,
		feed: feed,
		isSupported: isSupported,
		get isActive() {
			return active;
		},
	};
})();

function feedRXRecorderFrame(f32) {
	if (window.RXRecorder) window.RXRecorder.feed(f32);
}

// ── Decode tagged audio frame (1-byte tag + payload) ──────────────────
function decodeRxAudioFrame(data) {
	if (!data || data.byteLength < 1) return null;
	const bytes = new Uint8Array(data);
	const tag = bytes[0];
	const payload = data.slice(1);

	if (tag === AUDIO_TAG_OPUS) {
		const dec = getRxOpusDecoder();
		if (!dec) return null;
		try {
			const f32 = dec.decode_float(payload);
			return new Float32Array(f32); // Copy out of WASM heap
		} catch (e) {
			return null;
		}
	}

	// PCM (0x00 or legacy untagged): Int16 → Float32
	try {
		if (payload.byteLength < 2) return null;
		const i16 = new Int16Array(payload);
		const f32 = new Float32Array(i16.length);
		const scale = 1.0 / 32767.0;
		for (let i = 0; i < i16.length; i++) f32[i] = i16[i] * scale;
		return f32;
	} catch (e) {
		return null;
	}
}

// ── Push RX frame to worklet (set during AudioRX_start) ───────────────
// No-op placeholder — replaced when AudioWorklet or ScriptProcessor is ready
var __pushRxFrameNoop = (f32) => {};
window.__pushRxFrame = __pushRxFrameNoop;

// ── WebSocket event handlers ──────────────────────────────────────────
function wsAudioRXopen() {
	console.log("Audio RX WebSocket connected");
}
function wsAudioRXclose(ev) {
	console.log("Audio RX WebSocket closed");
	if (ev && ev.code === 4001) {
		handleAuthExpired();
		return;
	}
	wsAudioRX = null;
}
function wsAudioRXerror(err) {
	console.warn("Audio RX WebSocket error:", err);
}

// ── Start audio RX (adapted from sunmrrc AudioRX_start) ───────────────
function AudioRX_start() {
	// Avoid duplicate connections — MUST be before cleanup to prevent
	// destroying a freshly-created AudioContext on re-entrant calls.
	if (
		wsAudioRX &&
		(wsAudioRX.readyState === WebSocket.OPEN ||
			wsAudioRX.readyState === WebSocket.CONNECTING)
	) {
		console.log("AudioRX already connected, skipping");
		return;
	}

	// Clean up old context
	if (AudioRX_context && AudioRX_context.state !== "closed") {
		try {
			AudioRX_context.close();
		} catch (e) {}
		AudioRX_context = null;
		AudioRX_source_node = null;
	}

	const url = wsUrlWithAuth("/WSaudioRX");
	console.log("Connecting Audio RX to", url);
	wsAudioRX = new WebSocket(url);
	wsAudioRX.binaryType = "arraybuffer";
	wsAudioRX.onopen = wsAudioRXopen;
	wsAudioRX.onclose = wsAudioRXclose;
	wsAudioRX.onerror = wsAudioRXerror;

	// ── Set onmessage EARLY so frames are buffered while worklet loads ──
	// Frames decoded here and queued to window.__earlyAudioQueue.
	// Once the worklet is ready, they're flushed to it.
	window.__earlyAudioQueue = [];
	wsAudioRX.onmessage = (msg) => {
		if (!window.__netBytes)
			window.__netBytes = {
				rxCtrl: 0,
				rxSpec: 0,
				rxAudio: 0,
				txCtrl: 0,
				txAudio: 0,
			};
		if (msg && msg.data && msg.data.byteLength) {
			window.__netBytes.rxAudio += msg.data.byteLength;
			var f32 = decodeRxAudioFrame(msg.data);
			if (f32) {
				feedRXRecorderFrame(f32);
				window.__earlyAudioQueue.push(f32);
				// Keep queue bounded (~1 second max)
				while (window.__earlyAudioQueue.length > 50)
					window.__earlyAudioQueue.shift();
				// Also try to push to worklet if it's ready
				if (window.__pushRxFrame !== window.__pushRxFrameNoop) {
					window.__pushRxFrame(f32);
				}
			}
		}
	};

	// ── AudioContext (48kHz, interactive latency) ──────────────────
	AudioRX_context = new (window.AudioContext || window.webkitAudioContext)({
		latencyHint: "interactive",
		sampleRate: AudioRX_sampleRate,
	});
	console.log(
		"AudioContext created, state:",
		AudioRX_context.state,
		", rate:",
		AudioRX_context.sampleRate,
	);

	if (AudioRX_context.state === "suspended") {
		console.log(
			"AudioContext suspended — will resume on first user interaction",
		);
		var resumeAudio = () => {
			AudioRX_context.resume()
				.then(() => console.log("AudioContext resumed"))
				.catch((e) => console.warn("Resume failed:", e));
		};
		document.addEventListener("click", resumeAudio, { once: true });
		document.addEventListener("touchstart", resumeAudio, { once: true });
		document.addEventListener("keydown", resumeAudio, { once: true });
	}

	AudioRX_gain_node = AudioRX_context.createGain();

	// ── AudioWorklet (desktop) or ScriptProcessor (iOS) ────────────
	var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
	var useAudioWorklet = !isIOS;

	(async () => {
		if (useAudioWorklet) {
			try {
				await AudioRX_context.audioWorklet.addModule(
					staticUrlWithAuth("/rx_worklet_processor.js?v=2"),
				);
				const rxNode = new AudioWorkletNode(AudioRX_context, "rx-player");
				AudioRX_source_node = rxNode;
				// Configure jitter buffer
				try {
					rxNode.port.postMessage({
						type: "config",
						prebufferMs: 120,
						recoveryMs: 60,
						maxMs: 300,
					});
				} catch (_) {}
				// Receive jitter buffer stats from worklet
				rxNode.port.onmessage = (ev) => {
					if (ev.data && ev.data.type === "stats") {
						window.__jitterMs = Math.round(ev.data.bufferMs || 0);
					}
				};
				// Wire the push callback
				window.__pushRxFrame = (f32) => {
					rxNode.port.postMessage({ type: "push", payload: f32 });
				};
				// Flush early-audio queue into worklet
				if (window.__earlyAudioQueue && window.__earlyAudioQueue.length > 0) {
					console.log(
						"Flushing " +
							window.__earlyAudioQueue.length +
							" early audio frames to worklet",
					);
					for (var i = 0; i < window.__earlyAudioQueue.length; i++) {
						rxNode.port.postMessage({
							type: "push",
							payload: window.__earlyAudioQueue[i],
						});
					}
					window.__earlyAudioQueue = [];
				}
				rxNode.connect(AudioRX_gain_node);
				AudioRX_gain_node.connect(AudioRX_context.destination);
				_applyAfGainToAudioNode();
				console.log(
					"Audio RX playback started (AudioWorklet) gain=" +
						AudioRX_gain_node.gain.value,
				);
			} catch (e) {
				console.warn("AudioWorklet failed, fallback to ScriptProcessor:", e);
				useAudioWorklet = false;
			}
		}

		if (!useAudioWorklet) {
			// ScriptProcessor fallback (iOS Safari)
			const BUFF_SIZE = 2048;
			AudioRX_source_node = AudioRX_context.createScriptProcessor(
				BUFF_SIZE,
				1,
				1,
			);
			var queue = [];
			var queuedSamples = 0;
			var prebufferSamples = Math.round(0.06 * AudioRX_sampleRate); // 60ms
			var maxBufferSamples = Math.round(0.3 * AudioRX_sampleRate); // 300ms cap
			var priming = true;

			AudioRX_source_node.onaudioprocess = (event) => {
				var out = event.outputBuffer.getChannelData(0);
				var needed = out.length;
				var written = 0;

				if (priming) {
					if (queuedSamples < prebufferSamples) {
						out.fill(0);
						return;
					}
					priming = false;
				}

				while (written < needed && queue.length > 0) {
					var chunk = queue[0];
					var n = Math.min(chunk.length, needed - written);
					out.set(chunk.subarray(0, n), written);
					written += n;
					queuedSamples -= n;
					if (n >= chunk.length) {
						queue.shift();
					} else {
						queue[0] = chunk.subarray(n);
					}
				}

				if (written < needed) {
					out.fill(0, written);
					queuedSamples = 0;
					queue = [];
					priming = true;
				}
			};

			// Wire push callback to queue (with latency cap)
			window.__pushRxFrame = (f32) => {
				queue.push(f32);
				queuedSamples += f32.length;
				// Drop oldest frames if buffer exceeds 300ms
				while (queuedSamples > maxBufferSamples && queue.length > 1) {
					queuedSamples -= queue.shift().length;
				}
			};

			// ── onmessage set AFTER processor is ready ──
			wsAudioRX.onmessage = (msg) => {
				if (!window.__netBytes)
					window.__netBytes = {
						rxCtrl: 0,
						rxSpec: 0,
						rxAudio: 0,
						txCtrl: 0,
						txAudio: 0,
					};
				if (msg && msg.data && msg.data.byteLength) {
					window.__netBytes.rxAudio += msg.data.byteLength;
					var float32Data = decodeRxAudioFrame(msg.data);
					if (float32Data) {
						feedRXRecorderFrame(float32Data);
						queue.push(float32Data);
						queuedSamples += float32Data.length;
						while (queuedSamples > maxBufferSamples && queue.length > 1) {
							queuedSamples -= queue.shift().length;
						}
					}
				}
			};

			AudioRX_source_node.connect(AudioRX_gain_node);
			AudioRX_gain_node.connect(AudioRX_context.destination);
			_applyAfGainToAudioNode();
			console.log(
				"Audio RX playback started (ScriptProcessor) gain=" +
					AudioRX_gain_node.gain.value,
			);
		}
	})();
}

// Legacy alias for backwards compat
function connectAudioRX() {
	AudioRX_start();
}

// ═══════════════════════════════════════════════════════════════════════
// ── Audio TX ──────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════

let wsAudioTX = null;
let txAudioStream = null;
let txOpusWorker = null;
let txAudioRunning = false;
const TX_AUDIO_MAX_BUFFERED_BYTES = 4096;

function connectAudioTX() {
	if (wsAudioTX && wsAudioTX.readyState === WebSocket.OPEN) return;

	const url = wsUrlWithAuth("/WSaudioTX");
	console.log("Connecting Audio TX to", url);
	wsAudioTX = new WebSocket(url);
	wsAudioTX.binaryType = "arraybuffer";

	wsAudioTX.onopen = () => {
		console.log("Audio TX WebSocket connected");
	};

	wsAudioTX.onclose = () => {
		console.log("Audio TX WebSocket closed");
		wsAudioTX = null;
	};

	wsAudioTX.onerror = () => {};
}

function ensureTXOpusWorker() {
	if (txOpusWorker) return txOpusWorker;
	txOpusWorker = new Worker("/tx_opus_worker.js?v=tx-audio-4");
	txOpusWorker.onmessage = (ev) => {
		const d = ev.data || {};
		if (d.type === "tx_audio" && d.data instanceof ArrayBuffer) {
			sendTXAudioFrame(d.data);
		}
	};
	return txOpusWorker;
}

function sendTXAudioFrame(buffer) {
	if (!wsAudioTX || wsAudioTX.readyState !== WebSocket.OPEN) return false;
	if (wsAudioTX.bufferedAmount > TX_AUDIO_MAX_BUFFERED_BYTES) {
		window.__txAudioDroppedFrames = (window.__txAudioDroppedFrames || 0) + 1;
		return false;
	}
	wsAudioTX.send(buffer);
	if (!window.__netBytes)
		window.__netBytes = {
			rxCtrl: 0,
			rxSpec: 0,
			rxAudio: 0,
			txCtrl: 0,
			txAudio: 0,
		};
	window.__netBytes.txAudio += buffer.byteLength || 0;
	return true;
}

var _txMicStream = null; // cached mic stream (reuse to avoid permission prompt)
var _txMicCtx = null; // cached TX AudioContext
var _txMicSource = null; // cached MediaStreamSource
var _txMicSp = null; // cached ScriptProcessor
var _txMicAw = null; // cached AudioWorkletNode
var _txMicMute = null; // zero-gain sink that keeps capture graph alive
var _txMicGainNode = null; // device-side software mic gain (browser-local)

// Device-side mic gain: software GainNode on the capture graph, slider
// 0–200 maps to linear 0–2× (100 = unity). Browser-local like the 🔊 Vol
// slider — independent of the radio's CAT mic_gain. Persisted in a cookie.
function _getDeviceMicGain() {
	var v = 100;
	try {
		var s = FT710Settings.getCookie("ft710_micVol");
		if (s !== null) v = parseInt(s);
	} catch (e) {}
	if (isNaN(v)) v = 100;
	return Math.max(0, Math.min(200, v)) / 100;
}

function _ensureMicGainNode() {
	if (!_txMicGainNode && _txMicCtx) {
		_txMicGainNode = _txMicCtx.createGain();
		_txMicGainNode.gain.value = _getDeviceMicGain();
	}
	return _txMicGainNode;
}

// Linear gain 0–2. setTargetAtTime avoids zipper noise on slider drags.
function _setDeviceMicGain(linear) {
	if (_txMicGainNode && _txMicCtx) {
		try {
			_txMicGainNode.gain.setTargetAtTime(linear, _txMicCtx.currentTime, 0.02);
		} catch (e) {
			_txMicGainNode.gain.value = linear;
		}
	}
}

function flushTXCapture() {
	if (_txMicAw && _txMicAw.port) {
		try {
			_txMicAw.port.postMessage({ type: "flush" });
		} catch (e) {}
	}
}

function resampleFloat32To48k(input, inRate) {
	if (!input || input.length === 0 || Math.abs(inRate - 48000) < 1) {
		return input;
	}
	const outLen = Math.max(1, Math.round((input.length * 48000) / inRate));
	const out = new Float32Array(outLen);
	const step = inRate / 48000;
	for (let i = 0; i < outLen; i++) {
		const pos = i * step;
		const idx = Math.floor(pos);
		const frac = pos - idx;
		const a = input[Math.min(idx, input.length - 1)];
		const b = input[Math.min(idx + 1, input.length - 1)];
		out[i] = a + (b - a) * frac;
	}
	return out;
}

function startTXAudio() {
	if (txAudioRunning) return;
	if (!wsAudioTX || wsAudioTX.readyState !== WebSocket.OPEN) {
		connectAudioTX();
	}

	const worker = ensureTXOpusWorker();
	txAudioRunning = true;
	flushTXCapture();
	worker.postMessage({ type: "start" });
	console.log("TX audio capture started");

	// If we already have a cached mic stream, reuse it — no permission prompt
	if (_txMicStream) {
		if (_txMicCtx && _txMicCtx.state === "suspended") {
			_txMicCtx.resume().catch(() => {});
		}
		return;
	}

	// First time: get mic permission
	console.log("First TX — requesting mic permission...");

	navigator.mediaDevices
		.getUserMedia({
			audio: {
				sampleRate: 48000,
				channelCount: 1,
				echoCancellation: false,
				noiseSuppression: false,
				autoGainControl: false,
			},
		})
		.then((stream) => {
			_txMicStream = stream;
			txAudioStream = stream;

			_txMicCtx = new (window.AudioContext || window.webkitAudioContext)({
				sampleRate: 48000,
			});
			if (_txMicCtx.state === "suspended") {
				_txMicCtx.resume().catch(() => {});
			}

			_txMicSource = _txMicCtx.createMediaStreamSource(stream);

			var canUseWorklet = !!(_txMicCtx.audioWorklet && window.AudioWorkletNode);

			return (
				canUseWorklet
					? _txMicCtx.audioWorklet.addModule(
							"/tx_capture_worklet.js?v=tx-audio-4",
						)
					: Promise.reject(new Error("AudioWorklet/SAB unavailable"))
			)
				.then(() => {
					_txMicAw = new AudioWorkletNode(_txMicCtx, "tx-capture", {
						numberOfInputs: 1,
						numberOfOutputs: 1,
						outputChannelCount: [1],
					});
					_txMicAw.port.onmessage = (ev) => {
						const d = ev.data || {};
						if (d.type === "frame" && d.frame && txOpusWorker) {
							txOpusWorker.postMessage(
								{
									type: "float_frame",
									frame: d.frame,
								},
								[d.frame],
							);
						}
					};
					_txMicMute = _txMicCtx.createGain();
					_txMicMute.gain.value = 0;

					_txMicSource.connect(_ensureMicGainNode());
					_txMicGainNode.connect(_txMicAw);
					_txMicAw.connect(_txMicMute);
					_txMicMute.connect(_txMicCtx.destination);
					flushTXCapture();
					console.log("TX audio capture initialized (AudioWorklet frame mode)");
				})
				.catch((e) => {
					console.warn(
						"TX AudioWorklet unavailable, fallback to ScriptProcessor:",
						e,
					);
					_txMicSp = _txMicCtx.createScriptProcessor(512, 1, 1);

					_txMicSp.onaudioprocess = (event) => {
						if (!txAudioRunning) return;
						const input = resampleFloat32To48k(
							event.inputBuffer.getChannelData(0),
							_txMicCtx.sampleRate || 48000,
						);
						if (txOpusWorker) {
							const i16 = new Int16Array(input.length);
							for (let i = 0; i < input.length; i++) {
								const v = input[i] * 32767;
								i16[i] = v > 32767 ? 32767 : v < -32768 ? -32768 : v | 0;
							}
							txOpusWorker.postMessage(
								{
									type: "frame",
									frame: i16.buffer,
								},
								[i16.buffer],
							);
						}
					};

					_txMicSource.connect(_ensureMicGainNode());
					_txMicGainNode.connect(_txMicSp);
					_txMicSp.connect(_txMicCtx.destination);
					console.log(
						"TX audio capture initialized (ScriptProcessor fallback)",
					);
				});
		})
		.catch((e) => {
			console.error("TX audio mic access failed:", e);
			// Fallback: direct Int16 PCM without Opus
			startTXAudioFallback();
		});
}

function startTXAudioFallback() {
	if (_txMicStream) {
		if (_txMicCtx && _txMicCtx.state === "suspended") {
			_txMicCtx.resume().catch(() => {});
		}
		return;
	}

	navigator.mediaDevices
		.getUserMedia({
			audio: {
				sampleRate: 48000,
				channelCount: 1,
				echoCancellation: false,
				noiseSuppression: false,
				autoGainControl: false,
			},
		})
		.then((stream) => {
			_txMicStream = stream;
			txAudioStream = stream;
			_txMicCtx = new (window.AudioContext || window.webkitAudioContext)({
				sampleRate: 48000,
			});
			if (_txMicCtx.state === "suspended") _txMicCtx.resume().catch(() => {});
			_txMicSource = _txMicCtx.createMediaStreamSource(stream);
			_txMicSp = _txMicCtx.createScriptProcessor(512, 1, 1);

			_txMicSp.onaudioprocess = (event) => {
				if (!txAudioRunning) return;
				const input = resampleFloat32To48k(
					event.inputBuffer.getChannelData(0),
					_txMicCtx.sampleRate || 48000,
				);
				const i16 = new Int16Array(input.length);
				for (let i = 0; i < input.length; i++) {
					const v = input[i] * 32767;
					i16[i] = v > 32767 ? 32767 : v < -32768 ? -32768 : v | 0;
				}
				const tagged = new Uint8Array(1 + i16.byteLength);
				tagged[0] = 0x00;
				tagged.set(new Uint8Array(i16.buffer), 1);
				sendTXAudioFrame(tagged.buffer);
			};

			_txMicSource.connect(_ensureMicGainNode());
			_txMicGainNode.connect(_txMicSp);
			_txMicSp.connect(_txMicCtx.destination);
			console.log("TX audio capture started (PCM fallback)");
		})
		.catch((e) => {
			console.error("TX audio fallback failed:", e);
			txAudioRunning = false;
		});
}

function stopTXAudio() {
	txAudioRunning = false;
	if (txOpusWorker) txOpusWorker.postMessage({ type: "stop" });
	flushTXCapture();
	// Keep mic stream alive for reuse — don't stop tracks
	if (wsAudioTX && wsAudioTX.readyState === WebSocket.OPEN) {
		wsAudioTX.send("s:");
	}
	console.log("TX audio stopped");
}

window.TXDebug = {
	startTone: (freq, level) => {
		connectAudioTX();
		ensureTXOpusWorker().postMessage({
			type: "tone_start",
			freq: freq || 1000,
			level: level || 0.2,
		});
		console.warn("TX debug tone started. Press/hold PTT to transmit it.");
	},
	stopTone: () => {
		if (txOpusWorker) txOpusWorker.postMessage({ type: "tone_stop" });
		console.warn("TX debug tone stopped.");
	},
};

// ── Bandwidth + Latency display ─────────────────────────────────────
(() => {
	window.__netBytes = {
		rxCtrl: 0,
		rxSpec: 0,
		rxAudio: 0,
		txCtrl: 0,
		txAudio: 0,
	};
	setInterval(() => {
		var b = window.__netBytes;
		var rxTotal = b.rxCtrl + b.rxSpec + b.rxAudio;
		var txTotal = b.txCtrl + b.txAudio;
		var rxKbps = (rxTotal * 8) / 1000;
		var txKbps = (txTotal * 8) / 1000;
		b.rxCtrl = b.rxSpec = b.rxAudio = b.txCtrl = b.txAudio = 0;
		var el = document.getElementById("status-bitrate");
		if (el) {
			el.textContent =
				"↓" + rxKbps.toFixed(0) + "K ↑" + txKbps.toFixed(0) + "K";
		}
		var rtt = window.__lastRtt ? window.__lastRtt + "ms" : "--";
		var jitter =
			typeof window.__jitterMs === "number" ? window.__jitterMs + "ms" : "--";
		var el2 = document.getElementById("status-latency");
		if (el2) {
			el2.textContent = "RTT " + rtt + " J" + jitter;
		}
	}, 1000);
})();

// ── Screen Wake Lock + Fullscreen (mobile keep-foreground) ────────────
// Wake Lock API (primary) + silent-video/-audio fallback (iOS Safari /
// older Android) keeps the screen on while the page is visible. Fullscreen
// hides browser chrome. Both need a secure context (HTTPS) and a user
// gesture.
//
// Fallback MUST start synchronously from the click handler — on iOS Safari
// mediaElement.play() is rejected outside a direct user-gesture call chain
// (await / setTimeout break it). That was the root cause of the first
// attempt: play() inside an async function lost the gesture.

var WakeLockMgr = (() => {
	var sentinel = null; // WakeLockSentinel
	var videoEl = null; // <video> fallback
	var audioEl = null; // <audio> fallback (belt-and-suspenders)
	var requested = false; // user wants wake-lock
	var btn = null;
	var _gestured = false; // first user gesture has fired

	// ── helpers ──────────────────────────────────────────────

	function wakeLockAvailable() {
		return "wakeLock" in navigator;
	}
	function active() {
		return sentinel !== null || videoEl !== null || audioEl !== null;
	}

	function setBtn(on, hint) {
		if (!btn) return;
		btn.setAttribute("aria-pressed", String(on));
		btn.classList.toggle("active", on);
		if (hint === "unsupported") {
			btn.title = "防锁屏（使用视频/音频保活）";
			btn.style.opacity = "0.55";
		} else {
			btn.title = on ? "屏幕常亮中（点击关闭）" : "保持屏幕常亮";
			btn.style.opacity = "";
		}
	}

	// ── fallback (synchronous — called inside click handler) ──

	function startFallback() {
		var ok = false;
		// 1. Silent <video> — the proven NoSleep technique.
		if (!videoEl) {
			try {
				videoEl = document.createElement("video");
				videoEl.setAttribute("playsinline", "");
				videoEl.setAttribute("muted", "");
				videoEl.loop = true;
				videoEl.width = 1;
				videoEl.height = 1;
				videoEl.style.cssText =
					"position:fixed;bottom:0;right:0;width:1px;height:1px;opacity:0.01;pointer-events:none";
				// Tiny silent WAV — universally decodable.
				videoEl.src =
					"data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=";
				document.body.appendChild(videoEl);
				videoEl.play(); // synchronous
				ok = true;
			} catch (e) {
				videoEl = null;
			}
		}
		// 2. Silent <audio> as additional anchor (belt & suspenders).
		if (!audioEl) {
			try {
				audioEl = new Audio(
					"data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=",
				);
				audioEl.loop = true;
				audioEl.volume = 0;
				audioEl.play(); // synchronous
				ok = true;
			} catch (e) {
				audioEl = null;
			}
		}
		return ok;
	}

	function stopFallback() {
		if (videoEl) {
			try {
				videoEl.pause();
				videoEl.removeAttribute("src");
				videoEl.load();
				videoEl.remove();
			} catch (e) {}
			videoEl = null;
		}
		if (audioEl) {
			try {
				audioEl.pause();
				audioEl.removeAttribute("src");
				audioEl.load();
			} catch (e) {}
			audioEl = null;
		}
	}

	// ── Wake Lock (async, called after sync fallback) ───────

	async function acquireWakeLock() {
		if (!wakeLockAvailable()) return false;
		try {
			sentinel = await navigator.wakeLock.request("screen");
			sentinel.addEventListener("release", () => {
				sentinel = null;
				if (requested && document.visibilityState === "visible") {
					setTimeout(acquireWakeLock, 500);
				}
				setBtn(active(), wakeLockAvailable() ? false : "unsupported");
			});
			return true;
		} catch (e) {
			return false;
		}
	}

	// ── public API ──────────────────────────────────────────

	async function enable() {
		requested = true;
		var ok = false;
		if (!active()) {
			// Fallback must have been started sync by the click handler.
			// If enable() is called from visibilitychange (not a click),
			// fallback may not be running — start it via wakeLock only.
		}
		ok = (await acquireWakeLock()) || active();
		setBtn(ok, wakeLockAvailable() ? false : "unsupported");
	}

	function disable() {
		requested = false;
		if (sentinel) {
			try {
				sentinel.release();
			} catch (e) {}
			sentinel = null;
		}
		stopFallback();
		setBtn(false, false);
	}

	function init() {
		btn = document.getElementById("btn-wakelock");
		if (!btn) return;

		// Click handler: start fallback SYNCHRONOUSLY (iOS requires a direct
		// user-gesture call chain for media.play()), then async enable.
		btn.addEventListener("click", () => {
			if (active()) {
				disable();
				return;
			}
			var hasWL = wakeLockAvailable();
			// Sync: start fallback from the click gesture
			if (!hasWL) startFallback();
			// Async: request Wake Lock (the browser extends transient activation
			// to async operations for ~5 s, so this is fine).
			requested = true;
			acquireWakeLock().then((ok) => {
				setBtn(ok || active(), hasWL ? false : "unsupported");
			});
		});

		// Re-acquire Wake Lock when the page returns to foreground.
		document.addEventListener("visibilitychange", () => {
			if (requested && document.visibilityState === "visible" && !sentinel) {
				acquireWakeLock().then((ok) => {
					setBtn(active(), wakeLockAvailable() ? false : "unsupported");
				});
			}
		});
	}

	// ── Auto-enable on first user gesture (any tap) ────────
	// The first touch/click/key anywhere on the page auto-enables the
	// wake lock so the operator doesn't have to remember.
	// Retries on subsequent gestures if initial attempt fails (e.g.
	// browser transient-activation window expired before async
	// completion).  A 30 s periodic refresh re-acquires silently-
	// released sentinels (some desktop browsers release after a few
	// minutes even with a visible foreground tab).
	function initAutoEnable() {
		function onGesture(ev) {
			if (active()) return; // already running — nothing to do
			var hasWL = wakeLockAvailable();
			if (!hasWL) startFallback();
			requested = true;
			acquireWakeLock().then((ok) => {
				setBtn(ok || active(), hasWL ? false : "unsupported");
			});
		}
		// Listen continuously (not {once}) so a failed attempt gets
		// retried on the very next tap/click/key.
		document.addEventListener("touchstart", onGesture, { passive: false });
		document.addEventListener("pointerdown", onGesture);
		document.addEventListener("keydown", onGesture);

		// Periodic refresh: re-request if sentinel was silently released
		// (some browsers drop it after a timeout even while visible).
		setInterval(() => {
			if (requested && document.visibilityState === "visible" && !active()) {
				console.log("WakeLockMgr: periodic refresh (sentinel lost)");
				var hasWL = wakeLockAvailable();
				if (!hasWL) startFallback();
				acquireWakeLock().then((ok) => {
					setBtn(ok || active(), hasWL ? false : "unsupported");
				});
			}
		}, 30000);
	}

	return { init: init, initAutoEnable: initAutoEnable, active: active };
})();

var FullscreenMgr = (() => {
	var btn = null;

	function isFullscreen() {
		return !!(document.fullscreenElement || document.webkitFullscreenElement);
	}

	function setBtn(active) {
		if (!btn) return;
		btn.setAttribute("aria-pressed", active ? "true" : "false");
		btn.classList.toggle("active", active);
		btn.title = active ? "退出全屏" : "全屏切换";
	}

	async function enter() {
		var el = document.documentElement;
		try {
			if (el.requestFullscreen) await el.requestFullscreen();
			else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
			else console.warn("Fullscreen API unavailable on this platform.");
		} catch (e) {
			console.warn("enterFullscreen failed:", e && e.name);
		}
	}

	function exit() {
		try {
			if (document.exitFullscreen) document.exitFullscreen();
			else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
		} catch (e) {}
	}

	function toggle() {
		isFullscreen() ? exit() : enter();
	}

	function init() {
		btn = document.getElementById("btn-fullscreen");
		if (btn) btn.addEventListener("click", toggle);
		var sync = () => {
			setBtn(isFullscreen());
		};
		document.addEventListener("fullscreenchange", sync);
		document.addEventListener("webkitfullscreenchange", sync);
	}

	return { init: init, toggle: toggle, isFullscreen: isFullscreen };
})();

// ── Audio gain helper ────────────────────────────────────────────────
// The 🔊 Vol slider is browser-side playback volume, persisted in a
// cookie — it is deliberately NOT the radio's CAT AF gain, so the
// CAT poll can never fight the user's volume setting.
// A 10× boost compensates for quiet FT-710 USB audio; clamped at 10× to
// prevent runaway gain from blowing out speakers.  Re-set per radio model
// on fullState (IC-7300 → 1.0).
var AUDIO_GAIN_BOOST = 10.0;
var AUDIO_GAIN_MAX = 10.0;
// During TX/TUNE the radio may pass sidetone / its own audio back over the
// USB RX path, which the operator hears as a self-monitoring echo through the
// speakers. Dim the RX playback gain to kill that echo. 0 = full mute; raise
// it (e.g. 0.15) if you want to monitor your own transmitted audio instead.
var AUDIO_TX_DIM_FACTOR = 0.0;

function _getBrowserVolume() {
	var v = 128;
	try {
		var s = FT710Settings.getCookie("ft710_afVol");
		if (s !== null) v = parseInt(s);
	} catch (e) {}
	if (isNaN(v)) v = 128;
	return Math.max(0, Math.min(255, v));
}

// Mic gain is a radio-side CAT setting the radio itself does not restore
// for the web UI; the user's last value lives in a cookie and is pushed
// back to the radio on every (re)connect when it differs from the
// fullState value. The settings poll then confirms it like any other set.
function _applySavedMicGain() {
	var s = FT710Settings.getCookie("ft710_micGain");
	if (s === null) return;
	var v = parseInt(s);
	if (isNaN(v)) return;
	v = Math.max(0, Math.min(100, v));
	if (v !== radioState.mic_gain) sendCommand("mic_gain", v);
}

// True while the radio is keyed (PTT or TUNE). Drives RX playback dimming.
function _rxTransmitting() {
	return (
		typeof radioState !== "undefined" &&
		(radioState.is_transmitting ||
			radioState.tx_status === 1 ||
			radioState.tx_status === 2)
	);
}

// Smoothly set the RX GainNode target. setTargetAtTime gives click-free
// transitions (important for PTT mute/unmute); falls back to a direct set.
function _setRxGainTarget(target) {
	if (typeof AudioRX_gain_node === "undefined" || !AudioRX_gain_node) return;
	try {
		var ctx = AudioRX_context;
		if (ctx && ctx.currentTime !== undefined) {
			AudioRX_gain_node.gain.setTargetAtTime(target, ctx.currentTime, 0.015);
			return;
		}
	} catch (e) {}
	AudioRX_gain_node.gain.value = target;
}

function _applyAfGainToAudioNode() {
	if (typeof AudioRX_gain_node === "undefined" || !AudioRX_gain_node) return;
	var raw = _getBrowserVolume() / 255.0;
	var boosted = Math.min(AUDIO_GAIN_MAX, raw * AUDIO_GAIN_BOOST);
	// Dim RX playback during transmit to prevent self-monitoring echo.
	_setRxGainTarget(_rxTransmitting() ? boosted * AUDIO_TX_DIM_FACTOR : boosted);
}

document.addEventListener("DOMContentLoaded", () => {
	WakeLockMgr.init();
	WakeLockMgr.initAutoEnable();
	FullscreenMgr.init();
});
