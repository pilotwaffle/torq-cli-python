"use strict";

const POLL_MS = 3000;
const STATES = ["dormant", "queued", "running", "sealed", "blocked", "needs_you", "failed", "interrupted", "abandoned"];
const TALLIES = ["sealed", "running", "queued", "dormant", "blocked", "needs_you", "failed", "interrupted", "abandoned"];
const FALLBACK_ROLES = ["g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui"];
const SEEN_ACTIONS_KEY = "torq.fleet.notified-actions.v1";

const byId = (id) => document.getElementById(id);
const node = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
};
const human = (value) => String(value ?? "unknown").replaceAll("_", " ");
const upper = (value) => human(value).toUpperCase();
const safeState = (value) => STATES.includes(String(value)) ? String(value) : "queued";
const valueOrDash = (value) => value === null || value === undefined || value === "" ? "—" : String(value);

let latestSnapshot = null;
let elapsedTimer = null;
let pollTimer = null;
let eventStream = null;
let streamFallbackTimer = null;
let streamRetryTimer = null;
let refreshInFlight = false;

function setText(id, value) {
  byId(id).textContent = valueOrDash(value);
}

function announce(message, danger = false) {
  const banner = byId("system-banner");
  banner.textContent = message;
  banner.className = `system-banner visible${danger ? " danger" : ""}`;
}

function clearAnnouncement() {
  byId("system-banner").className = "system-banner";
}

function setPoll(label, state) {
  byId("poll-label").textContent = label;
  byId("poll-dot").className = state;
}

function durationText(snapshot) {
  const run = snapshot?.run;
  if (!run || !run.started_at || !run.updated_at) return "—";
  const start = Date.parse(run.started_at);
  const end = run.workflow_state === "open" || run.workflow_state === "action_open"
    ? Date.now()
    : Date.parse(run.updated_at);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "—";
  const seconds = Math.floor((end - start) / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  const value = hours > 0
    ? `${hours}h ${String(minutes).padStart(2, "0")}m`
    : `${minutes}m ${String(remainder).padStart(2, "0")}s`;
  return run.workflow_state === "closed" || run.workflow_state === "abandoned"
    ? `~${value}`
    : value;
}

function updateElapsed() {
  const value = durationText(latestSnapshot);
  setText("elapsed", value);
  setText("monitor-elapsed", value);
}

function renderRail(lanes) {
  const rail = byId("lane-pips");
  const monitor = byId("monitor-pips");
  rail.replaceChildren();
  monitor.replaceChildren();
  const rows = lanes.length ? lanes : FALLBACK_ROLES.map((role) => ({ role, state: "dormant" }));
  rows.slice(0, 6).forEach((lane) => {
    const state = safeState(lane.state);
    const item = node("li", "", lane.role);
    item.dataset.state = state;
    item.setAttribute("aria-label", `${lane.role}: ${human(state)}`);
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
    if (count === 0 && !["sealed", "running", "queued", "dormant"].includes(state)) return;
    const group = node("div");
    group.append(node("dt", "", human(state)), node("dd", "", count));
    list.append(group);
  });
}

function detailDefinition(label, value) {
  const fragment = document.createDocumentFragment();
  fragment.append(node("dt", "", label), node("dd", "", valueOrDash(value)));
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

function renderLane(lane, index) {
  const state = safeState(lane.state);
  const article = node("article", "lane");
  article.dataset.state = state;

  const main = node("div", "lane-main lane-columns");
  const role = node("div", "lane-role");
  role.append(node("strong", "", lane.role), node("span", "", [lane.provider, lane.model].filter(Boolean).join(" / ") || lane.kind || "unassigned"));
  const stateChip = node("span", "state-chip", human(state));
  const attempt = node("span", "lane-number", lane.attempt_ordinal ? `#${lane.attempt_ordinal}` : "—");
  const sequence = node("span", "lane-number", lane.latest_sequence ? `SEQ ${lane.latest_sequence}` : "—");
  const summary = node("span", "lane-summary", laneSummary(lane));
  const toggle = node("button", "lane-toggle", "+");
  const detailId = `lane-detail-${index}`;
  toggle.type = "button";
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
    detailDefinition("Writer key", lane.latest_writer_key_id),
    detailDefinition("Dispatch", lane.dispatch_message),
    detailDefinition("Reason", lane.reason),
    detailDefinition("Contract", [lane.contract_id, lane.contract_version].filter(Boolean).join(" @ ")),
    detailDefinition("Prompt", [lane.prompt_id, lane.prompt_version].filter(Boolean).join(" @ "))
  );
  evidence.append(definitions);

  const historySection = node("section", "detail-group");
  historySection.append(node("h3", "", "Attempt history"));
  const history = node("ol", "history");
  const transitions = (lane.attempts || []).flatMap((item) => item.transitions || []);
  if (!transitions.length) {
    history.append(node("li", "", "No attempt transitions recorded."));
  } else {
    transitions.forEach((transition) => {
      const item = node("li");
      item.append(node("span", "", `#${transition.sequence}`), node("span", "", `${human(transition.transition)} · ${human(transition.writer_role)}`));
      history.append(item);
    });
  }
  historySection.append(history);
  detail.append(evidence, historySection);

  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!open));
    toggle.setAttribute("aria-label", `${open ? "Show" : "Hide"} ${lane.role} evidence detail`);
    detail.classList.toggle("open", !open);
  });
  article.append(main, detail);
  return article;
}

function renderLanes(lanes) {
  const list = byId("lane-list");
  list.replaceChildren();
  lanes.forEach((lane, index) => list.append(renderLane(lane, index)));
  if (!lanes.length) list.append(node("p", "empty-copy", "No evidence-backed lane data is available."));
  list.setAttribute("aria-busy", "false");
}

function readSeenActions() {
  try {
    const raw = JSON.parse(localStorage.getItem(SEEN_ACTIONS_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw.filter((item) => typeof item === "string") : []);
  } catch (_error) {
    return new Set();
  }
}

function storeSeenActions(seen) {
  try { localStorage.setItem(SEEN_ACTIONS_KEY, JSON.stringify([...seen].slice(-200))); } catch (_error) { /* storage is optional */ }
}

function notifyActions(actions, runId) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const seen = readSeenActions();
  actions.filter((action) => action.state === "open").forEach((action) => {
    const id = String(action.action_id || action.opened_sequence || "unknown");
    if (seen.has(id)) return;
    new Notification("TORQ Fleet needs you", {
      body: `Run ${runId || "unknown"} has an open governed action.`,
      tag: `torq-fleet-${id}`,
      renotify: false,
    });
    seen.add(id);
  });
  storeSeenActions(seen);
}

function renderActions(actions, runId) {
  const open = actions.filter((action) => action.state === "open");
  const list = byId("action-list");
  list.replaceChildren();
  if (!open.length) {
    list.append(node("p", "empty-copy", "No open operator actions."));
  } else {
    open.forEach((action) => {
      const item = node("div", "action-item");
      item.append(
        node("strong", "", action.summary || human(action.action_type || action.type || "Operator decision required")),
        node("span", "", `${valueOrDash(action.action_id)} · OPENED SEQ ${valueOrDash(action.opened_sequence)}`)
      );
      list.append(item);
    });
  }
  notifyActions(actions, runId);
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
  const labels = {
    metered_equivalent_usd: "Metered equivalent",
    direct_billed_usd: "Direct billed this run",
    billed_usd: "Direct billed this run",
    pricing_coverage: "Pricing coverage",
    priced_lanes: "Priced lanes",
    unpriced_lanes: "Unpriced lanes",
  };
  const entries = Object.entries(settlement || {}).filter(([key, value]) => key in labels && typeof value !== "object");
  if (!entries.length) {
    const group = node("div");
    group.append(node("dt", "", "Coverage"), node("dd", "", "No usage sealed"));
    list.append(group);
    return;
  }
  entries.forEach(([key, value]) => {
    const group = node("div");
    group.append(node("dt", "", labels[key]), node("dd", "", formatSettlement(key, value)));
    list.append(group);
  });
}

function renderUnavailable(snapshot) {
  const verification = snapshot?.verification || {};
  const label = upper(verification.state || "unavailable");
  setText("verify-label", label);
  setText("monitor-verify", label);
  setText("run-state", "UNAVAILABLE");
  setText("monitor-run", "Evidence data suppressed");
  announce(`Evidence unavailable: ${human(verification.finding || "verification failed")}. Run values are suppressed.`, true);
  renderRail([]);
  renderLanes([]);
  renderActions([], null);
  renderTallies({});
  renderSettlement(null);
}

function render(snapshot) {
  latestSnapshot = snapshot;
  if (snapshot.data_status === "unavailable" || !snapshot.run) {
    renderUnavailable(snapshot);
    return;
  }
  const run = snapshot.run;
  const summary = snapshot.summary || {};
  const lanes = Array.isArray(snapshot.lanes) ? snapshot.lanes : [];
  const actions = Array.isArray(snapshot.actions) ? snapshot.actions : [];
  const verification = snapshot.verification || {};
  const verifyLabel = upper(verification.state);

  setText("verify-label", verifyLabel);
  setText("run-state", upper(run.workflow_state));
  setText("open-count", summary.open_actions || 0);
  setText("run-id", run.run_id);
  setText("execution-state", run.execution_state);
  setText("workflow-state", run.workflow_state);
  setText("profile-id", run.profile_id);
  setText("receipt-count", `${valueOrDash(run.receipt_count)} RECEIPTS`);
  setText("monitor-verify", verifyLabel);
  setText("monitor-run", `${run.run_id} · ${human(run.workflow_state)}`);
  setText("monitor-open", `${summary.open_actions || 0} OPEN`);

  renderRail(lanes);
  renderTallies(summary);
  renderLanes(lanes);
  renderActions(actions, run.run_id);
  renderSettlement(snapshot.settlement);
  updateElapsed();

  if (snapshot.data_status === "reduction_error") {
    announce(`State reduction error: ${(summary.reduction_errors || []).map(human).join(", ")}.`, true);
  } else if (verification.state === "live_catching_up") {
    announce("Verified manifest coverage is catching up. Uncovered receipts are intentionally hidden.");
  } else {
    clearAnnouncement();
  }
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const response = await fetch("/api/v1/fleet", { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      const finding = result.finding || `HTTP ${response.status}`;
      if (response.status === 401) announce("Fleet session expired. Relaunch `torq fleet --serve` to authenticate again.", true);
      else announce(`Fleet read blocked: ${human(finding)}.`, true);
      setPoll("DISCONNECTED", "stale");
      return;
    }
    const envelope = await response.json();
    render(envelope.snapshot);
    setPoll("POLLING / 3S", "online");
  } catch (_error) {
    announce("Fleet is unreachable. The last verified display is retained while reconnecting.", true);
    setPoll("RECONNECTING", "stale");
  } finally {
    refreshInFlight = false;
  }
}

function startPolling() {
  if (pollTimer !== null) return;
  refresh();
  pollTimer = window.setInterval(refresh, POLL_MS);
  if ("EventSource" in window && streamRetryTimer === null) {
    streamRetryTimer = window.setTimeout(() => {
      streamRetryTimer = null;
      if (pollTimer !== null) window.clearInterval(pollTimer);
      pollTimer = null;
      connectEvents();
    }, 30000);
  }
}

function connectEvents() {
  if (!("EventSource" in window)) {
    startPolling();
    return;
  }
  eventStream = new EventSource("/api/v1/fleet/events");
  const scheduleFallback = () => {
    if (streamFallbackTimer !== null) return;
    streamFallbackTimer = window.setTimeout(() => {
      eventStream?.close();
      eventStream = null;
      streamFallbackTimer = null;
      startPolling();
    }, 5000);
  };
  scheduleFallback();
  eventStream.onopen = () => {
    if (streamFallbackTimer !== null) window.clearTimeout(streamFallbackTimer);
    streamFallbackTimer = null;
    if (streamRetryTimer !== null) window.clearTimeout(streamRetryTimer);
    streamRetryTimer = null;
  };
  eventStream.addEventListener("fleet", (event) => {
    try {
      const envelope = JSON.parse(event.data);
      render(envelope.snapshot);
      if (streamFallbackTimer !== null) window.clearTimeout(streamFallbackTimer);
      streamFallbackTimer = null;
      if (pollTimer !== null) window.clearInterval(pollTimer);
      pollTimer = null;
      setPoll("LIVE / SSE", "online");
    } catch (_error) {
      announce("Fleet received an invalid event. The last verified display is retained.", true);
    }
  });
  eventStream.onerror = () => {
    scheduleFallback();
  };
}

function configureNotifications() {
  const button = byId("notify-button");
  if (!("Notification" in window)) {
    button.textContent = "Alerts unavailable";
    button.disabled = true;
    return;
  }
  const update = () => {
    button.textContent = Notification.permission === "granted" ? "Alerts enabled" : "Enable alerts";
    button.disabled = Notification.permission === "granted";
  };
  update();
  button.addEventListener("click", async () => {
    await Notification.requestPermission();
    update();
    if (latestSnapshot) notifyActions(latestSnapshot.actions || [], latestSnapshot.run?.run_id);
  });
}

renderRail([]);
configureNotifications();
connectEvents();
elapsedTimer = setInterval(updateElapsed, 1000);
window.addEventListener("pagehide", () => {
  clearInterval(elapsedTimer);
  if (pollTimer !== null) clearInterval(pollTimer);
  if (streamFallbackTimer !== null) clearTimeout(streamFallbackTimer);
  if (streamRetryTimer !== null) clearTimeout(streamRetryTimer);
  eventStream?.close();
}, { once: true });
