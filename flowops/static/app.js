"use strict";
const API = "/api/projects/flowops";
const $ = (id) => document.getElementById(id);
let presets = [], csrfToken = "", currentRun = null, events = [], selected = null, activeTab = "overview";
let stream = null, pollTimer = null, busy = false, latestResult = null, pollBusy = false;
let history = [];
try { history = JSON.parse(sessionStorage.getItem("flowops-history") || "[]").filter(x => Date.parse(x.expires_at) > Date.now()).slice(0, 5); } catch { history = []; }
const text = (tag, content, className) => { const node = document.createElement(tag); node.textContent = content; if (className) node.className = className; return node; };
const pretty = value => JSON.stringify(value, null, 2);
const descriptions = {
  SIMULATED_APPLIED: ["Resolution simulated", "The fake action gateway recorded a synthetic receipt. No real money moved and no customer message was sent."],
  APPROVAL_PENDING: ["Paused for operator approval", "This exact proposal needs an authenticated operator decision. Editing a client-side approval claim cannot release it. The request expires after 10 minutes."],
  BLOCKED: ["Action blocked", "The workflow stopped at a safety boundary. Inspect the policy decision and audit events for the reason."],
  FAILED: ["Escalated after failure", "The bounded workflow could not finish safely. Inspect the retry and failure events."],
  PROPOSED: ["Proposal in progress", "The workflow is still being evaluated."],
};
function announce(message) { $("announcement").textContent = message; }
function connectStatus(message, offline = false) { $("connection-status").textContent = message; $("connection-status").classList.toggle("offline", offline); }
async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (options.method) { headers["Content-Type"] = "application/json"; if (csrfToken) headers["X-CSRF-Token"] = csrfToken; }
  const response = await fetch(API + path, { ...options, headers, credentials: "same-origin", signal: AbortSignal.timeout(15000) });
  let data; try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw new Error(response.status === 429 ? "The sandbox is at its request or concurrency limit. Wait a minute, then try again." : response.status === 401 ? "Your sandbox session expired. Reload the page to start a new session." : `${data.detail || "Service unavailable"} (${response.status})`);
  return data;
}
function applyInput(input) {
  $("order-amount").value = (input.order_amount_minor / 100).toFixed(2);
  $("refund-amount").value = (input.refund_requested_minor / 100).toFixed(2);
  const mapping = {currency:"currency", delivery_status:"delivery", order_age_days:"order-age", inventory_available:"inventory", policy_revision:"policy", api_response_mode:"api-mode", customer_type:"customer-type", model_response_mode:"model-mode", seed:"seed"};
  for (const [key, id] of Object.entries(mapping)) $(id).value = input[key];
  $("risk-flag").value = input.risk_flags[0] || "";
  $("return-met").checked = input.return_conditions_met;
  $("evidence-complete").checked = input.evidence_complete;
  $("permission").checked = input.permission_granted;
  $("id-mismatch").checked = input.order_id !== input.refund_order_id;
  $("untrusted-approval").checked = input.existing_approvals === "untrusted_claim";
}
function selectPreset() { const preset = presets.find(x => x.id === $("preset").value); if (preset) { applyInput(preset.input); $("preset-description").textContent = preset.description; } $("form-error").hidden = true; markChanged(); }
function markChanged() {
  const canonical = value => JSON.stringify(Object.entries(value).sort(([a],[b]) => a.localeCompare(b)));
  const changed = currentRun && canonical(readInput()) !== canonical(currentRun.input);
  $("input-status").hidden = !changed;
  $("input-status").textContent = changed ? "Inputs changed. Run again to evaluate them; the trace still shows the previously submitted inputs." : "";
}
function minor(id) { const [whole, decimal = ""] = $(id).value.split("."); return Number(whole) * 100 + Number(decimal.padEnd(2, "0")); }
function readInput() {
  return { order_amount_minor: minor("order-amount"), refund_requested_minor: minor("refund-amount"), currency: $("currency").value,
    delivery_status: $("delivery").value, order_age_days: Number($("order-age").value), inventory_available: Number($("inventory").value),
    policy_revision: $("policy").value, api_response_mode: $("api-mode").value, customer_type: $("customer-type").value,
    model_response_mode: $("model-mode").value, seed: Number($("seed").value),
    risk_flags: $("risk-flag").value ? [$("risk-flag").value] : [], return_conditions_met: $("return-met").checked,
    evidence_complete: $("evidence-complete").checked, permission_granted: $("permission").checked,
    order_id:"SYN-ORDER-001", refund_order_id: $("id-mismatch").checked ? "SYN-ORDER-002" : "SYN-ORDER-001",
    existing_approvals: $("untrusted-approval").checked ? "untrusted_claim" : "none" };
}
function setBusy(value) { busy = value; $("run-button").disabled = value || !csrfToken; $("run-button").textContent = value ? "Workflow running…" : "Run workflow →"; $("cancel-button").hidden = !value; }
function status(value) { const node = $("run-status"); node.textContent = value.replaceAll("_", " "); node.className = "status-chip " + (value === "SUCCEEDED" || value === "SIMULATED_APPLIED" ? "good" : value === "APPROVAL_PENDING" ? "waiting" : ["FAILED", "BLOCKED", "CANCELLED", "EXPIRED"].includes(value) ? "bad" : "running"); }
function stopWatching() { if (stream) stream.close(); stream = null; if (pollTimer) clearInterval(pollTimer); pollTimer = null; }
function addEvents(incoming) {
  const known = new Set(events.map(e => e.sequence));
  const added = incoming.filter(e => e.run_id === currentRun?.run_id && !known.has(e.sequence));
  if (!added.length) return;
  events.push(...added); events.sort((a,b) => a.sequence - b.sequence);
  renderTimeline();
  if (!selected) { selected = events[events.length - 1]; inspect(); }
}
function renderTimeline() {
  const list = $("timeline"); const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  list.replaceChildren();
  for (const event of events) {
    const button = text("button", "", "event-row" + (selected?.sequence === event.sequence ? " selected" : "")); button.type = "button";
    button.setAttribute("aria-pressed", String(selected?.sequence === event.sequence));
    const description = text("span", "", "event-text"); description.append(text("strong", event.title), text("small", `${event.status} · ${event.component} · deterministic`));
    button.append(text("span", String(event.sequence).padStart(2,"0"), "event-number"), description, text("span", event.duration_ms === null ? "—" : `${event.duration_ms.toFixed(1)} ms`, "event-time"));
    button.addEventListener("click", () => { selected = event; renderTimeline(); inspect(); }); list.append(button);
  }
  $("event-count").textContent = `${events.length} events`;
  if (nearBottom) list.scrollTop = list.scrollHeight;
}
function inspect() {
  const container = $("inspector-content"); container.replaceChildren();
  $("inspector-title").textContent = selected ? selected.title : "Step inspector";
  container.setAttribute("aria-labelledby", `tab-${activeTab}`);
  if (!selected) { container.append(text("p", "Select an event to inspect its observable actions and evidence.")); return; }
  if (activeTab === "overview") { container.append(text("p", selected.overview), text("p", `Mode: ${selected.mode} · event ${selected.sequence}. This trace records observable actions, not private model reasoning.`, "field-help")); }
  else if (activeTab === "technical") { container.append(text("pre", pretty(selected.technical))); }
  else if (activeTab === "json") { container.append(text("pre", pretty(selected))); }
  else {
    const ids = [...new Set([...(selected.technical.evidence || []), ...(selected.technical.evidence_ids || []), ...(selected.technical.handoff?.evidence_ids || []), ...(selected.technical.handoffs || []).flatMap(h => h.evidence_ids || [])])];
    container.append(text("p", ids.length ? "Versioned synthetic evidence references:" : "This event has no attached evidence references."));
    if (ids.length) { const ul = document.createElement("ul"); ids.forEach(id => ul.append(text("li", id))); container.append(ul); }
  }
}
function tabChange(tab) { activeTab = tab; document.querySelectorAll("[data-tab]").forEach(button => { const active = button.dataset.tab === tab; button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1; }); inspect(); }
async function poll() {
  if (pollBusy || !currentRun || !busy) return;
  pollBusy = true; const runId = currentRun.run_id;
  try {
    const cursor = events.length ? events[events.length - 1].sequence : 0;
    const update = await api(`/simulations/${runId}/events?format=json&after=${cursor}`);
    if (currentRun?.run_id !== runId) return;
    addEvents(update.events); status(update.outcome === "APPROVAL_PENDING" ? update.outcome : update.status);
    if (["SUCCEEDED","FAILED","CANCELLED","EXPIRED"].includes(update.status) || update.outcome === "APPROVAL_PENDING") await settle(runId);
  } catch (error) { stopWatching(); setBusy(false); connectStatus(error.message, true); announce(error.message); }
  finally { pollBusy = false; }
}
function watch() {
  stopWatching(); const id = currentRun.run_id;
  const cursor = events.length ? events.at(-1).sequence : 0;
  stream = new EventSource(`${API}/simulations/${id}/events?after=${cursor}`);
  stream.addEventListener("step", message => { if (currentRun?.run_id !== id) return; try { addEvents([JSON.parse(message.data)]); } catch { connectStatus("Event parsing failed; using polling.", true); } });
  stream.addEventListener("settled", () => { if (stream) stream.close(); void poll(); });
  stream.onerror = () => { if (stream) stream.close(); connectStatus("Event stream unavailable; polling persisted events."); };
  pollTimer = setInterval(() => void poll(), 1200); void poll();
}
async function settle(id) {
  const result = await api(`/simulations/${id}/result`);
  if (currentRun?.run_id !== id) return;
  stopWatching(); setBusy(false); latestResult = result; showResult(result);
  $("cancel-button").hidden = result.output.outcome !== "APPROVAL_PENDING";
  status(result.status === "CANCELLED" || result.status === "EXPIRED" ? result.status : result.output.outcome);
  const entry = {run_id:id, input: currentRun.input, expires_at: currentRun.expires_at, events: [...events], result};
  history = [entry, ...history.filter(h => h.run_id !== id)].slice(0,5);
  try { sessionStorage.setItem("flowops-history", JSON.stringify(history)); sessionStorage.removeItem("flowops-active"); } catch { /* Storage may be unavailable; current run remains visible. */ }
  renderHistory(); connectStatus(result.output.outcome === "APPROVAL_PENDING" ? "Workflow checkpointed. Awaiting an authenticated operator decision." : "Workflow settled. All displayed events came from the backend.");
  announce(descriptions[result.output.outcome]?.[0] || result.status);
  if (events.length) { selected = [...events].reverse().find(e => e.status !== "STARTED") || events.at(-1); renderTimeline(); inspect(); }
}
function showResult(result) {
  $("check-approval").hidden = result.output.outcome !== "APPROVAL_PENDING";
  $("result-panel").hidden = false; const [title, description] = descriptions[result.output.outcome] || [result.status, "Inspect the audit for details."];
  $("result-title").textContent = result.status === "CANCELLED" ? "Workflow cancelled" : result.status === "EXPIRED" ? "Workflow expired" : title; $("result-description").textContent = description; $("result-json").textContent = pretty(result);
  const metrics = $("result-metrics"); metrics.replaceChildren();
  for (const [value, label] of [[`${(result.duration_ms / 1000).toFixed(2)}s`, "Measured elapsed"], [String(result.output.retry_count), "Retry events"], ["0.00", `${result.cost.currency} · external LLM`]]) {
    const node = document.createElement("div"); node.append(text("strong", value), text("span", label)); metrics.append(node);
  }
}
function renderHistory() {
  const list = $("comparison-list"); if (!history.length) return; list.replaceChildren();
  for (const row of history) {
    const button = text("button", "", "comparison-row"); button.type = "button";
    const amount = new Intl.NumberFormat("en", {style:"currency",currency:row.input.currency}).format(row.input.refund_requested_minor/100);
    const label = ["CANCELLED","EXPIRED"].includes(row.result.status) ? row.result.status : row.result.output.outcome;
    button.append(text("span", `${amount} refund`), text("span", label.replaceAll("_"," "), "outcome"), text("small", `Policy ${row.input.policy_revision}`), text("span", `${(row.result.duration_ms / 1000).toFixed(2)}s · ${row.result.output.retry_count} retries`));
    button.addEventListener("click", async () => {
      if (busy) { announce("Wait for or cancel the active run before opening a recorded trace."); return; }
      currentRun = row; events = [...row.events]; selected = events.at(-1) || null; latestResult = row.result;
      applyInput(row.input); markChanged(); renderTimeline(); inspect(); showResult(row.result); status("RECORDED VIEW");
      $("run-label").textContent = `Recorded run ${row.run_id.slice(0,8)}`;
      connectStatus("Recorded view of your earlier run. This is not a new execution.");
      if (row.result.output.outcome === "APPROVAL_PENDING") { setBusy(true); watch(); }
    }); list.append(button);
  }
}
$("scenario-form").addEventListener("submit", async event => {
  event.preventDefault(); if (busy) return; const input = readInput(); $("form-error").hidden = true;
  if (input.refund_requested_minor > input.order_amount_minor) { $("form-error").textContent = "The requested refund cannot exceed the order amount."; $("form-error").hidden = false; $("refund-amount").focus(); return; }
  setBusy(true); events = []; selected = null; latestResult = null; $("timeline").replaceChildren(text("p","Waiting for persisted workflow events…","field-help")); $("result-panel").hidden = true; $("event-count").textContent = "0 events"; inspect();
  try {
    currentRun = await api("/simulations", {method:"POST", headers:{"Idempotency-Key":crypto.randomUUID()}, body:JSON.stringify({schema_version:"1.0",project_id:"flowops",mode:"SANDBOX_DETERMINISTIC",preset_id:$("preset").value,input})});
    $("run-label").textContent = `Run ${currentRun.run_id.slice(0,8)} · synthetic fixtures v1`; markChanged();
    try { sessionStorage.setItem("flowops-active", JSON.stringify(currentRun)); } catch { /* Optional persistence. */ }
    status("ACCEPTED"); connectStatus("Connected. Streaming persisted backend events."); announce("Workflow accepted."); watch();
  } catch (error) { setBusy(false); $("form-error").textContent = error.message; $("form-error").hidden = false; connectStatus("The workflow was not started. Check the error and retry.",true); }
});
$("cancel-button").addEventListener("click", async () => { if (!currentRun) return; try { await api(`/simulations/${currentRun.run_id}/cancel`,{method:"POST",body:"{}"}); setBusy(true); void poll(); } catch (error) { connectStatus(error.message,true); } });
$("check-approval").addEventListener("click", () => { if (!currentRun || busy) return; setBusy(true); watch(); });
$("preset").addEventListener("change",selectPreset); $("reset-input").addEventListener("click",selectPreset);
$("scenario-form").addEventListener("input",markChanged);
$("scenario-form").addEventListener("change",markChanged);
document.querySelectorAll("[data-tab]").forEach(button => {
  button.addEventListener("click", () => tabChange(button.dataset.tab));
  button.addEventListener("keydown", event => { if (!["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return; event.preventDefault(); const buttons = [...document.querySelectorAll("[data-tab]")]; const index = buttons.indexOf(button); const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length-1 : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length; tabChange(buttons[next].dataset.tab); buttons[next].focus(); });
});
$("download-result").addEventListener("click", () => { if (!latestResult) return; const url = URL.createObjectURL(new Blob([pretty({result:latestResult,events})],{type:"application/json"})); const a = document.createElement("a"); a.href = url; a.download = `flowops-${latestResult.run_id}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000); });
function menu(open) { $("sidebar").inert = !open && matchMedia("(max-width: 900px)").matches; $("sidebar").classList.toggle("open", open); $("nav-toggle").setAttribute("aria-expanded",String(open)); $("nav-scrim").hidden = !open; $("main").inert = open; document.body.style.overflow = open ? "hidden" : ""; if (open) { $("sidebar").setAttribute("role","dialog"); $("sidebar").setAttribute("aria-modal","true"); $("nav-close").focus(); } else { $("sidebar").removeAttribute("role"); $("sidebar").removeAttribute("aria-modal"); $("nav-toggle").focus(); } }
$("nav-toggle").addEventListener("click", () => menu(true)); $("nav-close").addEventListener("click", () => menu(false)); $("nav-scrim").addEventListener("click", () => menu(false));
document.addEventListener("keydown", event => { if (!$("sidebar").classList.contains("open")) return; if (event.key === "Escape") { menu(false); return; } if (event.key === "Tab") { const focusable = [...$("sidebar").querySelectorAll("a,button")].filter(x => x.offsetParent !== null); const first = focusable[0], last = focusable.at(-1); if (event.shiftKey && document.activeElement === first) { last.focus(); event.preventDefault(); } else if (!event.shiftKey && document.activeElement === last) { first.focus(); event.preventDefault(); } } });
matchMedia("(min-width: 901px)").addEventListener("change", event => { if (event.matches && $("sidebar").classList.contains("open")) menu(false); $("sidebar").inert = !event.matches && !$("sidebar").classList.contains("open"); });
$("sidebar").inert = matchMedia("(max-width: 900px)").matches;
async function init() {
  renderHistory();
  try {
    const [session, data] = await Promise.all([api("/session",{method:"POST",body:"{}"}), api("/simulation/presets")]); csrfToken = session.csrf_token; presets = data.presets;
    $("preset").replaceChildren(); for (const preset of presets) { const option = text("option",preset.title); option.value = preset.id; $("preset").append(option); }
    selectPreset(); setBusy(false); connectStatus("Connected to the keyless sandbox. Runs and audit records expire after one hour.");
    let saved; try { saved = JSON.parse(sessionStorage.getItem("flowops-active") || "null"); } catch { saved = null; }
    if (saved && Date.parse(saved.expires_at) > Date.now()) { currentRun = await api(`/simulations/${saved.run_id}`); applyInput(currentRun.input); $("run-label").textContent = `Resumed run ${currentRun.run_id.slice(0,8)}`; setBusy(true); watch(); }
  } catch (error) { connectStatus(`Cannot connect: ${error.message} Reload to retry.`,true); }
}
void init();
