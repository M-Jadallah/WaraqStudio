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
  preparing: false,
  polling: false,
  limit: 60,
  fileLimit: 60,
  sourceRevision: null,
  selected: new Set(),
  signatures: new WeakMap(),
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
          ui.pending || !ui.online || (ui.preparing && button.id === "upload")),
    );
  $("discover").disabled = ui.pending || !ui.online || !!ui.state?.source.busy;
}
function showLogin() {
  ui.state = null;
  ui.online = false;
  $("workspace").hidden = true;
  $("navigation").hidden = true;
  $("logout").hidden = true;
  $("login").hidden = internetMode;
  $("connection").classList.remove("online");
  text("connection", "يلزم الاقتران");
  for (const id of ["jobs", "workers", "services", "files", "source-items"])
    $(id).replaceChildren();
  ui.sourceRevision = null;
  ui.selected.clear();
  if (internetMode) {
    $("device-context").hidden = true;
    $("internet-home").hidden = false;
    text("device-error", "يلزم اقتران هذا الهاتف بهذا الكمبيوتر. افتح إعدادات ورق على الكمبيوتر وامسح رمز QR جديدًا.");
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
        (path === "/api/state" || path.startsWith("/api/devices/")) && pageActive)
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
  if (!pageActive || ui.pending || !ui.online) return false;
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
      if (node?.contains(document.activeElement)) return;
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
function jobCard(job) {
  const card = el("article", "job"),
    top = el("div", "card-top");
  top.append(
    el("h3", "", job.title),
    el("span", "badge " + job.status, job.statusLabel),
  );
  card.append(top);
  card.append(
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
    card.append(progress);
  }
  if (job.retryAt)
    card.append(el("div", "meta", "المحاولة القادمة: " + job.retryAt));
  if (job.error) {
    const details = el("details");
    details.append(el("summary", "", "عرض سبب التوقف"), el("p", "", job.error));
    card.append(details);
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
  if (actions.childElementCount) card.append(actions);
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
      const card = el("article", "service"),
        top = el("div", "card-top");
      top.append(
        el("h3", "", labels[service.stage]),
        el("span", "badge", service.paused ? "متوقفة يدويًا" : "تستقبل المهام"),
      );
      card.append(
        top,
        actionButton(
          service.paused ? "استئناف الخدمة" : "إيقاف بعد المهام الحالية",
          () =>
            mutate("stage", { stage: service.stage, paused: !service.paused }),
        ),
      );
      return card;
    },
  );
  reconcile(
    $("workers"),
    ui.state.workers,
    (w) => w.id,
    (worker) => {
      const card = el("article", "worker"),
        top = el("div", "card-top");
      const state = worker.job_id
        ? "يعمل · " + worker.elapsed + " ث"
        : worker.error
          ? "يحتاج إجراء"
          : !worker.enabled
            ? "معطّل"
            : worker.paused
              ? "متوقف"
              : "جاهز";
      top.append(el("h3", "", worker.name), el("span", "badge", state));
      card.append(
        top,
        el(
          "p",
          "meta",
          [worker.stageLabel, worker.providerLabel, worker.modelResolved]
            .filter(Boolean)
            .join(" · "),
        ),
      );
      if (worker.cooldownSeconds > 0)
        card.append(
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
        card.append(detail);
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
        const card = el("article", "result");
        card.append(
          el("h3", "", job.title),
          el(
            "p",
            "meta",
            [job.template, job.folder_name].filter(Boolean).join(" · "),
          ),
          fileLinks(job),
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
  try {
    await api(internetMode ? devicePath("/logout") : "/api/logout", {});
    if (internetMode) location.replace("/internet");
    else showLogin();
  } catch (error) {
    toast(error.message, true);
  }
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
  if (ui.preparing || ui.pending) return;
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
}, 2500);

async function loadDevices() {
  $("devices-refresh").disabled = true;
  try {
    const result = await api("/api/devices");
    if (!pageActive) return;
    $("device-list").replaceChildren();
    for (const device of result.devices || []) {
      const card = el("article", "result");
      const link = el("a", "device-link", device.name || device.device_name || "كمبيوتر ورق");
      link.href = "/devices/" + encodeURIComponent(device.device_id);
      card.append(link, el("p", "muted", device.online ? "متصل" : "غير متصل — أبقِ ورق مفتوحًا على هذا الكمبيوتر"));
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
  if (!pairingTicket || pairClaim) return;
  $("internet-pair-button").disabled = true;
  text("internet-pair-error", "");
  try {
    const result = await api("/api/pair/start", { ticket: pairingTicket, phone_name: $("phone-name").value.trim() });
    pairingTicket = "";
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
  }
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
