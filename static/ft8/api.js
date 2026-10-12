// REST client for the platform-hosted FT8 surface (/api/ft8/*).
//
// Authentication is the platform's cookie (AUTH_COOKIE); the login page is
// the platform's own.  401 means the platform session is gone, so reload
// (which lands on the platform login) instead of leaving a dead cockpit.

let revision = 0;

export function currentRevision() {
  return revision;
}

async function request(path, { method = "GET", body, idempotencyKey } = {}) {
  const headers = {};
  if (body !== undefined) headers["content-type"] = "application/json";
  if (idempotencyKey) headers["idempotency-key"] = idempotencyKey;
  const response = await fetch(`/api/ft8${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  let payload = {};
  try { payload = await response.json(); } catch { /* empty body */ }
  if (typeof payload.revision === "number") revision = payload.revision;
  if (response.status === 401) location.reload();
  return { status: response.status, ...payload };
}

export function key() {
  return crypto.randomUUID();
}

export const api = {
  state: () => request("/state"),

  select: (candidate) =>
    request("/operation/select", {
      method: "POST",
      idempotencyKey: key(),
      body: {
        dx_call: candidate.call,
        dx_grid: candidate.grid || "",
        snr_db: candidate.snr,
        text: candidate.text,
        is_cq: candidate.is_cq,
        slot_id: candidate.slot_id,
        freq: candidate.freq,
      },
    }),
  reply: (candidate) =>
    request("/operation/reply", {
      method: "POST",
      idempotencyKey: key(),
      body: candidate
        ? {
            dx_call: candidate.call,
            dx_grid: candidate.grid || "",
            snr_db: candidate.snr,
            text: candidate.text,
            is_cq: candidate.is_cq,
            slot_id: candidate.slot_id,
            freq: candidate.freq,
          }
        : {},
    }),
  cq: (loop = false) =>
    request("/operation/cq", { method: "POST", idempotencyKey: key(), body: { loop } }),
  qsos: () => request("/logs/qsos"),
  dxcc: () => request("/dxcc"),
  settings: () => request("/settings"),
  putSetting: (settingKey, value) =>
    request("/settings", { method: "PUT", idempotencyKey: key(), body: { [settingKey]: value } }),
  txOff: () => request("/operation/enable_tx_off", { method: "POST", idempotencyKey: key() }),
  stop: () => request("/operation/stop", { method: "POST", idempotencyKey: key() }),
  clearFault: (interlock) =>
    request("/operation/clear-fault", {
      method: "POST",
      idempotencyKey: key(),
      body: interlock ? { interlock } : {},
    }),
  band: (freqHz) =>
    request("/radio/band", {
      method: "POST",
      idempotencyKey: key(),
      body: { freq_hz: freqHz },
    }),
  mode: (target) =>
    request("/mode", { method: "POST", idempotencyKey: key(), body: { mode: target } }),
};
