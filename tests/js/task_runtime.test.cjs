"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const context = {
  console,
  globalThis: null,
  document: { getElementById: () => null },
  Promise,
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, {filename:"task.js"});
const {MutationQueue, EditGate, requestIdForPlan} = context.TorqTask;

(async () => {
  const queue = new MutationQueue();
  const order = [];
  let release;
  const promiseGate = new Promise((resolve) => { release = resolve; });
  const first = queue.enqueue(async () => { await promiseGate; order.push("save"); });
  const second = queue.enqueue(() => { order.push("start"); });
  await Promise.resolve();
  assert.deepEqual(order, [], "later mutation must not overtake pending save");
  release();
  await Promise.all([first, second]);
  assert.deepEqual(order, ["save", "start"]);

  const gate = new EditGate();
  const ticket = gate.capture("project-a", "draft-a");
  assert.equal(gate.accepts(ticket, "project-a", "draft-a"), true);
  gate.edited();
  assert.equal(gate.accepts(ticket, "project-a", "draft-a"), false, "editing rejects stale plan response");
  const switchTicket = gate.capture("project-a", "draft-a2");
  assert.equal(gate.accepts(switchTicket, "project-b", "draft-a2"), false, "project switch rejects stale load");

  const values = new Map();
  const storage = {getItem:(key) => values.get(key) || null, setItem:(key, value) => values.set(key, value)};
  const crypto = {randomUUID:() => "stable-request"};
  const hash = `sha256:${"1".repeat(64)}`;
  assert.equal(requestIdForPlan(hash, storage, crypto), "stable-request");
  assert.equal(requestIdForPlan(hash, storage, {randomUUID:() => "different"}), "stable-request", "ambiguous retry reuses request identity");
  assert.equal(requestIdForPlan(hash, {getItem:() => {throw new Error("blocked");}}, crypto), `request-${hash.slice(7,31)}`);
  console.log("task runtime contract checks passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
