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
const {MutationQueue, EditGate, ReviewGate, RecoveryGate, HistoryGate, stableRequestId, forgetAfterDefiniteRejection, requestIdForPlan, unwrap, formatNewlineMetadata, formatCheckOutput, canRecover, applicationApplied, applicationBlocksApply, applicationRetryIdentity, applicationOutcomeMessage} = context.TorqTask;

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

  const review = new ReviewGate();
  const oldSelection = review.select("task-a", 7);
  const currentSelection = review.select("task-b", 11);
  assert.equal(review.accepts(oldSelection), false, "delayed review cannot replace the current selection");
  assert.equal(review.accepts(currentSelection), true);
  const pendingMutation = review.capture();
  review.select("task-a", 7);
  assert.equal(review.accepts(pendingMutation), false, "delayed mutation cannot enable another candidate");
  const recovery = new RecoveryGate();
  const oldRecovery = recovery.select("project-a");
  recovery.select("project-b");
  assert.equal(recovery.accepts(oldRecovery), false, "delayed recovery status cannot replace a switched project");
  assert.equal(canRecover({state:"recovery_required",owned:false}), false, "another installation's recovery is never actionable");
  assert.equal(canRecover({state:"recovery_required",owned:true}), true);
  const history = new HistoryGate();
  const oldQuery = history.select("first goal");
  const currentQuery = history.select("second goal");
  assert.equal(history.accepts(oldQuery), false, "delayed old-query history cannot replace a new search");
  assert.equal(history.accepts(currentQuery), true);
  const hash = `sha256:${"1".repeat(64)}`;
  const retryCandidate = {review_hash:hash}, retryAcceptance = {decision_id:"decision-a"};
  assert.equal(applicationApplied({state:"rejected",application_id:"application-r"}), false);
  assert.equal(applicationApplied({state:"rolled_back",application_id:"application-b"}), false);
  assert.equal(applicationApplied({state:"recovery_required",application_id:"application-x"}), false);
  assert.equal(applicationApplied({state:"applied",application_id:"application-ok"}), true);
  assert.equal(applicationBlocksApply({state:"rejected",application_id:"application-r"}), false, "signed rejection permits a fresh explicitly eligible Apply");
  assert.equal(applicationBlocksApply({state:"rolled_back",application_id:"application-b"}), false, "signed rollback permits a fresh explicitly eligible Apply");
  assert.equal(applicationBlocksApply({state:"recovery_required",application_id:"application-x"}), true);
  assert.equal(applicationRetryIdentity(retryCandidate,retryAcceptance,{state:"rejected",application_id:"application-r"}), `${hash}.decision-a.application-r.rejected`);
  assert.match(applicationOutcomeMessage({state:"rolled_back",application_id:"application-b"}), /original project files were restored/);

  const values = new Map();
  const storage = {getItem:(key) => values.get(key) || null, setItem:(key, value) => values.set(key, value), removeItem:(key) => values.delete(key)};
  const crypto = {randomUUID:() => "stable-request"};
  assert.equal(requestIdForPlan(hash, storage, crypto), "stable-request");
  assert.equal(requestIdForPlan(hash, storage, {randomUUID:() => "different"}), "stable-request", "ambiguous retry reuses request identity");
  assert.equal(requestIdForPlan(hash, {getItem:() => {throw new Error("blocked");}}, crypto), `request-${hash.slice(7,31)}`);

  let generated = 0;
  const mutationCrypto = {randomUUID:() => `mutation-${++generated}`};
  assert.equal(stableRequestId("accept", hash, storage, mutationCrypto), "mutation-1");
  assert.equal(stableRequestId("accept", hash, storage, mutationCrypto), "mutation-1", "accept retry reuses its identity");
  assert.equal(stableRequestId("apply", hash, storage, mutationCrypto), "mutation-2", "apply has a separate identity");
  assert.equal(stableRequestId("continue", "application-a", storage, mutationCrypto), "mutation-3");
  assert.equal(forgetAfterDefiniteRejection(new Error("response_lost"), "continue", "application-a", storage), false);
  assert.equal(stableRequestId("continue", "application-a", storage, mutationCrypto), "mutation-3", "unknown outcome preserves Continue identity");
  const rejected = new Error("candidate_continuation_draft_occupied"); rejected.status = 409;
  assert.equal(forgetAfterDefiniteRejection(rejected, "continue", "application-a", storage), true);
  assert.equal(stableRequestId("continue", "application-a", storage, mutationCrypto), "mutation-4", "definite rejected response rotates Continue identity");

  const directReview = {
    schema: "torq-task-review-v1",
    review_hash: hash,
    review: {task_id: "task-a", ready_sequence: 7},
  };
  assert.equal(unwrap(directReview, "review"), directReview, "direct review envelope must retain its schema and hash");
  const accepted = {
    schema: "torq-candidate-acceptance-v1",
    state: "accepted",
    candidate: {review_hash: hash},
    acceptance: {decision_id: "decision-a"},
    source_changed: false,
  };
  const wrappedAccept = {status: "accepted", result: accepted};
  assert.equal(unwrap(wrappedAccept, "acceptance"), accepted, "mutation wrapper must yield the full acceptance result");
  assert.equal(unwrap(accepted, "acceptance"), accepted, "direct acceptance must not collapse to DecisionRef");
  const continued = {schema:"torq-candidate-continuation-v1", draft:{project_id:"calculator-project", revision:4}};
  const wrappedContinue = {status:"accepted", result:continued};
  assert.equal(unwrap(wrappedContinue, "draft"), continued, "wrapped Continue must retain its schema envelope");
  assert.equal(unwrap(wrappedContinue, "draft").draft.project_id, "calculator-project");
  assert.equal(
    formatNewlineMetadata({style:"crlf", final_newline:false, bytes:27, line_count:2}, "ignored\n"),
    "CRLF · 27 bytes · no final newline",
    "structured backend newline metadata must not be string-coerced",
  );
  assert.equal(
    formatCheckOutput({contract:"torq-structural-result-v1",checked:[{path:"calculator.py",validator:"python_ast"}],status:"passed"}),
    "Status: passed\nChecked files:\n• calculator.py — python ast",
    "structured check output must remain readable",
  );
  console.log("task runtime contract checks passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
