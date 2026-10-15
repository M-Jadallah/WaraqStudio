"use strict";
const $ = (id) => document.getElementById(id);
// A page owns one immutable target. Device changes navigate to a fresh document;
// old responses and partially prepared forms can never target the next device.
const deviceMatch = location.pathname.match(/^\/devices\/([A-Za-z0-9_-]+)\/?$/);
const targetDevice = deviceMatch ? deviceMatch[1] : null;
let internetMode = !!targetDevice || /^\/(internet|pair)(\/|$)/.test(location.pathname);
let ready = false;
let pageActive = true;
let pairClaim = null;
let pairStatusBusy = false;
let pairingBusy = false;
let pairingTicket = location.pathname.replace(/\/$/, "") === "/pair"
  ? location.hash.slice(1) : "";
if (pairingTicket) history.replaceState(null, "", location.pathname + location.search);
window.addEventListener("pagehide", () => { pageActive = false; });
window.addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });
const devicePath = (suffix) => internetMode
  ? "/api/devices/" + encodeURIComponent(targetDevice) + suffix
  : "/api" + suffix;
const ui = {
  state: null,
  online: false,
  pending: false,
  updateLocked: false,
  preparing: false,
  polling: false,
  limit: 60,
  fileLimit: 60,
  sourceRevision: null,
  selected: new Set(),
  signatures: new WeakMap(),
  library: { files: [], loaded: false, loading: false, sequence: 0, revision: null, next: null, busy: false, organizing: false, mustRefresh: false },
  previewSequence: 0,
  sessionEpoch: 0,
};
let toastTimer;
function text(id, value) {
  const node = $(id);
  if (node.textContent !== String(value)) node.textContent = value;
}
function el(tag, cls, value) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (value !== undefined) node.textContent = value;
  return node;
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  text("toast", message);
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  toastTimer = setTimeout(() => ($("toast").hidden = true), 7000);
}
function enableActions() {
  document
    .querySelectorAll("[data-mutate]")
    .forEach(
      (button) =>
        (button.disabled =
          ui.updateLocked || ui.pending || !ui.online || (ui.preparing && button.id === "upload")),
    );
  $("discover").disabled = ui.updateLocked || ui.pending || !ui.online || !!ui.state?.source.busy;
  $("library-organize").disabled ||= !ui.state?.capabilities?.library_organize;
}
function showLogin() {
  ui.sessionEpoch++;
  ui.state = null;
  ui.online = false;
  $("workspace").hidden = true;
  $("navigation").hidden = true;
  $("logout").hidden = true;
  $("login").hidden = internetMode;
  $("connection").classList.remove("online");
  text("connection", "يلزم الاقتران");
  for (const id of ["jobs", "workers", "services", "files", "source-items", "library-files"])
    $(id).replaceChildren();
  ui.sourceRevision = null;
  ui.selected.clear();
  ui.library.sequence++;
  ui.library.files = [];
  ui.library.loaded = false;
  ui.library.loading = false;
  ui.library.busy = false;
  ui.library.organizing = false;
  ui.previewSequence++;
  $("preview-text").textContent = "";
  $("file-preview").close();
  if ($("confirmation").open) $("confirm-no").click();
  for (const id of ["template", "folder", "library-group"]) {
    $(id).replaceChildren();
    delete $(id).dataset.signature;
  }
  for (const id of ["source-name", "source-status", "library-status", "library-error", "preview-name", "preview-status"]) text(id, "");
  if (internetMode) {
    $("device-context").hidden = true;
    $("internet-home").hidden = false;
    text("device-error", "يلزم اقتران هذا الهاتف بهذا الكمبيوتر. افتح إعدادات ورق على الكمبيوتر وامسح رمز QR جديدًا.");
    $("device-list").replaceChildren();
    loadDevices();
  }
}
async function api(path, data) {
  let response;
  try {
    response = await fetch(path, {
      method: data === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers:
        data === undefined
          ? {}
          : { "Content-Type": "application/json", "X-Waraq-Client": "1" },
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: AbortSignal.timeout(18000),
    });
  } catch {
    const error = new Error(
      "تعذر الاتصال بالكمبيوتر. تحقق من حالة الطابور قبل إعادة إرسال الأمر.",
    );
    error.network = true;
    throw error;
  }
  let result;
  try { result = await response.json(); }
  catch { throw new Error("ورد رد غير صالح من البوابة. حدّث الحالة قبل إعادة إرسال أي أمر."); }
  if (!response.ok) {
    const error = new Error(result.error || "تعذر تنفيذ الطلب.");
    error.status = response.status;
    if ((response.status === 401 || response.status === 403) &&
        (path === "/api/state" || path.startsWith("/api/devices/") || path.startsWith("/api/library/")) && pageActive)
      showLogin();
    throw error;
  }
  return result;
}
async function poll() {
  if (!ready || !pageActive || ui.polling || document.hidden || (internetMode && !targetDevice)) return;
  ui.polling = true;
  try {
    const state = await api(devicePath("/state"));
    if (!pageActive) return;
    if (!ui.state) {
      document.querySelectorAll("[data-page]").forEach((page) => {
        page.hidden = page.dataset.page !== "queue";
      });
      document.querySelectorAll("[data-tab]").forEach((tab) => {
        if (tab.dataset.tab === "queue") tab.setAttribute("aria-current", "page");
        else tab.removeAttribute("aria-current");
      });
    }
    ui.state = state;
    ui.online = true;
    $("login").hidden = true;
    $("workspace").hidden = false;
    $("navigation").hidden = false;
    $("logout").hidden = false;
    $("offline").hidden = true;
    $("connection").classList.add("online");
    text("connection", internetMode ? "متصل بالجهاز المحدد" : "متصل بالكمبيوتر");
    render();
  } catch (error) {
    if (!pageActive) return;
    if (error.status !== 401 && error.status !== 403) {
      ui.online = false;
      $("offline").hidden = false;
      text("connection", "الاتصال متوقف");
      $("connection").classList.remove("online");
      if (internetMode) {
        $("workspace").hidden = false;
        $("logout").hidden = false;
        $("device-context").hidden = false;
        if (!ui.state) document.querySelectorAll("[data-page]").forEach((page) => { page.hidden = true; });
        text("offline", error.message + " لا تُرسل الأوامر أثناء الانقطاع. حدّث الحالة بعد عودة الكمبيوتر.");
      } else if (!ui.state) {
        $("login").hidden = false;
        text("pair-error", error.message);
      }
    }
  } finally {
    ui.polling = false;
    enableActions();
  }
}
async function mutate(action, data = {}) {
  if (!pageActive || ui.updateLocked || ui.pending || !ui.online) return false;
  ui.pending = true;
  enableActions();
  try {
    const result = await api(devicePath("/action/" + action), data);
    if (!pageActive) return false;
    toast(result.message || "تم التنفيذ.");
    await poll();
    return true;
  } catch (error) {
    toast(error.message, true);
    if (error.network) {
      ui.online = false;
      $("offline").hidden = false;
    }
    return false;
  } finally {
    ui.pending = false;
    enableActions();
  }
}
function confirmAction(message) {
  if (ui.updateLocked) return Promise.resolve(false);
  text("confirm-message", message);
  const dialog = $("confirmation");
  dialog.showModal();
  $("confirm-no").focus();
  return new Promise((resolve) => {
    let done = false;
    function finish(value) {
      if (done) return;
      done = true;
      dialog.close();
      $("confirm-no").onclick = null;
      $("confirm-yes").onclick = null;
      dialog.oncancel = null;
      resolve(value);
    }
    $("confirm-no").onclick = () => finish(false);
    $("confirm-yes").onclick = () => finish(true);
    dialog.oncancel = (event) => {
      event.preventDefault();
      finish(false);
    };
  });
}
function actionButton(label, callback, danger = false) {
  const button = el("button", danger ? "quiet danger" : "quiet", label);
  button.type = "button";
  button.dataset.mutate = "";
  button.onclick = callback;
  return button;
}
function fileLinks(job) {
  const actions = el("div", "actions");
  for (const kind of job.files) {
    const link = el(
      "a",
      "",
      kind === "document" ? "تنزيل Word" : "تنزيل التفريغ",
    );
    link.href = devicePath("/files/" + encodeURIComponent(job.id) + "/" + kind);
    link.download = "";
    link.setAttribute("aria-label", link.textContent + " — " + job.title);
    actions.append(link);
  }
  return actions;
}
function reconcile(container, items, key, create, signature = JSON.stringify) {
  const existing = new Map(
    [...container.children].map((node) => [node.dataset.key, node]),
  );
  items.forEach((item, index) => {
    const id = String(key(item));
    let node = existing.get(id);
    const sig = signature(item);
    existing.delete(id);
    if (!node || ui.signatures.get(node) !== sig) {
      // Preserve keyboard focus and an expanded error while progress updates arrive.
      if (node?.contains(document.activeElement) && (!node.dataset.version || node.dataset.version === String(item.version))) return;
      const open = !!node?.querySelector("details[open]");
      const next = create(item);
      next.dataset.key = id;
      ui.signatures.set(next, sig);
      if (open && next.querySelector("details"))
        next.querySelector("details").open = true;
      if (node) node.replaceWith(next);
      node = next;
    }
    if (container.children[index] !== node)
      container.insertBefore(node, container.children[index] || null);
  });
  existing.forEach((node) => node.remove());
}
function empty(container, message) {
  reconcile(
    container,
    [{ id: "empty", message }],
    (item) => item.id,
    (item) => el("p", "empty", item.message),
  );
}
function compactCard(title, status, cls = "") {
  const card = el("article", "compact-row " + cls), main = el("div", "row-main");
  const heading = el("div", "row-heading"), name = el("h3", "row-title", title);
  name.title = title;
  heading.append(name, el("span", "badge", status));
  const details = el("details", "row-more"), summary = el("summary", "", "المزيد");
  summary.setAttribute("aria-label", "المزيد — " + title);
  const content = el("div", "row-detail");
  content.append(el("h3", "full-title", title));
  details.append(summary, content);
  main.append(heading);
  card.append(main, details);
  return {card, main, content, details};
}
function viewButton(details, title) {
  const button = el("button", "quiet row-action", "عرض");
  button.setAttribute("aria-label", "عرض تفاصيل — " + title);
  button.onclick = () => { details.open = !details.open; };
  return button;
}
function jobCard(job) {
  const {card, main, content, details} = compactCard(job.title, job.statusLabel, "job");
  card.querySelector(".badge").classList.add(job.status);
  main.append(viewButton(details, job.title));
  content.append(
    el(
      "div",
      "meta",
      [job.stageLabel, job.template, job.folder_name]
        .filter(Boolean)
        .join(" · "),
    ),
  );
  if (job.status === "running") {
    const progress = el("progress");
    progress.max = 100;
    if (job.progress >= 0) progress.value = Math.min(100, job.progress);
    progress.setAttribute("aria-label", "تقدم " + job.title);
    content.append(progress);
  }
  if (job.retryAt)
    content.append(el("div", "meta", "المحاولة القادمة: " + job.retryAt));
  if (job.error) {
    const details = el("details");
    details.append(el("summary", "", "عرض سبب التوقف"), el("p", "", job.error));
    content.append(details);
  }
  const actions = fileLinks(job);
  if (
    ["failed", "blocked", "uncertain", "paused", "cancelled"].includes(
      job.status,
    ) ||
    (job.status === "pending" && job.retryAt)
  ) {
    actions.append(
      actionButton("إعادة المحاولة", async () => {
        const uncertain = job.status === "uncertain";
        if (
          uncertain &&
          !(await confirmAction(
            "نتيجة التفريغ السابقة غير مؤكدة. قد تؤدي إعادة الطلب إلى تكلفة Deepgram إضافية. هل تريد إعادة المحاولة؟",
          ))
        )
          return;
        await mutate("retry", {
          id: job.id,
          stage: job.current,
          confirm_uncertain: uncertain,
        });
      }),
    );
  }
  if (!["completed", "cancelled"].includes(job.status))
    actions.append(
      actionButton(
        "إلغاء",
        async () => {
          if (
            await confirmAction(
              "إلغاء المهمة «" +
                job.title +
                "»؟ قد يستمر طلب التفريغ الذي أُرسل للخدمة وتبقى تكلفته.",
            )
          )
            await mutate("cancel", { id: job.id });
        },
        true,
      ),
    );
  if (actions.childElementCount) content.append(actions);
  return card;
}
function renderJobs() {
  if (!ui.state) return;
  const query = $("search").value.trim().toLocaleLowerCase(),
    filter = $("filter").value;
  const jobs = ui.state.jobs.filter(
    (job) =>
      job.title.toLocaleLowerCase().includes(query) &&
      (filter === "all" ||
        job.status === filter ||
        (filter === "attention" &&
          ["failed", "blocked", "uncertain"].includes(job.status))),
  );
  text(
    "job-count",
    jobs.length
      ? "عرض " +
          Math.min(ui.limit, jobs.length) +
          " من " +
          jobs.length +
          " مهمة"
      : "",
  );
  if (jobs.length)
    reconcile($("jobs"), jobs.slice(0, ui.limit), (job) => job.id, jobCard);
  else
    empty(
      $("jobs"),
      query || filter !== "all"
        ? "لا توجد مهام تطابق البحث."
        : "طابورك فارغ. أضف رابطًا أو تفريغًا من «إضافة مصدر».",
    );
  $("more").hidden = jobs.length <= ui.limit;
  enableActions();
}
function updateOptions(id, items, defaultLabel) {
  const select = $(id),
    signature = JSON.stringify(items);
  if (select.dataset.signature === signature) return;
  const previous = select.value;
  select.replaceChildren();
  if (defaultLabel) select.append(new Option(defaultLabel, ""));
  for (const item of items) select.append(new Option(item.name, item.id));
  if ([...select.options].some((option) => option.value === previous))
    select.value = previous;
  select.dataset.signature = signature;
}
function renderSource() {
  const source = ui.state.source;
  text(
    "source-status",
    source.busy ? "جارٍ قراءة المصدر على الكمبيوتر…" : source.error || "",
  );
  $("source-status").classList.toggle("error", !!source.error);
  text("discover", source.busy ? "جارٍ القراءة…" : "قراءة المصدر");
  if (source.busy) return;
  if (ui.sourceRevision === source.revision) return;
  ui.sourceRevision = source.revision;
  ui.selected = new Set();
  $("source-items").replaceChildren();
  $("source-selection").hidden = source.items.length === 0;
  text("source-name", source.title);
  const fragment = document.createDocumentFragment();
  source.items.forEach((item, index) => {
    const label = el("label", "source-item"),
      checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.disabled = !item.available;
    checkbox.checked = !!item.available;
    checkbox.dataset.index = index;
    label.title = item.title;
    checkbox.setAttribute("aria-label", "تحديد — " + item.title);
    if (item.available) ui.selected.add(index);
    checkbox.onchange = () => {
      if (checkbox.checked) ui.selected.add(index);
      else ui.selected.delete(index);
      selectionCount();
    };
    label.append(
      checkbox,
      el(
        "span",
        "",
        item.position +
          ". " +
          item.title +
          (item.available ? "" : " · غير متاح"),
      ),
    );
    fragment.append(label);
  });
  $("source-items").append(fragment);
  selectionCount();
}
function selectionCount() {
  text("enqueue", "إضافة المحدد إلى الطابور (" + ui.selected.size + ")");
}
function renderWorkers() {
  const labels = {
    download: "التنزيل",
    deepgram: "التفريغ",
    opencode: "إنتاج Word",
  };
  reconcile(
    $("services"),
    Object.keys(labels).map((stage) => ({
      stage,
      paused: ui.state.workers.some((w) => w.stage === stage && w.paused),
    })),
    (s) => s.stage,
    (service) => {
      const {card, main, content} = compactCard(labels[service.stage], service.paused ? "متوقفة يدويًا" : "تستقبل المهام", "service");
      main.append(
        actionButton(
          service.paused ? "استئناف" : "إيقاف",
          () =>
            mutate("stage", { stage: service.stage, paused: !service.paused }),
        ),
      );
      content.append(el("p", "muted", "الإيقاف يمنع استقبال مهام جديدة؛ العمال يكملون مهامهم الحالية."));
      return card;
    },
  );
  reconcile(
    $("workers"),
    ui.state.workers,
    (w) => w.id,
    (worker) => {
      const state = worker.job_id
        ? "يعمل · " + worker.elapsed + " ث"
        : worker.error
          ? "يحتاج إجراء"
          : !worker.enabled
            ? "معطّل"
            : worker.paused
              ? "متوقف"
              : "جاهز";
      const {card, main, content, details} = compactCard(worker.name, state, "worker");
      main.append(viewButton(details, worker.name));
      content.append(
        el(
          "p",
          "meta",
          [worker.stageLabel, worker.providerLabel, worker.modelResolved]
            .filter(Boolean)
            .join(" · "),
        ),
      );
      if (worker.cooldownSeconds > 0)
        content.append(
          el(
            "p",
            "meta",
            "تهدئة YouTube: " +
              Math.ceil(worker.cooldownSeconds / 60) +
              " دقيقة",
          ),
        );
      if (worker.error) {
        const detail = el("details");
        detail.append(el("summary", "", "التفاصيل"), el("p", "", worker.error));
        content.append(detail);
      }
      return card;
    },
  );
}
function renderFiles() {
  if (!ui.state) return;
  const query = $("file-search").value.trim().toLocaleLowerCase(),
    jobs = ui.state.jobs.filter(
      (job) =>
        job.files.length && job.title.toLocaleLowerCase().includes(query),
    );
  if (!jobs.length)
    empty(
      $("files"),
      query
        ? "لا توجد نتائج تطابق البحث."
        : "ستظهر النتائج هنا بمجرد اكتمال التفريغ أو مستند Word.",
    );
  else
    reconcile(
      $("files"),
      jobs.slice(0, ui.fileLimit),
      (job) => job.id,
      (job) => {
        const {card, main, content} = compactCard(job.title, "نتيجة مهمة", "result");
        const links = fileLinks(job);
        const primary = links.firstElementChild;
        if (primary) { primary.className = "row-action"; primary.textContent = "تنزيل"; main.append(primary); }
        content.append(
          el(
            "p",
            "meta",
            [job.template, job.folder_name].filter(Boolean).join(" · "),
          ),
          links,
        );
        return card;
      },
      (job) =>
        JSON.stringify([
          job.id,
          job.title,
          job.files,
          job.template,
          job.folder_name,
        ]),
    );
  $("more-files").hidden = jobs.length <= ui.fileLimit;
}
function libraryVisible() {
  return !document.hidden && !$("files-page").hidden && !$("library-panel").hidden;
}
function fileSize(bytes) {
  return bytes < 1024 * 1024 ? Math.ceil(bytes / 1024) + " كيلوبايت" : (bytes / (1024 * 1024)).toFixed(1) + " ميغابايت";
}
function libraryFilePath(file, operation) {
  return devicePath("/library/files/" + encodeURIComponent(file.file_id) + "/" + operation)
    + "?expected_version=" + encodeURIComponent(file.version);
}
function libraryRow(file) {
  const kinds = {document: "Word", transcript: "TXT", audio: "صوت"};
  const {card, main, content, details} = compactCard(file.name, (kinds[file.kind] || file.kind) + " · " + fileSize(file.size), "library-file");
  card.dataset.version = file.version;
  if (file.can_download) {
    const link = el("a", "row-action", "تنزيل");
    link.href = libraryFilePath(file, "download");
    link.download = "";
    link.setAttribute("aria-label", "تنزيل — " + file.name);
    main.append(link);
  } else main.append(viewButton(details, file.name));
  content.append(el("p", "meta", "المادة: " + (file.group_name || "غير مصنف")));
  const modified = new Date(typeof file.modified === "number" ? file.modified * 1000 : file.modified);
  if (!Number.isNaN(modified.getTime())) content.append(el("p", "meta", "التعديل: " + modified.toLocaleString("ar-JO")));
  const actions = el("div", "actions");
  const copy = el("button", "quiet", "نسخ اسم الملف");
  copy.onclick = async () => {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(file.name);
      else {
        const input = el("textarea"); input.value = file.name; document.body.append(input); input.select();
        let copied;
        try { copied = document.execCommand("copy"); } finally { input.remove(); }
        if (!copied) throw new Error();
      }
      toast("نُسخ اسم الملف.");
    } catch { toast("تعذر النسخ. الاسم الكامل ظاهر في التفاصيل.", true); }
  };
  actions.append(copy);
  if (file.can_preview && ui.state?.capabilities?.library_preview) {
    const button = el("button", "quiet", "معاينة النص");
    button.onclick = () => previewFile(file);
    actions.append(button);
  }
  if (file.can_delete && ui.state?.capabilities?.library_delete) {
    const button = actionButton("حذف نسخة المكتبة", async () => {
      if (ui.library.mustRefresh) {
        toast("حدّث المكتبة أولًا للتحقق من نتيجة الطلب السابق.", true);
        return;
      }
      const device = internetMode ? $("device-name").textContent : "هذا الكمبيوتر";
      if (!(await confirmAction("حذف نسخة المكتبة «" + file.name + "» من «" + device + "»؟ يبقى أصل المهمة محفوظًا."))) return;
      if (!ui.online) return;
      ui.library.mustRefresh = true;
      const success = await mutate("library_delete", {file_id: file.file_id, expected_version: file.version, confirm: true});
      if (success) { ui.library.mustRefresh = false; await loadLibrary({refresh: true}); }
      else text("library-error", "قد تكون نتيجة الحذف غير مؤكدة. حدّث المكتبة قبل أي محاولة جديدة.");
    }, true);
    actions.append(button);
  }
  content.append(actions);
  return card;
}
async function previewFile(file) {
  if (ui.updateLocked) return;
  const sequence = ++ui.previewSequence;
  text("preview-name", file.name);
  text("preview-text", "");
  text("preview-status", "جارٍ قراءة النص…");
  $("file-preview").showModal();
  try {
    const result = await api(libraryFilePath(file, "preview"));
    if (!pageActive || sequence !== ui.previewSequence || !ui.state) return;
    text("preview-name", result.name);
    text("preview-text", result.text);
    text("preview-status", result.truncated ? "معاينة مختصرة؛ نزّل الملف لقراءة النص كاملًا." : fileSize(result.size));
  } catch (error) {
    if (sequence === ui.previewSequence) text("preview-status", error.message);
  }
}
$("preview-close").onclick = () => $("file-preview").close();
$("file-preview").addEventListener("close", () => { ui.previewSequence++; text("preview-text", ""); });
async function loadLibrary({append = false, refresh = false} = {}) {
  if (!ui.state?.capabilities?.library || !ui.online || !pageActive || ui.pending) return;
  if (append && (!ui.library.next || ui.library.loading)) return;
  const sequence = ++ui.library.sequence;
  ui.library.loading = true;
  text("library-error", "");
  text("library-status", refresh ? "جارٍ تحديث فهرس المكتبة…" : "جارٍ قراءة المكتبة…");
  $("library-more").disabled = true;
  const params = new URLSearchParams({kind: $("library-kind").value, group_id: $("library-group").value || "all", query: $("library-search").value.trim(), limit: "50"});
  if (append) params.set("cursor", ui.library.next);
  if (refresh) params.set("refresh", "1");
  try {
    const result = await api(devicePath("/library/files") + "?" + params);
    if (!pageActive || sequence !== ui.library.sequence || !ui.state) return;
    ui.library.busy = !!result.busy;
    ui.library.organizing = !!result.organizing;
    if (!result.busy) {
      const files = append ? [...ui.library.files, ...result.files] : result.files;
      ui.library.files = [...new Map(files.map((file) => [file.file_id, file])).values()];
      ui.library.loaded = true;
      ui.library.next = result.next_cursor;
      ui.library.revision = result.revision;
      if (refresh && !ui.pending) ui.library.mustRefresh = false;
      updateOptions("library-group", result.groups || [], "كل المواد");
      if (ui.library.files.length) reconcile($("library-files"), ui.library.files, (file) => file.file_id, libraryRow);
      else empty($("library-files"), "لا توجد ملفات تطابق البحث. جرّب تنظيم النتائج أو تغيير التصفية.");
    }
    text("library-status", result.busy ? "جارٍ فهرسة ملفات الكمبيوتر…" : (result.organizing ? "جارٍ تنظيم النتائج… " : "") + "عرض " + ui.library.files.length + " من " + result.total + " ملف");
    text("library-error", result.error || "");
    $("library-more").hidden = !ui.library.next || !!result.busy;
  } catch (error) {
    if (sequence !== ui.library.sequence) return;
    text("library-error", error.message + (error.status === 409 ? " حدّث المكتبة للبدء من قائمة جديدة." : ""));
  } finally {
    if (sequence === ui.library.sequence) {
      ui.library.loading = false;
      $("library-more").disabled = false;
      enableActions();
    }
  }
}
function selectFilesView(library) {
  $("library-panel").hidden = !library;
  $("results-panel").hidden = library;
  $("library-tab").setAttribute("aria-pressed", String(library));
  $("results-tab").setAttribute("aria-pressed", String(!library));
  if (library && !ui.library.loaded) loadLibrary();
}
$("library-tab").onclick = () => selectFilesView(true);
$("results-tab").onclick = () => selectFilesView(false);
$("library-refresh").onclick = () => loadLibrary({refresh: true});
$("library-more").onclick = () => loadLibrary({append: true});
let librarySearchTimer;
$("library-search").oninput = () => { clearTimeout(librarySearchTimer); librarySearchTimer = setTimeout(() => loadLibrary(), 300); };
for (const id of ["library-kind", "library-group"]) $(id).onchange = () => loadLibrary();
$("library-organize").onclick = async () => { if (await mutate("library_organize")) await loadLibrary(); };
function render() {
  for (const key of ["total", "active", "done", "attention"])
    text(key, ui.state.stats[key]);
  text("queue-state", ui.state.running ? "الطابور يعمل" : "الطابور متوقف");
  text(
    "queue-toggle",
    ui.state.running ? "إيقاف بعد المهام الحالية" : "تشغيل الطابور",
  );
  $("auto-stop").hidden = !ui.state.autoStop;
  updateOptions("template", ui.state.templates, "الإعداد الافتراضي");
  updateOptions("folder", ui.state.folders);
  renderJobs();
  renderWorkers();
  renderFiles();
  renderSource();
  const libraryAvailable = !!ui.state.capabilities?.library;
  $("library-compat").hidden = libraryAvailable;
  $("library-controls").hidden = !libraryAvailable;
  $("library-organize").hidden = !ui.state.capabilities?.library_organize;
  enableActions();
}
$("pair-form").onsubmit = async (event) => {
  event.preventDefault();
  text("pair-error", "");
  $("pair-button").disabled = true;
  text("pair-button", "جارٍ الاتصال…");
  try {
    await api("/api/pair", { code: $("code").value });
    $("code").value = "";
    await poll();
  } catch (error) {
    text("pair-error", error.message);
    $("code").focus();
  } finally {
    $("pair-button").disabled = false;
    text("pair-button", "الاتصال بورق");
  }
};
$("logout").onclick = async () => {
  if (ui.updateLocked || ui.pending) return;
  ui.pending = true;
  enableActions();
  try {
    await api(internetMode ? devicePath("/logout") : "/api/logout", {});
    if (internetMode) location.replace("/internet");
    else showLogin();
  } catch (error) {
    toast(error.message, true);
  } finally { ui.pending = false; enableActions(); }
};
$("queue-toggle").onclick = async () => {
  const desired = !ui.state.running;
  if (
    desired &&
    !(await confirmAction(
      "تشغيل الطابور باستخدام إعدادات العمال الحالية على الكمبيوتر؟ قد تبدأ طلبات مدفوعة لدى مزوّدي التفريغ والنماذج.",
    ))
  )
    return;
  await mutate("queue", { running: desired });
};
$("refresh").onclick = poll;
$("search").oninput = () => {
  ui.limit = 60;
  renderJobs();
};
$("filter").onchange = () => {
  ui.limit = 60;
  renderJobs();
};
$("more").onclick = () => {
  ui.limit += 60;
  renderJobs();
};
$("file-search").oninput = () => {
  ui.fileLimit = 60;
  renderFiles();
};
$("more-files").onclick = () => {
  ui.fileLimit += 60;
  renderFiles();
};
const taskOptions = () => ({
  template_id: $("template").value,
  folder_id: $("folder").value,
});
$("youtube-form").onsubmit = async (event) => {
  event.preventDefault();
  await mutate("discover", { url: $("url").value.trim() });
};
$("enqueue").onclick = async () => {
  if (!ui.selected.size) {
    toast("حدد فيديو واحدًا على الأقل.", true);
    return;
  }
  if (
    await mutate("enqueue", {
      ...taskOptions(),
      preset: $("preset").value,
      revision: ui.state.source.revision,
      indices: [...ui.selected].sort((a, b) => a - b),
    })
  )
    $("url").value = "";
};
for (const [id, checked] of [
  ["select-all", true],
  ["select-none", false],
])
  $(id).onclick = () => {
    $("source-items")
      .querySelectorAll("input:not(:disabled)")
      .forEach((input) => {
        input.checked = checked;
        if (checked) ui.selected.add(Number(input.dataset.index));
        else ui.selected.delete(Number(input.dataset.index));
      });
    selectionCount();
  };
$("upload-form").onsubmit = async (event) => {
  event.preventDefault();
  if (ui.updateLocked || ui.preparing || ui.pending) return;
  const files = [...$("transcripts").files];
  if (
    !files.length ||
    files.length > 20 ||
    files.reduce((total, file) => total + file.size, 0) > 10 * 1024 * 1024
  ) {
    toast("اختر حتى 20 ملف TXT بحجم إجمالي لا يتجاوز 10 ميغابايت.", true);
    return;
  }
  const options = taskOptions();
  ui.preparing = true;
  enableActions();
  text("upload", "جارٍ تجهيز التفريغات…");
  try {
    const payload = [];
    for (const file of files) {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      for (let i = 0; i < bytes.length; i += 8192)
        binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
      payload.push({ name: file.name, content: btoa(binary) });
    }
    text("upload", "جارٍ الرفع والإضافة…");
    if (await mutate("upload", { ...options, files: payload }))
      $("transcripts").value = "";
  } catch {
    toast("تعذرت قراءة ملفات الهاتف. أعد اختيارها وحاول مجددًا.", true);
  } finally {
    ui.preparing = false;
    text("upload", "رفع التفريغات وإضافتها");
    enableActions();
  }
};
document.querySelectorAll("[data-tab]").forEach(
  (button) =>
    (button.onclick = () => {
      document.querySelectorAll("[data-tab]").forEach((tab) => {
        if (tab === button) tab.setAttribute("aria-current", "page");
        else tab.removeAttribute("aria-current");
      });
      document
        .querySelectorAll("[data-page]")
        .forEach(
          (page) => (page.hidden = page.dataset.page !== button.dataset.tab),
        );
      window.scrollTo(0, 0);
      const heading = $(button.dataset.tab + "-title");
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
      if (button.dataset.tab === "files" && !ui.library.loaded) loadLibrary();
    }),
);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { poll(); if (pairClaim) pollPairStatus(); }
});
window.addEventListener("online", poll);
boot();
setInterval(() => {
  if (ui.state || (internetMode && targetDevice)) poll();
  if (pairClaim) pollPairStatus();
  if (libraryVisible() && (ui.library.busy || ui.library.organizing) && !ui.library.loading) loadLibrary();
}, 2500);

async function loadDevices() {
  const epoch = ui.sessionEpoch;
  $("devices-refresh").disabled = true;
  try {
    const result = await api("/api/devices");
    if (!pageActive || epoch !== ui.sessionEpoch) return;
    $("device-list").replaceChildren();
    for (const device of result.devices || []) {
      const {card, main, content} = compactCard(device.name || device.device_name || "كمبيوتر ورق", device.online ? "متصل" : "غير متصل", "device-row");
      const link = el("a", "device-link", device.name || device.device_name || "كمبيوتر ورق");
      link.href = "/devices/" + encodeURIComponent(device.device_id);
      link.textContent = "فتح";
      link.setAttribute("aria-label", device.name || device.device_name || "كمبيوتر ورق");
      link.className = "row-action";
      main.append(link);
      content.append(el("p", "muted", "أبقِ ورق مفتوحًا والكمبيوتر مستيقظًا."));
      $("device-list").append(card);
    }
    if (!result.devices?.length) empty($("device-list"), "لم يقترن هذا المتصفح بأي كمبيوتر بعد.");
    if (targetDevice) {
      const device = result.devices?.find((item) => item.device_id === targetDevice);
      if (device) {
        text("device-name", device.name || device.device_name || "كمبيوتر ورق");
        if (device.last_seen) {
          const date = new Date(typeof device.last_seen === "number" ? device.last_seen * 1000 : device.last_seen);
          if (!Number.isNaN(date.getTime())) text("device-last-seen", "آخر ظهور: " + date.toLocaleString("ar-JO"));
        }
      }
    }
  } catch (error) {
    if (error.status === 401) empty($("device-list"), "لم يقترن هذا المتصفح بأي كمبيوتر بعد.");
    else text("device-error", error.message);
  } finally { $("devices-refresh").disabled = false; }
}
$("devices-refresh").onclick = () => { text("device-error", ""); loadDevices(); };

$("internet-pair-form").onsubmit = async (event) => {
  event.preventDefault();
  if (ui.updateLocked || !pairingTicket || pairClaim) return;
  pairingBusy = true;
  $("internet-pair-button").disabled = true;
  text("internet-pair-error", "");
  try {
    const ticket = pairingTicket;
    pairingTicket = "";
    const result = await api("/api/pair/start", { ticket, phone_name: $("phone-name").value.trim() });
    pairClaim = result.claim_id;
    text("pair-device-name", result.device_name);
    text("pair-match-code", result.match_code);
    $("internet-pair-form").hidden = true;
    $("pair-waiting").hidden = false;
    await pollPairStatus();
  } catch (error) {
    // Pairing is a one-use operation: an uncertain request is never replayed.
    pairingTicket = "";
    text("internet-pair-error", error.message + (error.status === 409
      ? " راجع حالة الاقتران في ورق على الكمبيوتر قبل المحاولة من جديد."
      : " راجع إعدادات الاقتران على الكمبيوتر، ثم امسح رمز QR جديدًا للمحاولة مجددًا."));
  } finally { pairingBusy = false; }
};
async function pollPairStatus() {
  if (!pairClaim || pairStatusBusy || document.hidden || !pageActive) return;
  pairStatusBusy = true;
  try {
    const result = await api("/api/pair/status/" + encodeURIComponent(pairClaim));
    if (!pageActive) return;
    text("internet-pair-error", "");
    if (result.status === "approved" && result.device_id) {
      pairClaim = null;
      location.replace("/devices/" + encodeURIComponent(result.device_id));
    } else if (result.status !== "pending") {
      pairClaim = null;
      text("pair-waiting-message", "لم يكتمل الاقتران. راجع الطلب والاقتران الحالي على الكمبيوتر قبل إنشاء رمز QR جديد.");
    }
  } catch (error) {
    text("internet-pair-error", error.message);
    if (error.status && error.status < 500) pairClaim = null;
  } finally { pairStatusBusy = false; }
}
window.waraqPwaBridge = {
  isBusy: () => ui.pending || ui.preparing || $("confirmation").open || $("file-preview").open || pairingBusy || !!pairClaim,
  setUpdateLock: (locked) => { ui.updateLocked = !!locked; enableActions(); },
  acceptPairLink: async (value) => {
    if (!internetMode || targetDevice || ui.updateLocked || window.waraqPwaBridge.isBusy()) return false;
    let url;
    try { url = new URL(value); } catch { return false; }
    if (url.origin !== location.origin || url.username || url.password || url.pathname !== "/pair" || url.search || !/^[A-Za-z0-9_-]{20,200}$/.test(url.hash.slice(1))) return false;
    pairingTicket = url.hash.slice(1);
    history.replaceState(null, "", "/internet");
    $("internet-home").hidden = true;
    $("internet-pair").hidden = false;
    $("internet-pair-form").hidden = false;
    $("pair-waiting").hidden = true;
    $("internet-pair-button").disabled = false;
    text("internet-pair-error", "");
    $("phone-name").focus();
    return true;
  },
};
async function boot() {
  if (!internetMode) {
    try { internetMode = (await api("/api/bootstrap")).mode === "internet"; }
    catch { /* Older LAN servers intentionally have no bootstrap endpoint. */ }
  }
  ready = true;
  if (!internetMode) { await poll(); return; }
  text("connection", "بوابة الإنترنت");
  text("logout", "إلغاء اقتران هذا الكمبيوتر");
  $("login").hidden = true;
  if (location.pathname.replace(/\/$/, "") === "/pair") {
    $("internet-pair").hidden = false;
    if (!pairingTicket) {
      $("internet-pair-button").disabled = true;
      text("internet-pair-error", "رابط الاقتران غير مكتمل. امسح رمز QR جديدًا من إعدادات ورق على الكمبيوتر.");
    }
  } else if (targetDevice) {
    $("device-context").hidden = false;
    text("device-name", "كمبيوتر ورق · " + targetDevice.slice(0, 8));
    await loadDevices();
    await poll();
  } else {
    $("internet-home").hidden = false;
    await loadDevices();
  }
}
