"use strict";

(function chatModule(global) {
  const MAX_ATTACHMENTS = 6;
  const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;
  const POLL_INTERVAL_MS = 2500;
  const STREAM_RETRY_MS = 10000;
  const VIEW_KEY = "torq.workspace.view.v1";
  const DRAFT_KEY_PREFIX = "torq.workspace.draft.v1.";
  const ALLOWED_TYPES = new Map([
    ["image/png", { extensions: [".png"], signature: [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a] }],
    ["image/jpeg", { extensions: [".jpg", ".jpeg"], signature: [0xff, 0xd8, 0xff] }],
    ["application/pdf", { extensions: [".pdf"], signature: [0x25, 0x50, 0x44, 0x46, 0x2d] }],
    ["application/json", { extensions: [".json"], signature: null }],
    ["image/gif", { extensions: [".gif"], signature: [0x47, 0x49, 0x46] }],
    ["image/webp", { extensions: [".webp"], signature: null }],
    ["text/plain", { extensions: [".txt"], signature: null }],
    ["text/markdown", { extensions: [".md", ".markdown"], signature: null }],
  ]);
  const TERMINAL_STATES = new Set(["completed", "cancelled", "failed", "cancellation_uncertain"]);

  const byId = (id) => global.document?.getElementById(id) || null;
  const friendly = (value) => String(value ?? "unknown").replaceAll("_", " ");
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
        help: byId("chat-help"),
        clear: byId("clear-draft"),
        draftState: byId("draft-state"),
        workspace: byId("workspace-view"),
        fleetBoard: byId("fleet-board"),
        workspaceButton: byId("workspace-view-button"),
        fleetButton: byId("fleet-view-button"),
        composerSlot: byId("workspace-composer-slot"),
        transcriptSlot: byId("workspace-transcript-slot"),
        fleetComposerSlot: byId("fleet-composer-slot"),
        fleetTranscriptSlot: byId("fleet-transcript-slot"),
        transcriptSection: byId("chat-workspace-section"),
        refresh: byId("workspace-refresh"),
        setup: byId("workspace-setup"),
        details: byId("workspace-details"),
        detailsSummary: byId("workspace-details-summary"),
        suggestions: byId("workspace-suggestions"),
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
      this.workspace = null;
      this.workspaceId = null;
      this.draftRevision = 0;
      this.draftKey = null;
      this.storageAvailable = true;
      this.view = "fleet";
      this.submissionPending = false;
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
      const capabilities = this.workspace?.capabilities || {};
      const canDiscuss = Boolean(capabilities.can_discuss_run && this.runtimeAvailable && !active && !this.submissionPending);
      const canCancel = Boolean(active && (capabilities.can_cancel || capabilities.can_discuss_run));
      const attachmentTypes = Array.isArray(capabilities.attachment_types) ? capabilities.attachment_types : [];
      this.elements.send.disabled = !canDiscuss;
      this.elements.stop.disabled = !canCancel;
      this.elements.input.disabled = false;
      this.elements.attachments.disabled = active || !canDiscuss || !attachmentTypes.length;
      this.elements.attachments.hidden = !attachmentTypes.length;
      this.elements.composer.dataset.active = String(active);
    }

    setView(view, persist = true) {
      this.view = view === "workspace" ? "workspace" : "fleet";
      const composerTarget = this.view === "workspace" ? this.elements.composerSlot : this.elements.fleetComposerSlot;
      const transcriptTarget = this.view === "workspace" ? this.elements.transcriptSlot : this.elements.fleetTranscriptSlot;
      composerTarget.append(this.elements.composer);
      transcriptTarget.append(this.elements.transcriptSection);
      this.elements.workspace.hidden = this.view !== "workspace";
      this.elements.workspaceButton.setAttribute("aria-pressed", String(this.view === "workspace"));
      this.elements.fleetButton.setAttribute("aria-pressed", String(this.view === "fleet"));
      if (global.document.body?.dataset) global.document.body.dataset.view = this.view;
      const skip = byId("skip-link");
      if (skip) {
        skip.setAttribute("href", this.view === "workspace" ? "#workspace-view" : "#fleet-board");
        skip.textContent = this.view === "workspace" ? "Skip to Workspace" : "Skip to Fleet board";
      }
      byId("chat-kicker").textContent = this.view === "workspace" ? "Discussion" : "OWNED PROVIDER SESSION";
      byId("chat-title").textContent = this.view === "workspace" ? "Conversation" : "Governed conversation";
      byId("chat-evidence-note").textContent = this.view === "workspace"
        ? "Accepted messages become signed evidence"
        : "SIGNED TERMINAL EVIDENCE";
      if (persist) {
        try { global.localStorage?.setItem(VIEW_KEY, this.view); } catch (_error) { /* preference applies in this tab */ }
      }
    }

    configureView() {
      let selected = "fleet";
      try {
        const stored = global.localStorage?.getItem(VIEW_KEY);
        if (stored === "workspace" || stored === "fleet") selected = stored;
      } catch (_error) { /* Fleet remains the compatibility default */ }
      this.setView(selected, false);
      this.elements.workspaceButton.addEventListener("click", () => {
        this.setView("workspace");
        this.elements.workspace.focus?.();
      });
      this.elements.fleetButton.addEventListener("click", () => {
        this.setView("fleet");
        this.elements.fleetBoard.focus?.();
      });
    }

    setDraftState(text, error = false) {
      this.elements.draftState.textContent = text;
      this.elements.draftState.dataset.error = String(error);
    }

    persistDraft() {
      if (!this.draftKey) {
        this.setDraftState("Held in this tab");
        return;
      }
      try {
        global.sessionStorage.setItem(this.draftKey, this.elements.input.value);
        this.storageAvailable = true;
        this.setDraftState(this.elements.input.value ? "Saved for this tab" : "No draft saved");
      } catch (_error) {
        this.storageAvailable = false;
        this.setDraftState("Held in this tab only", true);
      }
    }

    loadDraft(workspaceId) {
      if (typeof workspaceId !== "string" || !/^workspace_[a-f0-9]{32}$/.test(workspaceId)) return;
      if (this.workspaceId === workspaceId) return;
      if (this.workspaceId && this.workspaceId !== workspaceId) {
        this.persistDraft();
        this.elements.input.value = "";
        this.draftRevision += 1;
      }
      this.workspaceId = workspaceId;
      this.draftKey = `${DRAFT_KEY_PREFIX}${workspaceId}`;
      if (this.elements.input.value) {
        this.persistDraft();
        return;
      }
      try {
        const saved = global.sessionStorage.getItem(this.draftKey);
        if (typeof saved === "string") this.elements.input.value = saved;
        this.storageAvailable = true;
        this.setDraftState(saved ? "Restored for this tab" : "No draft saved");
      } catch (_error) {
        this.storageAvailable = false;
        this.setDraftState("Held in this tab only", true);
      }
    }

    clearDraft() {
      this.elements.input.value = "";
      this.draftRevision += 1;
      this.persistDraft();
      this.elements.input.focus();
    }

    renderWorkspace(metadata) {
      this.workspace = metadata;
      this.loadDraft(metadata?.workspace_id);
      const root = metadata?.root || {};
      const capabilities = metadata?.capabilities || {};
      const guidance = metadata?.guidance || {};
      const runId = metadata?.selected_run_id || null;
      const mode = root.run_mode === "dry_run" ? "Verified dry-run" : friendly(root.run_mode);
      const title = runId ? "Discuss this run" : "Welcome to TORQ";
      const context = runId ? runId : root.kind === "collection" ? "Run collection" : "Local workspace";
      byId("workspace-title").textContent = title;
      byId("workspace-context").textContent = context;
      byId("workspace-summary").textContent = guidance.message || "Review this workspace and choose the next action.";
      byId("next-action-title").textContent = capabilities.can_discuss_run
        ? "Ask about this run"
        : capabilities.can_cancel
        ? "TORQ is responding"
        : root.kind === "collection"
        ? "Choose a run"
        : "Setup needed";
      byId("next-action-message").textContent = guidance.message || "Discussion is unavailable.";
      byId("next-action-remediation").textContent = guidance.remediation || guidance.consequence || "";
      byId("workspace-root-kind").textContent = friendly(root.kind);
      byId("workspace-run-id").textContent = runId || "None selected";
      byId("workspace-trust").textContent = root.trusted ? friendly(root.verification_state) : "Not verified";
      byId("workspace-mode").textContent = mode;
      byId("workspace-provider").textContent = metadata?.provider?.name || friendly(metadata?.provider?.configuration);
      byId("composer-purpose").textContent = runId ? "Discuss this run" : "Draft";

      const picker = byId("run-picker");
      const list = byId("run-picker-list");
      list.replaceChildren();
      const runs = Array.isArray(metadata?.runs) ? metadata.runs : [];
      picker.hidden = root.kind !== "collection" || !runs.length;
      runs.forEach((candidate) => {
        const row = createNode("div", "run-picker-row");
        row.append(
          createNode("strong", "", candidate),
          createNode("code", "", `torq fleet --run-root .\\${candidate} --serve`),
        );
        list.append(row);
      });

      const allowed = Array.isArray(capabilities.attachment_types) ? capabilities.attachment_types : [];
      this.elements.attachments.setAttribute("accept", allowed.join(","));
      this.elements.attachments.setAttribute("multiple", "");
      this.elements.help.textContent = allowed.length
        ? "Supported files: text, Markdown, JSON, images, or PDF. Up to 6 files, 5 MB each. Ctrl or Cmd+Enter sends."
        : "Attachments are unavailable for this provider. Your draft stays in this browser tab.";
      this.updateControls();
    }

    bind() {
      this.configureView();
      this.elements.transcript.setAttribute("role", "log");
      this.elements.transcript.setAttribute("aria-live", "off");
      this.elements.status.setAttribute("role", "status");
      this.elements.status.setAttribute("aria-live", "polite");
      this.elements.input.setAttribute("aria-keyshortcuts", "Control+Enter Meta+Enter");
      this.elements.composer.addEventListener("submit", (event) => {
        event.preventDefault();
        void this.submit();
      });
      this.elements.stop.addEventListener("click", () => void this.cancel());
      this.elements.clear.addEventListener("click", () => this.clearDraft());
      this.elements.refresh.addEventListener("click", () => void this.refreshWorkspace(true));
      this.elements.setup.addEventListener("click", () => {
        this.elements.details.open = true;
        this.elements.detailsSummary.focus();
      });
      this.elements.input.addEventListener("input", () => {
        this.draftRevision += 1;
        this.persistDraft();
      });
      this.elements.input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          void this.submit();
        }
      });
      this.elements.attachments.addEventListener("change", () => void this.stageFiles());
      if (typeof this.elements.suggestions.querySelectorAll === "function") {
        this.elements.suggestions.querySelectorAll("[data-prompt]").forEach((button) => {
          button.addEventListener("click", () => {
            if (this.elements.input.value) {
              this.setStatus("Your current draft was kept. Clear it before inserting a suggestion.", "ready");
              this.elements.input.focus();
              return;
            }
            this.elements.input.value = button.dataset.prompt || "";
            this.draftRevision += 1;
            this.persistDraft();
            this.elements.input.focus();
          });
        });
      }
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
      if (this.activeTurnId || this.submissionPending) return;
      if (!this.workspace?.capabilities?.can_discuss_run || !this.runtimeAvailable) {
        this.setStatus(this.workspace?.guidance?.message || "Discussion is unavailable. Your draft is preserved.", "error");
        return;
      }
      const text = this.elements.input.value.trim();
      if (!text && !this.pendingFiles.length) {
        this.setStatus("Enter a request or attach a file.", "error");
        this.elements.input.focus();
        return;
      }
      const submittedRevision = this.draftRevision;
      const submittedValue = this.elements.input.value;
      const turnId = global.crypto?.randomUUID?.() || `turn-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      this.submissionPending = true;
      this.activeTurnId = turnId;
      this.updateControls();
      this.setStatus("Dispatching governed turn…", "active");
      try {
        const attachments = await Promise.all(this.pendingFiles.map(serializeAttachment));
        await this.request("/api/v1/chat/turns", {
          method: "POST",
          body: JSON.stringify({ turn_id: turnId, text, attachments }),
        });
        this.submissionPending = false;
        this.activeTurnId = turnId;
        if (this.draftRevision === submittedRevision && this.elements.input.value === submittedValue) {
          this.elements.input.value = "";
          this.draftRevision += 1;
          this.persistDraft();
        }
        this.elements.attachments.value = "";
        this.pendingFiles = [];
        this.renderPendingFiles();
        this.setStatus("Running · Stop terminates the owned provider process.", "active");
        this.elements.stop.focus();
      } catch (error) {
        this.submissionPending = false;
        this.activeTurnId = null;
        this.updateControls();
        this.persistDraft();
        this.setStatus(`Turn refused: ${error.message || error}. Your draft is preserved.`, "error");
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
      byId("chat-empty")?.remove?.();
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
      if (!this.submissionPending) this.activeTurnId = serverActive;
      this.updateControls();
      if (!this.runtimeAvailable) this.setStatus(`Chat unavailable · ${snapshot?.finding || "runtime not enabled"}.`, "error");
      else if (serverActive) this.setStatus("Running · live state restored.", "active");
      else if (snapshot?.status === "cancellation_uncertain") this.setStatus("Cancellation uncertain · recovery required.", "error");
      else this.setStatus("Ready", "ready");
      if (wasActive && !serverActive && !this.submissionPending && this.runtimeAvailable) {
        this.elements.input.focus();
        void this.refreshWorkspace();
      }
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
        void this.refreshWorkspace();
      }
      this.updateControls();
      if (TERMINAL_STATES.has(status) && this.runtimeAvailable) this.elements.input.focus();
      return true;
    }

    async refreshWorkspace(manual = false) {
      try {
        const metadata = await this.request("/api/v1/workspace");
        this.renderWorkspace(metadata);
        if (metadata?.capabilities?.chat_stream_available) {
          await this.refresh();
          if (manual && metadata?.capabilities?.can_discuss_run) {
            this.setStatus("Workspace checked. Discussion is ready.", "ready");
          }
        } else {
          this.runtimeAvailable = false;
          this.updateControls();
          this.setStatus(metadata?.guidance?.message || "Discussion is unavailable. Your draft is preserved.", "error");
          this.stream?.close();
          this.stream = null;
          this.endPolling();
        }
        return metadata;
      } catch (_error) {
        this.runtimeAvailable = false;
        this.updateControls();
        this.setStatus("Workspace is disconnected. Your draft is preserved; use Check again to reconnect.", "error");
        return null;
      }
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
      if (!this.workspace?.capabilities?.chat_stream_available) return;
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
      if (!this.workspace?.capabilities?.chat_stream_available) return;
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
      const metadata = await this.refreshWorkspace();
      if (metadata?.capabilities?.chat_stream_available) this.connectEvents();
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
