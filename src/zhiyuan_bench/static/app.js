const I18N = {
  zh: {
    brand: "知远评测", connecting: "连接中", connected: "服务已连接", disconnected: "连接已断开",
    newRun: "新建评测", records: "测试记录", recordsAt: "记录目录", newEvaluation: "新建对比评测",
    branches: "测试分支", immutableSha: "启动后固定为 commit SHA", baseline: "基线分支", candidate: "候选分支",
    requireReviewer: "要求 reviewer subagent", loading: "加载中", suites: "测试集", selectAll: "全选",
    suite: "测试集", bridge: "Bridge", samples: "样本", runSettings: "运行设置", sampleLimit: "样本上限",
    concurrency: "并发数", start: "开始评测", recordsSubtitle: "按创建时间倒序", created: "创建时间",
    comparison: "分支对比", status: "状态", noRecords: "暂无测试记录", backRecords: "返回记录",
    staticReport: "静态报告", resume: "恢复评测", completed: "已完成", currentSuite: "当前测试集",
    progress: "进度", attempts: "尝试", liveEvents: "实时事件", full: "完整", dynamic: "动态",
    chooseSuite: "至少选择一个测试集", differentBranches: "请选择两个不同分支", creating: "正在创建记录",
    savedNotStarted: "记录已保存，但当前已有评测在运行", started: "评测已启动", resumed: "恢复请求已提交",
    loadFailed: "加载失败", requestFailed: "请求失败", noEvents: "等待事件", running: "运行中", succeeded: "成功",
    failed: "失败", skipped: "已跳过", createdStatus: "待运行", cancelled: "已取消", completedWithIssues: "完成但有异常",
    refresh: "刷新", theme: "切换主题", open: "打开记录", selected: "已选择"
  },
  en: {
    brand: "Zhiyuan Bench", connecting: "Connecting", connected: "Service connected", disconnected: "Disconnected",
    newRun: "New run", records: "Records", recordsAt: "Records root", newEvaluation: "New comparison",
    branches: "Branches", immutableSha: "Pinned to commit SHAs at creation", baseline: "Baseline", candidate: "Candidate",
    requireReviewer: "Require reviewer subagent", loading: "Loading", suites: "Suites", selectAll: "Select all",
    suite: "Suite", bridge: "Bridge", samples: "Samples", runSettings: "Run settings", sampleLimit: "Sample limit",
    concurrency: "Concurrency", start: "Start evaluation", recordsSubtitle: "Newest first", created: "Created",
    comparison: "Branch comparison", status: "Status", noRecords: "No records", backRecords: "Back to records",
    staticReport: "Static report", resume: "Resume", completed: "Completed", currentSuite: "Current suite",
    progress: "Progress", attempts: "Attempts", liveEvents: "Live events", full: "Full", dynamic: "Dynamic",
    chooseSuite: "Select at least one suite", differentBranches: "Select two different branches", creating: "Creating record",
    savedNotStarted: "Record saved, but another evaluation is active", started: "Evaluation started", resumed: "Resume requested",
    loadFailed: "Load failed", requestFailed: "Request failed", noEvents: "Waiting for events", running: "Running", succeeded: "Succeeded",
    failed: "Failed", skipped: "Skipped", createdStatus: "Pending", cancelled: "Cancelled", completedWithIssues: "Completed with issues",
    refresh: "Refresh", theme: "Toggle theme", open: "Open record", selected: "selected"
  }
};

const state = {
  locale: localStorage.getItem("zhiyuan-locale") || "zh",
  config: null,
  suites: [],
  branches: [],
  campaigns: [],
  currentCampaign: null,
  currentView: "create",
  eventSource: null,
  refreshTimer: null,
  toastTimer: null
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const t = (key) => I18N[state.locale][key] || key;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  svg.classList.add("icon");
  use.setAttribute("href", `#icon-${name}`);
  svg.append(use);
  return svg;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || `${t("requestFailed")} (${response.status})`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

function setConnection(connected) {
  const target = $("#connection-state");
  target.textContent = t(connected ? "connected" : "disconnected");
  target.dataset.connected = connected ? "true" : "false";
}

function showToast(message) {
  const toast = $("#toast");
  window.clearTimeout(state.toastTimer);
  toast.textContent = message;
  toast.hidden = false;
  requestAnimationFrame(() => toast.classList.add("visible"));
  state.toastTimer = window.setTimeout(() => {
    toast.classList.remove("visible");
    window.setTimeout(() => { toast.hidden = true; }, 180);
  }, 4200);
}

function applyTranslations() {
  document.documentElement.lang = state.locale === "zh" ? "zh-CN" : "en";
  document.title = t("brand");
  $$(`[data-i18n]`).forEach((node) => { node.textContent = t(node.dataset.i18n); });
  $("#language-toggle").textContent = state.locale === "zh" ? "EN" : "中";
  $("#theme-toggle").dataset.zyTooltip = t("theme");
  $("#theme-toggle").ariaLabel = t("theme");
  $("#refresh-history").dataset.zyTooltip = t("refresh");
  $("#refresh-history").ariaLabel = t("refresh");
  $("#sample-limit").placeholder = t("full");
  renderSuites();
  renderHistory();
  if (state.currentCampaign) renderDetail(state.currentCampaign);
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("zhiyuan-theme", theme);
}

function switchView(view) {
  if (view === state.currentView) return;
  const current = $(`[data-view-panel="${state.currentView}"]`);
  const next = $(`[data-view-panel="${view}"]`);
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const finish = () => {
    current.hidden = true;
    current.classList.remove("active", "leaving");
    next.hidden = false;
    requestAnimationFrame(() => next.classList.add("active"));
  };
  current.classList.add("leaving");
  window.setTimeout(finish, reduced ? 0 : 150);
  state.currentView = view;
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view || (view === "detail" && item.dataset.view === "history")));
  if (view === "history") loadHistory();
}

function branchOptions(select, preferredNames, preferredIndex) {
  const previous = select.value;
  select.replaceChildren();
  state.branches.forEach((branch) => {
    const option = element("option", "", branch.name);
    option.value = branch.name;
    option.dataset.revision = branch.revision;
    select.append(option);
  });
  if (previous && state.branches.some((branch) => branch.name === previous)) select.value = previous;
  else {
    const preferred = preferredNames.find((name) => state.branches.some((branch) => branch.name === name));
    if (preferred) select.value = preferred;
    else if (state.branches[preferredIndex]) select.value = state.branches[preferredIndex].name;
  }
  select.removeAttribute("aria-busy");
}

function renderBranches() {
  branchOptions($("#baseline-branch"), ["eval_candidate_1", "main"], 0);
  branchOptions($("#candidate-branch"), ["eval_candidate_2", "dev"], state.branches.length > 1 ? 1 : 0);
}

function renderSuites() {
  if (!state.suites.length) return;
  const body = $("#suite-list");
  const selected = new Set($$(".suite-checkbox:checked").map((box) => box.value));
  const preserve = $$(".suite-checkbox").length > 0;
  body.replaceChildren();
  state.suites.forEach((suite) => {
    const row = element("tr");
    const checkCell = element("td", "check-column");
    const check = element("input", "suite-checkbox");
    check.type = "checkbox";
    check.value = suite.id;
    check.checked = preserve ? selected.has(suite.id) : true;
    check.setAttribute("aria-label", suite.id);
    check.addEventListener("change", updateSuiteSelection);
    checkCell.append(check);
    const suiteCell = element("td");
    suiteCell.append(element("span", "suite-title", suite.id), element("span", "suite-description", suite.description));
    row.append(checkCell, suiteCell, element("td", "", suite.bridge), element("td", "", suite.expected_samples ?? t("dynamic")));
    body.append(row);
  });
  updateSuiteSelection();
}

function updateSuiteSelection() {
  const boxes = $$(".suite-checkbox");
  const count = boxes.filter((box) => box.checked).length;
  $("#suite-selection").textContent = `${count} / ${boxes.length}`;
  const all = $("#select-all-suites");
  all.checked = count === boxes.length;
  all.indeterminate = count > 0 && count < boxes.length;
}

function statusKey(status) {
  return ({ created: "createdStatus", running: "running", succeeded: "succeeded", failed: "failed", skipped: "skipped", cancelled: "cancelled", completed_with_issues: "completedWithIssues" })[status] || status;
}

function statusNode(status) {
  return element("span", `status-text status-${status}`, t(statusKey(status)));
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(state.locale === "zh" ? "zh-CN" : "en", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function campaignBranches(campaign) {
  return campaign.candidates.map((item) => `${item.source_ref} · ${item.revision.slice(0, 8)}`).join("  →  ");
}

function renderHistory() {
  const body = $("#history-list");
  const empty = $("#history-empty");
  if (!body || !state.campaigns.length) {
    if (body) body.replaceChildren();
    if (empty) empty.hidden = false;
    $("#history-count").textContent = state.campaigns.length;
    return;
  }
  empty.hidden = true;
  body.replaceChildren();
  state.campaigns.forEach((campaign) => {
    const row = element("tr");
    row.tabIndex = 0;
    row.addEventListener("click", () => openCampaign(campaign.campaign_id));
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") openCampaign(campaign.campaign_id); });
    const created = element("td");
    created.append(element("span", "", formatDate(campaign.created_at)), element("span", "history-id", campaign.campaign_id));
    const comparison = element("td", "branch-comparison");
    campaign.candidates.forEach((item) => comparison.append(element("span", "", item.source_ref), element("code", "", item.revision.slice(0, 8))));
    const counts = campaign.counts || {};
    const suiteText = `${counts.succeeded || 0}/${campaign.suites.length}`;
    const action = element("td");
    const button = element("button", "row-action secondary");
    button.type = "button";
    button.dataset.zyTooltip = t("open");
    button.setAttribute("aria-label", t("open"));
    button.append(icon("chevron"));
    action.append(button);
    const statusCell = element("td");
    statusCell.append(statusNode(campaign.status));
    row.append(created, comparison, element("td", "", suiteText), statusCell, action);
    body.append(row);
  });
  $("#history-count").textContent = state.campaigns.length;
}

async function loadHistory() {
  try {
    state.campaigns = await api("/api/campaigns");
    renderHistory();
  } catch (error) {
    showToast(`${t("loadFailed")}: ${error.message}`);
  }
}

function progressCell(suite) {
  const cell = element("td");
  const wrap = element("div", "progress-cell");
  const progress = suite.progress;
  if (progress && Number.isInteger(progress.total) && progress.total > 0) {
    const bar = element("progress");
    bar.max = progress.total;
    bar.value = progress.completed || 0;
    wrap.append(bar, element("span", "", `${progress.completed || 0}/${progress.total}`));
  } else {
    wrap.append(element("span", "", "-"));
  }
  cell.append(wrap);
  return cell;
}

function renderDetail(campaign) {
  state.currentCampaign = campaign;
  $("#detail-title").textContent = campaign.campaign_id;
  $("#detail-branches").textContent = campaignBranches(campaign);
  $("#detail-status").replaceChildren(statusNode(campaign.status));
  const succeeded = campaign.counts?.succeeded || 0;
  $("#detail-completed").textContent = `${succeeded} / ${campaign.suites.length}`;
  $("#detail-current").textContent = campaign.current_suite || "-";
  $("#static-report").href = `/api/campaigns/${encodeURIComponent(campaign.campaign_id)}/report`;
  $("#resume-run").disabled = campaign.status === "running" || campaign.status === "succeeded";
  const body = $("#progress-list");
  body.replaceChildren();
  campaign.suites.forEach((suite) => {
    const row = element("tr");
    const suiteCell = element("td");
    suiteCell.append(element("span", "suite-title", suite.id), element("span", "suite-description", suite.bridge));
    const statusCell = element("td");
    statusCell.append(statusNode(suite.status));
    row.append(suiteCell, statusCell, progressCell(suite), element("td", "", suite.attempts ?? "-"));
    body.append(row);
  });
}

async function loadDetail(id, quiet = false) {
  try {
    const campaign = await api(`/api/campaigns/${encodeURIComponent(id)}`);
    renderDetail(campaign);
    return campaign;
  } catch (error) {
    if (!quiet) showToast(`${t("loadFailed")}: ${error.message}`);
    return null;
  }
}

function appendEvent(event) {
  const list = $("#event-list");
  const item = element("li");
  item.append(element("span", "event-time", formatDate(event.timestamp)), element("span", "", event.event_type.replaceAll("_", " ")), element("span", "event-status", event.status || ""));
  list.prepend(item);
  while (list.children.length > 80) list.lastElementChild.remove();
}

function openEventStream(id) {
  if (state.eventSource) state.eventSource.close();
  $("#event-list").replaceChildren(element("li", "event-empty", t("noEvents")));
  $("#event-state").textContent = "SSE";
  const source = new EventSource(`/api/campaigns/${encodeURIComponent(id)}/events`);
  state.eventSource = source;
  const types = ["campaign_created", "campaign_started", "suite_started", "suite_phase_started", "suite_phase_progress", "suite_run_finished", "suite_finished", "campaign_finished"];
  const receive = (message) => {
    const empty = $(".event-empty");
    if (empty) empty.remove();
    try { appendEvent(JSON.parse(message.data)); } catch { return; }
    window.clearTimeout(state.refreshTimer);
    state.refreshTimer = window.setTimeout(() => loadDetail(id, true), 120);
  };
  types.forEach((type) => source.addEventListener(type, receive));
  source.onopen = () => { $("#event-state").textContent = t("connected"); };
  source.onerror = () => { $("#event-state").textContent = t("connecting"); };
}

async function openCampaign(id) {
  const campaign = await loadDetail(id);
  if (!campaign) return;
  switchView("detail");
  openEventStream(id);
}

async function submitCampaign(event) {
  event.preventDefault();
  const selectedSuites = $$(".suite-checkbox:checked").map((box) => box.value);
  const baseline = $("#baseline-branch").value;
  const candidate = $("#candidate-branch").value;
  const validation = $("#form-validation");
  validation.textContent = "";
  if (!selectedSuites.length) { validation.textContent = t("chooseSuite"); return; }
  if (baseline === candidate) { validation.textContent = t("differentBranches"); return; }
  const reviewers = [];
  if ($("#baseline-reviewer").checked) reviewers.push("baseline");
  if ($("#candidate-reviewer").checked) reviewers.push("candidate");
  const limitValue = $("#sample-limit").value;
  const button = $("#start-run");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  validation.textContent = t("creating");
  try {
    const campaign = await api("/api/campaigns", {
      method: "POST",
      body: JSON.stringify({
        branches: [{ label: "baseline", ref: baseline }, { label: "candidate", ref: candidate }],
        suites: selectedSuites,
        reviewer_required_candidates: reviewers,
        limit: limitValue ? Number(limitValue) : null,
        concurrency: Number($("#concurrency").value),
        run: true
      })
    });
    validation.textContent = "";
    showToast(t("started"));
    await loadHistory();
    await openCampaign(campaign.campaign_id);
  } catch (error) {
    if (error.status === 409 && error.body?.campaign) {
      validation.textContent = "";
      showToast(t("savedNotStarted"));
      await loadHistory();
      await openCampaign(error.body.campaign.campaign_id);
    } else {
      validation.textContent = error.message;
      showToast(`${t("requestFailed")}: ${error.message}`);
    }
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

async function resumeCurrent() {
  if (!state.currentCampaign) return;
  const button = $("#resume-run");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    await api(`/api/campaigns/${encodeURIComponent(state.currentCampaign.campaign_id)}/run`, { method: "POST", body: "{}" });
    showToast(t("resumed"));
    openEventStream(state.currentCampaign.campaign_id);
    await loadDetail(state.currentCampaign.campaign_id, true);
  } catch (error) {
    showToast(`${t("requestFailed")}: ${error.message}`);
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

async function bootstrap() {
  try {
    const [config, suites, branches, campaigns] = await Promise.all([
      api("/api/config"), api("/api/suites"), api("/api/branches"), api("/api/campaigns")
    ]);
    state.config = config;
    state.suites = suites;
    state.branches = branches;
    state.campaigns = campaigns;
    $("#repo-path").textContent = config.repo;
    $("#records-root").textContent = config.records_root;
    renderBranches();
    renderSuites();
    renderHistory();
    setConnection(true);
  } catch (error) {
    setConnection(false);
    showToast(`${t("loadFailed")}: ${error.message}`);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const savedTheme = localStorage.getItem("zhiyuan-theme");
  setTheme(savedTheme || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  applyTranslations();
  $("#theme-toggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  $("#language-toggle").addEventListener("click", () => {
    state.locale = state.locale === "zh" ? "en" : "zh";
    localStorage.setItem("zhiyuan-locale", state.locale);
    applyTranslations();
  });
  $$(`[data-view]`).forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $("#select-all-suites").addEventListener("change", (event) => {
    $$(".suite-checkbox").forEach((box) => { box.checked = event.target.checked; });
    updateSuiteSelection();
  });
  $("#campaign-form").addEventListener("submit", submitCampaign);
  $("#refresh-history").addEventListener("click", loadHistory);
  $("#resume-run").addEventListener("click", resumeCurrent);
  bootstrap();
  window.setInterval(() => {
    if (state.currentView === "detail" && state.currentCampaign?.status === "running") loadDetail(state.currentCampaign.campaign_id, true);
  }, 3000);
});
