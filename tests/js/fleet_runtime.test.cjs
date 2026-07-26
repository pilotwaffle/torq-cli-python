"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const startup = source.lastIndexOf("renderRail([]);");
assert.ok(startup > 0, "fleet startup seam missing");
const storage = new Map();
const notices = [];
function Notification(title, options) { notices.push({ title, options }); }
Notification.permission = "granted";
const window = {
  Notification,
  setTimeout: (callback) => { callback(); return 1; },
  clearTimeout: () => {},
};
const context = {
  console,
  window,
  Notification,
  document: {},
  crypto: { randomUUID: () => "runtime-id" },
  localStorage: {
    getItem: (key) => storage.has(key) ? storage.get(key) : null,
    setItem: (key, value) => storage.set(key, value),
  },
};
vm.createContext(context);
vm.runInContext(source.slice(0, startup) + `
  globalThis.underTest = {
    safeState,
    validEnvelope,
    meaningfulChanges,
    stateFingerprint,
    durationText,
    notifyActions,
  };
`, context, { filename: "fleet-runtime-test.js" });
const api = context.underTest;
const envelope = {
  snapshot: {
    verification: { state: "live_verified" },
    run: {
      run_id: "run-runtime",
      workflow_state: "closed",
      started_at: "2026-07-25T12:00:00Z",
      updated_at: "2026-07-25T12:01:05Z",
    },
    lanes: [{ role: "g1d", state: "queued" }],
    actions: [],
  },
  annotations: [],
  session: { write_capable: true, read_only_reason: null, token: "secret" },
  eligibility: {},
  pending: [],
};
assert.equal(api.validEnvelope(envelope), true);
assert.equal(api.safeState("invented"), "queued");
assert.equal(api.durationText(envelope.snapshot), "~1m 05s");
assert.equal(api.stateFingerprint(envelope).includes("secret"), false);

const changed = structuredClone(envelope);
changed.snapshot.lanes[0].state = "blocked";
changed.snapshot.actions.push({ action_id: "approve-1", state: "open" });
changed.annotations.push({
  kind: "orphaned",
  scope: "run",
  observed_at: "2026-07-25T12:01:05Z",
});
const messages = api.meaningfulChanges(envelope, changed);
assert.deepEqual(
  Array.from(messages),
  ["g1d blocked", "1 new operator action", "orphaned"],
);

api.notifyActions(changed.snapshot.actions, "run-runtime");
api.notifyActions(changed.snapshot.actions, "run-runtime");
assert.equal(notices.length, 1, "notification must be deduplicated across replay");
const seen = JSON.parse(storage.get("torq.fleet.notified-actions.v2"));
assert.deepEqual(Array.from(seen), ["run-runtime:approve-1"]);
