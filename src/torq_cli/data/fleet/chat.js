"use strict";

(function chatModule(global) {
  const MAX_ATTACHMENTS = 6;
  const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;
  const POLL_INTERVAL_MS = 2500;
  const STREAM_RETRY_MS = 10000;
  const ALLOWED_TYPES = new Map([
    ["image/png", { extensions: [".png"], signature: [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a] }],
    ["image/jpeg", { extensions: [".jpg", ".jpeg"], signature: [0xff, 0xd8, 0xff] }],
    ["application/pdf", { extensions: [".pdf"], signature: [0x25, 0x50, 0x44, 0x46, 0x2d] }],
    ["text/plain", { extensions: [".txt"], signature: null }],
    ["text/markdown", { extensions: [".md", ".markdown"], signature: null }],
  ]);
  const TERMINAL_STATES = new Set(["completed", "cancelled", "failed", "cancellation_uncertain"]);

  const byId = (id) => global.document?.getElementById(id) || null;
  const createNode = (tag, className, text) => {
    const element = global.document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  };

  class EventGate {
    constructor() {
      this.lastSequence = 0;
      this.identities = new Set();
    }

    accept(event) {
      const sequence = Number(event?.sequence);
      const identity = typeof event?.event_id === "string" ? event.event_id : "";
      if (!Number.isSafeInteger(sequence) || sequence <= 0) return false;
      if (sequence <= this.lastSequence || (identity && this.identities.has(identity))) return false;
      this.lastSequence = sequence;
      if (identity) {
        this.identities.add(identity);
        if (this.identities.size > 2048) this.identities.delete(this.identities.values().next().value);
      }
      return true;
    }

    advance(sequence) {
      const value = Number(sequence);
      if (Number.isSafeInteger(value) && value > this.lastSequence) this.lastSequence = value;
    }
  }

  function extensionOf(name) {
    const lower = String(name || "").toLowerCase();
    const dot = lower.lastIndexOf(".");
    return dot >= 0 ? lower.slice(dot) : "";
  }

  async function validateFile(file) {
    if (!file || typeof file.name !== "string" || !file.name || file.name.length > 255) {
      throw new Error("attachment_name_invalid");
    }
    if (/[\u0000-\u001f\u007f/:\\]/.test(file.name)) throw new Error("attachment_name_invalid");
    if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > MAX_ATTACHMENT_BYTES) {
      throw new Error("attachment_size_invalid");
    }
    const mediaType = String(file.type || "").toLowerCase();
    const policy = ALLOWED_TYPES.get(mediaType);
    if (!policy || !policy.extensions.includes(extensionOf(file.name))) throw new Error("attachment_type_unsupported");
    if (policy.signature) {
      const prefix = new Uint8Array(await file.slice(0, policy.signature.length).arrayBuffer());
      const matches = policy.signature.every((byte, index) => prefix[index] === byte);
      if (!matches) throw new Error("attachment_signature_mismatch");
    }
    return { file, name: file.name, media_type: mediaType };
  }

  function bytesToBase64(bytes) {
    let binary = "";
    const stride = 0x8000;
    for (let index = 0; index < bytes.length; index += stride) {
      binary += String.fromCharCode(...bytes.subarray(index, index + stride));
    }
    return global.btoa(binary);
  }

  async function serializeAttachment(attachment) {
    const bytes = new Uint8Array(await attachment.file.arrayBuffer());
    return {
      name: attachment.name,
      media_type: attachment.media_type,
      content_base64: bytesToBase64(bytes),
    };
  }

  function renderMessage(container, message) {
    const row = createNode("article", "chat-record");
    const role = String(message?.role || message?.kind || "system").toLowerCase();
    row.dataset.role = role;
    const heading = createNode("header", "chat-record-meta");
    heading.append(
      createNode("strong", "chat-record-role", role.replaceAll("_", " ")),
      createNode("span", "chat-record-sequence", message?.sequence ? `SEQ ${message.sequence}` : "PENDING"),
    );
    if (message?.truncated) heading.append(createNode("span", "chat-record-sequence", "TRUNCATED"));
    const content = createNode("pre", "chat-record-content", message?.content ?? message?.text ?? "");
    row.append(heading, content);
    if (Array.isArray(message?.attachments) && message.attachments.length) {
      const files = createNode("ul", "chat-record-files");
      message.attachments.forEach((attachment) => {
        files.append(createNode("li", "", `${attachment.name || "attachment"} · ${attachment.media_type || "unknown"}`));
      });
      row.append(files);
    }
    container.append(row);
    return row;
  }

  class ChatRuntime {
    constructor() {
      this.elements = {
        transcript: byId("chat-transcript"),
        announcer: byId("chat-announcer"),
        composer: byId("chat-composer"),
        input: byId("chat-input"),
        attachments: byId("chat-attachments"),
        attachmentList: byId("chat-attachment-list"),
        send: byId("chat-send"),
        stop: byId("chat-stop"),
        status: byId("chat-status"),
      };
      this.gate = new EventGate();
      this.activeTurnId = null;
      this.pendingFiles = [];
      this.renderedMessages = new Set();
      this.stream = null;
      this.pollTimer = null;
      this.retryTimer = null;
      this.provisionalNode = null;
      this.provisionalContent = "";
      this.running = false;
      this.runtimeAvailable = false;
      this.snapshotInitialized = false;
    }

    completeDom() {
      return Object.values(this.elements).every(Boolean);
    }

    setStatus(text, mode = "idle") {
      this.elements.status.textContent = text;
      this.elements.status.dataset.mode = mode;
    }

    updateControls() {
      const active = Boolean(this.activeTurnId);
      this.elements.send.disabled = active || !this.runtimeAvailable;
      this.elements.stop.disabled = !active;
      this.elements.input.disabled = active || !this.runtimeAvailable;
      this.elements.attachments.disabled = active || !this.runtimeAvailable;
      this.elements.composer.dataset.active = String(active);
    }

    bind() {
      this.elements.transcript.setAttribute("role", "log");
      this.elements.transcript.setAttribute("aria-live", "off");
      this.elements.status.setAttribute("role", "status");
      this.elements.status.setAttribute("aria-live", "polite");
      this.elements.input.setAttribute("aria-keyshortcuts", "Control+Enter");
      this.elements.attachments.setAttribute("accept", [...ALLOWED_TYPES.keys()].join(","));
      this.elements.attachments.setAttribute("multiple", "");
      this.elements.composer.addEventListener("submit", (event) => {
        event.preventDefault();
        void this.submit();
      });
      this.elements.stop.addEventListener("click", () => void this.cancel());
      this.elements.input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && event.ctrlKey) {
          event.preventDefault();
          void this.submit();
        }
      });
      this.elements.attachments.addEventListener("change", () => void this.stageFiles());
      this.updateControls();
    }

    async stageFiles() {
      const candidates = [...(this.elements.attachments.files || [])];
      if (candidates.length > MAX_ATTACHMENTS) {
        this.elements.attachments.value = "";
        this.pendingFiles = [];
        this.renderPendingFiles();
        this.setStatus(`Attach no more than ${MAX_ATTACHMENTS} files.`, "error");
        return;
      }
      try {
        this.pendingFiles = await Promise.all(candidates.map(validateFile));
        this.renderPendingFiles();
        const names = this.pendingFiles.map((item) => item.name).join(", ");
        this.setStatus(names ? `Ready: ${names}` : "Ready", "ready");
      } catch (error) {
        this.elements.attachments.value = "";
        this.pendingFiles = [];
        this.renderPendingFiles();
        this.setStatus(String(error.message || error), "error");
      }
    }

    renderPendingFiles() {
      this.elements.attachmentList.replaceChildren();
      this.pendingFiles.forEach((attachment, index) => {
        const chip = createNode("span", "chat-attachment-chip");
        const size = `${Math.max(1, Math.ceil(attachment.file.size / 1024))} KB`;
        chip.append(createNode("span", "", `${attachment.name} · ${size}`));
        const remove = createNode("button", "", "×");
        remove.type = "button";
        remove.setAttribute("aria-label", `Remove ${attachment.name}`);
        remove.addEventListener("click", () => {
          this.pendingFiles.splice(index, 1);
          this.elements.attachments.value = "";
          this.renderPendingFiles();
          this.setStatus(this.pendingFiles.length ? `${this.pendingFiles.length} attachment(s) ready.` : "Ready", "ready");
        });
        chip.append(remove);
        this.elements.attachmentList.append(chip);
      });
    }

    async request(path, options = {}) {
      const response = await global.fetch(path, {
        credentials: "same-origin",
        cache: "no-store",
        ...options,
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.finding || body.error || `HTTP_${response.status}`);
      return body;
    }

    async submit() {
      if (this.activeTurnId) return;
      const text = this.elements.input.value.trim();
      if (!text && !this.pendingFiles.length) {
        this.setStatus("Enter a request or attach a file.", "error");
        this.elements.input.focus();
        return;
      }
      const turnId = global.crypto?.randomUUID?.() || `turn-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      this.activeTurnId = turnId;
      this.updateControls();
      this.setStatus("Dispatching governed turn…", "active");
      try {
        const attachments = await Promise.all(this.pendingFiles.map(serializeAttachment));
        await this.request("/api/v1/chat/turns", {
          method: "POST",
          body: JSON.stringify({ turn_id: turnId, text, attachments }),
        });
        this.elements.input.value = "";
        this.elements.attachments.value = "";
        this.pendingFiles = [];
        this.renderPendingFiles();
        this.setStatus("Running · Stop terminates the owned provider process.", "active");
        this.elements.stop.focus();
      } catch (error) {
        this.activeTurnId = null;
        this.updateControls();
        this.setStatus(`Turn refused: ${error.message || error}`, "error");
        this.elements.input.focus();
      }
    }

    async cancel() {
      const turnId = this.activeTurnId;
      if (!turnId) return;
      this.elements.stop.disabled = true;
      this.setStatus("Stopping provider process…", "stopping");
      try {
        await this.request(`/api/v1/chat/turns/${encodeURIComponent(turnId)}/cancel`, {
          method: "POST",
          body: "{}",
        });
        this.setStatus("Stop requested · awaiting confirmed process exit.", "stopping");
      } catch (error) {
        this.elements.stop.disabled = false;
        this.setStatus(`Stop failed: ${error.message || error}`, "error");
      }
    }

    appendMessage(message, announce = false) {
      const identity = String(message?.message_id || `${message?.sequence || "pending"}:${message?.role || "system"}`);
      if (this.renderedMessages.has(identity)) return;
      this.renderedMessages.add(identity);
      renderMessage(this.elements.transcript, message);
      this.elements.transcript.scrollTop = this.elements.transcript.scrollHeight;
      if (announce) {
        const role = String(message?.role || message?.kind || "system").replaceAll("_", " ");
        this.elements.announcer.textContent = `New ${role} message.`;
      }
    }

    applySnapshot(snapshot) {
      this.gate.advance(snapshot?.last_sequence);
      const messages = Array.isArray(snapshot?.messages) ? snapshot.messages : [];
      this.runtimeAvailable = snapshot?.data_status === "available";
      let newestMessage = null;
      messages.forEach((message) => {
        const before = this.renderedMessages.size;
        this.appendMessage(message);
        if (this.renderedMessages.size > before) newestMessage = message;
      });
      if (this.snapshotInitialized && newestMessage) {
        const role = String(newestMessage.role || newestMessage.kind || "system").replaceAll("_", " ");
        this.elements.announcer.textContent = `New ${role} message.`;
      }
      this.snapshotInitialized = true;
      if (!snapshot?.active_turn_id && this.provisionalNode) {
        this.provisionalNode.remove?.();
        this.provisionalNode = null;
        this.provisionalContent = "";
      }
      const wasActive = Boolean(this.activeTurnId);
      const serverActive = snapshot?.active_turn_id || null;
      this.activeTurnId = serverActive;
      this.updateControls();
      if (!this.runtimeAvailable) this.setStatus(`Chat unavailable · ${snapshot?.finding || "runtime not enabled"}.`, "error");
      else if (serverActive) this.setStatus("Running · live state restored.", "active");
      else if (snapshot?.status === "cancellation_uncertain") this.setStatus("Cancellation uncertain · recovery required.", "error");
      else this.setStatus("Ready", "ready");
      if (wasActive && !serverActive && this.runtimeAvailable) this.elements.input.focus();
    }

    applyEvent(event) {
      if (event?.type === "snapshot" && event.snapshot) {
        this.applySnapshot(event.snapshot);
        return true;
      }
      if (event?.type === "output_delta") {
        if (event.channel !== "stdout" || typeof event.text !== "string") return true;
        if (!this.provisionalNode) {
          this.provisionalNode = renderMessage(this.elements.transcript, {
            role: "assistant",
            content: "",
          });
          this.provisionalNode.dataset.provisional = "true";
          this.provisionalNode.setAttribute("aria-live", "off");
        }
        this.provisionalContent += event.text;
        const content = this.provisionalNode.children?.[1];
        if (content) content.textContent = this.provisionalContent;
        this.elements.transcript.scrollTop = this.elements.transcript.scrollHeight;
        return true;
      }
      if (!this.gate.accept(event)) return false;
      if (event.message) this.appendMessage({ ...event.message, sequence: event.message.sequence || event.sequence }, true);
      const status = String(event.status || event.type || "").toLowerCase();
      if (event.turn_id && !TERMINAL_STATES.has(status)) this.activeTurnId = event.turn_id;
      if (TERMINAL_STATES.has(status)) {
        if (!event.turn_id || event.turn_id === this.activeTurnId) this.activeTurnId = null;
        this.setStatus(
          status === "cancellation_uncertain" ? "Cancellation uncertain · recovery required." : status.replaceAll("_", " "),
          status === "failed" || status === "cancellation_uncertain" ? "error" : "ready",
        );
      }
      this.updateControls();
      if (TERMINAL_STATES.has(status) && this.runtimeAvailable) this.elements.input.focus();
      return true;
    }

    async refresh() {
      try {
        this.applySnapshot(await this.request("/api/v1/chat"));
      } catch (error) {
        this.runtimeAvailable = false;
        this.updateControls();
        this.setStatus(`Chat state unavailable: ${error.message || error}`, "error");
      }
    }

    beginPolling() {
      if (this.pollTimer) return;
      const poll = async () => {
        await this.refresh();
        if (this.pollTimer) this.pollTimer = global.setTimeout(poll, POLL_INTERVAL_MS);
      };
      this.pollTimer = global.setTimeout(poll, 0);
    }

    endPolling() {
      if (this.pollTimer) global.clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }

    connectEvents() {
      if (!("EventSource" in global)) {
        this.beginPolling();
        return;
      }
      this.stream?.close();
      this.stream = new global.EventSource("/api/v1/chat/events");
      this.stream.onopen = () => {
        this.endPolling();
        if (!this.activeTurnId) this.setStatus("Ready · live", "ready");
      };
      this.stream.onmessage = (message) => {
        try { this.applyEvent(JSON.parse(message.data)); }
        catch (_error) { this.setStatus("Malformed chat event ignored.", "error"); }
      };
      this.stream.onerror = () => {
        this.stream?.close();
        this.stream = null;
        this.beginPolling();
        if (!this.retryTimer) {
          this.retryTimer = global.setTimeout(() => {
            this.retryTimer = null;
            this.connectEvents();
          }, STREAM_RETRY_MS);
        }
      };
    }

    async start() {
      if (!this.completeDom() || this.running) return false;
      this.running = true;
      this.bind();
      await this.refresh();
      this.connectEvents();
      return true;
    }

    stop() {
      this.running = false;
      this.stream?.close();
      this.stream = null;
      this.endPolling();
      if (this.retryTimer) global.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  const api = { ChatRuntime, EventGate, renderMessage, validateFile, serializeAttachment };
  global.TorqChat = api;

  function autoStart() {
    const runtime = new ChatRuntime();
    void runtime.start();
    global.torqChatRuntime = runtime;
  }
  if (global.document) {
    if (global.document.readyState === "loading") global.document.addEventListener("DOMContentLoaded", autoStart, { once: true });
    else autoStart();
  }
})(globalThis);
