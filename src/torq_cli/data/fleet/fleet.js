"use strict";

const POLL_MS = 3000;
const STREAM_RETRY_MS = 30000;
const STATES = ["dormant", "queued", "running", "sealed", "blocked", "needs_you", "failed", "interrupted", "abandoned"];
const TALLIES = ["sealed", "running", "queued", "dormant", "blocked", "needs_you", "failed", "interrupted", "abandoned"];
const FALLBACK_ROLES = ["g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui"];
const SEEN_ACTIONS_KEY = "torq.fleet.notified-actions.v2";
const THEME_KEY = "torq.fleet.theme.v1";
const SAFE_VERIFICATION = new Set(["live_verified", "sealed_verified", "live_catching_up"]);
const MAX_ATTACHMENTS = 4;
const MAX_ATTACHMENT_BYTES = 700 * 1024;
const ATTACHMENT_TYPES = new Map([
  [".txt", "text/plain"],
  [".md", "text/markdown"],
  [".markdown", "text/markdown"],
  [".json", "application/json"],
  [".png", "image/png"],
  [".jpg", "image/jpeg"],
  [".jpeg", "image/jpeg"],
  [".pdf", "application/pdf"],
]);

const byId = (id) => document.getElementById(id);
const node = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
};
const human = (value) => String(value ?? "unknown").replaceAll("_", " ");
const presentationState = (value) => value === "sealed" ? "completed" : human(value);
const upper = (value) => human(value).toUpperCase();
const safeState = (value) => STATES.includes(String(value)) ? String(value) : "queued";
const valueOrDash = (value) => value === null || value === undefined || value === "" ? "—" : String(value);

let latestEnvelope = null;
let latestFingerprint = null;
let expandedRoles = new Set();
let rovingRole = null;
let elapsedTimer = null;
let pollTimer = null;
let eventStream = null;
let streamFallbackTimer = null;
let streamRetryTimer = null;
let refreshInFlight = false;
let recoverySecret = null;
let recoveryBinding = null;
const localMutations = new Map();
let stagedAttachments = [];

function setText(id, value) { byId(id).textContent = valueOrDash(value); }
function correlationId(prefix) {
  const suffix = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}

function announceLive(message) {
  const region = byId("live-announcer");
  region.textContent = "";
  window.setTimeout(() => { region.textContent = message; }, 20);
}

function showBanner(message, danger = false) {
  const banner = byId("system-banner");
  banner.textContent = message;
  banner.className = `system-banner visible${danger ? " danger" : ""}`;
}
function clearBanner() { byId("system-banner").className = "system-banner"; }
function setControlStatus(message) { byId("control-status").textContent = message || ""; }
function setPoll(label, state) {
  byId("poll-label").textContent = label;
  byId("poll-dot").className = state;
}

function durationText(snapshot) {
  const run = snapshot?.run;
  if (!run || !run.started_at || !run.updated_at) return "—";
  const start = Date.parse(run.started_at);
  const end = ["open", "action_open"].includes(run.workflow_state) ? Date.now() : Date.parse(run.updated_at);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "—";
  const seconds = Math.floor((end - start) / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  const value = hours ? `${hours}h ${String(minutes).padStart(2, "0")}m` : `${minutes}m ${String(remainder).padStart(2, "0")}s`;
  return ["closed", "abandoned"].includes(run.workflow_state) ? `~${value}` : value;
}
function updateElapsed() {
  const value = durationText(latestEnvelope?.snapshot);
  setText("elapsed", value);
  setText("monitor-elapsed", value);
}

function stateFingerprint(envelope) {
  return JSON.stringify({
    snapshot: envelope.snapshot,
    annotations: envelope.annotations,
    eligibility: envelope.eligibility,
    pending: envelope.pending,
    session: {
      write_capable: envelope.session?.write_capable,
      read_only_reason: envelope.session?.read_only_reason,
    },
  });
}

function validEnvelope(value) {
  return value && typeof value === "object" && value.snapshot && typeof value.snapshot === "object"
    && Array.isArray(value.annotations) && value.session && typeof value.session === "object"
    && value.eligibility && typeof value.eligibility === "object" && Array.isArray(value.pending);
}

function meaningfulChanges(previous, current) {
  if (!previous) return [];
  const messages = [];
  const previousLanes = new Map((previous.snapshot?.lanes || []).map((lane) => [lane.role, lane.state]));
  const changed = (current.snapshot?.lanes || []).filter((lane) => previousLanes.has(lane.role) && previousLanes.get(lane.role) !== lane.state);
  if (changed.length) messages.push(changed.map((lane) => `${lane.role} ${human(lane.state)}`).join(", "));
  const previousActions = new Set((previous.snapshot?.actions || []).filter((action) => action.state === "open").map((action) => action.action_id));
  const newActions = (current.snapshot?.actions || []).filter((action) => action.state === "open" && !previousActions.has(action.action_id));
  if (newActions.length) messages.push(`${newActions.length} new operator ${newActions.length === 1 ? "action" : "actions"}`);
  const previousAnnotations = new Set((previous.annotations || []).map((item) => `${item.kind}:${item.scope}:${item.observed_at}`));
  const annotations = (current.annotations || []).filter((item) => !previousAnnotations.has(`${item.kind}:${item.scope}:${item.observed_at}`));
  if (annotations.length) messages.push(annotations.map((item) => human(item.kind)).join(", "));
  return messages;
}

function renderRail(lanes) {
  const rail = byId("lane-pips");
  const monitor = byId("monitor-pips");
  rail.replaceChildren();
  monitor.replaceChildren();
  const rows = lanes.length ? lanes : FALLBACK_ROLES.map((role) => ({ role, state: "dormant" }));
  rows.slice(0, 6).forEach((lane) => {
    const state = safeState(lane.state);
    const item = node("li");
    item.dataset.state = state;
    item.setAttribute("aria-label", `${lane.role}: ${presentationState(state)}`);
    rail.append(item);
    const pip = node("i");
    pip.dataset.state = state;
    monitor.append(pip);
  });
}

function renderTallies(summary) {
  const list = byId("tallies");
  list.replaceChildren();
  TALLIES.forEach((state) => {
    const count = Number(summary?.[state] ?? 0);
    const group = node("div");
    group.dataset.state = state;
    group.append(node("dt", "", presentationState(state)), node("dd", "", count));
    list.append(group);
  });
}

function detailDefinition(label, value, fullLabel = null) {
  const fragment = document.createDocumentFragment();
  const term = node("dt", "", label);
  const description = node("dd", "", valueOrDash(value));
  if (fullLabel) description.setAttribute("aria-label", fullLabel);
  fragment.append(term, description);
  return fragment;
}

function laneSummary(lane) {
  if (lane.state === "abandoned") return "Attempt outcome unrecorded";
  if (lane.state === "sealed") return "Latest attempt completed and verified";
  if (lane.reason_gloss) return lane.reason_gloss;
  if (lane.dispatch_message && lane.latest_transition !== "run_planned") return lane.dispatch_message;
  if (lane.state === "dormant") return "Conditional repair lane not activated";
  if (lane.state === "queued") return "Awaiting governed dispatch";
  return human(lane.latest_transition || "run planned");
}

function toggleLane(button, detail, role) {
  const open = button.getAttribute("aria-expanded") === "true";
  button.setAttribute("aria-expanded", String(!open));
  button.setAttribute("aria-label", `${open ? "Show" : "Hide"} ${role} evidence detail`);
  detail.classList.toggle("open", !open);
  const history = detail.querySelector(".history");
  if (history) history.tabIndex = open ? -1 : 0;
  if (open) expandedRoles.delete(role); else expandedRoles.add(role);
}

function laneKeydown(event) {
  const toggles = [...byId("lane-list").querySelectorAll(".lane-toggle")];
  const current = toggles.indexOf(event.currentTarget);
  let target = current;
  if (event.key === "ArrowDown") target = Math.min(toggles.length - 1, current + 1);
  else if (event.key === "ArrowUp") target = Math.max(0, current - 1);
  else if (event.key === "Home") target = 0;
  else if (event.key === "End") target = toggles.length - 1;
  else return;
  event.preventDefault();
  toggles.forEach((toggle, index) => { toggle.tabIndex = index === target ? 0 : -1; });
  rovingRole = toggles[target]?.dataset.role || null;
  toggles[target]?.focus();
}

function renderLane(lane, index, rovingIndex) {
  const state = safeState(lane.state);
  const article = node("article", "lane");
  article.dataset.state = state;
  article.dataset.role = lane.role;
  article.setAttribute("role", "listitem");
  article.setAttribute("aria-label", `${lane.role}: ${presentationState(state)}`);
  const main = node("div", "lane-main lane-columns");
  const role = node("div", "lane-role");
  const identity = [lane.provider, lane.model].filter(Boolean).join(" / ") || lane.kind || "unassigned";
  role.append(node("strong", "", lane.role), node("span", "", identity));
  role.lastChild?.setAttribute("aria-label", identity);
  const stateChip = node("span", "state-chip", presentationState(state));
  const attempt = node("span", "lane-number", lane.attempt_ordinal ? `#${lane.attempt_ordinal}` : "—");
  const sequence = node("span", "lane-number", lane.latest_sequence ? `SEQ ${lane.latest_sequence}` : "—");
  const summary = node("span", "lane-summary", laneSummary(lane));
  const toggle = node("button", "lane-toggle", "+");
  const detailId = `lane-detail-${index}`;
  toggle.type = "button";
  toggle.dataset.role = lane.role;
  toggle.tabIndex = index === rovingIndex ? 0 : -1;
  toggle.setAttribute("aria-label", `Show ${lane.role} evidence detail`);
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-controls", detailId);
  main.append(role, stateChip, attempt, sequence, summary, toggle);

  const detail = node("div", "lane-detail");
  detail.id = detailId;
  const evidence = node("section", "detail-group");
  evidence.append(node("h3", "", "Current evidence"));
  const definitions = node("dl", "detail-list");
  definitions.append(
    detailDefinition("Transition", human(lane.latest_transition)),
    detailDefinition("Writer", lane.latest_writer_role),
    detailDefinition("Basis", lane.latest_evidence_basis),
    detailDefinition("Writer key", lane.latest_writer_key_id, `Writer key ${valueOrDash(lane.latest_writer_key_id)}`),
    detailDefinition("Dispatch", lane.dispatch_message),
    detailDefinition("Reason", lane.reason),
    detailDefinition("Contract", [lane.contract_id, lane.contract_version].filter(Boolean).join(" @ ")),
    detailDefinition("Prompt", [lane.prompt_id, lane.prompt_version].filter(Boolean).join(" @ "))
  );
  evidence.append(definitions);
  const historySection = node("section", "detail-group");
  historySection.append(node("h3", "", "Attempt history"));
  const history = node("ol", "history");
  history.tabIndex = -1;
  history.setAttribute("aria-label", `${lane.role} attempt history`);
  history.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); toggle.focus(); }
  });
  const transitions = (lane.attempts || []).flatMap((item) => item.transitions || []);
  if (!transitions.length) history.append(node("li", "", "No attempt transitions recorded."));
  else transitions.forEach((transition) => {
    const item = node("li");
    item.append(node("span", "", `#${transition.sequence}`), node("span", "", `${human(transition.transition)} · ${human(transition.writer_role)}`));
    history.append(item);
  });
  historySection.append(history);
  detail.append(evidence, historySection);
  toggle.addEventListener("click", () => toggleLane(toggle, detail, lane.role));
  toggle.addEventListener("keydown", laneKeydown);
  if (expandedRoles.has(lane.role)) toggleLane(toggle, detail, lane.role);
  article.append(main, detail);
  return article;
}

function renderLanes(lanes) {
  const list = byId("lane-list");
  const activeRole = document.activeElement?.classList?.contains("lane-toggle") ? document.activeElement.dataset.role : null;
  if (activeRole) rovingRole = activeRole;
  const rovingIndex = Math.max(0, lanes.findIndex((lane) => lane.role === rovingRole));
  list.replaceChildren();
  lanes.forEach((lane, index) => list.append(renderLane(lane, index, rovingIndex)));
  if (!lanes.length) list.append(node("p", "empty-copy", "No evidence-backed lane data is available."));
  list.setAttribute("aria-busy", "false");
  if (activeRole) list.querySelector(`.lane-toggle[data-role="${CSS.escape(activeRole)}"]`)?.focus();
}

function readSeenActions() {
  try {
    const raw = JSON.parse(localStorage.getItem(SEEN_ACTIONS_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw.filter((item) => typeof item === "string") : []);
  } catch (_error) { return new Set(); }
}
function persistSeenActions(seen) {
  try { localStorage.setItem(SEEN_ACTIONS_KEY, JSON.stringify([...seen])); return true; }
  catch (_error) { return false; }
}
function notifyActions(actions, runId) {
  if (!("Notification" in window) || Notification.permission !== "granted" || !runId) return;
  const seen = readSeenActions();
  actions.filter((action) => action.state === "open").forEach((action) => {
    const identity = `${runId}:${String(action.action_id || action.opened_sequence || "unknown")}`;
    if (seen.has(identity)) return;
    seen.add(identity);
    if (!persistSeenActions(seen)) return;
    new Notification("TORQ Fleet needs you", {
      body: "A governed run has an open operator action.",
      tag: `torq-fleet-${identity}`,
      renotify: false,
    });
  });
}

function reconcileLocalMutations(envelope) {
  const pending = new Set(envelope.pending || []);
  const covered = Number(envelope.snapshot?.verification?.covered_sequence || 0);
  let reconciled = false;
  localMutations.forEach((mutation, correlation) => {
    if (mutation.accepted && !pending.has(correlation) && covered > mutation.baseSequence) {
      localMutations.delete(correlation);
      reconciled = true;
    }
  });
  if (reconciled) setControlStatus("Verified evidence reconciled.");
  if (recoveryBinding) {
    const generation = envelope.snapshot?.verification?.manifest_generation;
    const runId = envelope.snapshot?.run?.run_id;
    if (generation !== recoveryBinding.generation || runId !== recoveryBinding.runId || !envelope.eligibility?.recover_run?.eligible) {
      recoverySecret = null;
      recoveryBinding = null;
    }
  }
}

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.finding || `HTTP_${response.status}`);
  return result;
}

function attachmentMediaType(file) {
  const lower = file.name.toLowerCase();
  const extension = [...ATTACHMENT_TYPES.keys()].find((item) => lower.endsWith(item));
  if (!extension) throw new Error("attachment_type_unsupported");
  const expected = ATTACHMENT_TYPES.get(extension);
  if (file.type && file.type.toLowerCase() !== expected) throw new Error("attachment_type_mismatch");
  return expected;
}

async function validateAttachment(file) {
  if (!file.name || file.name.length > 255 || /[\x00-\x1f\x7f/:\\]/.test(file.name)) {
    throw new Error("attachment_name_invalid");
  }
  if (!file.size || file.size > MAX_ATTACHMENT_BYTES) throw new Error("attachment_size_invalid");
  const mediaType = attachmentMediaType(file);
  const bytes = new Uint8Array(await file.slice(0, 24).arrayBuffer());
  const signature = (...values) => values.every((value, index) => bytes[index] === value);
  if (mediaType === "image/png" && !signature(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)) {
    throw new Error("attachment_signature_mismatch");
  }
  if (mediaType === "image/jpeg" && !signature(0xff, 0xd8, 0xff)) {
    throw new Error("attachment_signature_mismatch");
  }
  if (mediaType === "application/pdf" && !signature(0x25, 0x50, 0x44, 0x46, 0x2d)) {
    throw new Error("attachment_signature_mismatch");
  }
  return { file, mediaType };
}

function renderAttachments() {
  const list = byId("attachment-list");
  list.replaceChildren();
  stagedAttachments.forEach((attachment, index) => {
    const chip = node("span", "attachment-chip");
    chip.append(node("span", "", `${attachment.file.name} · ${Math.ceil(attachment.file.size / 1024)} KB`));
    const remove = node("button", "", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${attachment.file.name}`);
    remove.addEventListener("click", () => {
      stagedAttachments.splice(index, 1);
      renderAttachments();
    });
    chip.append(remove);
    list.append(chip);
  });
}

function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("attachment_read_failed"));
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.readAsDataURL(file);
  });
}

function composerAvailable() {
  return Boolean(
    latestEnvelope?.session?.write_capable
    && latestEnvelope?.eligibility?.context?.eligible
  );
}

function updateComposerAvailability() {
  const available = composerAvailable();
  byId("context-input").disabled = !available;
  byId("attach-button").disabled = !available;
  byId("context-submit").disabled = !available;
  if (!available && latestEnvelope) {
    byId("composer-status").textContent = `Unavailable: ${human(latestEnvelope.eligibility?.context?.reason || latestEnvelope.session?.read_only_reason || "read only")}.`;
  } else if (available && byId("composer-status").textContent.startsWith("Unavailable:")) {
    byId("composer-status").textContent = "";
  }
}

async function submitComposer(event) {
  event.preventDefault();
  const input = byId("context-input");
  const text = input.value.trim();
  if (!text && !stagedAttachments.length) {
    byId("composer-status").textContent = "Add text or an attachment first.";
    input.focus();
    return;
  }
  if (!composerAvailable()) {
    updateComposerAvailability();
    return;
  }
  const submit = byId("context-submit");
  submit.disabled = true;
  submit.setAttribute("aria-busy", "true");
  const queue = [...stagedAttachments];
  let accepted = 0;
  try {
    if (text) {
      byId("composer-status").textContent = "Recording text context…";
      await postJson("/api/v1/fleet/context", {
        correlation_id: correlationId("context"),
        input_kind: "inline_text",
        content: text,
        media_type: "text/plain",
      });
      accepted += 1;
      input.value = "";
    }
    for (const attachment of queue) {
      byId("composer-status").textContent = `Recording ${attachment.file.name}…`;
      await postJson("/api/v1/fleet/context", {
        correlation_id: correlationId("artifact"),
        input_kind: "file",
        content_base64: await fileBase64(attachment.file),
        media_type: attachment.mediaType,
        source_name: attachment.file.name,
      });
      accepted += 1;
      stagedAttachments = stagedAttachments.filter((item) => item !== attachment);
      renderAttachments();
    }
    byId("composer-status").textContent = `${accepted} governed ${accepted === 1 ? "item" : "items"} accepted. Waiting for verified evidence.`;
    await refresh();
  } catch (error) {
    byId("composer-status").textContent = `Blocked after ${accepted}: ${human(error.message)}.`;
  } finally {
    submit.disabled = !composerAvailable();
    submit.removeAttribute("aria-busy");
  }
}

function configureComposer() {
  const input = byId("context-input");
  const picker = byId("attachment-input");
  byId("attach-button").addEventListener("click", () => picker.click());
  picker.addEventListener("change", async () => {
    const files = [...picker.files];
    picker.value = "";
    if (stagedAttachments.length + files.length > MAX_ATTACHMENTS) {
      byId("composer-status").textContent = `Attach no more than ${MAX_ATTACHMENTS} files.`;
      return;
    }
    try {
      for (const file of files) stagedAttachments.push(await validateAttachment(file));
      renderAttachments();
      byId("composer-status").textContent = `${stagedAttachments.length} ${stagedAttachments.length === 1 ? "file" : "files"} ready.`;
    } catch (error) {
      byId("composer-status").textContent = `Attachment blocked: ${human(error.message)}.`;
    }
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      byId("context-composer").requestSubmit();
    }
  });
  byId("context-composer").addEventListener("submit", submitComposer);
  updateComposerAvailability();
}

async function resolveAction(action, resolution, button) {
  const correlation = correlationId("resolve");
  const baseSequence = Number(latestEnvelope?.snapshot?.verification?.covered_sequence || 0);
  localMutations.set(correlation, { target: action.action_id, baseSequence, accepted: false });
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  setControlStatus(`Submitting ${human(resolution)}…`);
  try {
    await postJson(`/api/v1/fleet/actions/${encodeURIComponent(action.action_id)}/resolve`, { correlation_id: correlation, resolution });
    localMutations.get(correlation).accepted = true;
    setControlStatus("Decision accepted. Waiting for verified evidence.");
  } catch (error) {
    localMutations.delete(correlation);
    button.disabled = false;
    button.removeAttribute("aria-busy");
    setControlStatus(`Decision blocked: ${human(error.message)}.`);
  }
}

function renderActions(actions, runId, eligibility, pending) {
  const open = actions.filter((action) => action.state === "open");
  const list = byId("action-list");
  list.replaceChildren();
  const pendingSet = new Set(pending || []);
  if (!open.length) list.append(node("p", "empty-copy", "No open operator actions."));
  open.forEach((action) => {
    const item = node("div", "action-item");
    item.setAttribute("role", "group");
    item.setAttribute("aria-label", `Action ${valueOrDash(action.action_id)}`);
    const title = node("strong", "", action.summary || human(action.action_type || action.type || "Operator decision required"));
    const meta = node("span", "action-meta", `${valueOrDash(action.action_id)} · OPENED SEQ ${valueOrDash(action.opened_sequence)}`);
    item.append(title, meta);
    const rule = eligibility?.[action.action_id] || { eligible: false, reason: "action_not_eligible" };
    if (rule.eligible) {
      const buttons = node("div", "action-buttons");
      (action.allowed_resolutions || []).forEach((resolution) => {
        const button = node("button", "action-button", human(resolution));
        button.type = "button";
        const local = [...localMutations.entries()].find(([, value]) => value.target === action.action_id);
        button.disabled = Boolean(local && (pendingSet.has(local[0]) || local[1].accepted));
        if (button.disabled) button.setAttribute("aria-busy", "true");
        button.addEventListener("click", () => resolveAction(action, resolution, button));
        buttons.append(button);
      });
      item.append(buttons);
    } else item.append(node("span", "action-meta", `Unavailable: ${human(rule.reason)}`));
    list.append(item);
  });
  notifyActions(actions, runId);
}

function cancelRecovery(focus = true) {
  recoverySecret = null;
  recoveryBinding = null;
  renderRecovery(latestEnvelope);
  if (focus) byId("recovery-control").querySelector("button")?.focus();
}

async function reviewRecovery(button) {
  const correlation = correlationId("recover");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  setControlStatus("Preparing a one-use recovery confirmation…");
  try {
    const result = await postJson("/api/v1/fleet/recover/confirm", { correlation_id: correlation });
    recoverySecret = result.confirmation_token;
    recoveryBinding = {
      correlation,
      runId: latestEnvelope.snapshot.run.run_id,
      generation: latestEnvelope.snapshot.verification.manifest_generation,
    };
    renderRecovery(latestEnvelope);
    byId("recovery-control").querySelector(".danger-button")?.focus();
    setControlStatus("Review the effect, then confirm abandonment.");
  } catch (error) {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    setControlStatus(`Recovery blocked: ${human(error.message)}.`);
  }
}

async function confirmRecovery(button) {
  if (!recoverySecret || !recoveryBinding) return;
  const token = recoverySecret;
  const correlation = recoveryBinding.correlation;
  const baseSequence = Number(latestEnvelope.snapshot.verification.covered_sequence || 0);
  recoverySecret = null;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  setControlStatus("Submitting abandonment. Waiting for verified evidence.");
  try {
    await postJson("/api/v1/fleet/recover", { correlation_id: correlation, confirmation_token: token });
    localMutations.set(correlation, { target: "recovery", baseSequence, accepted: true });
    recoveryBinding = null;
  } catch (error) {
    recoveryBinding = null;
    setControlStatus(`Recovery blocked: ${human(error.message)}. Review recovery again.`);
    renderRecovery(latestEnvelope);
  }
}

function renderRecovery(envelope) {
  const root = byId("recovery-control");
  root.replaceChildren();
  const recoveryMutation = [...localMutations.entries()].find(([, value]) => value.target === "recovery");
  const serverMutationPending = Array.isArray(envelope?.pending) && envelope.pending.length > 0;
  if (recoveryMutation || serverMutationPending) {
    const card = node("div", "recovery-card");
    card.setAttribute("role", "status");
    card.append(
      node("strong", "", "Recovery submitted"),
      node("p", "", "Waiting for verified mutation evidence. Another recovery cannot be started.")
    );
    root.append(card);
    return;
  }
  const rule = envelope?.eligibility?.recover_run || { eligible: false, reason: "recovery_not_available" };
  if (!rule.eligible) {
    if (rule.reason && rule.reason !== "recover_run_precondition_not_met") root.append(node("p", "empty-copy", `Recovery unavailable: ${human(rule.reason)}.`));
    return;
  }
  const card = node("div", "recovery-card");
  card.setAttribute("role", "group");
  card.setAttribute("aria-label", "Run recovery");
  if (!recoverySecret) {
    card.append(node("strong", "", "Recovery required"), node("p", "", "Review the interrupted run before recording abandonment."));
    const review = node("button", "danger-button", "Review recovery");
    review.type = "button";
    review.addEventListener("click", () => reviewRecovery(review));
    card.append(review);
  } else {
    const warningId = "recovery-effect";
    const warning = node("p", "", "This records the run as abandoned and closes it. No provider dispatch is performed.");
    warning.id = warningId;
    card.setAttribute("aria-describedby", warningId);
    const buttons = node("div", "action-buttons");
    const confirm = node("button", "danger-button", "Confirm abandonment");
    confirm.type = "button";
    confirm.setAttribute("aria-describedby", warningId);
    confirm.addEventListener("click", () => confirmRecovery(confirm));
    const cancel = node("button", "quiet-button", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", () => cancelRecovery());
    card.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); cancelRecovery(); } });
    buttons.append(confirm, cancel);
    card.append(node("strong", "", "Confirm recovery"), warning, buttons);
  }
  root.append(card);
}

function formatSettlement(key, value) {
  if (value === null || value === undefined) return "—";
  if (key.includes("usd") && /^-?\d+(\.\d+)?$/.test(String(value))) return `$${value}`;
  if (typeof value === "number") return value.toLocaleString();
  return human(value);
}
function renderSettlement(settlement) {
  const list = byId("settlement-list");
  list.replaceChildren();
  const labels = { metered_equivalent_usd: "Metered equivalent", direct_billed_usd: "Direct billed this run", billed_usd: "Direct billed this run", pricing_coverage: "Pricing coverage", priced_lanes: "Priced lanes", unpriced_lanes: "Unpriced lanes" };
  const entries = Object.entries(settlement || {}).filter(([key, value]) => key in labels && typeof value !== "object");
  if (!entries.length) {
    const group = node("div"); group.append(node("dt", "", "Coverage"), node("dd", "", "No completed usage")); list.append(group); return;
  }
  entries.forEach(([key, value]) => { const group = node("div"); group.append(node("dt", "", labels[key]), node("dd", "", formatSettlement(key, value))); list.append(group); });
}

function renderAnnotations(annotations) {
  const list = byId("annotation-list");
  list.replaceChildren();
  annotations.forEach((annotation) => {
    const item = node("li", "", `${human(annotation.kind)} · ${human(annotation.scope)}`);
    item.dataset.kind = annotation.kind;
    item.setAttribute("role", annotation.kind === "broker_unavailable" ? "alert" : "status");
    list.append(item);
  });
}

function renderUnavailable(envelope) {
  const snapshot = envelope.snapshot;
  const verification = snapshot?.verification || {};
  const label = upper(verification.state || "unavailable");
  setText("verify-label", label); setText("monitor-verify", label); setText("run-state", "UNAVAILABLE");
  setText("monitor-run", "Evidence data suppressed"); setText("open-count", 0); setText("monitor-open", "0 OPEN");
  showBanner(`Evidence unavailable: ${human(verification.finding || "verification failed")}. Run values are suppressed.`, true);
  renderRail([]); renderLanes([]); renderActions([], null, {}, []); renderRecovery(null); renderTallies({}); renderSettlement(null); renderAnnotations(envelope.annotations || []);
  updateComposerAvailability();
  byId("mini-monitor").setAttribute("aria-label", `TORQ Fleet monitor: ${human(label)}; evidence suppressed`);
}

function renderEnvelope(envelope) {
  const snapshot = envelope.snapshot;
  const verification = snapshot.verification || {};
  if (snapshot.data_status === "unavailable" || !snapshot.run || !SAFE_VERIFICATION.has(verification.state)) { renderUnavailable(envelope); return; }
  const run = snapshot.run;
  const summary = snapshot.summary || {};
  const lanes = Array.isArray(snapshot.lanes) ? snapshot.lanes : [];
  const actions = Array.isArray(snapshot.actions) ? snapshot.actions : [];
  const verifyLabel = upper(verification.state);
  setText("verify-label", verifyLabel); setText("run-state", upper(run.workflow_state)); setText("open-count", summary.open_actions || 0);
  setText("run-id", run.run_id); byId("run-id").setAttribute("aria-label", `Run ${run.run_id}`);
  setText("execution-state", run.execution_state); setText("workflow-state", run.workflow_state); setText("profile-id", run.profile_id);
  setText("receipt-count", `${valueOrDash(run.receipt_count)} RECEIPTS`); setText("monitor-verify", verifyLabel);
  setText("monitor-run", `${run.run_id} · ${human(run.workflow_state)}`); setText("monitor-open", `${summary.open_actions || 0} OPEN`);
  renderRail(lanes); renderTallies(summary); renderLanes(lanes);
  renderActions(actions, run.run_id, envelope.eligibility?.resolve_action || {}, envelope.pending);
  renderRecovery(envelope); renderSettlement(snapshot.settlement); renderAnnotations(envelope.annotations); updateElapsed();
  updateComposerAvailability();
  const annotation = envelope.annotations?.[0];
  if (annotation) showBanner(`${human(annotation.kind)}: ${human(annotation.scope)}.`, annotation.kind === "broker_unavailable");
  else if (snapshot.data_status === "reduction_error") showBanner(`State reduction error: ${(summary.reduction_errors || []).map(human).join(", ")}.`, true);
  else if (verification.state === "live_catching_up") showBanner("Verified manifest coverage is catching up. Uncovered receipts are hidden.");
  else if (!envelope.session?.write_capable) showBanner(`Read-only controls: ${human(envelope.session?.read_only_reason || "session_read_only")}.`);
  else clearBanner();
  byId("mini-monitor").setAttribute("aria-label", `TORQ Fleet monitor: ${human(verification.state)}; run ${run.run_id}; ${human(run.workflow_state)}; ${summary.open_actions || 0} open actions; elapsed ${durationText(snapshot)}`);
}

function applyEnvelope(envelope) {
  if (!validEnvelope(envelope)) throw new Error("fleet_envelope_invalid");
  reconcileLocalMutations(envelope);
  const changes = meaningfulChanges(latestEnvelope, envelope);
  const fingerprint = stateFingerprint(envelope);
  latestEnvelope = envelope;
  if (fingerprint === latestFingerprint) { updateElapsed(); return; }
  latestFingerprint = fingerprint;
  renderEnvelope(envelope);
  if (changes.length) announceLive(changes.join(". "));
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const response = await fetch("/api/v1/fleet", { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      showBanner(response.status === 401 ? "Fleet session expired. Relaunch torq fleet --serve." : `Fleet read blocked: ${human(result.finding || `HTTP ${response.status}`)}.`, true);
      setPoll("DISCONNECTED", "stale"); return;
    }
    applyEnvelope(await response.json()); setPoll("POLLING / 3S", "online");
  } catch (_error) { showBanner("Fleet is unreachable. The last verified display is retained while reconnecting.", true); setPoll("RECONNECTING", "stale"); }
  finally { refreshInFlight = false; }
}

function startPolling() {
  if (pollTimer !== null) return;
  refresh(); pollTimer = window.setInterval(refresh, POLL_MS);
  if ("EventSource" in window && streamRetryTimer === null) streamRetryTimer = window.setTimeout(() => {
    streamRetryTimer = null; if (pollTimer !== null) window.clearInterval(pollTimer); pollTimer = null; connectEvents();
  }, STREAM_RETRY_MS);
}

function connectEvents() {
  if (!("EventSource" in window)) { startPolling(); return; }
  eventStream = new EventSource("/api/v1/fleet/events");
  const scheduleFallback = () => {
    if (streamFallbackTimer !== null) return;
    streamFallbackTimer = window.setTimeout(() => { eventStream?.close(); eventStream = null; streamFallbackTimer = null; startPolling(); }, 5000);
  };
  scheduleFallback();
  eventStream.onopen = () => {
    if (streamFallbackTimer !== null) window.clearTimeout(streamFallbackTimer); streamFallbackTimer = null;
    if (streamRetryTimer !== null) window.clearTimeout(streamRetryTimer); streamRetryTimer = null;
  };
  eventStream.addEventListener("fleet", (event) => {
    try {
      applyEnvelope(JSON.parse(event.data));
      if (streamFallbackTimer !== null) window.clearTimeout(streamFallbackTimer); streamFallbackTimer = null;
      if (pollTimer !== null) window.clearInterval(pollTimer); pollTimer = null; setPoll("LIVE / SSE", "online");
    } catch (_error) { showBanner("Fleet received an invalid event. The last verified display is retained.", true); }
  });
  eventStream.onerror = scheduleFallback;
}

function configureNotifications() {
  const button = byId("notify-button");
  if (!("Notification" in window)) { button.textContent = "Alerts unavailable"; button.disabled = true; return; }
  const update = () => { button.textContent = Notification.permission === "granted" ? "Alerts enabled" : "Enable alerts"; button.disabled = Notification.permission === "granted"; };
  update();
  button.addEventListener("click", async () => {
    await Notification.requestPermission(); update();
    if (latestEnvelope) notifyActions(latestEnvelope.snapshot?.actions || [], latestEnvelope.snapshot?.run?.run_id);
  });
}

function configureTheme() {
  const control = byId("theme-control");
  const media = window.matchMedia("(prefers-color-scheme: light)");
  let selected = "system";
  try { const stored = localStorage.getItem(THEME_KEY); if (["system", "dark", "light"].includes(stored)) selected = stored; } catch (_error) { /* use system */ }
  const apply = () => { document.documentElement.dataset.theme = selected === "system" ? (media.matches ? "light" : "dark") : selected; control.value = selected; };
  control.addEventListener("change", () => { selected = control.value; try { localStorage.setItem(THEME_KEY, selected); } catch (_error) { /* theme still applies */ } apply(); });
  media.addEventListener("change", () => { if (selected === "system") apply(); });
  apply();
}

renderRail([]);
configureTheme();
configureNotifications();
configureComposer();
connectEvents();
elapsedTimer = window.setInterval(updateElapsed, 1000);
window.addEventListener("pagehide", () => {
  recoverySecret = null; recoveryBinding = null; window.clearInterval(elapsedTimer);
  if (pollTimer !== null) window.clearInterval(pollTimer);
  if (streamFallbackTimer !== null) window.clearTimeout(streamFallbackTimer);
  if (streamRetryTimer !== null) window.clearTimeout(streamRetryTimer);
  eventStream?.close();
}, { once: true });
