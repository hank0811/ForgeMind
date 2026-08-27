/* ForgeMind website interface -- vanilla JS controller.
 *
 * Talks only to the local API bridge (webapp/backend/app.py), which talks
 * only to the real forgemind package. This file contains no pipeline
 * logic of its own -- it renders whatever the backend reports and sends
 * user actions (approve/reject/submit-artifact) straight through.
 *
 * Live progress is polling, not a fabricated animation: state.json is
 * written synchronously on every real transition, so re-fetching
 * GET /api/tasks/<id> every POLL_MS genuinely reflects the pipeline's
 * current state.
 */
(() => {
  "use strict";

  const POLL_MS = 2500;
  const STAGE_ORDER = ["analyst", "architect_planner", "implementer", "tester", "reviewer", "finalizer"];
  const STAGE_LABELS = {
    analyst: "Analyst",
    architect_planner: "Architect",
    implementer: "Implementer",
    tester: "Tester",
    reviewer: "Reviewer",
    finalizer: "Finalizer",
  };
  // Mirrors state_machine.TERMINAL_STATES exactly.
  const TERMINAL_STATES = new Set(["COMPLETED", "FAILED", "CANCELLED", "PLAN_REJECTED", "FINAL_REJECTED"]);
  const IN_PROGRESS_TO_STAGE = {
    ANALYZING: 0, DESIGNING: 1, IMPLEMENTING: 2, TESTING: 3, REVIEWING: 4, FINALIZING: 5,
  };
  const IN_PROGRESS_TO_ROLE = {
    ANALYZING: "analyst", DESIGNING: "architect_planner", IMPLEMENTING: "implementer",
    TESTING: "tester", REVIEWING: "reviewer", FINALIZING: "finalizer",
  };

  const els = {};
  document.querySelectorAll("[id]").forEach((el) => { els[toCamel(el.id)] = el; });
  function toCamel(id) { return id.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }

  let pollTimer = null;
  let currentTaskId = null;
  let config = null;

  // ---- API -----------------------------------------------------------
  async function api(path, options) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) {
      const message = (data && (data.error || (data.errors && data.errors.join("; ")))) || `request failed (${res.status})`;
      const err = new Error(message);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  const getConfig = () => api("/api/config");
  const listTasks = () => api("/api/tasks");
  const getTask = (id) => api(`/api/tasks/${encodeURIComponent(id)}`);
  const createTask = (body) => api("/api/tasks", { method: "POST", body: JSON.stringify(body) });
  const approveTask = (id) => api(`/api/tasks/${encodeURIComponent(id)}/approve`, { method: "POST" });
  const rejectTask = (id, reason) => api(`/api/tasks/${encodeURIComponent(id)}/reject`, { method: "POST", body: JSON.stringify({ reason }) });
  const submitArtifact = (id, payload) => api(`/api/tasks/${encodeURIComponent(id)}/submit-artifact`, { method: "POST", body: JSON.stringify(payload) });
  const getArtifacts = (id) => api(`/api/tasks/${encodeURIComponent(id)}/artifacts`);
  const deleteTask = (id) => api(`/api/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });

  // ---- Boot ------------------------------------------------------------
  async function boot() {
    wireStaticHandlers();
    try {
      config = await getConfig();
      renderRunnerBadge();
    } catch (err) {
      showStartError(`Could not reach the ForgeMind backend: ${err.message}`);
    }

    const saved = localStorage.getItem("forgemind.lastTaskId");
    if (saved) {
      // If this saved task genuinely doesn't exist anymore, openTask()
      // already shows the friendly "not found" state -- that IS the
      // correct landing page here, not something to silently replace.
      await openTask(saved);
      return;
    }
    try {
      const tasks = await listTasks();
      const active = tasks.find((t) => !t.is_terminal);
      if (active) await openTask(active.task_id);
      else showStartView();
    } catch (_) {
      showStartView();
    }
  }

  function renderRunnerBadge() {
    els.runnerBadge.hidden = false;
    if (config.runner === "claude_cli") {
      els.runnerBadge.textContent = config.claude_cli_available
        ? "Claude CLI mode — automatic"
        : "Claude CLI mode — claude not found on PATH";
      els.runnerBadge.className = "badge " + (config.claude_cli_available ? "badge-info" : "badge-warn");
    } else {
      els.runnerBadge.textContent = "Manual mode — you write each artifact";
      els.runnerBadge.className = "badge badge-info";
    }
    if (els.modeHint) {
      els.modeHint.textContent = config.runner === "claude_cli"
        ? "This backend is configured for runner: claude_cli — each stage will run automatically through your authenticated Claude CLI."
        : "This backend is configured for runner: manual — after each stage you'll be asked to paste that stage's artifact yourself.";
    }
  }

  // ---- View switching --------------------------------------------------
  function showStartView() {
    stopPolling();
    currentTaskId = null;
    localStorage.removeItem("forgemind.lastTaskId");
    els.viewWorkflow.hidden = true;
    els.viewNotFound.hidden = true;
    els.viewStart.hidden = false;
  }

  function showWorkflowView() {
    els.viewStart.hidden = true;
    els.viewNotFound.hidden = true;
    els.viewWorkflow.hidden = false;
  }

  function showNotFoundView(taskId) {
    stopPolling();
    currentTaskId = null;
    localStorage.removeItem("forgemind.lastTaskId");
    els.notFoundMessage.textContent = taskId
      ? `Task ${taskId} doesn't exist, or was deleted.`
      : "This task doesn't exist, or was deleted.";
    els.viewStart.hidden = true;
    els.viewWorkflow.hidden = true;
    els.viewNotFound.hidden = false;
  }

  // ---- New task form -----------------------------------------------
  function wireStaticHandlers() {
    els.newTaskForm.addEventListener("submit", onCreateTask);
    els.historyToggle.addEventListener("click", openHistory);
    els.historyClose.addEventListener("click", closeHistory);
    els.newTaskToggle.addEventListener("click", showStartView);
    els.notFoundNewTask.addEventListener("click", showStartView);
    els.notFoundHistory.addEventListener("click", openHistory);
    els.approveBtn.addEventListener("click", onApprove);
    els.rejectBtn.addEventListener("click", onReject);
    els.manualSubmitBtn.addEventListener("click", onManualSubmit);
  }

  async function onCreateTask(evt) {
    evt.preventDefault();
    hideBanner(els.startError);
    const requestText = els.requestInput.value.trim();
    const workspacePath = els.workspaceInput.value.trim();
    if (!requestText) return;

    setBusy(els.startBtn, true, "Starting…");
    try {
      const { task_id } = await createTask({ request: requestText, workspace_path: workspacePath || null });
      els.requestInput.value = "";
      els.workspaceInput.value = "";
      await openTask(task_id);
    } catch (err) {
      showStartError(err.message);
    } finally {
      setBusy(els.startBtn, false, "Start ForgeMind");
    }
  }

  function showStartError(msg) {
    els.startError.textContent = msg;
    els.startError.hidden = false;
  }
  function hideBanner(el) { el.hidden = true; el.textContent = ""; }

  function setBusy(btn, busy, label) {
    btn.disabled = busy;
    btn.querySelector(".btn-label") ? (btn.querySelector(".btn-label").textContent = label) : (btn.textContent = label);
  }

  // ---- History panel -----------------------------------------------
  async function openHistory() {
    els.historyPanel.hidden = false;
    els.historyList.innerHTML = '<li class="history-empty">Loading…</li>';
    try {
      const tasks = await listTasks();
      renderHistory(tasks);
    } catch (err) {
      els.historyList.innerHTML = `<li class="history-empty">${escapeHtml(err.message)}</li>`;
    }
  }
  function closeHistory() { els.historyPanel.hidden = true; }

  function renderHistory(tasks) {
    if (!tasks.length) {
      els.historyList.innerHTML = '<li class="history-empty">No tasks yet.</li>';
      return;
    }
    els.historyList.innerHTML = "";
    for (const t of tasks) {
      const li = document.createElement("li");
      li.className = "history-row";

      const btn = document.createElement("button");
      btn.className = "history-item";
      btn.type = "button";
      const title = t.request ? truncate(t.request, 60) : t.task_id;
      btn.innerHTML = `<div class="history-item-title">${escapeHtml(title)}</div>
        <div class="history-item-state">${escapeHtml(t.state)}</div>
        <div class="history-item-time">${escapeHtml(formatTime(t.created_at))}</div>`;
      btn.addEventListener("click", async () => {
        closeHistory();
        await openTask(t.task_id);
      });

      const del = document.createElement("button");
      del.className = "history-delete";
      del.type = "button";
      del.title = "Delete this task";
      del.setAttribute("aria-label", "Delete this task");
      del.textContent = "✕";
      del.addEventListener("click", async (evt) => {
        evt.stopPropagation();
        if (!confirm(`Delete task ${t.task_id}? This cannot be undone.`)) return;
        try {
          await deleteTask(t.task_id);
          if (currentTaskId === t.task_id) showStartView();
          await openHistory();
        } catch (err) {
          alert(`Could not delete: ${err.message}`);
        }
      });

      li.appendChild(btn);
      li.appendChild(del);
      els.historyList.appendChild(li);
    }
  }

  // ---- Opening / polling a task -------------------------------------
  async function openTask(taskId) {
    let status;
    try {
      status = await getTask(taskId);
    } catch (err) {
      showNotFoundView(taskId); // any fetch failure here: never leave a broken page
      return false;
    }
    currentTaskId = taskId;
    localStorage.setItem("forgemind.lastTaskId", taskId);
    showWorkflowView();
    renderStatus(status);
    if (!status.is_terminal) {
      stopPolling();
      pollTimer = setInterval(refresh, POLL_MS);
    }
    try {
      const { artifacts: produced } = await getArtifacts(taskId);
      renderArtifacts(produced);
    } catch (_) { /* secondary info; see refresh() */ }
    return true;
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  async function refresh() {
    if (!currentTaskId) return;
    const taskId = currentTaskId;
    let status;
    try {
      status = await getTask(taskId);
    } catch (err) {
      stopPolling();
      if (err.status === 404) showNotFoundView(taskId);
      else renderFatal(err.message);
      return;
    }
    renderStatus(status);
    if (status.is_terminal) stopPolling();

    try {
      const { artifacts: produced } = await getArtifacts(taskId);
      renderArtifacts(produced);
    } catch (_) {
      // Artifacts are secondary information; a fetch failure here must
      // never overwrite the real status banner above with a fake one.
    }
  }

  function renderFatal(msg) {
    els.errorBanner.hidden = false;
    els.errorText.textContent = msg;
  }

  // ---- Rendering ------------------------------------------------------
  function renderStatus(status) {
    els.wfTaskId.textContent = status.task_id;
    els.wfRequest.textContent = status.request || "";

    renderStageTracker(status);

    const { label, tone, reason } = describeState(status);
    els.statusCard.dataset.tone = tone;
    els.statusLabel.textContent = label;
    els.statusReason.textContent = reason || "";
    els.statusSpinner.hidden = !status.running;

    if (status.error) {
      els.errorBanner.hidden = false;
      els.errorText.textContent = status.error;
    } else {
      els.errorBanner.hidden = true;
    }

    renderGovernance(status);
    renderManualSubmit(status);
    renderActivity(status.history || []);
  }

  function describeState(status) {
    const s = status.state;
    const lastReason = (status.history && status.history.length)
      ? status.history[status.history.length - 1].reason
      : null;

    if (s === "COMPLETED") return { label: "Completed", tone: "done", reason: "The pipeline finished successfully. See the artifacts below." };
    if (s === "FAILED") return { label: "Failed", tone: "failed", reason: lastReason || "The pipeline stopped with an error." };
    if (s === "CANCELLED") return { label: "Cancelled", tone: "failed", reason: lastReason || "" };
    if (s === "PLAN_REJECTED") return { label: "Plan rejected", tone: "failed", reason: lastReason || "" };
    if (s === "FINAL_REJECTED") return { label: "Final result rejected", tone: "failed", reason: lastReason || "" };
    if (s === "AWAITING_PLAN_APPROVAL") return { label: "Waiting for plan approval", tone: "waiting", reason: lastReason || "" };
    if (s === "AWAITING_FINAL_APPROVAL") return { label: "Waiting for final approval", tone: "waiting", reason: lastReason || "" };
    if (s === "BLOCKED") {
      const role = status.blocked_from ? IN_PROGRESS_TO_ROLE[status.blocked_from] : null;
      return {
        label: role ? `Waiting for the ${STAGE_LABELS[role]} artifact` : "Waiting for manual input",
        tone: "waiting",
        reason: "This task's runner needs a human to submit this stage's artifact before continuing.",
      };
    }
    if (s in IN_PROGRESS_TO_ROLE) {
      const role = IN_PROGRESS_TO_ROLE[s];
      return { label: `${STAGE_LABELS[role]} is working…`, tone: "active", reason: status.running ? "Running now." : "" };
    }
    return { label: s, tone: "active", reason: lastReason || "" };
  }

  function renderStageTracker(status) {
    const info = stageProgress(status);
    els.stageTracker.innerHTML = "";
    STAGE_ORDER.forEach((role, idx) => {
      const li = document.createElement("li");
      let stageStatus = "pending";
      if (info.failedIndex === idx) stageStatus = "failed";
      else if (idx < info.doneCount) stageStatus = "done";
      else if (idx === info.activeIndex) stageStatus = "active";
      li.dataset.status = stageStatus;
      li.innerHTML = `<span class="stage-dot">${stageStatus === "done" ? "✓" : idx + 1}</span>
        <span class="stage-label">${STAGE_LABELS[role]}</span>`;
      els.stageTracker.appendChild(li);
    });
  }

  function stageProgress(status) {
    const s = status.state;
    const table = {
      CREATED: { doneCount: 0, activeIndex: null },
      ANALYZING: { doneCount: 0, activeIndex: 0 },
      ANALYZED: { doneCount: 1, activeIndex: 1 },
      DESIGNING: { doneCount: 1, activeIndex: 1 },
      DESIGNED: { doneCount: 1, activeIndex: 1 },
      AWAITING_PLAN_APPROVAL: { doneCount: 1, activeIndex: 1 },
      PLAN_APPROVED: { doneCount: 2, activeIndex: 2 },
      IMPLEMENTING: { doneCount: 2, activeIndex: 2 },
      IMPLEMENTED: { doneCount: 3, activeIndex: 3 },
      TESTING: { doneCount: 3, activeIndex: 3 },
      TESTS_PASSED: { doneCount: 4, activeIndex: 4 },
      TESTS_FAILED: { doneCount: 2, activeIndex: 2 },
      REVIEWING: { doneCount: 4, activeIndex: 4 },
      REVIEWED: { doneCount: 4, activeIndex: 4 },
      AWAITING_FINAL_APPROVAL: { doneCount: 4, activeIndex: 4 },
      FINAL_APPROVED: { doneCount: 5, activeIndex: 5 },
      FINALIZING: { doneCount: 5, activeIndex: 5 },
      COMPLETED: { doneCount: 6, activeIndex: null },
    };
    if (s in table) return { ...table[s], failedIndex: -1 };

    if (s === "BLOCKED" && status.blocked_from in IN_PROGRESS_TO_STAGE) {
      const idx = IN_PROGRESS_TO_STAGE[status.blocked_from];
      return { doneCount: idx, activeIndex: idx, failedIndex: -1 };
    }
    // A rejection doesn't undo the stages that genuinely already
    // completed before it -- only the stage whose output was rejected (or,
    // for the final result, the Finalizer that consequently never ran)
    // renders as failed.
    if (s === "PLAN_REJECTED") return { doneCount: 1, activeIndex: null, failedIndex: 1 };
    if (s === "FINAL_REJECTED") return { doneCount: 5, activeIndex: null, failedIndex: 5 };
    if (s === "FAILED") {
      const from = (status.history && status.history.length) ? status.history[status.history.length - 1].from : null;
      const idx = IN_PROGRESS_TO_STAGE[from] ?? (from === "TESTS_FAILED" ? 2 : from === "REVIEWING" ? 4 : null);
      return { doneCount: idx ?? 0, activeIndex: null, failedIndex: idx ?? -1 };
    }
    return { doneCount: 0, activeIndex: null, failedIndex: -1 };
  }

  function renderGovernance(status) {
    const showing = status.state === "AWAITING_PLAN_APPROVAL" || status.state === "AWAITING_FINAL_APPROVAL";
    els.governancePanel.hidden = !showing;
    if (!showing) return;
    const lastReason = (status.history && status.history.length) ? status.history[status.history.length - 1].reason : "";
    els.governanceTitle.textContent = status.state === "AWAITING_PLAN_APPROVAL"
      ? "The plan needs your approval"
      : "The final result needs your approval";
    els.governanceBody.textContent = lastReason || "Governance requires a human decision before continuing.";
    els.rejectReasonWrap.hidden = false;
  }

  function renderManualSubmit(status) {
    const showing = status.state === "BLOCKED";
    els.manualSubmitPanel.hidden = !showing;
    if (!showing) return;
    const role = status.blocked_from ? IN_PROGRESS_TO_ROLE[status.blocked_from] : null;
    els.manualSubmitPanel.dataset.role = role || "";
    els.manualSubmitHint.textContent = role
      ? `Write the ${STAGE_LABELS[role]} stage's output below, then submit it to continue the pipeline -- exactly what \`forgemind submit-artifact\` does from the CLI.`
      : "Submit this stage's artifact to continue.";
  }

  function renderActivity(history) {
    if (!history.length) {
      els.activityFeed.innerHTML = '<li class="activity-empty">No activity yet.</li>';
      return;
    }
    els.activityFeed.innerHTML = "";
    for (const entry of [...history].reverse()) {
      const li = document.createElement("li");
      li.innerHTML = `<div class="activity-transition">${escapeHtml(entry.from)} → ${escapeHtml(entry.to)}</div>
        ${entry.reason ? `<div class="activity-reason">${escapeHtml(entry.reason)}</div>` : ""}
        <div class="activity-time">${escapeHtml(formatTime(entry.at))}</div>`;
      els.activityFeed.appendChild(li);
    }
  }

  function renderArtifacts(produced) {
    if (!produced.length) {
      els.artifactsList.innerHTML = '<p class="artifacts-empty">No artifacts produced yet.</p>';
      return;
    }
    els.artifactsList.innerHTML = "";
    for (const a of produced) {
      const details = document.createElement("details");
      details.className = "artifact-item";
      const statusVal = a.frontmatter ? a.frontmatter.status : null;
      details.innerHTML = `<summary>
          <span>${STAGE_LABELS[a.role] || a.role} — ${escapeHtml(a.filename)}</span>
          ${statusVal ? `<span class="artifact-status-pill" data-status="${escapeHtml(String(statusVal))}">${escapeHtml(String(statusVal))}</span>` : ""}
        </summary>
        <pre>${escapeHtml(a.body || a.error || "")}</pre>`;
      els.artifactsList.appendChild(details);
    }
  }

  // ---- Actions --------------------------------------------------------
  async function onApprove() {
    setBusy(els.approveBtn, true, "Approve");
    try {
      await approveTask(currentTaskId);
      await refresh();
      if (!pollTimer) pollTimer = setInterval(refresh, POLL_MS);
    } catch (err) {
      renderFatal(err.message);
    } finally {
      els.approveBtn.disabled = false;
    }
  }

  async function onReject() {
    const reason = els.rejectReason.value.trim();
    if (!reason) { els.rejectReason.focus(); return; }
    els.rejectBtn.disabled = true;
    try {
      await rejectTask(currentTaskId, reason);
      await refresh();
    } catch (err) {
      renderFatal(err.message);
    } finally {
      els.rejectBtn.disabled = false;
    }
  }

  async function onManualSubmit() {
    hideBanner(els.manualError);
    const role = els.manualSubmitPanel.dataset.role;
    const status = els.manualStatus.value.trim() || "ok";
    const body = els.manualBody.value.trim();
    if (!role) { els.manualError.textContent = "Unknown stage."; els.manualError.hidden = false; return; }
    if (!body) { els.manualBody.focus(); return; }

    els.manualSubmitBtn.disabled = true;
    try {
      await submitArtifact(currentTaskId, { stage: role, status, body });
      els.manualBody.value = "";
      els.manualStatus.value = "ok";
      await refresh();
      if (!pollTimer) pollTimer = setInterval(refresh, POLL_MS);
    } catch (err) {
      els.manualError.textContent = err.message;
      els.manualError.hidden = false;
    } finally {
      els.manualSubmitBtn.disabled = false;
    }
  }

  // ---- Utilities ------------------------------------------------------
  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function formatTime(iso) {
    try { return new Date(iso).toLocaleString(); } catch (_) { return iso; }
  }
  function truncate(str, max) {
    return str.length > max ? str.slice(0, max - 1) + "…" : str;
  }

  boot();
})();
