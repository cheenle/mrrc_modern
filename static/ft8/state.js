// Tiny client state store: streams and REST intents write, views subscribe.

const state = {
  revision: 0,
  lease: { held: false, mine: false },
  safety: { armed: false, ptt_on: false, faults: [] },
  sequencer: { state: "idle", tx_enabled: false, dx_call: "" },
  selected: null,
  radio: { freq_hz: null },
  station: { my_call: "", my_grid: "", worked_calls: [] },
  settings: null,
  candidates: [],
  connected: { state: false, decodes: false, waterfall: false },
};

const listeners = new Set();

export function getState() {
  return state;
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function patch(fragment) {
  Object.assign(state, fragment);
  for (const listener of listeners) listener(state);
}

export function applySnapshot(snapshot) {
  patch({
    revision: snapshot.revision ?? state.revision,
    lease: snapshot.lease ?? state.lease,
    safety: snapshot.safety ?? state.safety,
    sequencer: snapshot.sequencer ?? state.sequencer,
    selected: snapshot.selected ?? state.selected,
    radio: snapshot.radio ?? state.radio,
    station: snapshot.station ?? state.station,
    settings: snapshot.settings ?? state.settings,
  });
}

export function applyDecodeBatch(batch) {
  // Chronological feed (WSJT-X main window style): every decode is a row,
  // newest slot on top.  Row time is the slot's own end time, not receipt
  // time, so replayed history keeps its true age; re-applying a slot
  // (live + reconnect replay) replaces rather than duplicates its rows.
  const slotEnd = batch.slot_id * 15_000 + 15_000;
  const rows = (batch.messages || []).map((m) => ({
    ...m,
    slot_id: batch.slot_id,
    late: batch.late,
    _t: slotEnd,
  }));
  const kept = state.candidates.filter((c) => c.slot_id !== batch.slot_id);
  patch({ candidates: [...rows, ...kept].slice(0, 400) });
}
