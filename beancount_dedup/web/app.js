"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const $$ = (selector, parent = document) => Array.from(parent.querySelectorAll(selector));
  const PAGE_NAMES = { transactions: "交易复核", reconciliation: "账簿核对", exports: "导入导出", settings: "模型与规则" };
  const PLATFORM_NAMES = { alipay: "支付宝", wechat: "微信支付", bank: "银行卡", unionpay: "云闪付" };
  const SOURCE_NAMES = { rule: "规则分类", model: "1B 模型建议", manual: "手动分类", fallback: "尚未分类", transfer: "内部转账" };
  const RECON_NAMES = { not_checked: "尚未核对", matched: "已在账簿中", statement_only: "仅账单中", ambiguous: "需人工核对" };
  const PAGE_SIZE = 50;
  let snapshot = null;
  let currentPage = "transactions";
  let currentTablePage = 0;
  let selectedIds = new Set();
  let detailId = null;
  let detailDirty = false;
  let detailSignature = "";
  let tableSignature = "";
  let walletDirty = false;
  let rulesDirty = false;
  let rulesSignature = "";
  let ruleGroup = "merchant_overrides";
  let ruleDraft = { mode: "append", merchant_overrides: [], expense_rules: [], income_rules: [] };
  let reportTab = "matched";
  let reportSignature = "";
  let localBusy = "";
  let statePromise = null;
  let refreshTimer = null;
  let dismissedError = "";
  let connectionError = false;
  let pendingLedgerFiles = [];
  let exportResult = null;
  let exportSourceSignature = "";
  let downloadObjectURL = null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function setText(id, text) { $(id).textContent = text === undefined || text === null ? "—" : String(text); }
  function badge(text, color = "neutral") { return element("span", `badge ${color}`, text); }
  function isBusy() { return Boolean(localBusy || snapshot?.job?.running); }
  function transactions() { return Array.isArray(snapshot?.transactions) ? snapshot.transactions : []; }
  function ledgerLoaded() { return Boolean(snapshot?.ledger?.loaded || snapshot?.workspace?.has_ledger); }
  function hasStatements() { return Boolean(snapshot?.workspace?.has_statements || transactions().length); }
  function modelReady() { return Boolean(snapshot?.model?.ready); }
  function money(value, currency = "CNY", withCurrency = false) {
    const raw = String(value ?? "0");
    const number = Number(raw);
    let display = raw;
    if (Number.isFinite(number)) {
      display = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(number);
      if (number > 0) display = "+" + display;
    }
    return withCurrency ? `${display} ${currency}` : display;
  }
  function filename(file) { return file?.workbenchRelativePath || file?.webkitRelativePath || file?.name || ""; }
  function baseName(path) { return String(path || "").split(/[\\/]/).pop(); }
  function toast(message, error = false) {
    const node = element("div", `toast${error ? " error" : ""}`, message);
    $("toast-region").append(node);
    window.setTimeout(() => node.remove(), error ? 8000 : 4000);
  }
  function showPage(page, updateHash = true) {
    if (!PAGE_NAMES[page]) page = "transactions";
    currentPage = page;
    $$(".page").forEach((node) => { const active = node.id === `page-${page}`; node.hidden = !active; node.classList.toggle("active", active); });
    $$(".nav-item").forEach((node) => { const active = node.dataset.page === page; node.classList.toggle("active", active); if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
    setText("breadcrumb", PAGE_NAMES[page]);
    document.title = `${PAGE_NAMES[page]} · 账簿`;
    if (updateHash && window.location.hash !== `#${page}`) history.replaceState(null, "", `#${page}`);
  }

  async function requestJSON(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), options.timeout || 30000);
    try {
      const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options, signal: controller.signal });
      const text = await response.text();
      let data;
      try { data = text ? JSON.parse(text) : {}; } catch { throw new Error("工作台返回了无法读取的结果，请重试。"); }
      if (!response.ok) throw new Error(data.error || data.message || `操作未完成（${response.status}）`);
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("连接超时，请检查本地工作台是否仍在运行。");
      throw error;
    } finally { window.clearTimeout(timeout); }
  }
  function dataSignature() { return JSON.stringify([snapshot?.transactions, snapshot?.ledger, snapshot?.accounts, snapshot?.workspace?.currency]); }
  async function refreshState() {
    if (statePromise) return statePromise;
    statePromise = requestJSON("/api/state", { timeout: 10000 }).then((next) => {
      connectionError = false;
      applyState(next);
      return next;
    }).finally(() => { statePromise = null; });
    return statePromise;
  }
  async function waitForJob() {
    for (;;) {
      const next = await refreshState();
      if (!next.job?.running) {
        if (next.job?.error) throw new Error(next.job.error);
        return next;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
  }
  async function action(path, body = {}, label = "正在处理…", options = {}) {
    if (isBusy()) { toast("上一项操作还在进行，请稍候。"); return null; }
    if (!snapshot?.csrf_token) { toast("尚未连接到本地工作区，请稍后重试。", true); return null; }
    localBusy = label;
    renderBusy();
    try {
      const headers = { "X-Workbench-Token": snapshot.csrf_token, "Content-Type": options.raw ? "application/octet-stream" : "application/json" };
      const result = await requestJSON(path, { method: "POST", headers, body: options.raw ? body : JSON.stringify(body), timeout: options.raw ? 600000 : 30000 });
      if (result.error) throw new Error(result.error);
      if (options.afterSubmit) options.afterSubmit(result);
      if (result.workspace && result.csrf_token) applyState(result);
      if (result.job?.running) await waitForJob();
      else if (!options.noRefresh) {
        const next = await refreshState();
        if (next.job?.running) await waitForJob();
        else if (result.job && next.job?.error) throw new Error(next.job.error);
      }
      if (options.success) toast(options.success);
      return result;
    } catch (error) {
      toast(error.message || "操作未完成，请重试。", true);
      return null;
    } finally { localBusy = ""; renderBusy(); }
  }
  function scheduleRefresh() {
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(async () => {
      try { await refreshState(); }
      catch {
        connectionError = true;
        renderBusy();
      }
      scheduleRefresh();
    }, snapshot?.job?.running ? 500 : 2000);
  }
  function applyState(next) {
    snapshot = next;
    const availableIds = new Set(transactions().map((tx) => tx.id));
    selectedIds = new Set(Array.from(selectedIds).filter((id) => availableIds.has(id)));
    if (detailId && !availableIds.has(detailId)) { detailId = null; detailDirty = false; }
    setText("workspace-name", snapshot.workspace?.name || "本地工作区");
    $("demo-banner").hidden = !snapshot.workspace?.is_demo;
    ["transactions", "pending", "suggested", "matched"].forEach((name) => setText(`count-${name}`, snapshot.counts?.[name] ?? 0));
    const pending = snapshot.counts?.pending || 0;
    setText("nav-pending", pending);
    $("nav-pending").hidden = !pending;
    renderModel();
    renderAvailableAccounts();
    renderTransactions();
    renderDetail();
    renderLedger();
    renderReport();
    renderSources();
    loadRuleDraft();
    if (exportResult && exportSourceSignature !== dataSignature()) markPreviewStale();
    renderBusy();
  }
  function renderBusy() {
    const busy = isBusy();
    const job = snapshot?.job || {};
    $("job-banner").hidden = !busy;
    setText("job-label", job.running ? (job.label || "正在处理…") : localBusy);
    const error = connectionError ? "无法连接本地工作台。请确认工作台仍在运行；页面会自动重试连接。" : (job.error || "");
    $("error-banner").hidden = !error || error === dismissedError;
    setText("error-message", error);
    $$("[data-mutation]").forEach((button) => {
      let enabled = Boolean(snapshot) && !busy && !connectionError;
      const requires = button.dataset.requires;
      if (requires === "model-statements") enabled = enabled && hasStatements() && modelReady() && transactions().length > 0;
      if (requires === "ledger-statements") enabled = enabled && hasStatements() && ledgerLoaded();
      if (requires === "exportable") enabled = enabled && (transactions().length > 0 || ledgerLoaded());
      if (button.id === "batch-confirm") enabled = enabled && selectedIds.size > 0;
      button.disabled = !enabled;
    });
    $$("input[type=file]").forEach((input) => { input.disabled = busy || !snapshot; });
    $("classify-button").title = modelReady() ? "只为不确定的分类提供建议，仍需人工确认" : "请先到模型与规则页面载入本地 1B";
    $("export-native").disabled = !ledgerLoaded();
    if (!ledgerLoaded() && $("export-native").checked) document.querySelector('input[name="export-kind"][value="standalone"]').checked = true;
    const native = document.querySelector('input[name="export-kind"]:checked')?.value === "native";
    $("only-confirmed").disabled = native;
    $("exclude-matched").disabled = native;
  }
  function renderModel() {
    const ready = modelReady();
    setText("top-model-status", ready ? "本地 1B 已就绪" : "本地 1B 未连接");
    $("model-dot").classList.toggle("muted", !ready);
    setText("settings-model-badge", ready ? "已就绪" : "未连接");
    $("settings-model-badge").className = `badge ${ready ? "teal" : "neutral"}`;
    setText("settings-model-detail", snapshot.model?.detail || (ready ? "模型已在本机运行，可以开始辅助分类。" : "载入本地 GGUF 后，即可使用辅助分类。"));
  }
  function renderAvailableAccounts() {
    const accounts = snapshot.accounts?.available || [];
    const signature = JSON.stringify(accounts);
    if ($("account-options").dataset.signature === signature) return;
    $("account-options").replaceChildren(...accounts.map((account) => { const option = document.createElement("option"); option.value = account; return option; }));
    $("account-options").dataset.signature = signature;
  }
  function matchesFilter(tx, filter) {
    if (filter === "pending") return tx.review_status !== "confirmed";
    if (filter === "confirmed") return tx.review_status === "confirmed";
    if (filter === "suggested") return tx.classification_source === "model" || Boolean(tx.suggested_account);
    if (filter === "fallback") return tx.classification_source === "fallback";
    if (filter === "matched") return tx.reconciliation_status === "matched";
    if (filter === "differences") return ["statement_only", "ambiguous"].includes(tx.reconciliation_status);
    return true;
  }
  function filteredTransactions() {
    const search = $("transaction-search").value.trim().toLocaleLowerCase();
    return transactions().filter((tx) => matchesFilter(tx, $("transaction-filter").value) && (!search || [tx.date, tx.counterparty, tx.description, tx.account, tx.source_file, tx.amount, PLATFORM_NAMES[tx.platform]].join(" ").toLocaleLowerCase().includes(search)));
  }
  function visibleTransactions() { return filteredTransactions().slice(currentTablePage * PAGE_SIZE, (currentTablePage + 1) * PAGE_SIZE); }
  function reviewBadge(tx) {
    if (tx.review_status === "confirmed") return badge("已确认", "teal");
    if (tx.classification_source === "transfer" || tx.tx_type === "transfer") return badge("转账待核对", "amber");
    if (tx.classification_source === "model" || tx.suggested_account) return badge("1B 建议", "blue");
    if (tx.classification_source === "fallback") return badge("待分类", "amber");
    return badge("待复核", "neutral");
  }
  function renderTransactions(force = false) {
    const all = transactions();
    $("review-empty").hidden = all.length !== 0;
    $("review-workspace").hidden = all.length === 0;
    const filtered = filteredTransactions();
    const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    currentTablePage = Math.min(currentTablePage, totalPages - 1);
    const visible = visibleTransactions();
    const signature = JSON.stringify([visible, detailId, $("transaction-filter").value, $("transaction-search").value, currentTablePage]);
    if (force || signature !== tableSignature) {
      const fragment = document.createDocumentFragment();
      for (const tx of visible) {
        const row = element("tr", tx.id === detailId ? "selected" : "");
        row.dataset.transactionId = tx.id;
        row.tabIndex = 0;
        row.setAttribute("aria-selected", tx.id === detailId ? "true" : "false");
        const checkCell = element("td", "check-col");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = selectedIds.has(tx.id);
        checkbox.setAttribute("aria-label", `选择 ${tx.date} ${tx.counterparty || "交易"}`);
        checkbox.addEventListener("click", (event) => event.stopPropagation());
        checkbox.addEventListener("change", () => { checkbox.checked ? selectedIds.add(tx.id) : selectedIds.delete(tx.id); renderSelection(); });
        checkCell.append(checkbox);
        const dateCell = element("td", "date-cell", tx.date || "—");
        if (tx.time) dateCell.append(element("span", "cell-sub", tx.time.slice(0, 5)));
        const merchantCell = element("td", "merchant-cell");
        const merchantLine = element("div", "merchant-line");
        const transfer = tx.tx_type === "transfer" || tx.classification_source === "transfer" || tx.dedup_status === "internal_transfer";
        const amount = Number(tx.amount);
        const flow = transfer ? "转" : Number.isFinite(amount) && amount > 0 ? "收" : Number.isFinite(amount) && amount < 0 ? "支" : "记";
        const platform = element("span", `platform-icon ${Object.hasOwn(PLATFORM_NAMES, tx.platform) ? tx.platform : ""}`, flow);
        platform.title = `${PLATFORM_NAMES[tx.platform] || tx.platform || "账单"} · ${transfer ? "内部转账" : amount > 0 ? "收入" : amount < 0 ? "支出" : "待核对"}`;
        const merchant = element("strong", "merchant-name", tx.counterparty || "未提供交易对方");
        merchant.title = tx.counterparty || "";
        merchantLine.append(platform, merchant);
        merchantCell.append(merchantLine, element("span", "cell-sub", tx.description || "无交易说明"));
        const amountCell = element("td", `amount-cell${Number(tx.amount) > 0 ? " income" : ""}`, money(tx.amount, tx.currency));
        amountCell.append(element("span", "cell-sub", tx.currency || snapshot.workspace?.currency || "CNY"));
        const accountCell = element("td", "account-cell");
        const account = element("span", "account-name", tx.account || "尚未分类");
        account.title = tx.account || "";
        accountCell.append(account, reviewBadge(tx));
        if (tx.reconciliation_status === "matched") accountCell.append(badge("已入账", "neutral"));
        row.append(checkCell, dateCell, merchantCell, amountCell, accountCell, element("td", "row-arrow", "›"));
        const open = () => { detailId = tx.id; detailDirty = false; renderTransactions(true); renderDetail(true); if (window.innerWidth <= 930) $("transaction-detail").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" }); };
        row.addEventListener("click", open);
        row.addEventListener("keydown", (event) => { if (event.target === row && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); open(); } });
        fragment.append(row);
      }
      $("transaction-rows").replaceChildren(fragment);
      tableSignature = signature;
    }
    $("no-filter-results").hidden = filtered.length > 0;
    setText("table-summary", filtered.length ? `显示 ${currentTablePage * PAGE_SIZE + 1}–${Math.min((currentTablePage + 1) * PAGE_SIZE, filtered.length)} / ${filtered.length} 笔` : "共 0 笔交易");
    setText("page-number", `${currentTablePage + 1} / ${totalPages}`);
    $("page-previous").disabled = currentTablePage === 0;
    $("page-next").disabled = currentTablePage + 1 >= totalPages;
    renderSelection();
  }
  function renderSelection() {
    setText("selected-count", `已选 ${selectedIds.size} 笔`);
    $("batch-toolbar").hidden = selectedIds.size === 0;
    const visible = visibleTransactions();
    const checked = visible.filter((tx) => selectedIds.has(tx.id)).length;
    $("select-all").checked = Boolean(visible.length && checked === visible.length);
    $("select-all").indeterminate = checked > 0 && checked < visible.length;
    $("select-all").disabled = !visible.length;
    $$("tr[data-transaction-id]", $("transaction-rows")).forEach((row) => { row.querySelector('input[type="checkbox"]').checked = selectedIds.has(row.dataset.transactionId); });
    renderBusy();
  }
  function detailFact(list, label, value) {
    const row = element("div", "detail-fact"); row.append(element("dt", "", label), element("dd", "", value || "—")); list.append(row);
  }
  function renderDetail(force = false) {
    const tx = transactions().find((item) => item.id === detailId);
    if (!tx) {
      if (!detailSignature && !force) return;
      const empty = element("div", "detail-empty"); empty.append(element("span", "", "↖"), element("h3", "", "点开一笔交易"));
      const note = element("p", "", "查看来源、核对分类，再确认它的去处。"); empty.append(note); $("transaction-detail").replaceChildren(empty); detailSignature = ""; return;
    }
    const signature = JSON.stringify(tx);
    if ((!force && detailDirty) || (!force && signature === detailSignature)) return;
    const panel = $("transaction-detail");
    const top = element("div", "detail-top"); top.append(element("span", "detail-caption", "交易详情"), reviewBadge(tx));
    const amount = element("div", "detail-amount", money(tx.amount)); amount.append(element("small", "", tx.currency || snapshot.workspace?.currency || "CNY"));
    const meta = element("dl", "detail-meta");
    detailFact(meta, "交易时间", `${tx.date || ""} ${tx.time || ""}`.trim());
    detailFact(meta, "交易平台", PLATFORM_NAMES[tx.platform] || tx.platform);
    detailFact(meta, "来源文件", tx.source_file);
    detailFact(meta, "资产账户", tx.asset_account);
    detailFact(meta, "分类来源", SOURCE_NAMES[tx.classification_source] || tx.classification_source);
    detailFact(meta, "账簿核对", RECON_NAMES[tx.reconciliation_status] || "尚未核对");
    const children = [top, amount, element("h2", "detail-merchant", tx.counterparty || "未提供交易对方"), element("p", "detail-description", tx.description || "无交易说明"), meta];
    if (tx.suggested_account) {
      const box = element("div", "suggestion-box");
      const heading = element("div", "suggestion-heading"); heading.append(element("strong", "", "✦ 1B 分类建议"));
      const hasSelfRating = Number.isFinite(Number(tx.confidence)) && tx.confidence !== null;
      if (hasSelfRating) heading.append(element("span", "", `模型自评 ${Math.round(Number(tx.confidence) * 100)}%`));
      box.append(heading, element("div", "", tx.suggested_account));
      if (hasSelfRating) box.append(element("p", "", "分数由模型自评，仍需结合原始交易确认。"));
      if (tx.reason) box.append(element("p", "", tx.reason));
      const use = element("button", "text-button", "采用这项建议 →"); use.type = "button";
      use.addEventListener("click", () => { $("detail-account").value = tx.suggested_account; detailDirty = true; $("detail-account").focus(); });
      box.append(use); children.push(box);
    } else if (tx.reason) children.push(element("p", "field-hint", tx.reason));
    const field = element("div", "detail-field");
    const label = element("label", "", "分类账户"); label.htmlFor = "detail-account";
    const account = document.createElement("input"); account.id = "detail-account"; account.value = tx.account || ""; account.setAttribute("list", "account-options"); account.spellcheck = false; account.autocomplete = "off"; account.placeholder = "选择或输入 Beancount 账户";
    account.addEventListener("input", () => { detailDirty = true; });
    field.append(label, account, element("p", "field-hint", tx.tx_type === "transfer" || tx.classification_source === "transfer" ? "这是一笔转账，请确认另一端的实际账户。" : "可以选择已有账户，也可以填写自己的账户路径。"));
    const confirm = element("button", "button primary full-width", "保存分类并确认 ✓"); confirm.dataset.mutation = "";
    confirm.addEventListener("click", async () => {
      const value = account.value.trim();
      if (!value) { toast("请先填写分类账户。", true); account.focus(); return; }
      const result = await action("/api/transactions/update", { ids: [tx.id], account: value, confirm: true }, "正在保存分类…", { success: "已保存并确认这笔交易", afterSubmit: () => { detailDirty = false; } });
      if (result) { detailDirty = false; renderDetail(true); }
    });
    children.push(field, confirm, element("p", "detail-footnote", "确认表示你已复核分类。金额与原始账单保持一致。"));
    panel.replaceChildren(...children);
    detailSignature = signature;
    renderBusy();
  }

  function renderLedger() {
    const ledger = snapshot.ledger || {};
    $("ledger-loaded-badge").hidden = !ledgerLoaded();
    setText("ledger-summary", ledgerLoaded() ? `${ledger.filename || "已导入账簿"} · ${ledger.transaction_count ?? 0} 笔原生交易` : "导入主账簿，以及它 include 的文件。");
    const names = ledger.source_files?.length ? ledger.source_files : ledger.filename ? [ledger.filename] : [];
    $("ledger-files").replaceChildren(...names.map((item) => {
      const name = typeof item === "string" ? item : item.name;
      const row = element("div", "ledger-file-item"); row.append(element("span", "", "▤"), element("span", "", name));
      if (name === ledger.filename || baseName(name) === baseName(ledger.filename)) row.append(element("small", "", "主账簿"));
      return row;
    }));
    setText("reconcile-statement-count", hasStatements() ? `${transactions().length} 笔交易` : "尚未导入");
    setText("workspace-currency", snapshot.workspace?.currency || "CNY");
    if (!walletDirty) {
      const wallets = snapshot.accounts?.wallets || {};
      $$("input", $("wallet-form")).forEach((input) => { input.value = wallets[input.name] || ""; });
    }
  }
  function reportCount(report, type) { return type === "ambiguous" ? report.counts?.ambiguous_groups ?? report.ambiguous?.length ?? 0 : report.counts?.[type] ?? report[type]?.length ?? 0; }
  function renderReport(force = false) {
    const report = snapshot.ledger?.report;
    $("reconcile-empty").hidden = Boolean(report);
    $("reconcile-results").hidden = !report;
    if (!report) return;
    const signature = JSON.stringify([report, reportTab]);
    if (!force && signature === reportSignature) return;
    const kinds = [["matched", "已匹配", "账单与账簿一一对应"], ["statement_only", "仅账单中", "尚未在现有账簿找到"], ["ledger_only", "仅账簿中", "核对范围内的额外条目"], ["ambiguous", "需人工复核", "多个候选，交给你确认"]];
    $("reconciliation-stats").replaceChildren(...kinds.map(([key, title, caption]) => { const card = element("div", "stat-card"); card.append(element("span", "stat-label", title), element("strong", "", reportCount(report, key)), element("span", "stat-caption", caption)); return card; }));
    const scope = report.scope || {};
    const accounts = scope.accounts || [];
    const currency = scope.currency || snapshot.workspace?.currency || "CNY";
    setText("report-scope", `${accounts.length} 个资产账户 · ${currency} · 仅比较导入账单覆盖的日期范围`);
    setText("report-status", report.status === "matched" ? "范围内全部匹配" : "有项目待核对");
    $("report-status").className = `badge ${report.status === "matched" ? "teal" : "amber"}`;
    let totals = $("report-totals");
    if (!totals) { totals = element("div", "report-totals"); totals.id = "report-totals"; $("report-tabs").before(totals); }
    const amounts = report.totals || {};
    totals.replaceChildren(...[["账单净额", amounts.statement], ["账簿净额", amounts.ledger], ["净额差", amounts.statement_minus_ledger]].map(([label, value]) => {
      const item = element("div"); item.append(element("span", "", label), element("strong", "", value === undefined ? "—" : money(value, amounts.currency || currency, true))); return item;
    }));
    $$("[data-report-tab]").forEach((tab) => { const active = tab.dataset.reportTab === reportTab; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); const label = kinds.find((kind) => kind[0] === tab.dataset.reportTab)?.[1] || ""; tab.textContent = `${label} ${reportCount(report, tab.dataset.reportTab)}`; });
    const entries = report[reportTab] || [];
    if (!entries.length) {
      $("report-content").replaceChildren(element("div", "report-empty", ({ matched: "还没有已匹配的条目。", statement_only: "没有仅存在于账单中的交易。", ledger_only: "核对范围内没有额外账簿条目。", ambiguous: "没有存在歧义的候选，清清楚楚。" })[reportTab]));
    } else if (reportTab === "ambiguous") {
      $("report-content").replaceChildren(...entries.map((group, index) => {
        const box = element("section", "ambiguous-group"); box.append(element("h3", "", `第 ${index + 1} 组 · 多个候选，需人工确认`));
        const columns = element("div", "ambiguous-columns");
        for (const [key, title] of [["statements", "账单中的交易"], ["ledger_postings", "账簿中的候选"]]) {
          const column = element("div"); column.append(element("strong", "", title));
          for (const item of group[key] || []) { const record = element("div", "ambiguous-entry", `${item.date || ""} · ${item.counterparty || item.payee || "未提供交易对方"}`); record.append(element("span", "", `${money(item.amount, item.currency, true)} · ${item.account || ""}`)); column.append(record); }
          columns.append(column);
        }
        box.append(columns); return box;
      }));
    } else {
      const table = element("table", "report-table");
      const head = element("thead"); const header = element("tr"); ["日期", "交易 / 账簿来源", "资产账户", "金额"].forEach((title) => { const th = element("th", "", title); th.scope = "col"; header.append(th); }); head.append(header);
      const body = element("tbody");
      for (const record of entries) {
        const item = reportTab === "matched" ? record.statement : record;
        if (!item) continue;
        const row = element("tr"); const who = element("td", "", item.counterparty || item.payee || "未提供交易对方");
        who.append(element("small", "", item.description || item.narration || ""));
        const posting = reportTab === "matched" ? record.ledger_posting : reportTab === "ledger_only" ? item : null;
        if (posting) who.append(element("small", "", `${baseName(posting.filename)}${posting.line ? ` : ${posting.line}` : ""}`));
        row.append(element("td", "", item.date), who, element("td", "", item.account), element("td", "amount-cell", money(item.amount, item.currency, true))); body.append(row);
      }
      table.append(head, body); const scroll = element("div", "table-scroll"); scroll.append(table); $("report-content").replaceChildren(scroll);
    }
    reportSignature = signature;
  }
  function renderSources() {
    const files = snapshot.files || [];
    const items = files.map((file) => ({ name: file.name, caption: Number.isFinite(file.count) ? `${file.count} 笔账单交易` : "账单文件", extension: baseName(file.name).split(".").pop().slice(0, 4).toUpperCase() }));
    if (ledgerLoaded()) items.push({ name: snapshot.ledger?.filename || "Beancount 账簿", caption: `${snapshot.ledger?.transaction_count ?? 0} 笔原生交易`, extension: "BEAN" });
    setText("source-file-count", items.length);
    $("source-files").replaceChildren(...items.map((item) => { const row = element("div", "source-file-row"); const label = element("div", "source-file-name", item.name); label.append(element("small", "", item.caption)); row.append(element("span", "source-file-icon", item.extension), label); return row; }));
    if (!items.length) $("source-files").append(element("p", "field-hint", "导入后，文件会在这里整齐列出。"));
  }

  function loadRuleDraft(force = false) {
    const rules = snapshot.accounts?.rules || {};
    const signature = JSON.stringify(rules);
    if ((!force && rulesDirty) || (!force && signature === rulesSignature)) return;
    ruleDraft = { mode: rules.mode || rules.rules_mode || "append" };
    for (const key of ["merchant_overrides", "expense_rules", "income_rules"]) ruleDraft[key] = Object.entries(rules[key] || {}).map(([keyword, account]) => ({ keyword, account }));
    $$("input[name=rule-mode]").forEach((input) => { input.checked = input.value === ruleDraft.mode; });
    rulesSignature = signature; renderRuleRows();
  }
  function setRulesDirty() { rulesDirty = true; setText("rules-save-status", "有未保存的修改"); }
  function renderRuleRows(focusLast = false) {
    $$("[data-rule-tab]").forEach((tab) => { const active = tab.dataset.ruleTab === ruleGroup; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); });
    const rows = ruleDraft[ruleGroup] || [];
    $("rule-rows").replaceChildren(...rows.map((rule, index) => {
      const row = element("div", "rule-row");
      const keyword = document.createElement("input"); keyword.value = rule.keyword; keyword.placeholder = "例如：瑞幸"; keyword.setAttribute("aria-label", `第 ${index + 1} 条匹配关键词`);
      const account = document.createElement("input"); account.value = rule.account; account.placeholder = ruleGroup === "income_rules" ? "Income:Salary" : "Expenses:Food:Coffee"; account.spellcheck = false; account.setAttribute("list", "account-options"); account.setAttribute("aria-label", `第 ${index + 1} 条规则账户`);
      keyword.addEventListener("input", () => { rule.keyword = keyword.value; setRulesDirty(); }); account.addEventListener("input", () => { rule.account = account.value; setRulesDirty(); });
      const remove = element("button", "icon-button", "×"); remove.type = "button"; remove.setAttribute("aria-label", `删除第 ${index + 1} 条规则`); remove.addEventListener("click", () => { rows.splice(index, 1); setRulesDirty(); renderRuleRows(); });
      row.append(keyword, account, remove); return row;
    }));
    if (!rows.length) $("rule-rows").append(element("div", "rules-empty", "还没有自定义规则。添加一条，让分类更懂你。"));
    if (focusLast) $("rule-rows").lastElementChild?.querySelector("input")?.focus();
  }
  async function saveRules() {
    const body = { mode: ruleDraft.mode };
    for (const group of ["merchant_overrides", "expense_rules", "income_rules"]) {
      body[group] = {};
      for (const rule of ruleDraft[group]) {
        const keyword = rule.keyword.trim(); const account = rule.account.trim();
        if (!keyword && !account) continue;
        if (!keyword || !account) { toast("每条规则都需要填写关键词和账户。", true); return; }
        if (Object.hasOwn(body[group], keyword)) { toast(`关键词「${keyword}」重复了，请合并后保存。`, true); return; }
        Object.defineProperty(body[group], keyword, { value: account, enumerable: true, configurable: true });
      }
    }
    const result = await action("/api/rules", body, "正在应用分类规则…", { success: "规则已保存", afterSubmit: () => { rulesDirty = false; } });
    if (result) { rulesDirty = false; loadRuleDraft(true); setText("rules-save-status", "规则已保存，已应用到当前工作区。"); }
  }

  function fileBase64(file) {
    return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve({ name: filename(file), content: String(reader.result).split(",", 2)[1] }); reader.onerror = () => reject(new Error(`无法读取文件：${file.name}`)); reader.readAsDataURL(file); });
  }
  async function prepareFiles(files) {
    if (!files.length) return null;
    const bundle = [];
    for (const file of files) bundle.push(await fileBase64(file));
    return bundle;
  }
  async function importStatements(files) {
    if (!files.length || isBusy()) return;
    localBusy = "正在读取账单文件…"; renderBusy();
    let bundle;
    try { bundle = await prepareFiles(files); } catch (error) { toast(error.message, true); }
    finally { localBusy = ""; renderBusy(); }
    if (!bundle) return;
    const result = await action("/api/import/statements", { files: bundle }, "正在导入并解析账单…", { success: "账单已导入，请复核分类" });
    if (result) { showPage("transactions"); currentTablePage = 0; renderTransactions(true); }
  }
  async function importLedgerFiles(files, main) {
    if (!files.length || isBusy()) return;
    localBusy = "正在读取账簿文件…"; renderBusy();
    let bundle;
    try { bundle = await prepareFiles(files); } catch (error) { toast(error.message, true); }
    finally { localBusy = ""; renderBusy(); }
    if (!bundle) return;
    await action("/api/import/ledger", { files: bundle, main: main || filename(files[0]) }, "正在检查并导入账簿…", { success: "Beancount 账簿已导入" });
  }
  function chooseLedger(files) {
    const usable = Array.from(files).filter((file) => file.name !== ".DS_Store");
    if (!usable.length || isBusy()) return;
    if (usable.length === 1) { importLedgerFiles(usable, filename(usable[0])); return; }
    pendingLedgerFiles = usable;
    const placeholder = element("option", "", "请选择主账簿文件…"); placeholder.value = "";
    const candidates = usable.filter((file) => /\.(beancount|bean|bc|ledger)$/i.test(file.name));
    const options = (candidates.length ? candidates : usable).map((file) => { const option = element("option", "", filename(file)); option.value = filename(file); return option; });
    $("ledger-main-select").replaceChildren(placeholder, ...options);
    const likelyMain = usable.filter((file) => /(?:^|\/)(?:main|ledger)\.(?:beancount|bean|bc)$/i.test(filename(file)));
    if (likelyMain.length === 1) $("ledger-main-select").value = filename(likelyMain[0]);
    setText("ledger-dialog-description", `已选择 ${usable.length} 个文件。请选择入口账簿，其余文件会作为 include 一同保留。`);
    $("ledger-dialog").showModal();
    $("ledger-main-select").focus();
  }
  async function droppedFiles(transfer) {
    const items = Array.from(transfer.items || []);
    if (!items.some((item) => item.webkitGetAsEntry)) return Array.from(transfer.files || []);
    const results = [];
    async function walk(entry, prefix = "") {
      if (!entry) return;
      if (entry.isFile) {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
        Object.defineProperty(file, "workbenchRelativePath", { value: prefix + file.name }); results.push(file);
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        for (;;) {
          const children = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (!children.length) break;
          for (const child of children) await walk(child, prefix + entry.name + "/");
        }
      }
    }
    for (const item of items) await walk(item.webkitGetAsEntry?.());
    return results.length ? results : Array.from(transfer.files || []);
  }
  function confirmAction(title, description, accept) {
    return new Promise((resolve) => {
      setText("confirm-title", title); setText("confirm-description", description); setText("confirm-accept", accept);
      const dialog = $("confirm-dialog"); dialog.returnValue = "cancel";
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true }); dialog.showModal();
    });
  }
  function markPreviewStale() {
    if (!exportResult) return;
    $("export-download").hidden = true;
    setText("export-info", "内容或选项已改变，请重新生成预览。");
  }
  async function generateExport() {
    const kind = document.querySelector('input[name="export-kind"]:checked')?.value || "standalone";
    const result = await action("/api/export", { kind, only_confirmed: $("only-confirmed").checked, exclude_matched: $("exclude-matched").checked }, "正在生成 Beancount 预览…", { noRefresh: true });
    if (!result) return;
    exportResult = result; exportSourceSignature = dataSignature();
    $("export-preview").textContent = result.content || "";
    $("export-preview").hidden = false; $("preview-empty").hidden = true;
    const lineCount = String(result.content || "").split("\n").length;
    setText("export-info", `${result.filename || "ledger.beancount"} · ${result.transaction_count ?? 0} 笔交易 · ${lineCount} 行`);
    const warnings = Array.isArray(result.warnings) ? result.warnings : result.warnings ? [result.warnings] : [];
    $("export-warnings").replaceChildren(...warnings.map((warning) => element("p", "", warning)));
    $("export-warnings").hidden = !warnings.length;
    if (downloadObjectURL) URL.revokeObjectURL(downloadObjectURL);
    downloadObjectURL = null;
    const link = $("export-download");
    let safeURL = null;
    if (result.download_url) {
      try { const url = new URL(result.download_url, window.location.href); if (url.origin === window.location.origin) safeURL = url.href; } catch { /* Use the actual returned text as a local download. */ }
    }
    if (!safeURL) { downloadObjectURL = URL.createObjectURL(new Blob([result.content || ""], { type: "text/plain;charset=utf-8" })); safeURL = downloadObjectURL; }
    link.href = safeURL; link.download = result.filename || "ledger.beancount"; link.hidden = false;
  }

  document.addEventListener("click", async (event) => {
    const pageButton = event.target.closest("[data-page]");
    if (pageButton) { showPage(pageButton.dataset.page); return; }
    const closeButton = event.target.closest("[data-close-dialog]");
    if (closeButton) { $(closeButton.dataset.closeDialog).close("cancel"); return; }
    const filterButton = event.target.closest("[data-filter]");
    if (filterButton) { $("transaction-filter").value = filterButton.dataset.filter; currentTablePage = 0; renderTransactions(true); return; }
    const reportButton = event.target.closest("[data-report-tab]");
    if (reportButton) { reportTab = reportButton.dataset.reportTab; renderReport(true); return; }
    const ruleButton = event.target.closest("[data-rule-tab]");
    if (ruleButton) { ruleGroup = ruleButton.dataset.ruleTab; renderRuleRows(); return; }
    const button = event.target.closest("[data-action]");
    if (!button || button.disabled) return;
    const kind = button.dataset.action;
    if (kind === "import-statements") $("statement-input").click();
    if (kind === "import-ledger") $("ledger-input").click();
    if (kind === "import-ledger-folder") $("ledger-folder-input").click();
    if (kind === "import-model") $("model-input").click();
    if (kind === "classify") await action("/api/classify", {}, "本地 1B 正在辅助分类…", { success: "分类建议已生成，请逐笔复核" });
    if (kind === "reconcile") await action("/api/reconcile", { date_window_days: Number($("date-window").value) }, "正在逐笔核对账簿…", { success: "核对完成，所有不确定项已保留" });
    if (kind === "export") await generateExport();
    if (kind === "demo") {
      const hasData = transactions().length > 0 || ledgerLoaded();
      if (!hasData || await confirmAction("载入合成示例？", "合成示例会替换当前工作区。请先导出需要保留的内容；示例中的交易不是真实用户数据。", "载入合成示例")) {
        detailId = null; detailDirty = false; selectedIds.clear(); walletDirty = false; rulesDirty = false;
        const result = await action("/api/demo", {}, "正在准备合成账单…", { success: "合成示例已载入，可以试用整个流程" });
        if (result) { showPage("transactions"); $("transaction-filter").value = "all"; $("transaction-search").value = ""; currentTablePage = 0; renderTransactions(true); }
      }
    }
    if (kind === "reset" && await confirmAction("清空当前工作区？", "将清空当前交易、复核与核对视图。原始文件和本地上传副本会保留，请先导出需要的结果。", "清空工作区")) {
      detailId = null; detailDirty = false; selectedIds.clear(); walletDirty = false; rulesDirty = false;
      const result = await action("/api/reset", {}, "正在清空工作区…", { success: "工作区已清空" });
      if (result) { exportResult = null; $("export-preview").hidden = true; $("preview-empty").hidden = false; $("export-download").hidden = true; $("export-warnings").hidden = true; showPage("transactions"); }
    }
  });

  $("transaction-search").addEventListener("input", () => { currentTablePage = 0; renderTransactions(true); });
  $("transaction-filter").addEventListener("change", () => { currentTablePage = 0; renderTransactions(true); });
  $("select-all").addEventListener("change", (event) => { visibleTransactions().forEach((tx) => { event.target.checked ? selectedIds.add(tx.id) : selectedIds.delete(tx.id); }); renderTransactions(true); });
  $("clear-selection").addEventListener("click", () => { selectedIds.clear(); renderTransactions(true); });
  $("page-previous").addEventListener("click", () => { currentTablePage = Math.max(0, currentTablePage - 1); renderTransactions(true); });
  $("page-next").addEventListener("click", () => { currentTablePage += 1; renderTransactions(true); });
  $("batch-confirm").addEventListener("click", async () => {
    const body = { ids: Array.from(selectedIds), confirm: true };
    const account = $("batch-account").value.trim(); if (account) body.account = account;
    const result = await action("/api/transactions/update", body, "正在批量确认交易…", { success: "所选交易已确认", afterSubmit: () => { detailDirty = false; } });
    if (result) { body.ids.forEach((id) => selectedIds.delete(id)); $("batch-account").value = ""; renderTransactions(true); renderDetail(true); }
  });
  $("statement-input").addEventListener("change", (event) => { const files = Array.from(event.target.files); event.target.value = ""; importStatements(files); });
  $("ledger-input").addEventListener("change", (event) => { const files = Array.from(event.target.files); event.target.value = ""; chooseLedger(files); });
  $("ledger-folder-input").addEventListener("change", (event) => { const files = Array.from(event.target.files); event.target.value = ""; chooseLedger(files); });
  $("model-input").addEventListener("change", async (event) => {
    const file = event.target.files[0]; event.target.value = ""; if (!file) return;
    if (!/\.gguf$/i.test(file.name)) { toast("请选择 .gguf 模型文件。", true); return; }
    await action(`/api/model/import?filename=${encodeURIComponent(file.name)}`, file, "正在导入本地 1B 模型文件…", { raw: true, success: "本地模型已载入" });
  });
  $("ledger-import-form").addEventListener("submit", (event) => {
    event.preventDefault(); const main = $("ledger-main-select").value; if (!main) { $("ledger-main-select").reportValidity(); return; }
    const files = pendingLedgerFiles.slice(); $("ledger-dialog").close("import"); importLedgerFiles(files, main);
  });
  $("ledger-dialog").addEventListener("close", () => { pendingLedgerFiles = []; });
  $("wallet-form").addEventListener("input", () => { walletDirty = true; });
  $("wallet-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const wallets = {}; $$("input", $("wallet-form")).forEach((input) => { wallets[input.name] = input.value.trim(); });
    const result = await action("/api/accounts", { wallets }, "正在保存资产账户映射…", { success: "账户映射已更新，请重新核对账簿", afterSubmit: () => { walletDirty = false; } });
    if (result) { walletDirty = false; renderLedger(); }
  });
  $$("input[name=rule-mode]").forEach((input) => input.addEventListener("change", () => { if (input.checked) { ruleDraft.mode = input.value; setRulesDirty(); } }));
  $("add-rule").addEventListener("click", () => { ruleDraft[ruleGroup].push({ keyword: "", account: "" }); setRulesDirty(); renderRuleRows(true); });
  $("save-rules").addEventListener("click", saveRules);
  $$("input[name=export-kind],#only-confirmed,#exclude-matched").forEach((input) => input.addEventListener("change", () => { markPreviewStale(); renderBusy(); }));
  $("dismiss-error").addEventListener("click", () => { dismissedError = connectionError ? $("error-message").textContent : snapshot?.job?.error || ""; $("error-banner").hidden = true; });
  const drop = $("statement-drop");
  drop.addEventListener("click", () => { if (!isBusy()) $("statement-input").click(); });
  drop.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key) && !isBusy()) { event.preventDefault(); $("statement-input").click(); } });
  drop.addEventListener("dragover", (event) => { event.preventDefault(); if (!isBusy()) drop.classList.add("drag-over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag-over"));
  drop.addEventListener("drop", async (event) => {
    event.preventDefault(); drop.classList.remove("drag-over"); if (isBusy()) { toast("正在处理上一项操作，请稍候。"); return; }
    try { await importStatements(await droppedFiles(event.dataTransfer)); } catch (error) { toast(error.message || "无法读取拖入的文件。", true); }
  });
  document.addEventListener("dragover", (event) => { if (event.dataTransfer?.types.includes("Files")) event.preventDefault(); });
  document.addEventListener("drop", (event) => { if (event.dataTransfer?.types.includes("Files")) event.preventDefault(); });
  document.addEventListener("keydown", (event) => { if (event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey && !["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName) && currentPage === "transactions") { event.preventDefault(); $("transaction-search").focus(); } });
  window.addEventListener("hashchange", () => showPage(location.hash.slice(1), false));
  showPage(location.hash.slice(1) || "transactions", false);
  renderBusy();
  refreshState().catch(() => { connectionError = true; renderBusy(); }).finally(scheduleRefresh);
})();
