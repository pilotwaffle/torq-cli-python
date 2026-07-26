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
  focus() { if (!this.disabled) this.focused = true; }
}

const elements = new Map();
const document = {
  readyState: "loading",
  getElementById: (id) => elements.get(id) || null,
  createElement: (tag) => new Element(tag),
  addEventListener: () => {},
};
const context = {
  console,
  document,
  btoa: (value) => Buffer.from(value, "binary").toString("base64"),
  Uint8Array,
  Map,
  Set,
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
  ]) elements.set(id, new Element(id));
  const runtime = new ChatRuntime();
  runtime.bind();
  assert.equal(elements.get("chat-input").disabled, true, "chat starts unavailable and fail closed");
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
