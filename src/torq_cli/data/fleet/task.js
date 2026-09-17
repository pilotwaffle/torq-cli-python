(() => {
  "use strict";
  class MutationQueue {
    constructor() { this.tail = Promise.resolve(); }
    enqueue(work) { this.tail = this.tail.catch(() => {}).then(work); return this.tail; }
  }
  class EditGate {
    constructor() { this.epoch = 0; }
    edited() { this.epoch += 1; }
    capture(project, snapshot) { return {epoch:this.epoch, project, snapshot}; }
    accepts(ticket, project, snapshot) { return ticket.epoch === this.epoch && ticket.project === project && ticket.snapshot === snapshot; }
  }
  function requestIdForPlan(planHash, storage, cryptoSource) {
    const key = `torq.task.request.${planHash}`;
    try {
      const value = storage.getItem(key) || cryptoSource.randomUUID();
      storage.setItem(key, value);
      return value;
    } catch (_) { return `request-${planHash.slice(7,31)}`; }
  }
  globalThis.TorqTask = { MutationQueue, EditGate, requestIdForPlan };
  const byId = (id) => document.getElementById(id);
  const view = byId("task-view");
  const button = byId("task-view-button");
  if (!view || !button) return;

  const mutationQueue = new MutationQueue();
  const editGate = new EditGate();
  const state = { capabilities: null, project: "", revisions: {}, saved: {}, plan: null, active: null, loadToken: 0, saveTimer: null, initialized: false, draftReady: false };
  const lines = (value) => value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));

  async function request(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", headers: {"Content-Type":"application/json"}, ...options });
    const body = await response.json();
    if (!response.ok) throw new Error(body.finding || "task_request_failed");
    return body;
  }

  function setStatus(message) { byId("task-draft-status").textContent = message; }
  function setView() {
    document.body.dataset.dashboardView = "task";
    view.hidden = false;
    byId("workspace-view").hidden = true;
    byId("fleet-board").hidden = true;
    button.setAttribute("aria-pressed", "true");
    byId("workspace-view-button").setAttribute("aria-pressed", "false");
    byId("fleet-view-button").setAttribute("aria-pressed", "false");
    byId("skip-link").href = "#task-view";
    byId("skip-link").textContent = "Skip to new task";
    view.focus();
    if (!state.initialized) {
      state.initialized = true;
      refresh().catch(() => { state.initialized = false; setDraftReady(false); setStatus("Task setup is unavailable. Reload to try again."); });
    } else {
      loadHistory().catch(() => setStatus("Could not refresh task history."));
    }
  }
  button.addEventListener("click", setView);
  [byId("workspace-view-button"), byId("fleet-view-button")].forEach((item) => item.addEventListener("click", () => {
    view.hidden = true;
    button.setAttribute("aria-pressed", "false");
    delete document.body.dataset.dashboardView;
  }));

  function setDraftReady(ready) {
    state.draftReady = ready;
    ["task-project", "task-goal", "task-input-paths", "task-output-paths", "task-clear", "task-review"].forEach((id) => {
      const control = byId(id);
      if (control) control.disabled = !ready;
    });
  }

  async function loadDraft(project, expectedEpoch = editGate.epoch) {
    const token = ++state.loadToken;
    const epoch = expectedEpoch;
    const body = await request(`/api/v1/tasks/drafts/${encodeURIComponent(project)}`, {headers:{}});
    if (token !== state.loadToken || project !== state.project) return;
    const draft = body.draft;
    state.revisions[project] = draft ? draft.revision : 0;
    if (epoch !== editGate.epoch) return;
    byId("task-goal").value = draft ? draft.goal : "";
    byId("task-input-paths").value = draft ? draft.input_paths.join("\n") : "";
    byId("task-output-paths").value = draft ? draft.output_paths.join("\n") : "";
    state.saved[project] = JSON.stringify({project, goal:draft ? draft.goal : "", inputPaths:draft ? draft.input_paths : [], outputPaths:draft ? draft.output_paths : []});
    setStatus(draft && draft.goal ? `Saved on this installation · revision ${state.revisions[project]}` : "Drafts are saved on this installation.");
  }

  async function refresh() {
    setDraftReady(false);
    setStatus("Loading saved draft...");
    const initialEpoch = editGate.epoch;
    const caps = await request("/api/v1/tasks/capabilities", {headers:{}});
    state.capabilities = caps;
    const select = byId("task-project");
    const prior = state.project;
    select.innerHTML = caps.projects.map((item) => `<option value="${escape(item.id)}">${escape(item.label)}</option>`).join("");
    state.project = caps.projects.some((item) => item.id === prior) ? prior : (caps.projects[0] || {}).id || "";
    select.value = state.project;
    await loadDraft(state.project, initialEpoch);
    setDraftReady(true);
    await loadHistory();
    if (caps.active_task_id) {
      state.active = caps.active_task_id;
      pollTask();
    }
  }

  async function refreshTaskCapabilities() {
    state.capabilities = await request("/api/v1/tasks/capabilities", {headers:{}});
    return state.capabilities;
  }

  byId("task-project").addEventListener("change", async (event) => {
    const newProject = event.target.value;
    const pendingSave = byId("task-goal").value.trim() ? saveDraft().catch(() => {}) : Promise.resolve();
    setDraftReady(false);
    state.plan = null;
    byId("task-start").disabled = true;
    await pendingSave;
    state.project = newProject;
    try { await loadDraft(state.project); setDraftReady(true); }
    catch (error) { setStatus(error.message.replaceAll("_", " ")); }
  });

  const formSnapshot = () => ({
    project: state.project,
    goal: byId("task-goal").value,
    inputPaths: lines(byId("task-input-paths").value),
    outputPaths: lines(byId("task-output-paths").value)
  });
  const enqueueMutation = (work) => {
    return mutationQueue.enqueue(work);
  };

  function saveDraft() {
    if (!state.draftReady) return Promise.reject(new Error("task_draft_loading"));
    const snapshot = formSnapshot();
    const work = async () => {
    const {project, goal, inputPaths, outputPaths} = snapshot;
    if (!goal.trim()) throw new Error("Enter a goal before reviewing.");
    const encoded = JSON.stringify(snapshot);
    if (state.saved[project] === encoded) {
      return {project_id:project, goal, input_paths:inputPaths, output_paths:outputPaths, revision:state.revisions[project] || 0};
    }
    const body = await request(`/api/v1/tasks/drafts/${encodeURIComponent(project)}`, {
      method:"POST", body:JSON.stringify({goal, input_paths:inputPaths, output_paths:outputPaths, expected_revision:state.revisions[project] || 0})
    });
    state.revisions[project] = body.result.revision;
    state.saved[project] = encoded;
    if (project === state.project && JSON.stringify(formSnapshot()) === JSON.stringify(snapshot)) {
      setStatus(`Saved on this installation · revision ${body.result.revision}`);
    }
    return body.result;
    };
    return enqueueMutation(work);
  }

  ["task-goal","task-input-paths","task-output-paths"].forEach((id) => byId(id).addEventListener("input", () => {
    editGate.edited();
    state.plan = null;
    byId("task-start").disabled = true;
    byId("task-plan-summary").textContent = "Review the updated goal and scope before starting.";
    setStatus("Saving changes…");
    clearTimeout(state.saveTimer);
    if (byId("task-goal").value.trim()) state.saveTimer = setTimeout(() => saveDraft().catch((error) => setStatus(error.message.replaceAll("_", " "))), 650);
  }));

  byId("task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      clearTimeout(state.saveTimer);
      const reviewedSnapshot = JSON.stringify(formSnapshot());
      const reviewTicket = editGate.capture(state.project, reviewedSnapshot);
      const draft = await saveDraft();
      const reviewedProject = state.project;
      const body = await enqueueMutation(() => request("/api/v1/tasks/plans", {method:"POST", body:JSON.stringify({project_id:reviewedProject, draft_revision:draft.revision, input_paths:draft.input_paths, output_paths:draft.output_paths})}));
      if (!editGate.accepts(reviewTicket, state.project, JSON.stringify(formSnapshot())) || draft.revision !== state.revisions[state.project]) return;
      state.plan = body.result;
      const projectLabel = (state.capabilities.projects.find((item) => item.id === state.project) || {}).label || state.project;
      byId("task-plan-summary").innerHTML = `<dl><div><dt>Project</dt><dd>${escape(projectLabel)}</dd></div><div><dt>Provider</dt><dd>${escape(state.plan.provider)} · ${escape(state.plan.model)}</dd></div><div><dt>Check</dt><dd>Syntax and file format</dd></div></dl><strong>Allowed files</strong><ul class="task-file-list">${state.plan.output_paths.map((path) => `<li>${escape(path)}</li>`).join("")}</ul><p class="task-hint">The provider can return contents only for these paths. TORQ writes a separate candidate and runs Python syntax, strict JSON, or UTF-8 checks. It does not run unit tests or change your project.</p><details><summary>Plan digest</summary><code>${escape(state.plan.plan_hash)}</code></details>`;
      byId("task-start").disabled = !state.capabilities.can_start_task;
    } catch (error) { setStatus(error.message.replaceAll("_", " ")); }
  });

  byId("task-clear").addEventListener("click", async () => {
    try {
      clearTimeout(state.saveTimer);
      const project = state.project;
      const deleteTicket = editGate.capture(project, JSON.stringify(formSnapshot()));
      const body = await enqueueMutation(() => request(`/api/v1/tasks/drafts/${encodeURIComponent(project)}/delete`, {method:"POST", body:JSON.stringify({expected_revision:state.revisions[project] || 0})}));
      state.revisions[project] = body.result.revision;
      state.saved[project] = JSON.stringify({project, goal:"", inputPaths:[], outputPaths:[]});
      if (!editGate.accepts(deleteTicket, state.project, JSON.stringify(formSnapshot()))) return;
      ["task-goal","task-input-paths","task-output-paths"].forEach((id) => { byId(id).value = ""; });
      state.plan = null; byId("task-start").disabled = true; byId("task-plan-summary").textContent = "Save a goal to review its exact execution plan.";
      setStatus("Draft deleted from this installation.");
    } catch (error) { setStatus(error.message.replaceAll("_", " ")); }
  });

  byId("task-start").addEventListener("click", async () => {
    if (!state.plan) return;
    const plan = state.plan;
    const startTicket = editGate.capture(state.project, JSON.stringify(formSnapshot()));
    byId("task-start").disabled = true;
    try {
      const requestId = requestIdForPlan(plan.plan_hash, localStorage, globalThis.crypto);
      const body = await enqueueMutation(() => {
        if (state.plan !== plan || !editGate.accepts(startTicket, state.project, JSON.stringify(formSnapshot()))) throw new Error("task_plan_changed");
        return request("/api/v1/tasks", {method:"POST", body:JSON.stringify({request_id:requestId, plan_hash:plan.plan_hash})});
      });
      if (state.plan !== plan || !editGate.accepts(startTicket, state.project, JSON.stringify(formSnapshot()))) return;
      state.active = body.result.task_id;
      renderTask(body.result);
      pollTask();
    } catch (error) { if (state.plan === plan) byId("task-start").disabled = false; setStatus(`${error.message.replaceAll("_", " ")}. Select Start building to retry the same request.`); }
  });

  function renderTask(task) {
    const terminal = ["candidate_ready","failed","cancelled","interrupted","termination_unknown","untrusted"].includes(task.state);
    byId("task-current").innerHTML = `<span class="task-state">${escape(task.state.replaceAll("_"," "))}</span>${task.finding ? `<p>${escape(task.finding.replaceAll("_"," "))}</p>` : ""}${task.verified ? `<div class="task-result"><strong>Candidate verified</strong><br>${escape((task.candidate_files || []).join(", "))}<br>Structural check exit ${escape(task.check.exit_code)}</div>` : ""}`;
    byId("task-stop").disabled = terminal;
  }

  async function pollTask() {
    if (!state.active) return;
    try {
      const body = await request(`/api/v1/tasks/${encodeURIComponent(state.active)}`, {headers:{}});
      renderTask(body.task);
      if (!["candidate_ready","failed","cancelled","interrupted","termination_unknown","untrusted"].includes(body.task.state)) setTimeout(pollTask, 600);
      else { state.active = null; await loadHistory(); await refreshTaskCapabilities(); }
    } catch (_) { setTimeout(pollTask, 1200); }
  }

  byId("task-stop").addEventListener("click", async () => {
    if (!state.active) return;
    const activeTask = state.active;
    byId("task-stop").disabled = true;
    try {
      const body = await enqueueMutation(() => request(`/api/v1/tasks/${encodeURIComponent(activeTask)}/stop`, {method:"POST", body:""}));
      renderTask(body.result);
      if (["candidate_ready","failed","cancelled","interrupted","termination_unknown","untrusted"].includes(body.result.state)) {
        state.active = null;
        await loadHistory();
        await refreshTaskCapabilities();
      }
    }
    catch (error) { setStatus(error.message.replaceAll("_", " ")); }
  });

  async function loadHistory() {
    const body = await request("/api/v1/tasks", {headers:{}});
    byId("task-history-list").innerHTML = body.tasks.length ? body.tasks.map((task) => `<button type="button" class="task-history-row" data-task-id="${escape(task.task_id)}"><span><strong>${escape(task.goal_summary || "Candidate task")}</strong><small>${escape(task.project_label || "Configured project")}</small></span><span class="task-state">${escape(task.state.replaceAll("_"," "))}</span></button>`).join("") : "No candidate history yet.";
    byId("task-history-list").querySelectorAll("[data-task-id]").forEach((row) => row.addEventListener("click", async () => {
      const body = await request(`/api/v1/tasks/${encodeURIComponent(row.dataset.taskId)}`, {headers:{}});
      state.active = body.task.task_id;
      renderTask(body.task);
      if (!["candidate_ready","failed","cancelled","interrupted","termination_unknown","untrusted"].includes(body.task.state)) pollTask();
      else { state.active = null; await refreshTaskCapabilities(); }
    }));
  }

  request("/api/v1/tasks/capabilities", {headers:{}}).then((caps) => {
    state.capabilities = caps;
    button.hidden = false;
    if (new URLSearchParams(location.search).get("view") === "task") setView();
  }).catch(() => { button.hidden = true; });
})();
