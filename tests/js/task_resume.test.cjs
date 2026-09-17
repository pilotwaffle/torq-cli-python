"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2], "utf8");
class Element {
  constructor(id) {
    this.id = id; this.hidden = false; this.disabled = id.startsWith("task-");
    this.value = ""; this.innerHTML = ""; this.textContent = ""; this.listeners = {}; this.dataset = {};
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  async dispatch(name) { await Promise.all((this.listeners[name] || []).map((fn) => fn({target:this, preventDefault(){}}))); }
  setAttribute() {} focus() {} querySelector() { return null; } querySelectorAll() { return []; }
}
const ids = [
  "task-view", "task-view-button", "workspace-view", "fleet-board", "workspace-view-button", "fleet-view-button",
  "skip-link", "task-draft-status", "task-project", "task-goal", "task-input-paths", "task-output-paths",
  "task-start", "task-plan-summary", "task-form", "task-clear", "task-review", "task-stop", "task-current",
  "task-history-list", "task-history-search", "task-history-query", "task-history-more", "live-announcer",
  "task-compose-view", "task-review-view", "review-back", "review-title", "review-goal", "review-identity",
  "review-state", "review-alert", "review-tab-plan", "review-tab-changes", "review-tab-checks",
  "review-panel-plan", "review-panel-changes", "review-panel-checks", "review-change-count", "review-files",
  "review-diff", "review-correction", "review-correction-status", "review-correction-plan", "review-child-plan",
  "review-start-revision", "review-accept", "review-apply-section", "review-apply", "review-apply-copy",
  "review-continue-section", "review-continue", "review-hashes"
  , "task-recovery-panel", "task-recovery-title", "task-recovery-message", "task-recover"
];
const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
["plan", "changes", "checks"].forEach((name) => { elements[`review-tab-${name}`].dataset.reviewTab = name; });
const document = {
  body:new Element("body"),
  getElementById:(id) => elements[id] || null,
  querySelectorAll:(selector) => selector === "[data-review-tab]" ? [elements["review-tab-plan"], elements["review-tab-changes"], elements["review-tab-checks"]] : [],
};
const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
let capabilityCalls = 0;
async function fetch(path, options = {}) {
  const method = options.method || "GET";
  if (path === "/api/v1/tasks/capabilities") {
    capabilityCalls += 1;
    return {ok:true, json:async () => ({
      projects:[{id:"project", label:"Project"}],
      can_start_task:capabilityCalls >= 3,
      active_task_id:capabilityCalls === 2 ? "active-1" : null
    })};
  }
  if (path === "/api/v1/tasks/drafts/project" && method === "GET") {
    return {ok:true, json:async () => ({draft:{goal:"", input_paths:[], output_paths:[], revision:2}})};
  }
  if (path === "/api/v1/tasks/drafts/project" && method === "POST") {
    return {ok:true, json:async () => ({result:{project_id:"project", goal:"next", input_paths:[], output_paths:["next.py"], revision:3}})};
  }
  if (path === "/api/v1/tasks" && method === "GET") return {ok:true, json:async () => ({tasks:[]})};
  if (path === "/api/v1/task-history?q=&limit=50" && method === "GET") return {ok:true, json:async () => ({schema:"torq-candidate-history-v1", items:[], next_cursor:null})};
  if (path === "/api/v1/task-recovery/project" && method === "GET") return {ok:true, json:async () => ({schema:"torq-task-recovery-v1", state:"idle", owned:false, finding:null, application_id:null})};
  if (path === "/api/v1/tasks/active-1") {
    return {ok:true, json:async () => ({task:{task_id:"active-1", state:"candidate_ready", verified:true, candidate_files:["old.py"], check:{exit_code:0}}})};
  }
  if (path === "/api/v1/tasks/plans" && method === "POST") {
    return {ok:true, json:async () => ({result:{plan_hash:`sha256:${"1".repeat(64)}`, provider:"claude", model:"sonnet", output_paths:["next.py"]}})};
  }
  throw new Error(`unexpected ${method} ${path}`);
}
const context = {
  console, document, fetch, Promise, setTimeout, clearTimeout, location:{search:""},
  localStorage:{getItem:() => null, setItem:() => {}}, crypto:{randomUUID:() => "id"}, URLSearchParams
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, {filename:"task.js"});

(async () => {
  await delay(0);
  await elements["task-view-button"].dispatch("click");
  await delay(25);
  assert.equal(capabilityCalls, 3, "terminal resumed task refreshes start capability");
  elements["task-goal"].value = "next";
  elements["task-output-paths"].value = "next.py";
  await elements["task-goal"].dispatch("input");
  await elements["task-form"].dispatch("submit");
  assert.equal(elements["task-start"].disabled, false, "a reviewed second task can start after resumed task becomes terminal");
  console.log("resumed task capability refresh checks passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
