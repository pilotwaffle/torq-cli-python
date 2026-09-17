"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2], "utf8");
assert.equal(source.includes("innerHTML"), false, "runtime must not introduce an HTML injection sink");
for (const route of [
  "/api/v1/chat",
  "/api/v1/chat/turns",
  "/api/v1/chat/turns/${encodeURIComponent(turnId)}/cancel",
  "/api/v1/chat/events",
]) assert.ok(source.includes(route), `missing chat route ${route}`);

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.className = "";
    this._textContent = "";
    this.attributes = {};
    this.listeners = {};
    this.disabled = false;
    this.value = "";
    this.files = [];
    this.focused = false;
  }
  set innerHTML(_value) { throw new Error("unsafe innerHTML used"); }
  set textContent(value) { this._textContent = String(value); }
  get textContent() { return this._textContent; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelectorAll() { return []; }
  focus() { if (!this.disabled) this.focused = true; }
}

const elements = new Map();
const document = {
  readyState: "loading",
  body: { dataset: {} },
  getElementById: (id) => elements.get(id) || null,
  createElement: (tag) => new Element(tag),
  addEventListener: () => {},
};
const sessionValues = new Map();
const localValues = new Map();
const context = {
  console,
  document,
  btoa: (value) => Buffer.from(value, "binary").toString("base64"),
  Uint8Array,
  Map,
  Set,
  crypto: { randomUUID: () => "turn-runtime" },
  localStorage: {
    getItem: (key) => localValues.get(key) || null,
    setItem: (key, value) => localValues.set(key, value),
  },
  sessionStorage: {
    getItem: (key) => sessionValues.get(key) || null,
    setItem: (key, value) => sessionValues.set(key, value),
  },
};
vm.createContext(context);
vm.runInContext(source, context, { filename: "chat.js" });
const { ChatRuntime, EventGate, renderMessage, validateFile, serializeAttachment } = context.TorqChat;

const gate = new EventGate();
assert.equal(gate.accept({ event_id: "e1", sequence: 1 }), true);
assert.equal(gate.accept({ event_id: "e1", sequence: 2 }), false, "duplicate identity rejected");
assert.equal(gate.accept({ event_id: "e3", sequence: 3 }), true);
assert.equal(gate.accept({ event_id: "e2", sequence: 2 }), false, "out-of-order sequence rejected");
assert.equal(gate.accept({ event_id: "bad", sequence: "not-a-number" }), false);

const transcript = new Element("section");
const hostile = "<img src=x onerror=alert(1)>";
renderMessage(transcript, { role: "assistant", sequence: 4, content: hostile });
assert.equal(transcript.children.length, 1);
assert.equal(transcript.children[0].children[1].textContent, hostile, "message remains inert text");

function fakeFile(name, type, bytes) {
  const buffer = Uint8Array.from(bytes);
  return {
    name,
    type,
    size: buffer.length,
    slice: (start, end) => ({ arrayBuffer: async () => buffer.slice(start, end).buffer }),
    arrayBuffer: async () => buffer.buffer,
  };
}

(async () => {
  const pngBytes = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2];
  const valid = await validateFile(fakeFile("proof.png", "image/png", pngBytes));
  assert.equal(valid.media_type, "image/png");
  assert.deepEqual(
    JSON.parse(JSON.stringify(await serializeAttachment(valid))),
    {
      name: "proof.png",
      media_type: "image/png",
      content_base64: Buffer.from(pngBytes).toString("base64"),
    },
  );
  await assert.rejects(
    () => validateFile(fakeFile("proof.png", "image/png", [1, 2, 3, 4, 5, 6, 7, 8])),
    /attachment_signature_mismatch/,
  );
  await assert.rejects(
    () => validateFile(fakeFile("payload.svg", "image/svg+xml", [1])),
    /attachment_type_unsupported/,
  );
  await assert.rejects(
    () => validateFile(fakeFile("..\\escape.pdf", "application\/pdf", [0x25, 0x50, 0x44, 0x46, 0x2d])),
    /attachment_name_invalid/,
  );

  for (const id of [
    "chat-transcript", "chat-announcer", "chat-composer", "chat-input",
    "chat-attachments", "chat-attachment-list", "chat-send", "chat-stop", "chat-status",
    "chat-help", "clear-draft", "draft-state", "workspace-view", "fleet-board",
    "workspace-view-button", "fleet-view-button", "workspace-composer-slot",
    "workspace-transcript-slot", "fleet-composer-slot", "fleet-transcript-slot",
    "chat-workspace-section", "workspace-refresh", "workspace-suggestions", "skip-link",
    "workspace-setup", "workspace-details", "workspace-details-summary", "chat-kicker",
    "chat-title", "chat-evidence-note", "composer-purpose",
    "workspace-title", "workspace-context", "workspace-summary", "next-action-title",
    "next-action-message", "next-action-remediation", "workspace-root-kind",
    "workspace-run-id", "workspace-trust", "workspace-mode", "workspace-provider",
    "run-picker", "run-picker-list",
  ]) elements.set(id, new Element(id));
  const runtime = new ChatRuntime();
  runtime.bind();
  assert.equal(elements.get("chat-input").disabled, false, "draft remains editable while unavailable");
  assert.equal(elements.get("chat-send").disabled, true, "submission starts fail closed");
  assert.equal(elements.get("workspace-view").hidden, true, "Fleet remains the default view");
  elements.get("workspace-view-button").listeners.click();
  assert.equal(elements.get("workspace-view").hidden, false);
  assert.equal(localValues.get("torq.workspace.view.v1"), "workspace");
  runtime.workspace = {
    capabilities: {
      can_discuss_run: true,
      can_cancel: false,
      attachment_types: ["image/png"],
    },
  };
  runtime.applySnapshot({
    data_status: "available",
    active_turn_id: "turn-1",
    messages: [{ message_id: "restored", role: "user", content: "history" }],
    status: "running",
  });
  assert.equal(elements.get("chat-announcer").textContent, "", "initial restored history stays silent");
  assert.equal(elements.get("chat-stop").disabled, false, "Stop enabled for active turn");
  runtime.applySnapshot({
    data_status: "available",
    active_turn_id: null,
    messages: [
      { message_id: "restored", role: "user", content: "history" },
      { message_id: "answer", role: "assistant", content: "done" },
    ],
    status: "completed",
  });
  assert.equal(elements.get("chat-announcer").textContent, "New assistant message.");
  assert.equal(elements.get("chat-input").disabled, false);
  assert.equal(elements.get("chat-input").focused, true, "terminal snapshot restores composer focus");

  const workspaceA = `workspace_${"a".repeat(32)}`;
  const workspaceB = `workspace_${"b".repeat(32)}`;
  runtime.loadDraft(workspaceA);
  elements.get("chat-input").value = "draft A";
  elements.get("chat-input").listeners.input();
  runtime.loadDraft(workspaceB);
  assert.equal(elements.get("chat-input").value, "", "draft does not migrate across roots");
  elements.get("chat-input").value = "draft B";
  elements.get("chat-input").listeners.input();
  runtime.loadDraft(workspaceA);
  assert.equal(elements.get("chat-input").value, "draft A", "draft restores only in its root scope");

  runtime.runtimeAvailable = true;
  runtime.workspace.capabilities.can_discuss_run = true;
  let acceptRequest;
  runtime.request = () => new Promise((resolve) => { acceptRequest = resolve; });
  const pendingSubmit = runtime.submit();
  elements.get("chat-input").value = "follow-up written during acceptance";
  elements.get("chat-input").listeners.input();
  runtime.applySnapshot({ data_status: "available", active_turn_id: null, messages: [], status: "ready" });
  assert.equal(elements.get("chat-send").disabled, true, "stale snapshot cannot enable a duplicate submit");
  await Promise.resolve();
  acceptRequest({ status: "accepted" });
  await pendingSubmit;
  assert.equal(elements.get("chat-input").value, "follow-up written during acceptance", "in-flight edits survive acceptance");

  runtime.runtimeAvailable = true;
  runtime.request = async () => { throw new Error("offline"); };
  await runtime.refresh();
  assert.equal(runtime.runtimeAvailable, false, "refresh failure disables stale controls");
  assert.equal(elements.get("chat-send").disabled, true);

  const attachment = await validateFile(fakeFile("proof.png", "image/png", pngBytes));
  runtime.pendingFiles = [attachment];
  runtime.renderPendingFiles();
  const chip = elements.get("chat-attachment-list").children[0];
  assert.equal(chip.children[1].attributes["aria-label"], "Remove proof.png");
  chip.children[1].listeners.click();
  assert.equal(runtime.pendingFiles.length, 0, "staged attachments can be removed");
  console.log("chat runtime contract checks passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
