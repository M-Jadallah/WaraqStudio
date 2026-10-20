import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: win
    objectName: "mainWindow"
    width: 1380
    height: 900
    minimumWidth: 1060
    minimumHeight: 720
    visible: true
    title: "ورق | Waraq Studio"
    property int page: 0
    property var settings: JSON.parse(backend.settingsJson)
    readonly property string savedOutputRoot: JSON.parse(backend.settingsJson).output_root || ""
    readonly property var queueSnapshot: JSON.parse(backend.jobsJson)
    readonly property var appUpdateState: backend.appUpdate || ({status: "idle", version: "", progress: 0, message: "", hasToken: false})
    readonly property var remoteState: backend.remoteAccess || ({enabled: false, port: 8765, urls: [], code: "", qr: "", clients: 0, error: ""})
    readonly property var internetState: backend.internetAccess || ({registered: false, connected: false, busy: false, name: "", url: "", status: "متوقف", error: "", pairUrl: "", pendingPhones: [], phones: []})
    property bool dark: settings.theme !== "light"
    readonly property color bg: visualTheme.bg
    readonly property color panel: visualTheme.panel
    readonly property color line: visualTheme.line
    readonly property color ink: visualTheme.ink
    readonly property color muted: visualTheme.muted
    readonly property color accent: visualTheme.accent
    Theme { id: visualTheme; dark: win.dark; scale: win.uiScale }
    readonly property real contentUnits: (width - navRail.width - 32) / uiScale
    readonly property bool shortOperations: page === 0 && height / uiScale < 600
    readonly property int serviceColumns: contentUnits >= 1100 ? 3 : contentUnits >= 850 ? 2 : 1
    property int sourceTab: 0
    property int settingsTab: 0
    property string toastText: ""
    property bool toastError: false
    property var selectedJob: ({})
    property var bulkGroup: ({})
    property var editingWorker: ({})
    property string workerKind: ""
    property int workerIndex: 0
    property string filter: "all"
    property var selectedQueueIds: []
    property string search: ""
    property string sourceSearch: ""
    property string fileKind: "all"
    property var pendingFile: ({})
    property var pendingProvider: ({})
    property var pendingAccount: ({})
    readonly property var accountLoginChoice: backend.accountLoginChoice || ({})
    property var lastForce: ({provider: "", model: "", variant: ""})
    property bool skipForceQuestion: false
    property var pageNames: ["مركز العمليات", "إضافة مصدر", "العمال والخدمات", "مكتبة النتائج", "الملفات", "الإعدادات"]
    property var pageIcons: ["home", "plus", "workers", "library", "folder", "settings"]
    property var pageDescriptions: ["تابع المهام من المصدر إلى المستند", "اختر المصدر وجهّز خيارات المعالجة", "أدر العمال وسعة الخدمات", "مخرجات دروسك في مكان واحد", "تصفح ملفات المكتبة ومجلداتها", "خصص مساحة العمل وخيارات المعالجة"]
    readonly property real uiScale: backend.uiScale
    // List density: rows, paddings and gaps shrink together; fonts stay on fs().
    readonly property real density: settings.ui_compact === true ? 0.72 : 1.0
    property bool railOpen: settings.ui_rail_collapsed !== true
    readonly property bool narrowHeader: width - navRail.width < 960 * uiScale
    function fs(n) { return visualTheme.fs(n) }
    function rh(n) { return Math.max(20, Math.round(n * uiScale * density)) }
    function rs(n) { return Math.max(2, Math.round(n * density)) }
    function usageDate(value) {
        return typeof value === "number" && isFinite(value) && value > 0
            ? new Date(value * 1000).toLocaleString(Qt.locale(), "dd/MM/yyyy hh:mm") : "غير معروف"
    }
    function usageWindow(label, value) {
        value = value || {}
        var known = ["available", "stale"].indexOf(value.status) >= 0 && typeof value.remainingPercent === "number" && isFinite(value.remainingPercent)
        return label + " · المتبقي: " + (known ? value.remainingPercent + "٪" : "غير معروف")
            + (value.status === "stale" ? " (قراءة قديمة)" : "") + " · موعد التجدد (توقيت الجهاز): " + win.usageDate(value.resetsAt)
    }
    function usageMessage(usage, refreshing) {
        usage = usage || {}
        var messages = {unsupported: "قراءة الحصة غير مدعومة لطريقة الدخول الحالية.",
            unsupported_method: "قراءة الحصة غير مدعومة لطريقة الدخول الحالية.",
            signed_out: "سجّل الدخول لقراءة الحصة.", auth_required: "يلزم تجديد تسجيل الدخول لقراءة الحصة.",
            authentication_required: "يلزم تجديد تسجيل الدخول لقراءة الحصة.",
            network_error: "تعذّر الاتصال لقراءة الحصة. يمكنك إعادة المحاولة.",
            rate_limited: "خدمة الحصص طلبت الانتظار قبل إعادة المحاولة.",
            timeout: "انتهت مهلة قراءة الحصة. يمكنك إعادة المحاولة.",
            invalid_response: "تعذّر تفسير بيانات الحصة.", not_fetched: "لم تُقرأ الحصة بعد.", not_checked: "لم تُقرأ الحصة بعد.",
            token_sharing_unsupported: "قراءة الحصة غير مدعومة لتسجيل Sign in with ChatGPT الحالي.",
            method_unsupported: "قراءة الحصة غير مدعومة لطريقة الدخول الحالية.",
            native_version_unverified: "لم يُتحقق من دعم قراءة الحصة في إصدار OpenCode المثبّت.",
            usage_available: "", usage_partial: "بعض نوافذ الحصة غير متاحة.",
            windows_missing: "لم تُرجع الخدمة نوافذ الحصة.", usage_windows_unavailable: "نوافذ الحصة غير متاحة.",
            usage_read_failed: "تعذّرت قراءة الحصة. أعد المحاولة لاحقًا.",
            usage_unavailable: "الحصة غير متاحة حاليًا.",
            usage_cancelled: "أُلغي تحديث الحصة.", cancelled: "أُلغي تحديث الحصة.",
            usage_refresh_deferred: "تحديث الحصة مؤجّل حتى يصبح الحساب متاحًا.",
            account_not_ready: "الحساب غير جاهز لقراءة الحصة.",
            usage_runtime_unsupported: "إصدار OpenCode الحالي لا يدعم قراءة الحصة.",
            legacy_supported: "طريقة الدخول تدعم قراءة الحصة.",
            response_too_large: "تعذّر قبول استجابة خدمة الحصص.",
            unauthorized: "يلزم تجديد تسجيل الدخول لقراءة الحصة.",
            endpoint_unavailable: "قراءة الحصة غير مدعومة حاليًا لدى الخدمة.",
            throttled: "خدمة الحصص طلبت الانتظار قبل إعادة المحاولة.",
            http_error: "تعذّرت قراءة الحصة من الخدمة.",
            connection_failed: "تعذّر الاتصال بخدمة الحصص.",
            binding_invalid: "تغيّر تسجيل الحساب؛ يلزم التحقق من الدخول.",
            runtime_changed: "تغيّرت جلسة OpenCode؛ أعد تحديث الحصة.",
            identity_unavailable: "تعذّر التحقق من هوية الحساب لقراءة الحصة.",
            expired_pending_idle: "انتهت صلاحية الدخول؛ يُؤجّل تجديده حتى اكتمال مهام الحساب.",
            binding_changed: "تغيّرت هوية الحساب؛ أعد تحديث الحصة.",
            native_unavailable: "OpenCode غير متاح لقراءة الحصة.",
            maintenance_unreserved: "تحديث الحصة مؤجّل حتى يصبح الحساب متاحًا.",
            runtime_unavailable: "جلسة OpenCode غير متاحة لقراءة الحصة.",
            account_busy: "تحديث الحصة مؤجّل حتى اكتمال مهام الحساب.",
            activity_unverified: "يلزم التحقق من توقف جلسة الحساب السابقة.",
            native_refreshed: "جُدّد الدخول؛ يمكن تحديث الحصة.",
            refresh_unavailable: "تعذّر تجديد الدخول لقراءة الحصة.",
            runtime_pending_idle: "تحديث الحصة مؤجّل حتى اكتمال مهام الحساب."}
        var message = messages[usage.messageCode] || (usage.status === "unsupported" ? messages.unsupported
            : usage.status === "available" ? "" : "الحصة غير معروفة حاليًا؛ أعد المحاولة لاحقًا.")
        return (refreshing ? "جارٍ تحديث الحصة… " : "") + (usage.stale ? "قراءة قديمة. " : "") + message
    }
    function usageRefreshReason(usage) {
        usage = usage || {}
        var reasons = {refreshing: "تحديث الحصة جارٍ بالفعل.",
            managed_accounts_disabled: "فعّل استخدام حسابات ChatGPT المُدارة لتحديث الحصة.",
            account_not_ready: "الحساب غير جاهز؛ تحقق من تسجيل الدخول وحالة الجلسة.",
            usage_unsupported: "لا تدعم طريقة الدخول أو نسخة OpenCode الحالية قراءة الحصة.",
            usage_closed: "أُوقف تحديث الحصص أثناء إغلاق التطبيق.", usage_unavailable: "تحديث الحصة غير متاح حاليًا."}
        return usage.refreshBlockReason === "refresh_wait" ? "يمكن إعادة التحديث بعد " + (usage.refreshWaitSeconds || 0) + " ثانية."
            : reasons[usage.refreshBlockReason] || ""
    }
    function transcriptTemplateId() { return backend.templates.length > 0 ? (templateBox.currentValue || "") : "" }
    function chooseSourceOutputFolder() {
        var path = backend.choosePath("folder")
        if (!path) return
        var templateId = win.transcriptTemplateId()
        var folderId = sourceFolderCombo.currentValue || ""
        var updated = JSON.parse(backend.settingsJson)
        updated.output_root = path
        if (backend.saveSettings(JSON.stringify(updated))) {
            // Saving defaults republishes the template model; keep the user's
            // chosen template and subject when the ComboBoxes settle.
            Qt.callLater(function() {
                templateBox.currentIndex = Math.max(0, backend.templates.findIndex(function(t) { return t.id === templateId }))
                sourceFolderCombo.currentIndex = Math.max(0, backend.folders.findIndex(function(f) { return f.id === folderId }))
            })
        }
    }
    function hasLocalTranscript(urls) {
        for (var i = 0; i < urls.length; i++) {
            if (/^file:\/\/.*\.txt$/i.test(urls[i].toString())) return true
        }
        return false
    }
    color: bg
    palette.window: panel
    palette.windowText: ink
    palette.text: ink
    palette.base: visualTheme.inset
    palette.button: visualTheme.inset
    palette.buttonText: ink
    palette.highlight: accent
    palette.highlightedText: visualTheme.accentInk
    onClosing: function(close) { close.accepted = backend.prepareClose() }
    font.family: visualTheme.fontFamily
    font.weight: visualTheme.fontWeight
    font.pixelSize: win.fs(14)

    function matches(job) {
        return (filter === "all" || job.status === filter || (filter === "attention" && ["blocked", "failed", "uncertain"].indexOf(job.status) >= 0))
            && (search.length === 0 || job.title.toLowerCase().indexOf(search.toLowerCase()) >= 0)
    }
    function visibleQueueIds() {
        return win.queueSnapshot.filter(function(job) { return win.matches(job) }).map(function(job) { return job.id })
    }
    function visibleQueueSelectionCount() {
        return win.visibleQueueIds().filter(function(id) { return win.selectedQueueIds.indexOf(id) >= 0 }).length
    }
    function setQueueSelected(id, selected) {
        var next = win.selectedQueueIds.filter(function(value) { return value !== id })
        if (selected) next.push(id)
        win.selectedQueueIds = next
    }
    function selectVisibleQueue(selected) {
        var visibleIds = win.visibleQueueIds()
        var next = win.selectedQueueIds.filter(function(id) { return visibleIds.indexOf(id) < 0 })
        win.selectedQueueIds = selected ? next.concat(visibleIds) : next
    }
    function reconcileQueueSelection() {
        var existing = win.queueSnapshot.map(function(job) { return job.id })
        var next = win.selectedQueueIds.filter(function(id) { return existing.indexOf(id) >= 0 })
        if (next.length !== win.selectedQueueIds.length) win.selectedQueueIds = next
    }
    function deleteQueueSelection() {
        backend.deleteJobs(win.selectedQueueIds.slice())
        win.reconcileQueueSelection()
    }
    function attentionGroups() {
        var groups = {}
        win.queueSnapshot.forEach(function(job) {
            if (["failed", "blocked", "uncertain"].indexOf(job.status) < 0 || !win.matches(job)) return
            var key = job.current + ":" + job.status
            if (!groups[key]) groups[key] = {stage: job.current, status: job.status,
                label: job.stageLabel + " · " + job.statusLabel, ids: []}
            groups[key].ids.push(job.id)
        })
        return Object.keys(groups).map(function(key) { return groups[key] })
    }
    function stateColor(status) {
        if (status === "completed") return visualTheme.success
        if (status === "running") return visualTheme.info
        if (["failed", "blocked", "uncertain"].indexOf(status) >= 0) return visualTheme.warning
        return muted
    }
    function save() { return backend.saveSettings(JSON.stringify(settings)) }
    function resolvedModel(provider, model) {
        if (!provider || provider === "login" || provider === "custom") return model
        return model.indexOf(provider + "/") === 0 ? model : provider + "/" + model
    }
    function workerLine(w) {
        if (w.stage === "download") return ""
        if (w.stage === "deepgram") return (w.role === "backup" ? "احتياطي · " : "أساسي · ") +
            (w.hasKey ? "المفتاح محفوظ في مخزن الاعتماد" : "لا مفتاح محفوظ لهذا العامل")
        var parts = [w.providerLabel, w.modelResolved]
        if (w.accountName) parts.push("الحساب: " + w.accountName)
        if (w.accountStatus) parts.push(({running: "قيد التنفيذ", waiting: "بانتظار حساب متاح", cooldown: "بانتظار تجدد الحد"})[w.accountStatus] || w.accountStatus)
        if ((w.variant || "").length > 0) parts.push("تفكير " + w.variant)
        parts.push(!w.keyEnv ? "حسابات ChatGPT في ورق" : (w.hasKey ? "المفتاح محفوظ" : "لا مفتاح محفوظ"))
        return parts.join("  ·  ")
    }
    function providerName(id) {
        var info = backend.providers.find(function(p) { return p.id === id })
        return info ? info.name : id
    }
    function forceSummary() {
        if (!lastForce.provider) return "بدون فرض على عمال OpenCode"
        var text = "مفروض: " + win.providerName(lastForce.provider) + "  ·  " + win.resolvedModel(lastForce.provider, lastForce.model)
        return lastForce.variant ? text + "  ·  تفكير " + lastForce.variant : text
    }
    function openForceDialog(starting) {
        forceDialog.starting = starting
        var ids = backend.providers.map(function(p) { return p.id })
        var provider = lastForce.provider || (settings.opencode_workers[0] ? (settings.opencode_workers[0].provider || "login") : "login")
        forceProvider.currentIndex = Math.max(0, ids.indexOf(provider))
        var source = settings.opencode_workers.find(function(w) { return (w.provider || "login") === provider }) || settings.opencode_workers[0] || {}
        forceDialog.modelDraft = lastForce.provider ? lastForce.model : (source.model || "")
        forceModel.editText = forceDialog.modelDraft
        forceVariant.text = lastForce.provider ? lastForce.variant : (source.variant || "")
        forceDialog.open()
    }
    function applyForce(force, start) {
        var provider = force ? (forceProvider.currentValue || "") : ""
        lastForce = force ? {provider: provider, model: forceModel.editText.trim(), variant: forceVariant.text}
                          : {provider: "", model: "", variant: ""}
        backend.setProviderOverride(lastForce.provider, lastForce.model, lastForce.variant)
        forceDialog.close()
        if (start) backend.toggleRunning()
    }
    function runClicked() {
        if (backend.running) { backend.toggleRunning(); return }
        if (win.skipForceQuestion) { backend.toggleRunning(); return }
        win.openForceDialog(true)
    }
    function kindLabel(kind) { return kind === "document" ? "Word" : kind === "transcript" ? "تفريغ" : "صوت" }
    function fileSize(bytes) {
        if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " م.ب"
        if (bytes >= 1024) return Math.round(bytes / 1024) + " ك.ب"
        return bytes + " بايت"
    }
    function opencodeModelOptions(provider, saved) {
        var options = backend.opencodeModels.filter(function(id) {
            return !provider || provider === "login" || id.indexOf(provider + "/") === 0
        })
        if (saved && options.indexOf(saved) < 0) options.unshift(saved)
        return options
    }
    function deepgramModelOptions(saved) {
        var options = backend.deepgramModels.map(function(item) { return item.id })
        if (saved && options.indexOf(saved) < 0) options.unshift(saved)
        return options
    }
    function deepgramLanguageOptions(model, saved) {
        var aliases = {"nova-3": "nova-3-general", "nova-2": "nova-2-general", "nova": "nova-general"}
        var canonical = aliases[model] || model
        var entry = backend.deepgramModels.find(function(item) { return item.id === canonical })
        var codes = ["auto"].concat(entry ? entry.languages : [])
        if (saved && codes.indexOf(saved) < 0) codes.push(saved)
        return codes.map(function(code) {
            var labels = {"auto": "تحديد تلقائي", "ar": "العربية", "en": "الإنجليزية", "multi": "متعدد اللغات"}
            return {id: code, name: (labels[code] || code) + (labels[code] ? " (" + code + ")" : "")}
        })
    }
    function selectWorkerLanguage() {
        var choices = workerLanguage.model
        var index = choices.findIndex(function(item) { return item.id === workerDialog.languageDraft })
        workerLanguage.currentIndex = index < 0 ? 0 : index
        workerDialog.languageDraft = workerLanguage.currentValue || "auto"
    }
    function fillWorkerFields() {
        workerName.text = editingWorker.name
        workerDialog.modelDraft = editingWorker.model
        workerDialog.languageDraft = editingWorker.language || "auto"
        workerModel.editText = editingWorker.model
        workerExtra.text = editingWorker.variant || ""
        win.selectWorkerLanguage()
        workerProject.text = workerKind === "deepgram" ? editingWorker.project : editingWorker.agent
        workerRole.currentIndex = editingWorker.role === "backup" ? 1 : 0
        endpoint.currentIndex = Math.max(0, ["https://api.deepgram.com", "https://api.eu.deepgram.com", "https://api.au.deepgram.com"].indexOf(editingWorker.endpoint))
        diarize.checked = !!editingWorker.diarize
        var ids = backend.providers.map(function(p) { return p.id })
        workerProvider.currentIndex = Math.max(0, ids.indexOf(editingWorker.provider || "login"))
        workerEnabled.checked = editingWorker.enabled || false
        apiKey.text = ""
    }
    function editWorker(worker, kind) {
        workerKind = kind
        var arr = kind === "deepgram" ? settings.deepgram_workers : settings.opencode_workers
        workerIndex = arr.findIndex(function(w) { return w.id === worker.id })
        editingWorker = JSON.parse(JSON.stringify(arr[workerIndex]))
        workerDialog.open()
    }
    function openJob(jobId) {
        selectedJob = backend.jobById(String(jobId))
        jobDialog.open()
        Qt.callLater(win.updateJobLog)
    }
    // Set imperatively instead of a binding: backend ticks append at the end,
    // and rewriting the bound text used to reset the log view to the top.
    function updateJobLog() {
        if (!jobDialog.opened)
            return
        var text = backend.logs.filter(function(e){return e.job_id === selectedJob.id})
            .map(function(e){return e.time + "  " + e.message}).join("\n\n")
        if (text === jobLog.text)
            return
        var bar = logScroll.ScrollBar.vertical
        var atEnd = !bar || bar.position >= bar.positionMaximum - bar.size - 0.01
        var pos = bar ? bar.position : 0
        jobLog.text = text
        if (bar)
            bar.position = atEnd ? Math.max(0, bar.positionMaximum - bar.size)
                                 : Math.min(pos, Math.max(0, bar.positionMaximum - bar.size))
    }
    function openTemplates() { templatesDialog.open() }
    property var templ: []
    function loadTemplateFields() {
        if (tplBox.currentIndex >= 0 && tplBox.currentIndex < templ.length) {
            tplName.text = templ[tplBox.currentIndex].name
            tplText.text = templ[tplBox.currentIndex].text
        } else { tplName.text = ""; tplText.text = "" }
    }
    function commitTemplateFields() {
        if (tplBox.currentIndex >= 0 && tplBox.currentIndex < templ.length) {
            if (tplName.text.trim().length > 0) templ[tplBox.currentIndex].name = tplName.text.trim()
            templ[tplBox.currentIndex].text = tplText.text
        }
    }
    function reloadTemplates() {
        templ = JSON.parse(JSON.stringify(backend.templates))
        // Run after the popup content exists; dialog content is created on open.
        Qt.callLater(function() {
            tplBox.currentIndex = templ.length ? 0 : -1
            loadTemplateFields()
        })
    }

    Connections {
        target: backend
        function onNotice(message, error) { toastText = message; toastError = error; toastTimer.restart() }
        function onSettingsChanged() { settings = JSON.parse(backend.settingsJson); rawSettings.text = backend.settingsJson }
        function onAutoStopReached() {
            toastText = "اكتمل تنفيذ الطابور؛ سيُغلق التطبيق تلقائيًا."
            toastError = false
            autoStopTimer.restart()
        }
        function onChanged() {
            if (jobDialog.opened && selectedJob.id) {
                var currentJob = backend.jobById(String(selectedJob.id))
                if (currentJob.id) selectedJob = currentJob
                win.updateJobLog()
            }
        }
        function onJobsChanged() { win.reconcileQueueSelection() }
    }
    Connections {
        target: win
        // The file page reads the disk on demand, not on the 700ms UI timer.
        function onPageChanged() { if (win.page === 4) backend.refreshLibrary() }
    }
    Timer { id: toastTimer; interval: 9000; onTriggered: toastText = "" }
    Timer { id: autoStopTimer; interval: 2500; onTriggered: Qt.quit() }

    component LabelText: Text {
        color: win.ink
        font.family: visualTheme.fontFamily
        font.weight: visualTheme.fontWeight
        font.pixelSize: win.fs(14)
        horizontalAlignment: Text.AlignRight
        elide: Text.ElideRight
    }
    component SubText: LabelText { color: win.muted; font.pixelSize: win.fs(12) }
    component Box: Rectangle { color: win.panel; radius: 10; border.color: win.line }
    component AppButton: WButton { theme: visualTheme; uiScale: win.uiScale; foreground: win.ink; surface: visualTheme.inset }
    component AppField: Field { theme: visualTheme; uiScale: win.uiScale; fieldBackground: visualTheme.inset; fieldText: win.ink }
    component AppCombo: WComboBox { theme: visualTheme }
    component AppCheck: WCheckBox { theme: visualTheme }
    component AppSwitch: WSwitch { theme: visualTheme }
    component AppProgress: WProgressBar { theme: visualTheme }
    component AppScroll: WScrollBar { theme: visualTheme }
    component AppSlider: WSlider { theme: visualTheme }
    component AppDialog: WDialog { theme: visualTheme }
    component ActionRow: Flow {
        Layout.fillWidth: true
        Layout.preferredHeight: childrenRect.height
        spacing: 8
        layoutDirection: Qt.RightToLeft
    }
    component Rule: Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: win.line }
    component TableHead: Rectangle { Layout.fillWidth: true; implicitHeight: Math.round(36 * win.uiScale); color: visualTheme.inset; radius: 6 }
    component ServiceGroup: Box {
        id: group
        required property string stage
        required property string heading
        required property string symbol
        ColumnLayout {
            anchors.fill: parent; anchors.margins: 12; spacing: 10
            RowLayout {
                Layout.fillWidth: true
                SubText { text: backend.workers.filter(function(w) { return w.stage === group.stage }).length + " عامل" }
                Item { Layout.fillWidth: true }
                LabelText { text: group.heading; font.weight: visualTheme.fontWeight; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                Image { source: "icons/" + group.symbol + (win.dark ? "" : "-active") + ".svg"; Layout.preferredWidth: 24; Layout.preferredHeight: 24 }
            }
            Rule {}
            PinList {
                id: groupedWorkers
                objectName: "workers_" + group.stage
                Layout.fillWidth: true; Layout.fillHeight: true
                model: backend.workersModel
                ScrollBar.vertical: AppScroll {}
                delegate: Rectangle {
                    objectName: "workerCard"
                    width: groupedWorkers.width
                    height: row.stage === group.stage ? workerContent.implicitHeight + 2 * win.rs(10) + win.rs(8) : 0
                    visible: height > 0
                    color: visualTheme.inset; radius: 6
                    ColumnLayout {
                        id: workerContent
                        objectName: "workerContent"
                        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                        anchors.margins: win.rs(10); spacing: win.rs(6)
                        LabelText { text: row.name; font.weight: visualTheme.fontWeight; Layout.fillWidth: true }
                        SubText {
                            objectName: "workerStatus"
                            Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone
                            text: row.job_id ? "يعالج " + row.job_id + "  ·  " + row.elapsed + " ثانية"
                                : (!row.enabled ? "معطل"
                                : row.paused ? "الاستقبال متوقف"
                                : row.cooldownSeconds > 0 ? "تهدئة تقييد YouTube — يستأنف التنزيل تلقائيًا بعد " + Math.ceil(row.cooldownSeconds / 60) + " دقيقة"
                                : row.stageLabel + "  ·  جاهز لاستقبال مهمة")
                        }
                        SubText { objectName: "workerDetails"; text: win.workerLine(row); visible: text.length > 0; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; elide: Text.ElideNone }
                        SubText { objectName: "workerError"; text: row.error || ""; color: visualTheme.warning; visible: !!row.error; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; elide: Text.ElideNone }
                        AppSwitch {
                            objectName: "workerEnableBox"
                            text: "يستقبل مهامًا"
                            visible: row.stage !== "download"
                            checked: !!row.enabled
                            onClicked: backend.setWorkerEnabled(row.id, checked)
                            Accessible.name: "تفعيل العامل " + row.name
                            Layout.fillWidth: true
                        }
                        ActionRow {
                            AppButton { text: "إعدادات"; implicitHeight: 28; visible: row.stage !== "download"; onClicked: win.editWorker({id: row.id}, row.stage) }
                            AppButton { text: row.paused ? "استئناف الخدمة" : "إيقاف الخدمة"; implicitHeight: 28; onClicked: backend.pauseStage(row.stage, !row.paused) }
                            AppButton { text: "إعادة فحص العامل"; implicitHeight: 28; visible: !!row.error; onClicked: row.stage === "download" ? backend.recheckStage(row.stage) : backend.resetWorker(row.id) }
                        }
                    }
                }
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0
        ColumnLayout {
            Layout.minimumWidth: 0
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: headerContent.implicitHeight + 20
                color: win.bg
                ColumnLayout {
                    id: headerContent
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                    anchors.leftMargin: 16; anchors.rightMargin: 16; anchors.topMargin: 10
                    spacing: 7
                    RowLayout {
                        Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: 12
                        SubText { text: backend.running ? "● المحرك يعمل" : "○ المحرك متوقف"; color: backend.running ? visualTheme.success : win.muted }
                        SubText {
                            id: operationSummary
                            objectName: "operationOptionsSummary"
                            Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.preferredWidth: 1
                            text: win.internetState.error ? "تعذّر ربط الهاتف: " + win.internetState.error
                                : [win.lastForce.provider ? win.forceSummary() : "", autoStopBox.checked ? "إغلاق تلقائي مفعّل" : "", "الهاتف: " + win.internetState.status].filter(function(value) { return value.length > 0 }).join(" · ")
                            color: win.internetState.error ? visualTheme.warning : win.muted
                            activeFocusOnTab: true
                            ToolTip.visible: summaryHover.hovered || activeFocus
                            ToolTip.text: text
                            HoverHandler { id: summaryHover }
                            Accessible.name: text
                        }
                        LabelText { text: win.pageNames[win.page]; font.pixelSize: win.fs(20); font.weight: visualTheme.fontWeight; Layout.minimumWidth: 0; Layout.maximumWidth: (win.width - navRail.width - 32) * 0.4 }
                    }
                    SubText { text: win.pageDescriptions[win.page]; visible: win.page !== 0; Layout.fillWidth: true }
                    ActionRow {
                        id: operationToolbar
                        objectName: "operationToolbar"
                        AppButton {
                            objectName: "runButton"
                            text: backend.running ? (win.narrowHeader ? "إيقاف بعد الحالية" : "إيقاف بعد المهام الحالية") : "تشغيل الطابور"
                            primary: !backend.running
                            onClicked: win.runClicked()
                            Accessible.name: backend.running ? "إيقاف بعد المهام الحالية" : "تشغيل الطابور"
                            ToolTip.visible: hovered || activeFocus
                            ToolTip.text: Accessible.name
                        }
                        AppButton { objectName: "addSourceButton"; text: "إضافة مصدر"; gold: true; onClicked: win.page = 1 }
                        AppButton { objectName: "exportReportButton"; text: "تقرير Excel"; onClicked: backend.exportReport() }
                        AppButton { id: operationOptionsButton; objectName: "operationOptionsButton"; text: "خيارات التشغيل والاتصال"; onClicked: operationOptionsPopup.open() }
                    }
                }
                Popup {
                    id: operationOptionsPopup
                    objectName: "operationOptionsPopup"
                    parent: Overlay.overlay
                    width: Math.min(win.width - 32, Math.round(520 * win.uiScale))
                    height: Math.min(optionsContent.implicitHeight + 24, win.height - 80)
                    x: Math.max(16, Math.min(win.width - width - 16, operationOptionsButton.mapToItem(parent, 0, 0).x))
                    y: Math.max(16, Math.min(win.height - height - 16, operationOptionsButton.mapToItem(parent, 0, operationOptionsButton.height).y + 6))
                    padding: 12
                    modal: true; focus: true
                    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                    onOpened: forceButton.forceActiveFocus()
                    onClosed: operationOptionsButton.forceActiveFocus()
                    background: Rectangle { color: win.panel; border.color: win.line; radius: 10 }
                    contentItem: ScrollView {
                        id: runOptionsScroll
                        objectName: "operationOptionsScroll"
                        clip: true; contentWidth: availableWidth
                        ScrollBar.vertical: AppScroll {}
                        ColumnLayout {
                            id: optionsContent
                            width: runOptionsScroll.availableWidth
                            spacing: 10
                            LabelText { text: "خيارات التشغيل والاتصال"; font.pixelSize: win.fs(18); Layout.fillWidth: true }
                            AppButton { id: forceButton; objectName: "forceButton"; text: "فرض إعداد على عمال OpenCode"; Layout.fillWidth: true; onClicked: { operationOptionsPopup.close(); win.openForceDialog(false) } }
                            SubText { objectName: "forceSummary"; text: win.forceSummary(); color: win.lastForce.provider ? win.accent : win.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone }
                            AppCheck { id: autoStopBox; objectName: "autoStopBox"; text: "إيقاف التطبيق بعد اكتمال الطابور"; Layout.fillWidth: true; onClicked: backend.setAutoStop(checked); Accessible.name: "إيقاف التطبيق تلقائيًا بعد اكتمال الطابور" }
                            Rule {}
                            AppButton {
                                id: quickInternetButton; objectName: "quickInternetButton"
                                readonly property bool active: win.internetState.enabled === true || win.internetState.connected
                                text: active ? "إيقاف اتصال الإنترنت" : (win.internetState.registered ? "تفعيل اتصال الإنترنت" : "إعداد ربط الهاتف")
                                primary: !active
                                enabled: !win.internetState.busy
                                Layout.fillWidth: true
                                Accessible.name: text
                                onClicked: {
                                    if (active) backend.stopInternet()
                                    else if (win.internetState.registered) backend.startInternet()
                                    else { operationOptionsPopup.close(); win.page = 5 }
                                }
                            }
                            LabelText {
                                objectName: "quickInternetStatus"
                                text: "ربط الهاتف: " + win.internetState.status + (win.internetState.name ? " · " + win.internetState.name : "") + (win.internetState.error ? " · " + win.internetState.error : "")
                                color: win.internetState.error ? visualTheme.warning : (win.internetState.connected ? visualTheme.success : win.muted)
                                Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; elide: Text.ElideNone
                            }
                            AppButton { text: "تم"; Layout.alignment: Qt.AlignLeft; onClicked: operationOptionsPopup.close() }
                        }
                    }
                }
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.line }
            }

            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: 0
                Layout.margins: 16
                Layout.topMargin: win.shortOperations ? 6 : 16
                Layout.bottomMargin: win.shortOperations ? 6 : 16
                currentIndex: win.page

                // Operations
                ColumnLayout {
                    id: operationsPage
                    objectName: "operationsPage"
                    readonly property bool miniStages: win.contentUnits < 1000
                    readonly property real statusWidth: Math.round((miniStages ? 86 : 118) * win.uiScale)
                    readonly property real progressWidth: Math.round((miniStages ? 54 : 76) * win.uiScale)
                    readonly property real stagesWidth: Math.round((miniStages ? 138 : 282) * win.uiScale)
                    readonly property real actionWidth: Math.round(52 * win.uiScale)
                    readonly property real selectionWidth: Math.round(28 * win.uiScale)
                    readonly property real columnGap: Math.round(6 * win.uiScale)
                    spacing: Math.round(8 * win.uiScale)
                    RowLayout {
                        Layout.fillWidth: true; spacing: 8
                        Repeater {
                            model: [
                                {label: "تحتاج انتباهك", value: backend.stats.attention, color: visualTheme.warning},
                                {label: "مكتملة", value: backend.stats.done, color: visualTheme.success},
                                {label: "قيد التنفيذ", value: backend.stats.active, color: visualTheme.info},
                                {label: "إجمالي المهام", value: backend.stats.total, color: win.ink}
                            ]
                            delegate: Box {
                                required property var modelData
                                required property int index
                                objectName: "operationStat-" + index
                                Layout.fillWidth: true; Layout.preferredWidth: 1
                                Layout.preferredHeight: Math.round(40 * win.uiScale)
                                RowLayout {
                                    anchors.fill: parent; anchors.margins: 8; spacing: 6
                                    LabelText { text: modelData.value; font.pixelSize: win.fs(20); color: win.dark ? modelData.color : win.ink }
                                    SubText { text: modelData.label; Layout.fillWidth: true; Layout.minimumWidth: 0; Accessible.name: modelData.label + ": " + modelData.value }
                                }
                            }
                        }
                    }
                    RowLayout {
                        objectName: "queueSearchFilterRow"
                        Layout.fillWidth: true; spacing: 8
                        AppButton {
                            id: bulkRetryButton
                            objectName: "bulkRetryMenuButton"
                            text: "إعادة جماعية"
                            visible: win.filter === "attention"
                            enabled: win.attentionGroups().length > 0
                            onClicked: bulkRetryPopup.open()
                            Popup {
                                id: bulkRetryPopup
                                objectName: "bulkRetryPopup"
                                y: parent.height + 4
                                width: Math.min(420 * win.uiScale, operationsPage.width)
                                height: Math.min(bulkRetryContent.implicitHeight + 20, win.height * 0.55)
                                padding: 10; focus: true; modal: true
                                onClosed: bulkRetryButton.forceActiveFocus()
                                background: Rectangle { color: win.panel; border.color: win.line; radius: 8 }
                                contentItem: ScrollView {
                                    id: bulkRetryScroll
                                    clip: true; contentWidth: availableWidth
                                    ColumnLayout {
                                        id: bulkRetryContent
                                        width: bulkRetryScroll.availableWidth; spacing: 8
                                        Repeater {
                                            model: win.attentionGroups()
                                            delegate: AppButton {
                                                required property var modelData
                                                Layout.fillWidth: true
                                                text: "إعادة " + modelData.label + " (" + modelData.ids.length + ")"
                                                onClicked: { win.bulkGroup = modelData; bulkRetryPopup.close(); bulkRetry.open() }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        AppField { objectName: "queueSearch"; Layout.fillWidth: true; Layout.minimumWidth: 60; placeholderText: "بحث في المهام"; horizontalAlignment: Text.AlignRight; onTextChanged: win.search = text; Accessible.name: "بحث في المهام" }
                        AppCombo {
                            objectName: "queueFilterCombo"
                            visible: win.contentUnits < 850
                            Layout.preferredWidth: Math.round(135 * win.uiScale)
                            model: [{id:"all",label:"الكل"},{id:"running",label:"نشطة"},{id:"completed",label:"مكتملة"},{id:"attention",label:"تحتاج إجراء"}]
                            textRole: "label"; valueRole: "id"
                            currentIndex: Math.max(0, model.findIndex(function(value) { return value.id === win.filter }))
                            onActivated: win.filter = currentValue
                            Accessible.name: "تصفية حالة المهام"
                        }
                        RowLayout {
                            visible: win.contentUnits >= 850; spacing: 6
                            Repeater {
                                model: [{id:"attention",label:"تحتاج إجراء"},{id:"completed",label:"مكتملة"},{id:"running",label:"نشطة"},{id:"all",label:"الكل"}]
                                delegate: AppButton {
                                    required property var modelData
                                    objectName: "queueFilter-" + modelData.id
                                    text: modelData.label; primary: win.filter === modelData.id
                                    onClicked: win.filter = modelData.id
                                }
                            }
                        }
                        LabelText { text: "الطابور"; visible: win.contentUnits >= 1000; font.pixelSize: win.fs(16) }
                    }
                    RowLayout {
                        objectName: "queueSelectionActions"
                        visible: win.selectedQueueIds.length > 0
                        Layout.fillWidth: true; spacing: 8
                        AppButton { objectName: "deleteSelectedJobsButton"; text: "حذف المحدد"; danger: true; enabled: win.selectedQueueIds.length > 0; onClicked: win.deleteQueueSelection() }
                        LabelText { objectName: "selectedJobsCount"; text: "المحدد: " + win.selectedQueueIds.length }
                        SubText {
                            objectName: "queueDeletionNotice"
                            text: "الحذف يزيل السجلات فقط؛ تبقى الملفات محفوظة. يشمل المحدد خارج الفلتر أيضًا."
                            Layout.fillWidth: true; Layout.minimumWidth: 0
                            activeFocusOnTab: true
                            Accessible.name: text
                            ToolTip.visible: deletionHover.hovered || activeFocus
                            ToolTip.text: text
                            HoverHandler { id: deletionHover }
                        }
                    }
                    Box {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        Layout.minimumHeight: Math.round((settings.ui_compact ? 36 : 44) * win.uiScale) + Math.round(36 * win.uiScale) + 2
                        ColumnLayout {
                            anchors.fill: parent; spacing: 0
                            RowLayout {
                                objectName: "queueTableHeader"
                                Layout.fillWidth: true; Layout.preferredHeight: Math.round(36 * win.uiScale)
                                Layout.leftMargin: 10; Layout.rightMargin: 10; spacing: operationsPage.columnGap
                                SubText { text: "عرض"; Layout.maximumWidth: operationsPage.actionWidth; Layout.preferredWidth: operationsPage.actionWidth; horizontalAlignment: Text.AlignHCenter }
                                SubText { text: "الحالة"; Layout.maximumWidth: operationsPage.statusWidth; Layout.preferredWidth: operationsPage.statusWidth }
                                SubText { text: "التقدم"; Layout.maximumWidth: operationsPage.progressWidth; Layout.preferredWidth: operationsPage.progressWidth }
                                SubText { text: "المراحل"; Layout.maximumWidth: operationsPage.stagesWidth; Layout.preferredWidth: operationsPage.stagesWidth }
                                SubText { text: "الفيديو / الملف"; Layout.fillWidth: true; Layout.minimumWidth: 0 }
                                AppCheck {
                                    objectName: "selectAllJobsBox"
                                    Layout.maximumWidth: operationsPage.selectionWidth; Layout.preferredWidth: operationsPage.selectionWidth
                                    text: ""
                                    enabled: win.visibleQueueIds().length > 0
                                    tristate: true
                                    checkState: win.visibleQueueIds().length > 0 && win.visibleQueueSelectionCount() === win.visibleQueueIds().length ? Qt.Checked : win.visibleQueueSelectionCount() > 0 ? Qt.PartiallyChecked : Qt.Unchecked
                                    nextCheckState: function() { return checkState === Qt.Checked ? Qt.Unchecked : Qt.Checked }
                                    onClicked: win.selectVisibleQueue(checkState === Qt.Checked)
                                    Accessible.name: "تحديد الكل في العرض"
                                    ToolTip.visible: hovered || activeFocus
                                    ToolTip.text: "تحديد الكل في العرض. الحذف يزيل السجلات فقط وتبقى الملفات؛ يشمل المحدد خارج الفلتر أيضًا."
                                }
                            }
                            Rectangle { Layout.fillWidth: true; height: 1; color: win.line }
                            PinList {
                                id: jobsList
                                objectName: "jobsList"
                                Layout.fillWidth: true; Layout.fillHeight: true
                                clip: true
                                model: backend.jobsModel
                                ScrollBar.vertical: AppScroll { objectName: "queueScrollBar" }
                                delegate: Rectangle {
                                    id: queueRow
                                    objectName: "queueRow-" + row.id
                                    width: jobsList.width
                                    height: win.matches(row) ? Math.round((settings.ui_compact ? 36 : 44) * win.uiScale) : 0
                                    visible: height > 0
                                    activeFocusOnTab: visible
                                    Accessible.role: Accessible.Button
                                    Accessible.name: row.title + " — " + row.statusLabel
                                    Keys.onReturnPressed: win.openJob(row.id)
                                    color: activeFocus || (row && win.selectedQueueIds.indexOf(row.id) >= 0) || rowMouse.containsMouse ? visualTheme.hover : (index % 2 === 0 ? win.panel : visualTheme.inset)
                                    border.width: activeFocus ? 2 : 0; border.color: win.accent
                                    ToolTip.visible: rowMouse.containsMouse || activeFocus
                                    ToolTip.delay: 700
                                    ToolTip.text: row.title + "\n#" + String(row.position).padStart(3,"0") + " · " + row.video_id + " · " + row.statusLabel + (row.retryAt ? " · محاولة قادمة " + row.retryAt : " · " + (row.worker || row.stageLabel))
                                    MouseArea { id: rowMouse; anchors.fill: parent; hoverEnabled: true; onClicked: win.openJob(row.id); cursorShape: Qt.PointingHandCursor }
                                    RowLayout {
                                        id: queueContent
                                        anchors.fill: parent; anchors.leftMargin: 10; anchors.rightMargin: 10; spacing: operationsPage.columnGap
                                        AppButton { text: "عرض"; Layout.maximumWidth: operationsPage.actionWidth; Layout.preferredWidth: operationsPage.actionWidth; Layout.minimumWidth: 0; implicitHeight: Math.round(28 * win.uiScale); onClicked: win.openJob(row.id); Accessible.name: "عرض تفاصيل " + row.title }
                                        LabelText {
                                            objectName: "queueStatus-" + row.id
                                            text: row.statusLabel; color: win.stateColor(row.status)
                                            Layout.maximumWidth: operationsPage.statusWidth; Layout.preferredWidth: operationsPage.statusWidth; Layout.minimumWidth: 0
                                            font.pixelSize: win.fs(12)
                                        }
                                        ColumnLayout {
                                            Layout.maximumWidth: operationsPage.progressWidth; Layout.preferredWidth: operationsPage.progressWidth; Layout.minimumWidth: 0; spacing: 2
                                            SubText { text: row.progress >= 0 ? Math.round(row.progress) + "٪" : row.status === "running" ? "جارٍ" : "—"; Layout.fillWidth: true; font.pixelSize: win.fs(11) }
                                            AppProgress { Layout.fillWidth: true; from: 0; to: 100; value: Math.max(0, row.progress); indeterminate: row.status === "running" && row.progress < 0; implicitHeight: 5 }
                                        }
                                        Item {
                                            id: stageColumn
                                            objectName: "queueStageColumn-" + row.id
                                            Layout.maximumWidth: operationsPage.stagesWidth; Layout.preferredWidth: operationsPage.stagesWidth
                                            Layout.preferredHeight: stageFlow.implicitHeight
                                            Row {
                                                id: stageFlow
                                                objectName: "queueStages-" + row.id
                                                anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                                                layoutDirection: Qt.RightToLeft
                                                spacing: Math.round(3 * win.uiScale)
                                                Repeater {
                                                    model: row.steps
                                                    delegate: StageBadge {
                                                        required property var modelData
                                                        objectName: "queueStage-" + row.id + "-" + modelData.name
                                                        theme: visualTheme
                                                        step: modelData
                                                        compact: true
                                                        mini: operationsPage.miniStages
                                                    }
                                                }
                                            }
                                        }
                                        Item {
                                            id: taskHeading
                                            objectName: "queueTaskHeading-" + row.id
                                            Layout.fillWidth: true; Layout.minimumWidth: 0
                                            Layout.fillHeight: true
                                            LabelText { id: jobTitle; objectName: "queueTitle-" + row.id; text: row.title; anchors.fill: parent; verticalAlignment: Text.AlignVCenter; maximumLineCount: 1; wrapMode: Text.NoWrap }
                                        }
                                        AppCheck {
                                            objectName: "selectJob-" + row.id
                                            implicitHeight: Math.round(28 * win.uiScale)
                                            Layout.maximumWidth: operationsPage.selectionWidth; Layout.preferredWidth: operationsPage.selectionWidth
                                            checked: !!row && win.selectedQueueIds.indexOf(row.id) >= 0
                                            onClicked: win.setQueueSelected(row.id, checked)
                                            Accessible.name: "تحديد " + row.title
                                        }
                                    }
                                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.line }
                                }
                                ColumnLayout {
                                    anchors.centerIn: parent; spacing: 14; visible: jobsList.count === 0
                                    LabelText { text: "مساحة عملك جاهزة"; font.pixelSize: win.fs(22); Layout.alignment: Qt.AlignHCenter }
                                    SubText { text: "أضف مصدرًا لبدء المعالجة"; Layout.alignment: Qt.AlignHCenter }
                                    AppButton { text: "إضافة المصدر الأول"; primary: true; Layout.alignment: Qt.AlignHCenter; onClicked: win.page = 1 }
                                }
                                SubText { anchors.centerIn: parent; visible: jobsList.count > 0 && win.queueSnapshot.filter(win.matches).length === 0; text: "لا توجد مهام مطابقة للبحث والتصفية" }
                            }
                        }
                    }
                }

                // Source selection
                ScrollView {
                    id: sourceScroll
                    objectName: "sourceScroll"
                    Layout.minimumWidth: 0
                    clip: true
                    contentWidth: availableWidth
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    Flow {
                        layoutDirection: Qt.RightToLeft; spacing: 16
                        id: sourcePage
                        width: sourceScroll.availableWidth

                        Box {
                            id: sourceOptionsPanel
                            width: win.contentUnits >= 850 ? 400 : sourcePage.width
                            height: sourceImportContent.implicitHeight + 2 * win.rs(14)
                            ColumnLayout {
                                id: sourceImportContent
                                anchors.fill: parent; anchors.margins: win.rs(14); spacing: win.rs(8)
                                LabelText { text: "إعدادات الناتج"; font.pixelSize: win.fs(18); Layout.fillWidth: true }
                                SubText { text: "اختر القالب ومجلد الناتج قبل إضافة التفريغات أو فيديوهات YouTube."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                GridLayout {
                                    Layout.fillWidth: true
                                    columns: 2
                                    columnSpacing: win.rs(8); rowSpacing: win.rs(6)
                                    LabelText { text: "قالب البرومبت"; Layout.row: 0; Layout.column: 0 }
                                    AppCombo { id: templateBox; objectName: "templateBox"; model: backend.templates; textRole: "name"; valueRole: "id"; Layout.row: 0; Layout.column: 1; Layout.fillWidth: true; Layout.minimumWidth: 80; Layout.preferredWidth: 200; Accessible.name: "قالب البرومبت" }
                                    LabelText { text: "مجلد المادة / الدرس"; Layout.row: parent.columns === 2 ? 1 : 0; Layout.column: parent.columns === 2 ? 0 : 2 }
                                    RowLayout {
                                        Layout.row: parent.columns === 2 ? 1 : 0; Layout.column: parent.columns === 2 ? 1 : 3
                                        Layout.fillWidth: true; Layout.minimumWidth: 80; spacing: win.rs(8)
                                        AppCombo { id: sourceFolderCombo; objectName: "sourceFolderCombo"; model: backend.folders; textRole: "name"; valueRole: "id"; Layout.fillWidth: true; Layout.minimumWidth: 80; Layout.preferredWidth: 200; Accessible.name: "مجلد المادة أو الدرس" }
                                        AppButton { text: "مجلد جديد"; implicitHeight: win.rh(36); onClicked: folderDialog.open() }
                                    }
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true; spacing: win.rs(8)
                                    AppButton { objectName: "sourceOutputFolderButton"; text: "اختيار المجلد"; implicitHeight: win.rh(36); onClicked: win.chooseSourceOutputFolder() }
                                    AppField { objectName: "sourceOutputFolderPath"; Layout.fillWidth: true; Layout.minimumWidth: 80; text: win.savedOutputRoot; readOnly: true; selectByMouse: true; Accessible.name: "مسار مجلد النتائج الافتراضي" }
                                    LabelText { text: "مجلد النتائج الافتراضي"; font.pixelSize: win.fs(13) }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    AppButton { objectName: "sourceYoutubeTab"; text: "YouTube"; primary: win.sourceTab === 0; Layout.fillWidth: true; onClicked: win.sourceTab = 0 }
                                    AppButton { objectName: "sourceLocalTab"; text: "TXT / صوت"; primary: win.sourceTab === 1; Layout.fillWidth: true; onClicked: win.sourceTab = 1 }
                                }
                                LabelText { visible: win.sourceTab === 1; text: "تفريغات جاهزة إلى Word"; font.weight: visualTheme.fontWeight; Layout.fillWidth: true }
                                ActionRow {
                                    visible: win.sourceTab === 1
                                    AppButton { objectName: "importTranscriptsButton"; text: "إضافة ملفات TXT"; implicitHeight: win.rh(36); onClicked: { if (backend.importTranscripts(win.transcriptTemplateId(), sourceFolderCombo.currentValue || "") > 0) win.page = 0 } }
                                    AppButton { objectName: "importTranscriptFolderButton"; text: "إضافة مجلد تفريغات"; implicitHeight: win.rh(36); onClicked: { if (backend.importTranscriptFolder(win.transcriptTemplateId(), sourceFolderCombo.currentValue || "") > 0) win.page = 0 } }
                                    AppButton { text: "إدارة القوالب"; implicitHeight: win.rh(36); onClicked: win.openTemplates() }
                                }
                                SubText { visible: win.sourceTab === 1; text: "يمكنك أيضًا إفلات ملفات TXT في التطبيق. تُنتج Word بالقالب والمجلد المحددين أعلاه، دون تنزيل أو تفريغ صوتي. مسار النتائج هو الافتراضي للمهام الجديدة؛ مجلد المادة ينظّم نسخ المكتبة."; Layout.fillWidth: true; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignRight }
                                Rectangle { Layout.fillWidth: true; height: 1; color: win.line }
                                LabelText { visible: win.sourceTab === 0; text: "رابط YouTube"; font.weight: visualTheme.fontWeight; Layout.fillWidth: true }
                                ColumnLayout {
                                    visible: win.sourceTab === 0; Layout.fillWidth: true; spacing: 10
                                    AppButton { objectName: "discoverButton"; text: backend.busy ? "جارٍ القراءة…" : "قراءة المصدر"; primary: true; enabled: !backend.busy && urlField.text.length > 0; onClicked: backend.discover(urlField.text, forceRefresh.checked) }
                                    AppField { id: urlField; objectName: "urlField"; Layout.fillWidth: true; Layout.minimumWidth: 0; placeholderText: "https://www.youtube.com/playlist?list=…"; Accessible.name: "رابط YouTube"; onAccepted: if (!backend.busy) backend.discover(text, forceRefresh.checked) }
                                }
                                ActionRow {
                                    AppButton { visible: win.sourceTab === 1; objectName: "importLocalButton"; text: "استيراد صوت"; implicitHeight: win.rh(36); onClicked: backend.importLocal(sourceFolderCombo.currentValue || "", win.transcriptTemplateId()) }
                                    AppButton { text: "إلغاء القراءة"; visible: backend.busy; implicitHeight: 32; onClicked: backend.cancelDiscovery() }
                                    AppCheck { visible: win.sourceTab === 0; id: forceRefresh; text: "تحديث المعلومات المحفوظة"; palette.windowText: win.muted }
                                }
                            }
                        }
                        ColumnLayout {
                            width: win.contentUnits >= 850 ? Math.max(0, sourcePage.width - sourceOptionsPanel.width - 16) : sourcePage.width
                            height: Math.max(480 * win.uiScale, sourceScroll.availableHeight - 4)
                            spacing: 12
                        ActionRow {
                            AppButton { text: "تطبيق النطاق"; onClicked: backend.selectRange(rangeField.text) }
                            AppField { id: rangeField; width: 140; placeholderText: "1-10,15"; Accessible.name: "نطاق الفيديوهات" }
                            AppButton { text: "إلغاء التحديد"; onClicked: backend.selectRange("none") }
                            AppButton { text: "تحديد الكل"; onClicked: backend.selectRange("all") }
                            LabelText { text: backend.sourceTitle || "الفيديوهات المختارة"; width: Math.min(280, parent.width); font.pixelSize: win.fs(18) }
                        }
                        AppField { Layout.fillWidth: true; placeholderText: "بحث في أسماء فيديوهات القائمة"; horizontalAlignment: Text.AlignRight; onTextChanged: win.sourceSearch = text; Accessible.name: "بحث في القائمة" }
                        Box {
                            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: win.rh(160)
                            PinList {
                                id: sourceList
                                objectName: "sourceList"
                                anchors.fill: parent; anchors.margins: win.rs(8); clip: true
                                model: backend.sourceModel
                                ScrollBar.vertical: AppScroll {}
                                delegate: Item {
                                    width: sourceList.width; height: win.sourceSearch === "" || row.title.indexOf(win.sourceSearch) >= 0 ? win.rh(54) : 0
                                    visible: height > 0
                                    RowLayout {
                                        anchors.fill: parent; anchors.leftMargin: win.rs(8); anchors.rightMargin: win.rs(8); spacing: win.rs(8)
                                        AppButton { text: "للأسفل"; implicitWidth: 60; implicitHeight: Math.max(22, Math.round(28 * win.density)); enabled: index < sourceList.count - 1; onClicked: backend.moveSource(index, 1) }
                                        AppButton { text: "للأعلى"; implicitWidth: 60; implicitHeight: Math.max(22, Math.round(28 * win.density)); enabled: index > 0; onClicked: backend.moveSource(index, -1) }
                                        SubText { text: row.available ? (row.duration ? Math.round(row.duration / 60) + " دقيقة" : "") : "غير متاح"; Layout.preferredWidth: 85 }
                                        LabelText { text: row.title; Layout.fillWidth: true }
                                        SubText { text: "#" + row.position }
                                        AppCheck { checked: row.selected; enabled: row.available; onClicked: backend.selectSource(index, checked); Accessible.name: "تحديد " + row.title }
                                    }
                                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.line }
                                }
                                SubText { anchors.centerIn: parent; text: backend.busy ? "تُقرأ المعلومات مع مراعاة فواصل طلبات YouTube…" : "ستظهر قائمة الفيديوهات هنا"; visible: sourceList.count === 0 }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: win.rs(8)
                            LabelText { text: "مراحل معالجة الفيديو / الصوت" }
                            AppCombo { id: preset; objectName: "sourcePreset"; model: ["الدورة الكاملة حتى Word", "تنزيل وتفريغ فقط", "تنزيل فقط", "القالب المخصص من الإعدادات"]; Layout.fillWidth: true; Layout.minimumWidth: 80; Layout.preferredWidth: 230; Accessible.name: "مراحل المعالجة" }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            AppButton { objectName: "enqueueSourceButton"; text: "إضافة إلى الطابور"; gold: true; enabled: backend.hasSourceSelection; onClicked: { backend.enqueue(["full","transcript","download","custom"][preset.currentIndex], backend.templates.length > 0 ? templateBox.currentValue : "", sourceFolderCombo.currentValue || ""); win.page = 0 } }
                            Item { Layout.fillWidth: true }
                        }
                        }
                    }
                }

                // Workers
                ScrollView {
                    id: workersPageScroll
                    objectName: "workersPageScroll"
                    clip: true
                    contentWidth: availableWidth
                    ColumnLayout {
                    width: workersPageScroll.availableWidth
                    spacing: win.rs(18)
                    SubText { text: "حسابات ChatGPT وسعة الخدمات في مساحة واحدة. كل عامل يعالج مهمة واحدة في الوقت نفسه."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    Box {
                        objectName: "chatgptAccountsPanel"
                        Layout.fillWidth: true
                        Layout.preferredHeight: accountPanelContent.implicitHeight + 2 * win.rs(12)
                        ColumnLayout {
                            id: accountPanelContent
                            anchors.fill: parent; anchors.margins: win.rs(12); spacing: win.rs(8)

                            RowLayout {
                                Layout.fillWidth: true; spacing: win.rs(10)
                                AppButton {
                                    objectName: "accountEngineRecoverButton"; text: "فحص المحرك"
                                    enabled: !backend.accountBusy
                                    onClicked: backend.recoverAccountEngine()
                                    Accessible.name: "فحص المحرك والجلسات السابقة"
                                }
                                AppButton {
                                    id: accountHelpButton; objectName: "accountHelpButton"
                                    text: "حول الحسابات"; onClicked: accountHelpPopup.open()
                                    Popup {
                                        id: accountHelpPopup; objectName: "accountHelpPopup"; width: Math.min(420 * win.uiScale, win.width - 32 * win.uiScale); padding: 18 * win.uiScale
                                        margins: 12 * win.uiScale
                                        focus: true; modal: true
                                        onOpened: accountHelpClose.forceActiveFocus()
                                        onClosed: accountHelpButton.forceActiveFocus()
                                        y: parent.height; closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                                        background: Rectangle { color: win.panel; border.color: win.line; radius: 10 }
                                        contentItem: ColumnLayout {
                                            spacing: 12 * win.uiScale
                                            SubText {
                                                Layout.fillWidth: true
                                                text: "تُوزَّع المهام بحسب التفضيل والترتيب. تعرض الحصص حدود Codex المتاحة للاشتراك. الموافقة تتم في المتصفح، وتبقى كل محاولة مرتبطة بحساب واحد.\nتُعرض مواعيد التجدد وآخر قراءة بتوقيت الجهاز. افتح التفاصيل لقراءة رسائل الحصة والأخطاء كاملة."
                                                wrapMode: Text.WordWrap; elide: Text.ElideNone
                                            }
                                            AppButton { id: accountHelpClose; objectName: "accountHelpCloseButton"; text: "إغلاق"; onClicked: accountHelpPopup.close(); Layout.alignment: Qt.AlignLeft }
                                        }
                                    }
                                }
                                SubText {
                                    objectName: "accountEngineHealth"; text: backend.accountEngine.message || ""
                                    Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone
                                }
                                LabelText { text: "حسابات ChatGPT"; font.pixelSize: win.fs(22) }
                                SubText { text: accountList.count + " حساب"; color: visualTheme.accent }
                            }
                            Flow {
                                Layout.fillWidth: true; spacing: win.rs(8); layoutDirection: Qt.RightToLeft
                                AppCheck {
                                    objectName: "managedAccountsEnabled"; text: "الحسابات المُدارة"
                                    checked: settings.accounts_enabled === true; palette.windowText: win.ink
                                    onClicked: { settings.accounts_enabled = checked; win.save() }
                                    Accessible.name: "استخدام حسابات ChatGPT المُدارة"
                                }
                                LabelText { text: "إجمالي التزامن"; height: usageInterval.height; verticalAlignment: Text.AlignVCenter }
                                SpinBox {
                                    objectName: "accountConcurrency"; from: 1; to: 16; value: settings.accounts_max_concurrency || 5
                                    onValueModified: { settings.accounts_max_concurrency = value; win.save() }
                                    Accessible.name: "إجمالي مهام حسابات ChatGPT المتزامنة"
                                }
                                AppButton { objectName: "accountUsageRefreshAllButton"; text: "تحديث الحصص"; onClicked: backend.refreshAllAccountUsage(); Accessible.name: "تحديث حصص جميع حسابات ChatGPT" }
                                AppCheck {
                                    objectName: "accountUsageAutoRefresh"; text: "تحديث الحصص تلقائيًا"; palette.windowText: win.ink
                                    checked: settings.accounts_auto_refresh !== false
                                    onClicked: { settings.accounts_auto_refresh = checked; win.save() }
                                    Accessible.name: text
                                }
                                LabelText { text: "الفاصل بالثواني"; height: usageInterval.height; verticalAlignment: Text.AlignVCenter }
                                SpinBox {
                                    id: usageInterval; objectName: "accountUsageRefreshInterval"; from: 60; to: 1800; stepSize: 30
                                    value: settings.accounts_refresh_seconds || 300
                                    onValueModified: { settings.accounts_refresh_seconds = value; win.save() }
                                    Accessible.name: "الفاصل بين تحديثات حصص ChatGPT بالثواني"
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true; spacing: win.rs(8)
                                AppButton {
                                    objectName: "addAccountButton"; text: "إضافة حساب"; enabled: accountAlias.text.trim().length > 0
                                    onClicked: { backend.addAccount(accountAlias.text); accountAlias.clear() }
                                }
                                AppField {
                                    id: accountAlias; objectName: "accountAliasField"; Layout.fillWidth: true; maximumLength: 80
                                    placeholderText: "مثل: الحساب الأول"; Accessible.name: "اسم حساب ChatGPT الجديد"
                                    onAccepted: { if (text.trim()) { backend.addAccount(text); clear() } }
                                }
                                LabelText { text: "اسم الحساب" }
                            }
                            RowLayout {
                                visible: backend.accountEngine.migrationRequired === true
                                Layout.fillWidth: true; spacing: win.rs(10)
                                AppButton {
                                    objectName: "accountMigrationDismissButton"; text: "فهمت"; visible: backend.accountEngine.migrationRequired === true
                                    enabled: !backend.accountBusy; onClicked: backend.acknowledgeAccountMigration()
                                }
                                SubText {
                                    objectName: "accountMigrationNotice"; visible: backend.accountEngine.migrationRequired === true
                                    text: "أصبح تسجيل الحسابات داخل محرك ورق المدمج. أضف حساباتك وسجّل الدخول مجددًا؛ بيانات الدخول القديمة لا تُنسخ تلقائيًا."
                                    Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone
                                }
                            }

                            ColumnLayout {
                                id: accountTable
                                objectName: "accountTable"
                                Layout.fillWidth: true; spacing: 0
                                readonly property real identityWidth: 210 * win.uiScale
                                readonly property real actionsWidth: 132 * win.uiScale
                                readonly property real viewportWidth: Math.max(1, width - identityWidth - actionsWidth)
                                readonly property real middleWidth: Math.max(viewportWidth, 676 * win.uiScale)
                                readonly property real unit: middleWidth / 676
                                readonly property real horizontalOffset: accountTableScroller.contentX
                                Rectangle {
                                    objectName: "accountTableHeader"; Layout.fillWidth: true
                                    implicitHeight: 48 * win.uiScale; color: visualTheme.inset
                                    LabelText { x: 8; width: accountTable.actionsWidth - 16; height: parent.height; text: "الإجراءات"; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter }
                                    Flickable {
                                        id: accountTableScroller; objectName: "accountTableHorizontalScroll"
                                        x: accountTable.actionsWidth; width: accountTable.viewportWidth; height: parent.height
                                        contentWidth: accountTable.middleWidth; contentHeight: height; clip: true
                                        flickableDirection: Flickable.HorizontalFlick; boundsBehavior: Flickable.StopAtBounds
                                        onContentWidthChanged: contentX = Math.max(0, contentWidth - width)
                                        onWidthChanged: contentX = Math.max(0, Math.min(contentX, contentWidth - width))
                                        Row {
                                            width: accountTable.middleWidth; height: parent.height; layoutDirection: Qt.RightToLeft
                                            LabelText { width: 132 * accountTable.unit; height: parent.height; text: "الحالة"; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter }
                                            LabelText { width: 72 * accountTable.unit; height: parent.height; text: "الاشتراك"; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter }
                                            LabelText { width: 56 * accountTable.unit; height: parent.height; text: "النشطة"; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter }
                                            LabelText { width: 156 * accountTable.unit; height: parent.height; text: "المتبقي · ٥ ساعات"; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter }
                                            LabelText { width: 156 * accountTable.unit; height: parent.height; text: "المتبقي · أسبوعي"; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter }
                                            LabelText { width: 104 * accountTable.unit; height: parent.height; text: "آخر قراءة ناجحة"; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap }
                                        }
                                    }
                                    LabelText { x: parent.width - accountTable.identityWidth; width: accountTable.identityWidth - 14; height: parent.height; text: "الحساب · الأولوية"; verticalAlignment: Text.AlignVCenter }
                                }
                                ScrollBar {
                                    id: accountHorizontalBar; objectName: "accountHorizontalScrollBar"; Layout.fillWidth: true
                                    Layout.leftMargin: accountTable.actionsWidth; Layout.rightMargin: accountTable.identityWidth
                                    Layout.preferredHeight: 18 * win.uiScale
                                    orientation: Qt.Horizontal; policy: ScrollBar.AlwaysOn
                                    size: accountTable.viewportWidth / accountTable.middleWidth
                                    position: accountTableScroller.contentX / accountTable.middleWidth
                                    active: true; visible: accountTable.middleWidth > accountTable.viewportWidth + 1
                                    focusPolicy: Qt.StrongFocus; activeFocusOnTab: true
                                    onPositionChanged: if (Math.abs(accountTableScroller.contentX - position * accountTable.middleWidth) > 0.5) accountTableScroller.contentX = position * accountTable.middleWidth
                                    Keys.onPressed: function(event) {
                                        var maximum = Math.max(0, accountTableScroller.contentWidth - accountTableScroller.width)
                                        if (event.key === Qt.Key_Left) accountTableScroller.contentX = Math.max(0, accountTableScroller.contentX - 80 * win.uiScale)
                                        else if (event.key === Qt.Key_Right) accountTableScroller.contentX = Math.min(maximum, accountTableScroller.contentX + 80 * win.uiScale)
                                        else if (event.key === Qt.Key_Home) accountTableScroller.contentX = 0
                                        else if (event.key === Qt.Key_End) accountTableScroller.contentX = maximum
                                        else return
                                        event.accepted = true
                                    }
                                    background: Rectangle { radius: 4; color: visualTheme.inset; border.color: accountHorizontalBar.activeFocus ? win.accent : win.line; border.width: accountHorizontalBar.activeFocus ? 2 : 1 }
                                    padding: 3
                                    contentItem: Rectangle { implicitHeight: 12 * win.uiScale; implicitWidth: 36 * win.uiScale; radius: 5; color: accountHorizontalBar.pressed || accountHorizontalBar.activeFocus ? win.accent : win.line }
                                    Accessible.name: "تمرير أعمدة الحصص والحالة أفقيًا مع تثبيت الحساب والإجراءات"
                                }
                                PinList {
                                    id: accountList; objectName: "accountList"
                                    Layout.fillWidth: true; Layout.preferredHeight: count ? Math.min(448 * win.uiScale, Math.max(112 * win.uiScale, contentHeight)) : win.rh(100)
                                    spacing: 0; clip: true; model: backend.accountsModel
                                    ScrollBar.vertical: AppScroll {}
                                    delegate: Rectangle {
                                        id: accountRow
                                        property var accountData: (typeof row !== "undefined" && row) ? row : ({id: "", alias: "", error: "", statusText: "", quotaText: ""})
                                        property bool detailsOpen: false
                                        property string menuAccountId: ""
                                        readonly property real summaryHeight: (112 + (accountData.loggingIn ? 38 : 0) + (accountData.uncertain ? 38 : 0)) * win.uiScale
                                        objectName: "accountCard"; width: accountList.width
                                        height: summaryHeight + (detailsOpen ? accountContent.implicitHeight + 26 * win.uiScale : 0)
                                        color: index % 2 === 0 ? win.panel : visualTheme.inset
                                        onAccountDataChanged: {
                                            if (accountActionsMenu.visible && menuAccountId !== accountData.id) accountActionsMenu.close()
                                        }
                                        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.line }
                                        Item {
                                            objectName: "accountIdentityCell"
                                            x: parent.width - accountTable.identityWidth; width: accountTable.identityWidth; height: accountRow.summaryHeight
                                            ColumnLayout {
                                                anchors.fill: parent; anchors.margins: 8 * win.uiScale; spacing: 2 * win.uiScale
                                                RowLayout {
                                                    Layout.fillWidth: true
                                                    SubText { text: "#" + (index + 1) }
                                                    Item { Layout.fillWidth: true }
                                                    SubText { text: accountData.preferred ? "المفضّل ★" : ""; color: visualTheme.warning }
                                                }
                                                LabelText {
                                                    objectName: "accountAliasLabel"; textFormat: Text.PlainText; text: accountData.alias
                                                    font.pixelSize: win.fs(15); Layout.fillWidth: true; maximumLineCount: 1
                                                    Accessible.name: accountData.alias
                                                    ToolTip.visible: aliasHover.hovered; ToolTip.text: text
                                                    HoverHandler { id: aliasHover }
                                                }
                                                SubText {
                                                    textFormat: Text.PlainText; text: accountData.email || "—"; Layout.fillWidth: true
                                                    horizontalAlignment: Text.AlignLeft; LayoutMirroring.enabled: false
                                                    ToolTip.visible: emailHover.hovered; ToolTip.text: text
                                                    HoverHandler { id: emailHover }
                                                }
                                                AppCheck {
                                                    text: "يستقبل مهامًا"; checked: accountData.enabled; enabled: !accountData.busy
                                                    palette.windowText: win.muted
                                                    onClicked: backend.setAccountEnabled(accountData.id, checked)
                                                    Accessible.name: "تفعيل الحساب " + accountData.alias
                                                }
                                                Item { Layout.fillHeight: true }
                                            }
                                        }
                                        Item {
                                            objectName: "accountMiddleViewport"
                                            x: accountTable.actionsWidth; width: accountTable.viewportWidth; height: accountRow.summaryHeight; clip: true
                                            Row {
                                                objectName: "accountMiddleColumns"; x: -accountTable.horizontalOffset
                                                width: accountTable.middleWidth; height: parent.height; layoutDirection: Qt.RightToLeft
                                                Item {
                                                    width: 132 * accountTable.unit; height: parent.height
                                                    ColumnLayout {
                                                        anchors.fill: parent; anchors.margins: 8 * win.uiScale; spacing: 3 * win.uiScale
                                                        SubText {
                                                            objectName: "accountStatus"; textFormat: Text.PlainText
                                                            text: accountData.statusText || "غير معروف"; Layout.fillWidth: true
                                                            wrapMode: Text.WordWrap; maximumLineCount: 2; color: win.ink; Accessible.name: text
                                                        }
                                                        SubText {
                                                            text: accountData.error ? "تنبيه · افتح التفاصيل" : accountData.cooldownText || ""
                                                            visible: text.length > 0; color: visualTheme.warning
                                                            Layout.fillWidth: true; wrapMode: Text.WordWrap; maximumLineCount: 2
                                                        }
                                                        Item { Layout.fillHeight: true }
                                                    }
                                                }
                                                LabelText {
                                                    width: 72 * accountTable.unit; height: parent.height; textFormat: Text.PlainText
                                                    text: accountData.subscription || "—"; wrapMode: Text.WrapAnywhere
                                                    horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                                                }
                                                LabelText {
                                                    objectName: "accountActiveCount"; width: 56 * accountTable.unit; height: parent.height
                                                    text: typeof accountData.inFlight === "number" ? accountData.inFlight : "—"; font.pixelSize: win.fs(22)
                                                    horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                                                }
                                                AccountQuotaCell {
                                                    objectName: "accountFiveHourQuotaCell"; theme: visualTheme; uiScale: win.uiScale
                                                    width: 156 * accountTable.unit; height: parent.height
                                                    label: "المتبقي · ٥ ساعات"; windowData: (accountData.usage || {}).fiveHour || ({})
                                                    resetText: win.usageDate(windowData.resetsAt)
                                                }
                                                AccountQuotaCell {
                                                    objectName: "accountWeeklyQuotaCell"; theme: visualTheme; uiScale: win.uiScale
                                                    width: 156 * accountTable.unit; height: parent.height
                                                    label: "المتبقي · أسبوعي"; windowData: (accountData.usage || {}).weekly || ({})
                                                    resetText: win.usageDate(windowData.resetsAt)
                                                }
                                                SubText {
                                                    width: 104 * accountTable.unit; height: parent.height; leftPadding: 8; rightPadding: 8
                                                    text: win.usageDate((accountData.usage || {}).lastSuccessAt) + "\nتوقيت الجهاز"
                                                    wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                                                }
                                            }
                                        }
                                        Rectangle {
                                            objectName: "accountActionsCell"; width: accountTable.actionsWidth; height: accountRow.summaryHeight; color: accountRow.color
                                            Rectangle { anchors.right: parent.right; width: 1; height: parent.height; color: win.line }
                                            ColumnLayout {
                                                anchors.fill: parent; anchors.margins: 8 * win.uiScale; spacing: 2 * win.uiScale
                                                RowLayout {
                                                    Layout.fillWidth: true; spacing: 4 * win.uiScale
                                                    AppButton { objectName: "accountRefreshButton"; text: "فحص"; Layout.fillWidth: true; Layout.minimumWidth: 0; implicitWidth: 60 * win.uiScale; enabled: !accountData.busy && !accountData.inFlight; onClicked: backend.refreshAccount(accountData.id); Accessible.name: "فحص الحساب " + accountData.alias }
                                                    AppButton {
                                                        id: accountMoreButton; objectName: "accountMoreButton"; text: "⋯"; Layout.preferredWidth: 36 * win.uiScale; Layout.minimumWidth: 0
                                                        onClicked: { accountRow.menuAccountId = accountData.id; accountActionsMenu.open() }
                                                        Accessible.name: "المزيد من إجراءات " + accountData.alias
                                                    }
                                                }
                                                AppButton { objectName: "accountUsageRefreshButton"; text: accountData.usageRefreshing ? "جارٍ التحديث…" : "تحديث الحصة"; Layout.fillWidth: true; enabled: accountData.usage && accountData.usage.canRefresh === true && !accountData.loggingIn && !accountData.busy; onClicked: backend.refreshAccountUsage(accountData.id); Accessible.name: "تحديث الحصة " + accountData.alias }
                                                AppButton {
                                                    objectName: "accountDetailsButton"; text: accountRow.detailsOpen ? "إخفاء التفاصيل" : "التفاصيل"
                                                    Layout.fillWidth: true; implicitHeight: 20 * win.uiScale; font.pixelSize: win.fs(11)
                                                    onClicked: accountRow.detailsOpen = !accountRow.detailsOpen
                                                    Accessible.name: text + " " + accountData.alias
                                                }
                                                AppButton { objectName: "accountCancelLoginButton"; text: "إلغاء الدخول"; Layout.fillWidth: true; visible: accountData.loggingIn; onClicked: backend.cancelAccountLogin(accountData.id); Accessible.name: text + " " + accountData.alias }
                                                AppButton { text: "فحص الجلسة السابقة"; Layout.fillWidth: true; visible: accountData.uncertain; enabled: !accountData.busy && !accountData.inFlight; onClicked: backend.recheckAccount(accountData.id); Accessible.name: text + " " + accountData.alias }
                                                Item { Layout.fillHeight: true }
                                            }
                                        }
                                        Rectangle { x: parent.width - accountTable.identityWidth; width: 1; height: accountRow.summaryHeight; color: win.line }
                                        Popup {
                                            id: accountActionsMenu; objectName: "accountActionsMenu"
                                            parent: accountMoreButton; y: accountMoreButton.height; width: Math.min(330 * win.uiScale, win.width - 32 * win.uiScale)
                                            margins: 12 * win.uiScale
                                            focus: true; modal: true
                                            onOpened: {
                                                if (accountLoginAction.enabled) accountLoginAction.forceActiveFocus()
                                                else accountManageUsageAction.forceActiveFocus()
                                            }
                                            onClosed: if (accountRow.menuAccountId === accountData.id && accountMoreButton.visible) accountMoreButton.forceActiveFocus()
                                            padding: 14 * win.uiScale; closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                                            background: Rectangle { color: win.panel; border.color: win.line; radius: 10 }
                                            contentItem: ColumnLayout {
                                                spacing: 10 * win.uiScale
                                                LabelText { text: "إجراءات · " + accountData.alias; Layout.fillWidth: true; wrapMode: Text.WordWrap; textFormat: Text.PlainText }
                                                SubText { text: "إدارة الاستخدام تُفتح في المتصفح؛ اختر الحساب المطلوب فيه صراحةً. لا يسجّل ورق دخوله تلقائيًا."; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone }
                                        Flow {
                                            Layout.fillWidth: true; spacing: win.rs(6)
                                            AppButton { id: accountLoginAction; objectName: "accountLoginButton"; text: "تسجيل / إعادة الدخول"; enabled: !accountData.busy && !accountData.inFlight && !backend.accountBusy; onClicked: { accountActionsMenu.close(); backend.loginAccount(accountData.id) } Accessible.name: text + " " + accountData.alias }
                                            AppButton { id: accountManageUsageAction; objectName: "accountManageUsageButton"; text: "إدارة الاستخدام"; onClicked: { accountActionsMenu.close(); backend.manageAccountUsage() } Accessible.name: "إدارة الاستخدام في المتصفح؛ اختر حساب " + accountData.alias + " صراحةً" }
                                            AppButton { objectName: "accountPreferButton"; text: "تفضيل"; enabled: !accountData.preferred && !accountData.busy; onClicked: { accountActionsMenu.close(); backend.preferAccount(accountData.id) } Accessible.name: "تفضيل " + accountData.alias }
                                            AppButton { objectName: "accountUpButton"; text: "للأعلى"; enabled: index > 0 && !accountData.busy; onClicked: { accountActionsMenu.close(); backend.moveAccount(accountData.id, -1) } Accessible.name: "رفع أولوية " + accountData.alias }
                                            AppButton { objectName: "accountDownButton"; text: "للأسفل"; enabled: index < accountList.count - 1 && !accountData.busy; onClicked: { accountActionsMenu.close(); backend.moveAccount(accountData.id, 1) } Accessible.name: "خفض أولوية " + accountData.alias }
                                            AppButton { objectName: "accountRenameButton"; text: "تسمية"; enabled: !accountData.busy && !accountData.inFlight; onClicked: { accountActionsMenu.close(); win.pendingAccount = accountData; accountRename.text = accountData.alias; renameAccountDialog.open() } Accessible.name: "تغيير اسم " + accountData.alias }
                                            AppButton { objectName: "accountRemoveButton"; text: "حذف"; enabled: !accountData.busy && !accountData.inFlight; onClicked: { accountActionsMenu.close(); win.pendingAccount = accountData; removeAccountDialog.open() } Accessible.name: "حذف " + accountData.alias }
                                        }

                                            }
                                        }
                                        ColumnLayout {
                                            id: accountContent; objectName: "accountContent"
                                            visible: accountRow.detailsOpen; x: 14 * win.uiScale; y: accountRow.summaryHeight + 10 * win.uiScale
                                            width: parent.width - 28 * win.uiScale; spacing: 7 * win.uiScale
                                            SubText { text: accountData.cooldownText || ""; visible: text.length > 0; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone }
                                        ColumnLayout {
                                            objectName: "accountUsagePanel"; Layout.fillWidth: true; spacing: win.rs(3)
                                            property var usage: accountData.usage || ({})
                                            SubText { objectName: "accountUsageFiveHour"; textFormat: Text.PlainText; text: win.usageWindow("خمس ساعات", parent.usage.fiveHour); Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone; Accessible.name: accountData.alias + " " + text }
                                            SubText { objectName: "accountUsageWeekly"; textFormat: Text.PlainText; text: win.usageWindow("أسبوعية", parent.usage.weekly); Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone; Accessible.name: accountData.alias + " " + text }
                                            SubText { objectName: "accountUsageUpdated"; text: "آخر قراءة ناجحة (توقيت الجهاز): " + win.usageDate(parent.usage.lastSuccessAt); Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone; Accessible.name: accountData.alias + " " + text }
                                            SubText { objectName: "accountUsageMessage"; text: win.usageMessage(parent.usage, accountData.usageRefreshing); visible: text.length > 0; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone; Accessible.name: accountData.alias + " " + text }
                                            SubText { objectName: "accountUsageRefreshReason"; text: win.usageRefreshReason(parent.usage); visible: text.length > 0; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone; Accessible.name: accountData.alias + " " + text }
                                            SubText { objectName: "accountUsageBrowserNote"; text: "إدارة الاستخدام تُفتح في المتصفح؛ اختر الحساب المطلوب فيه صراحةً. لا يسجّل ورق دخوله تلقائيًا."; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone }
                                        }

                                        SubText { objectName: "accountError"; text: accountData.error; visible: !!accountData.error; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone; color: win.dark ? "#f4b880" : "#9a4409" }

                                        SubText {
                                            objectName: "accountDeviceCode"; textFormat: Text.PlainText
                                            visible: !!accountData.userCode; text: "رمز الجهاز: " + (accountData.userCode || "")
                                            Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone
                                        }

                                        }
                                    }
                                    SubText { anchors.centerIn: parent; visible: accountList.count === 0; text: "أضف حسابًا ثم سجّل دخوله في المتصفح." }
                                }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true; spacing: win.rs(10)
                        AppButton { text: "إضافة عامل OpenCode"; onClicked: { var w = JSON.parse(JSON.stringify(settings.opencode_workers[0])); w.id = "oc" + Date.now(); w.name = "OpenCode " + (settings.opencode_workers.length + 1); w.provider = w.provider || "login"; settings.opencode_workers.push(w); save() } }
                        AppButton { objectName: "providersButton"; text: "المزودون"; onClicked: { providersDialog.open() } }
                        AppButton { text: "إضافة عامل Deepgram"; onClicked: { var w = JSON.parse(JSON.stringify(settings.deepgram_workers[0])); w.id = "dg" + Date.now(); w.name = "Deepgram " + (settings.deepgram_workers.length + 1); w.project = "proj-" + (settings.deepgram_workers.length + 1); settings.deepgram_workers.push(w); save() } }
                    }
                    ScrollView {
                        id: workersScroll
                        Layout.fillWidth: true; Layout.preferredHeight: Math.ceil(3 / win.serviceColumns) * 430 * win.uiScale + (Math.ceil(3 / win.serviceColumns) - 1) * win.rs(12)
                        clip: true; contentWidth: availableWidth
                        GridLayout {
                            width: workersScroll.availableWidth
                            columns: win.serviceColumns; columnSpacing: 12; rowSpacing: 12; layoutDirection: Qt.RightToLeft
                            ServiceGroup { stage: "download"; heading: "تنزيل YouTube"; symbol: "download"; Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.preferredHeight: 430 * win.uiScale }
                            ServiceGroup { stage: "deepgram"; heading: "التفريغ الصوتي"; symbol: "audio"; Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.preferredHeight: 430 * win.uiScale }
                            ServiceGroup { stage: "opencode"; heading: "OpenCode / Word"; symbol: "document"; Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.preferredHeight: 430 * win.uiScale }
                        }
                    }
                    Box {
                        Layout.fillWidth: true; Layout.preferredHeight: workerHelp.implicitHeight + 24
                        SubText { id: workerHelp; anchors.fill: parent; anchors.margins: 12; verticalAlignment: Text.AlignVCenter; wrapMode: Text.WordWrap; text: "مفاتيح Deepgram و OpenRouter محفوظة في Windows Credential Manager، مفتاح مستقل لكل عامل. عمال Deepgram ذوو معرّف المشروع نفسه يتقاسمون حد التزامن؛ اجعل المعرّف مطابقًا لمشروع المفتاح. عامل OpenCode بمزود غير «تسجيل الدخول» يعمل في مجلد بيانات مستقل فلا تتعارض العمليات المتوازية." }
                    }
                }
                }

                // Library
                ColumnLayout {
                    spacing: 12
                    ActionRow {
                        AppButton { text: "فتح مجلد النتائج"; onClicked: backend.openPath(settings.output_root) }
                        AppButton { text: "تصدير Word"; onClicked: backend.exportWord() }
                        AppButton { text: "تقرير Excel"; onClicked: backend.exportReport() }
                        LabelText { text: backend.stats.documents + " مستند Word"; font.pixelSize: win.fs(20) }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        AppField { Layout.fillWidth: true; placeholderText: "بحث في عناوين النتائج"; horizontalAlignment: Text.AlignRight; onTextChanged: backend.setResultsQuery(text); Accessible.name: "بحث في النتائج" }
                        AppCombo { model: [{id:"all", name:"كل المجلدات"}].concat(backend.folders); textRole: "name"; valueRole: "id"; Layout.preferredWidth: 180; onActivated: backend.setResultsFolder(currentValue); Accessible.name: "تصفية النتائج حسب المجلد" }
                        AppButton { text: "مجلد جديد"; onClicked: folderDialog.open() }
                    }
                    TableHead {
                        RowLayout {
                            anchors.fill: parent; anchors.margins: 10
                            SubText { text: "Word / TXT / صوت / تفاصيل"; Layout.preferredWidth: 320 }
                            SubText { text: "المدة"; Layout.preferredWidth: 50; visible: win.contentUnits >= 850 }
                            SubText { text: "عنوان الدرس · المجلد"; Layout.fillWidth: true }
                        }
                    }
                    PinList {
                        id: libraryList
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 0
                        objectName: "resultsList"
                        model: backend.resultsModel
                        ScrollBar.vertical: AppScroll {}
                        delegate: Box {
                            width: libraryList.width; height: Math.max(win.rh(72), resultTitle.implicitHeight + 36 * win.uiScale)
                            color: index % 2 === 0 ? win.panel : visualTheme.inset
                            radius: 0
                            RowLayout {
                                anchors.fill: parent; anchors.margins: win.rs(10); spacing: win.rs(8)
                                AppButton { text: "فتح Word"; primary: true; implicitHeight: Math.max(22, Math.round(28 * win.density)); enabled: !!row.document; onClicked: backend.openPath(row.document) }
                                AppButton { text: "التفريغ"; implicitHeight: Math.max(22, Math.round(28 * win.density)); enabled: !!row.transcript; onClicked: backend.openPath(row.transcript) }
                                AppButton { text: "الصوت"; implicitHeight: Math.max(22, Math.round(28 * win.density)); enabled: !!row.audio; onClicked: backend.openPath(row.audio) }
                                AppButton { text: "التفاصيل"; implicitHeight: Math.max(22, Math.round(28 * win.density)); onClicked: win.openJob(row.id) }
                                SubText { text: row.duration ? Math.round(row.duration / 60) + " د" : "—"; Layout.preferredWidth: 50; visible: win.contentUnits >= 850 }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    LabelText { id: resultTitle; text: row.title; font.pixelSize: win.fs(14); Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; maximumLineCount: 2 }
                                    SubText { text: (row.folder_name ? row.folder_name + " · " : "") + (row.document ? "DOCX · تم التحقق من بنية المستند والنص" : "نتائج وسيطة محفوظة"); Layout.fillWidth: true }
                                }
                            }
                        }
                        SubText { anchors.centerIn: parent; visible: libraryList.count === 0; text: "تظهر هنا الملفات الناتجة فور حفظها." }
                    }
                }

                // Files
                ColumnLayout {
                    spacing: 12
                    ActionRow {
                        AppButton { objectName: "organizeButton"; text: "تنظيم الآن"; primary: true; enabled: !backend.libraryBusy; onClicked: backend.organizeLibrary() }
                        AppButton { text: "تحديث"; enabled: !backend.libraryBusy; onClicked: backend.refreshLibrary() }
                        AppButton { text: "فتح مجلد المكتبة"; onClicked: backend.openPath(settings.library_root) }
                        AppButton { text: "فتح ملف من الجهاز"; onClicked: { var path = backend.choosePath("file"); if (path) backend.openPath(path) } }
                        LabelText { text: backend.libraryCount + " ملف في المكتبة"; font.pixelSize: win.fs(20) }
                    }
                    GridLayout {
                        Layout.fillWidth: true; columns: win.contentUnits >= 850 ? 6 : 3; columnSpacing: 8; rowSpacing: 6
                        AppField { Layout.fillWidth: true; placeholderText: "بحث في أسماء الملفات"; horizontalAlignment: Text.AlignRight; onTextChanged: backend.setFilesQuery(text); Accessible.name: "بحث في الملفات" }
                        AppCombo { model: [{name:"كل المجلدات", group:"all"}].concat(backend.folders.map(function(f) { return {name: f.name, group: f.id ? f.name : ""} })); textRole: "name"; Layout.preferredWidth: 150; onActivated: backend.setFilesFolder(model[currentIndex].group); Accessible.name: "تصفية الملفات حسب المجلد" }
                        Repeater {
                            model: [{id: "all", label: "الكل"}, {id: "document", label: "Word"}, {id: "transcript", label: "التفريغات"}, {id: "audio", label: "الصوت"}]
                            delegate: AppCheck {
                                required property var modelData
                                objectName: "fileKind_" + modelData.id
                                text: modelData.label
                                checked: win.fileKind === modelData.id
                                palette.windowText: win.ink
                                onClicked: { win.fileKind = modelData.id; backend.setFilesKind(modelData.id) }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true; Layout.fillHeight: true; spacing: 12
                        Box {
                            Layout.preferredWidth: 170 * win.uiScale
                            Layout.fillHeight: true
                            visible: win.contentUnits >= 850
                            ColumnLayout {
                                anchors.fill: parent; anchors.margins: 10
                                LabelText { text: "مجلدات المواد"; font.weight: visualTheme.fontWeight; Layout.fillWidth: true }
                                AppButton { text: "كل المجلدات"; Layout.fillWidth: true; onClicked: backend.setFilesFolder("all") }
                                PinList {
                                    Layout.fillWidth: true; Layout.fillHeight: true; model: backend.folders
                                    ScrollBar.vertical: AppScroll {}
                                    delegate: AppButton {
                                        required property var modelData
                                        width: ListView.view.width; text: modelData.name
                                        onClicked: backend.setFilesFolder(modelData.id ? modelData.name : "")
                                    }
                                }
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 0
                            TableHead {
                                RowLayout { anchors.fill: parent; anchors.margins: 10; SubText { text: "إجراءات الملف"; Layout.preferredWidth: 310 } SubText { text: "الاسم · النوع · الحجم · آخر تعديل"; Layout.fillWidth: true } }
                            }
                    PinList {
                        id: fileList
                        objectName: "fileList"
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: win.rs(6)
                        model: backend.filesModel
                        ScrollBar.vertical: AppScroll {}
                        delegate: Box {
                            width: fileList.width; height: Math.max(win.rh(62), fileTitle.implicitHeight + 34 * win.uiScale)
                            color: index % 2 === 0 ? win.panel : visualTheme.inset; radius: 0
                            RowLayout {
                                anchors.fill: parent; anchors.margins: win.rs(10); spacing: win.rs(8)
                                AppButton { text: "فتح"; primary: true; implicitHeight: Math.max(22, Math.round(28 * win.density)); onClicked: backend.openPath(row.path) }
                                AppButton { text: "فتح المجلد"; implicitHeight: Math.max(22, Math.round(28 * win.density)); onClicked: backend.openPath(row.folder) }
                                AppButton { text: "نسخ المسار"; implicitHeight: Math.max(22, Math.round(28 * win.density)); onClicked: backend.copyText(row.path) }
                                AppButton { text: "حذف"; danger: true; implicitHeight: Math.max(22, Math.round(28 * win.density)); onClicked: { win.pendingFile = {path: row.path, name: row.name}; deleteFileDialog.open() } }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    LabelText { id: fileTitle; text: row.name; font.pixelSize: win.fs(14); Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; maximumLineCount: 2 }
                                    SubText { text: (row.group ? row.group + "  ·  " : "") + win.kindLabel(row.kind) + "  ·  " + win.fileSize(row.size) + "  ·  " + Qt.formatDateTime(new Date(row.modified * 1000), "yyyy-MM-dd HH:mm") }
                                }
                            }
                        }
                        SubText {
                            anchors.centerIn: parent; visible: fileList.count === 0; wrapMode: Text.WordWrap
                            width: parent.width - 60; horizontalAlignment: Text.AlignHCenter
                            text: backend.libraryBusy ? "جارٍ التنظيم…" : "لا ملفات في المكتبة بعد. «تنظيم الآن» ينسخ نتائج المهام المكتملة إلى مجلدات Word والتفريغات والصوت داخل: " + settings.library_root
                        }
                    }
                        }
                    }
                }

                // Settings
                ColumnLayout {
                    Layout.minimumWidth: 0
                    spacing: 12
                    ActionRow {
                        AppButton { text: "حفظ الإعدادات"; primary: true; onClicked: { settings.prompt = promptField.text; save() } }
                        AppButton { objectName: "templatesButton"; text: "قوالب البرومبت"; onClicked: templatesDialog.open() }
                        AppButton { text: "استيراد إعدادات السكربت"; onClicked: backend.importLegacy() }
                        AppButton { text: "فحص البيئة"; onClicked: backend.checkEnvironment() }
                        AppButton { text: win.dark ? "المظهر الفاتح" : "المظهر الداكن"; onClicked: { settings.theme = win.dark ? "light" : "dark"; save() } }
                    }
                    RowLayout {
                        layoutDirection: Qt.RightToLeft
                        Layout.fillWidth: true
                        Repeater {
                            model: ["المظهر والواجهة", "المسارات والأدوات", "المعالجة والمكتبة", "البرومبت والقوالب"]
                            delegate: AppButton {
                                required property int index
                                required property string modelData
                                objectName: "settingsTab" + index
                                text: modelData; primary: win.settingsTab === index; Layout.fillWidth: true
                                onClicked: win.settingsTab = index
                            }
                        }
                    }
                    ScrollView {
                        id: settingsScroll; objectName: "settingsScroll"
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                        contentWidth: availableWidth
                        ColumnLayout {
                            width: settingsScroll.availableWidth; spacing: 12
                    Box {
                        visible: win.settingsTab === 0; Layout.fillWidth: true; implicitHeight: uiScaleColumn.implicitHeight + 28
                        ColumnLayout {
                            id: uiScaleColumn; objectName: "appearancePanel"; anchors.fill: parent; anchors.margins: 14; spacing: 8
                            LabelText { text: "حجم الواجهة"; font.pixelSize: win.fs(18); Layout.alignment: Qt.AlignRight }
                            RowLayout {
                                Layout.fillWidth: true; spacing: 12
                                AppButton { objectName: "uiScaleReset"; text: "افتراضي"; implicitHeight: 30; enabled: Math.round(win.uiScale * 100) !== 100; onClicked: { settings.ui_scale = 1.0; save() } }
                                Item { Layout.fillWidth: true }
                                LabelText { objectName: "uiScaleValue"; text: Math.round(win.uiScale * 100) + "٪"; font.pixelSize: win.fs(15) }
                                AppSlider {
                                    id: uiScaleSlider
                                    objectName: "uiScaleSlider"
                                    Layout.preferredWidth: 260
                                    from: 80; to: 140; stepSize: 5
                                    value: Math.round(win.uiScale * 100)
                                    onMoved: { settings.ui_scale = Math.round(value) / 100; save() }
                                    Accessible.name: "حجم خطوط وعناصر الواجهة"
                                }
                            }
                            AppCheck {
                                text: "قوائم مضغوطة — صفوف أكثر وأقل فراغًا في كل القوائم"
                                checked: settings.ui_compact === true
                                palette.windowText: win.ink
                                onClicked: { settings.ui_compact = checked; save() }
                                Accessible.name: "تفعيل العرض المضغوط للقوائم"
                            }
                            SubText { text: "يُطبَّق فورًا على الخطوط والأزرار والصفوف في كل الصفحات، ويُحفظ مع بقية الإعدادات."; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                        }
                    }
                            Box {
                                objectName: "remoteAccessCard"; visible: win.settingsTab === 0
                                Layout.fillWidth: true; Layout.minimumWidth: 0
                                implicitHeight: remoteColumn.implicitHeight + 28
                                ColumnLayout {
                                    id: remoteColumn; anchors.fill: parent; anchors.margins: 14; spacing: win.rs(10)
                                    LabelText { text: "الوصول من الهاتف · Wi-Fi"; font.pixelSize: win.fs(18); Layout.fillWidth: true }
                                    SubText { text: "تحكّم في هذه النسخة من متصفح هاتفك على شبكة Wi-Fi نفسها. أبقِ ورق مفتوحًا والكمبيوتر مستيقظًا."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    Flow {
                                        Layout.fillWidth: true; Layout.preferredHeight: childrenRect.height; spacing: win.rs(8)
                                        AppButton { objectName: "startRemoteButton"; text: "تفعيل الوصول من الهاتف"; primary: true; visible: !win.remoteState.enabled; onClicked: backend.startRemote(remotePort.value) }
                                        AppButton { objectName: "stopRemoteButton"; text: "إيقاف الوصول وقطع الاتصالات"; visible: win.remoteState.enabled; onClicked: backend.stopRemote() }
                                        AppButton { objectName: "renewRemoteCodeButton"; text: "تجديد الرمز وقطع الاتصالات"; visible: win.remoteState.enabled; onClicked: backend.renewRemoteCode() }
                                        AppButton { text: "تحديث عناوين الشبكة"; visible: win.remoteState.enabled; onClicked: backend.refreshRemoteAddresses() }
                                    }
                                    RowLayout {
                                        visible: !win.remoteState.enabled; Layout.fillWidth: true
                                        Item { Layout.fillWidth: true }
                                        SpinBox { id: remotePort; objectName: "remotePort"; from: 1024; to: 65535; value: 8765; editable: true; Accessible.name: "منفذ الوصول من الهاتف" }
                                        LabelText { text: "المنفذ" }
                                    }
                                    LabelText { text: win.remoteState.error || ""; visible: text.length > 0; color: win.dark ? "#ffba82" : "#9a4409"; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    RowLayout {
                                        visible: win.remoteState.enabled; Layout.fillWidth: true; spacing: 16
                                        Rectangle {
                                            visible: win.remoteState.qr.length > 0; color: "white"; radius: 8
                                            Layout.preferredWidth: 140; Layout.preferredHeight: 140
                                            Image { objectName: "remoteQr"; anchors.fill: parent; anchors.margins: 4; source: win.remoteState.qr; sourceSize.width: 240; sourceSize.height: 240; fillMode: Image.PreserveAspectFit; Accessible.name: "رمز QR لفتح لوحة الهاتف" }
                                        }
                                        ColumnLayout {
                                            Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: 6
                                            LabelText { text: "امسح QR أو اكتب أحد الروابط في متصفح الهاتف، ثم أدخل رمز الدخول:"; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                            LabelText { objectName: "remotePairCode"; text: win.remoteState.code; font.pixelSize: win.fs(28); font.letterSpacing: 4; font.weight: visualTheme.fontWeight; Layout.fillWidth: true; Accessible.name: "رمز الدخول " + text }
                                            SubText { text: "صالح حتى " + (win.remoteState.codeExpires || "") + " · الأجهزة المتصلة: " + win.remoteState.clients; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                            Repeater {
                                                model: win.remoteState.urls
                                                delegate: RowLayout {
                                                    required property string modelData
                                                    Layout.fillWidth: true
                                                    AppButton { text: "نسخ"; onClicked: backend.copyText(modelData) }
                                                    TextField { text: modelData; readOnly: true; selectByMouse: true; Layout.fillWidth: true; Layout.minimumWidth: 0; horizontalAlignment: TextInput.AlignLeft; color: win.ink; Accessible.name: "رابط لوحة الهاتف" }
                                                }
                                            }
                                            SubText { visible: win.remoteState.urls.length === 0; text: "لم يُعثر على عنوان شبكة محلية. اتصل بشبكة Wi-Fi ثم اضغط تحديث عناوين الشبكة."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                        }
                                    }
                                    SubText { visible: win.remoteState.enabled; text: "إذا طلب جدار حماية Windows إذنًا، اسمح لورق على الشبكات الخاصة فقط. إن لم يفتح الرابط، تحقق من أن الجهازين خارج شبكة الضيوف ومن تعطيل عزل الأجهزة في الراوتر. الاتصال HTTP مخصص لشبكتك الموثوقة؛ لا تفتح المنفذ على الإنترنت."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                }
                            }
                            Box {
                                objectName: "internetAccessCard"; visible: win.settingsTab === 0
                                Layout.fillWidth: true; Layout.minimumWidth: 0
                                implicitHeight: internetColumn.implicitHeight + 28
                                ColumnLayout {
                                    id: internetColumn; anchors.fill: parent; anchors.margins: 14; spacing: win.rs(10)
                                    LabelText { text: "الوصول من الهاتف · الإنترنت"; font.pixelSize: win.fs(18); Layout.fillWidth: true }
                                    SubText { text: "اربط هذه النسخة ببوابتك على VPS، ثم اقترن بالهاتف. كل جهاز له هوية وصلاحيات مستقلة، والتنفيذ يبقى على هذا الكمبيوتر."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    SubText { objectName: "internetOnePhonePolicy"; text: "هاتف واحد لكل جهاز. لتبديل الهاتف، ألغِ ربط الهاتف الحالي أولًا، ثم أنشئ رابط اقتران جديدًا."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    LabelText { objectName: "internetStatus"; text: win.internetState.status + (win.internetState.name ? " · " + win.internetState.name : ""); color: win.internetState.connected ? (win.dark ? "#61ddbb" : "#08765b") : win.ink; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    LabelText { objectName: "internetError"; text: win.internetState.error || ""; visible: text.length > 0; color: win.dark ? "#ffba82" : "#9a4409"; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    GridLayout {
                                        visible: !win.internetState.registered; columns: 2; Layout.fillWidth: true; columnSpacing: 12; rowSpacing: 8
                                        AppField { id: internetGateway; objectName: "internetGateway"; Layout.fillWidth: true; Layout.minimumWidth: 0; text: win.internetState.url || "https://waraqstudio.jadallah.work"; placeholderText: "https://waraqstudio.jadallah.work"; Accessible.name: "عنوان بوابة ورق على الإنترنت" }
                                        LabelText { text: "عنوان البوابة" }
                                        AppField { id: internetDeviceName; objectName: "internetDeviceName"; Layout.fillWidth: true; Layout.minimumWidth: 0; placeholderText: "كمبيوتر المنزل"; maximumLength: 120; Accessible.name: "اسم هذا الجهاز" }
                                        LabelText { text: "اسم الجهاز" }
                                        AppField { id: internetEnrollment; objectName: "internetEnrollment"; Layout.fillWidth: true; Layout.minimumWidth: 0; echoMode: TextInput.Password; placeholderText: "رمز تسجيل أحادي الاستخدام"; maximumLength: 256; Accessible.name: "رمز تسجيل الجهاز الصادر من الخادم" }
                                        LabelText { text: "رمز التسجيل" }
                                    }
                                    Flow {
                                        Layout.fillWidth: true; Layout.preferredHeight: childrenRect.height; spacing: win.rs(8)
                                        AppButton { objectName: "registerInternetButton"; text: win.internetState.busy ? "جارٍ التسجيل…" : "تسجيل هذا الجهاز"; primary: true; visible: !win.internetState.registered; enabled: !win.internetState.busy && internetGateway.text.trim().length > 0 && internetDeviceName.text.trim().length > 0 && internetEnrollment.text.length > 0; onClicked: { backend.registerInternet(internetGateway.text.trim(), internetEnrollment.text, internetDeviceName.text.trim()); internetEnrollment.text = "" } }
                                        AppButton { objectName: "startInternetButton"; text: "تفعيل اتصال الإنترنت"; primary: true; visible: win.internetState.registered && !win.internetState.connected; enabled: !win.internetState.busy; onClicked: backend.startInternet() }
                                        AppButton { objectName: "stopInternetButton"; text: "إيقاف اتصال الإنترنت"; visible: win.internetState.registered; onClicked: backend.stopInternet() }
                                        AppButton { objectName: "pairInternetButton"; text: "ربط هاتف جديد"; primary: true; visible: win.internetState.connected; onClicked: backend.createInternetPair() }
                                        AppButton { objectName: "refreshInternetPhonesButton"; text: "تحديث الهواتف المقترنة"; visible: win.internetState.connected; onClicked: backend.refreshInternetPhones() }
                                        AppButton { objectName: "forgetInternetButton"; text: "إلغاء تسجيل هذا الجهاز"; visible: win.internetState.registered; enabled: win.internetState.connected; onClicked: forgetInternetDialog.open() }
                                    }
                                    SubText { visible: win.internetState.registered; text: win.internetState.url + "\nمعرّف هذه النسخة: " + (win.internetState.deviceId || ""); Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                                    RowLayout {
                                        visible: win.internetState.pairUrl.length > 0 && win.internetState.connected; Layout.fillWidth: true; spacing: 16
                                        Rectangle {
                                            color: "white"; radius: 8; Layout.preferredWidth: 160; Layout.preferredHeight: 160
                                            Image { objectName: "internetQr"; anchors.fill: parent; anchors.margins: 4; source: backend.internetQr; sourceSize.width: 320; sourceSize.height: 320; fillMode: Image.PreserveAspectFit; Accessible.name: "رمز QR لاقتران الهاتف بهذا الكمبيوتر عبر الإنترنت" }
                                        }
                                        ColumnLayout {
                                            Layout.fillWidth: true; Layout.minimumWidth: 0
                                            LabelText { text: "امسح QR بهاتفك، ثم راجع اسم الجهاز ورمز المطابقة قبل الموافقة على طلب الربط أدناه."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                            AppButton { text: "نسخ رابط الاقتران"; onClicked: backend.copyText(win.internetState.pairUrl) }
                                            SubText { text: "الرابط مؤقت وأحادي الاستخدام. لا تشاركه مع هاتف غير مقصود."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                        }
                                    }
                                    Repeater {
                                        model: win.internetState.pendingPhones || []
                                        delegate: ColumnLayout {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            LabelText { text: "طلب اقتران: " + modelData.phone_name + " · رمز المطابقة: " + modelData.match_code; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                            RowLayout {
                                                Layout.fillWidth: true
                                                AppButton { text: "رفض"; onClicked: backend.rejectInternetPhone(modelData.claim_id) }
                                                AppButton { objectName: "approveInternetPhoneButton"; text: "موافقة على ربط هذا الهاتف"; primary: true; onClicked: backend.approveInternetPhone(modelData.claim_id) }
                                                Item { Layout.fillWidth: true }
                                            }
                                        }
                                    }
                                    Repeater {
                                        model: win.internetState.phones || []
                                        delegate: RowLayout {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            AppButton { text: "إلغاء الربط"; enabled: win.internetState.connected; onClicked: backend.revokeInternetPhone(modelData.id) }
                                            LabelText { text: modelData.name || "هاتف مقترن"; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                        }
                                    }
                                    SubText { text: "يتطلب بوابة منشورة على دومين HTTPS. يتوقف التحكم عند إغلاق ورق أو نوم الكمبيوتر؛ يبقى الطابور يعمل عند انقطاع الإنترنت. الاتصال معطّل عند كل تشغيل حتى تفعّله."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                }
                            }
                            Box {
                                visible: win.settingsTab === 1; Layout.fillWidth: true; implicitHeight: storageColumn.implicitHeight + 40
                                ColumnLayout {
                                    id: storageColumn; objectName: "pathsPanel"; anchors.fill: parent; anchors.margins: 14; spacing: 10
                                    LabelText { text: "الملفات والأدوات"; font.pixelSize: win.fs(18); Layout.alignment: Qt.AlignRight }
                                    Repeater {
                                        model: [{key:"output_root",label:"مجلد النتائج",kind:"folder"},{key:"library_root",label:"مجلد المكتبة",kind:"folder"},{key:"cookies_file",label:"ملف كوكيز Netscape",kind:"file"},{key:"opencode_executable",label:"ملف OpenCode التنفيذي",kind:"file"},{key:"opencode_context",label:"مجلد المشروع السابق",kind:"folder"},{key:"opencode_data_root",label:"مجلد بيانات عمال OpenCode",kind:"folder"}]
                                        delegate: RowLayout {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            AppButton { text: "اختيار"; implicitHeight: 34; onClicked: { var path = backend.choosePath(modelData.kind); if(path) { settings[modelData.key] = path; pathField.text = path } } }
                                            AppField { id: pathField; Layout.fillWidth: true; text: settings[modelData.key]; onEditingFinished: settings[modelData.key] = text; Accessible.name: modelData.label }
                                            LabelText { text: modelData.label; Layout.preferredWidth: 185 }
                                        }
                                    }
                                }
                            }
                            Box {
                                objectName: "appUpdateCard"
                                visible: win.settingsTab === 1; Layout.fillWidth: true; Layout.minimumWidth: 0; implicitHeight: appUpdateColumn.implicitHeight + 28
                                ColumnLayout {
                                    id: appUpdateColumn; anchors.fill: parent; anchors.margins: 14; spacing: win.rs(8)
                                    LabelText { text: "تحديث ورق"; font.pixelSize: win.fs(18); Layout.fillWidth: true }
                                    SubText { text: "الإصدار الحالي: " + backend.version + (win.appUpdateState.version ? "  ·  أحدث إصدار: " + win.appUpdateState.version : ""); Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    LabelText { objectName: "appUpdateMessage"; text: win.appUpdateState.message || "افحص توفر إصدار جديد من GitHub."; color: win.appUpdateState.status === "error" ? (win.dark ? "#ffba82" : "#9a4409") : win.ink; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    AppProgress { objectName: "appUpdateProgress"; visible: win.appUpdateState.status === "downloading"; Layout.fillWidth: true; from: 0; to: 100; value: win.appUpdateState.progress || 0; Accessible.name: "تقدم تنزيل تحديث ورق" }
                                    Flow {
                                        Layout.fillWidth: true; Layout.preferredHeight: childrenRect.height; spacing: win.rs(8)
                                        AppButton { objectName: "checkAppUpdateButton"; text: win.appUpdateState.status === "checking" ? "جارٍ الفحص…" : "فحص التحديث"; enabled: ["checking", "downloading"].indexOf(win.appUpdateState.status) < 0; onClicked: backend.checkAppUpdate() }
                                        AppButton { objectName: "downloadAppUpdateButton"; text: "تنزيل الإصدار الجديد"; primary: true; visible: win.appUpdateState.status === "available"; onClicked: backend.downloadAppUpdate() }
                                        AppButton { objectName: "cancelAppUpdateButton"; text: "إلغاء التنزيل"; visible: win.appUpdateState.status === "downloading"; onClicked: backend.cancelAppUpdate() }
                                        AppButton { objectName: "installAppUpdateButton"; text: "تثبيت التحديث وإغلاق التطبيق"; primary: true; visible: win.appUpdateState.status === "downloaded"; onClicked: backend.installAppUpdate() }
                                    }
                                    SubText { text: "ينزّل التطبيق الإصدار مباشرة ويتحقق منه، ثم يفتح المثبّت ويغلق ورق. تُحفظ بياناتك وإعداداتك."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    LabelText { text: "رمز GitHub للمستودع الخاص (اختياري)"; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    RowLayout {
                                        Layout.fillWidth: true; spacing: win.rs(8)
                                        AppButton { objectName: "saveUpdateTokenButton"; text: "حفظ الرمز"; enabled: updateTokenField.text.trim().length > 0 && ["checking", "downloading"].indexOf(win.appUpdateState.status) < 0; onClicked: { backend.saveUpdateToken(updateTokenField.text.trim()); updateTokenField.text = "" } }
                                        AppButton { objectName: "removeUpdateTokenButton"; text: "إزالة الرمز"; visible: win.appUpdateState.hasToken === true; enabled: ["checking", "downloading"].indexOf(win.appUpdateState.status) < 0; onClicked: { backend.saveUpdateToken(""); updateTokenField.text = "" } }
                                        AppField { id: updateTokenField; objectName: "updateTokenField"; Layout.fillWidth: true; Layout.minimumWidth: 0; echoMode: TextInput.Password; placeholderText: "GitHub token"; Accessible.name: "رمز GitHub لقراءة المستودع الخاص" }
                                    }
                                    SubText { objectName: "updateTokenStatus"; text: win.appUpdateState.hasToken ? "الرمز محفوظ في مخزن اعتماد Windows." : "لا يوجد رمز محفوظ. استخدم رمزًا يملك صلاحية Contents: read للمستودع الخاص."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                }
                            }
                            Box {
                                visible: win.settingsTab === 1; Layout.fillWidth: true; implicitHeight: youtubeUpdateColumn.implicitHeight + 32
                                ColumnLayout {
                                    id: youtubeUpdateColumn; anchors.fill: parent; anchors.margins: 14; spacing: 8
                                    LabelText { text: "أداة جلب بيانات YouTube"; font.pixelSize: win.fs(18); Layout.alignment: Qt.AlignRight }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        AppButton { objectName: "youtubeUpdateButton"; text: backend.youtubeUpdating ? "جارٍ التحديث…" : "تحديث yt-dlp"; enabled: !backend.youtubeUpdating; onClicked: backend.updateYoutube() }
                                        Item { Layout.fillWidth: true }
                                        SubText { text: "الإصدار المستخدم: " + backend.youtubeVersion }
                                    }
                                    SubText { text: "يجلب أحدث إصدار مستقر رسمي ويتحقق من بصمته، ثم يستخدمه التطبيق لقراءة المصادر والتنزيل. يحتاج إلى اتصال إنترنت."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                }
                            }
                            Box {
                                visible: win.settingsTab === 2; Layout.fillWidth: true; implicitHeight: limitsColumn.implicitHeight + 40
                                ColumnLayout {
                                    id: limitsColumn; objectName: "processingPanel"; anchors.fill: parent; anchors.margins: 14; spacing: 10
                                    LabelText { text: "السعة والترتيب"; font.pixelSize: win.fs(18); Layout.alignment: Qt.AlignRight }
                                    Repeater {
                                        model: [{key:"project_concurrency",label:"حد تزامن Deepgram لكل مشروع"},{key:"max_pending_audio",label:"أقصى ملفات صوت بانتظار التفريغ"},{key:"youtube_sleep_min",label:"أقل انتظار بين التنزيلات — ثانية"},{key:"youtube_sleep_max",label:"أقصى انتظار بين التنزيلات — ثانية"},{key:"youtube_request_delay",label:"الفاصل بين طلبات YouTube — ثانية"},{key:"youtube_cooldown",label:"تهدئة تقييد YouTube — ثانية"},{key:"chunk_seconds",label:"طول الجزء الصوتي — ثانية"},{key:"opencode_timeout",label:"مهلة OpenCode — ثانية"}]
                                        delegate: RowLayout {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            AppField { Layout.preferredWidth: 160; text: String(settings[modelData.key]); validator: IntValidator {bottom:1;top:43200} onEditingFinished: settings[modelData.key] = Number(text); Accessible.name: modelData.label }
                                            Item { Layout.fillWidth: true }
                                            LabelText { text: modelData.label }
                                        }
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        AppField { Layout.preferredWidth: 160; text: String(settings.youtube_retry_attempts); validator: IntValidator {bottom:0;top:6} onEditingFinished: settings.youtube_retry_attempts = Number(text); Accessible.name: "عدد إعادات تنزيل YouTube التلقائية" }
                                        Item { Layout.fillWidth: true }
                                        LabelText { text: "إعادات تنزيل YouTube التلقائية (0 = بدون إعادة)" }
                                    }
                                    AppCheck { text: "ترتيب صارم: تنتظر المهمة عبور المهام السابقة من المرحلة نفسها"; checked: settings.strict_order; onClicked: settings.strict_order = checked; palette.windowText: win.ink; Layout.alignment: Qt.AlignRight }
                                    RowLayout {
                                        Layout.alignment: Qt.AlignRight
                                        AppCheck { text: "OpenCode / Word"; checked: settings.opencode_enabled; onClicked: settings.opencode_enabled = checked; palette.windowText: win.ink }
                                        AppCheck { text: "Deepgram"; checked: settings.deepgram_enabled; onClicked: settings.deepgram_enabled = checked; palette.windowText: win.ink }
                                        AppCheck { text: "التنزيل"; checked: settings.download_enabled; onClicked: settings.download_enabled = checked; palette.windowText: win.ink }
                                        LabelText { text: "القالب المخصص" }
                                    }
                                }
                            }
                            Box {
                                visible: win.settingsTab === 2; Layout.fillWidth: true; implicitHeight: libraryColumn.implicitHeight + 40
                                ColumnLayout {
                                    id: libraryColumn; anchors.fill: parent; anchors.margins: 14; spacing: 10
                                    LabelText { text: "مكتبة الملفات"; font.pixelSize: win.fs(18); Layout.alignment: Qt.AlignRight }
                                    SubText { text: "نسخة منظّمة من النتائج داخل مجلدات Word والتفريغات والصوت لسهولة الوصول من خارج التطبيق. المهمة تُعاد معالجتها فتُحدَّث نسختها في المكتبة."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                    AppCheck { text: "تنظيم تلقائي بعد اكتمال كل مرحلة"; checked: settings.library_auto; onClicked: settings.library_auto = checked; palette.windowText: win.ink; Layout.alignment: Qt.AlignRight }
                                    AppCheck { text: "انسخ الصوت أيضًا إلى المكتبة (يضاعف المساحة)"; checked: settings.library_copy_audio; onClicked: settings.library_copy_audio = checked; palette.windowText: win.ink; Layout.alignment: Qt.AlignRight }
                                }
                            }
                            Box {
                                visible: win.settingsTab === 3; Layout.fillWidth: true; implicitHeight: 300 * win.uiScale
                                ColumnLayout {
                                    anchors.fill: parent; anchors.margins: 14; spacing: 8
                                    LabelText { text: "تعليمات إنتاج مستند Word"; font.pixelSize: win.fs(18); Layout.alignment: Qt.AlignRight }
                                    SubText { text: "المتغيرات: {transcript_path}   {output_path}   {title}   {position}"; Layout.fillWidth: true }
                                    ScrollView {
                                        Layout.fillWidth: true; Layout.fillHeight: true
                                        TextArea { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; id: promptField; objectName: "promptField"; text: settings.prompt; color: win.ink; wrapMode: TextEdit.Wrap; selectByMouse: true; font.pixelSize: win.fs(14); horizontalAlignment: Text.AlignRight; background: Rectangle { color: visualTheme.inset; radius: 8; border.color: win.line } }
                                    }
                                }
                            }
                            AppButton { visible: win.settingsTab === 3; text: "تحرير جميع الإعدادات المتقدمة (JSON)"; Layout.alignment: Qt.AlignRight; onClicked: { rawSettings.text = backend.settingsJson; advancedDialog.open() } }
                            SubText { text: "الإعدادات الجديدة تُحفظ للمهام الجديدة. مفاتيح العمال ومسارات الاتصال قابلة للإصلاح للمهام المنتظرة."; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                        }
                    }
                }
            }
        }

        // Right navigation rail (collapsible; state persists in ui_rail_collapsed)
        Rectangle {
            id: navRail
            objectName: "navRail"
            Layout.preferredWidth: railOpen ? 220 : 64
            Layout.minimumWidth: railOpen ? 220 : 64
            Layout.maximumWidth: railOpen ? 220 : 64
            Layout.fillHeight: true
            color: visualTheme.rail
            Behavior on Layout.preferredWidth { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
            ColumnLayout {
                anchors.fill: parent; anchors.leftMargin: 8; anchors.rightMargin: 8
                anchors.topMargin: 12; anchors.bottomMargin: 12; spacing: 6
                RowLayout {
                    Layout.fillWidth: true; Layout.bottomMargin: 14; spacing: 4
                    AppButton {
                        objectName: "railToggle"
                        text: railOpen ? "»" : "«"
                        implicitWidth: 32; implicitHeight: 32
                        onClicked: { settings.ui_rail_collapsed = railOpen; save() }
                        Accessible.name: railOpen ? "طي القائمة الجانبية" : "توسيع القائمة الجانبية"
                        ToolTip.visible: hovered
                        ToolTip.delay: 500
                        ToolTip.text: Accessible.name
                    }
                    Item { Layout.fillWidth: true }
                    ColumnLayout {
                        visible: railOpen; spacing: 0
                        Text { text: "وَرَق"; color: visualTheme.railInk; font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; font.pixelSize: win.fs(28);  Layout.alignment: Qt.AlignRight }
                        Text { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; text: "WARAQ STUDIO"; color: visualTheme.railActive; font.pixelSize: win.fs(10); font.letterSpacing: 2; Layout.alignment: Qt.AlignRight }
                    }
                    Text { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; visible: !railOpen; text: "و"; color: visualTheme.railActive; font.pixelSize: win.fs(24);  }
                }
                Repeater {
                    model: win.pageNames
                    delegate: Button {
                        font.family: visualTheme.fontFamily
                        font.weight: visualTheme.fontWeight
                        required property string modelData
                        required property int index
                        objectName: "nav" + index
                        Layout.fillWidth: true; implicitHeight: win.rh(42)
                        Layout.minimumWidth: 0
                        onClicked: win.page = index
                        Accessible.name: modelData
                        ToolTip.visible: !railOpen && hovered
                        ToolTip.delay: 400
                        ToolTip.text: modelData
                        background: Rectangle { radius: 6; color: win.page === index ? visualTheme.railActive : (parent.hovered ? visualTheme.railHover : "transparent"); border.color: parent.activeFocus ? visualTheme.railInk : "transparent" }
                        contentItem: RowLayout {
                            spacing: 10
                            Text {
                                font.family: visualTheme.fontFamily
                                font.weight: visualTheme.fontWeight
                                Layout.fillWidth: true; visible: railOpen
                                text: modelData; font.pixelSize: win.fs(14);
                                color: win.page === index ? visualTheme.railSelectedInk : visualTheme.railInk
                                horizontalAlignment: Text.AlignRight; elide: Text.ElideRight
                            }
                            Image { source: "icons/" + win.pageIcons[index] + (win.page === index ? "-active" : "") + ".svg"; Layout.preferredWidth: 20; Layout.preferredHeight: 20; Layout.alignment: Qt.AlignHCenter }
                        }
                    }
                }
                Item { Layout.fillHeight: true }
                Rectangle { Layout.fillWidth: true; height: 1; color: visualTheme.line; visible: railOpen }
                Text { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; visible: railOpen; text: "سير عملك، تحت سيطرتك"; color: visualTheme.railMuted; font.pixelSize: win.fs(12); Layout.fillWidth: true; elide: Text.ElideRight; horizontalAlignment: Text.AlignRight; Layout.topMargin: 10 }
                Text { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; visible: railOpen; text: "نسخة محلية  ·  " + backend.version; color: visualTheme.railMuted; font.pixelSize: win.fs(11); Layout.alignment: Qt.AlignRight; Layout.bottomMargin: 4 }
            }
        }
    }

    DropArea {
        id: transcriptDropArea
        objectName: "transcriptDropArea"
        anchors.fill: parent
        z: 100
        onEntered: function(drag) { drag.accepted = drag.hasUrls && win.hasLocalTranscript(drag.urls) }
        onDropped: function(drop) {
            if (!drop.hasUrls || !win.hasLocalTranscript(drop.urls)) { drop.accepted = false; return }
            var urls = []
            for (var i = 0; i < drop.urls.length; i++) urls.push(drop.urls[i].toString())
            var added = backend.importDroppedTranscripts(urls, win.transcriptTemplateId(), sourceFolderCombo.currentValue || "")
            if (added > 0) { drop.accept(Qt.CopyAction); win.page = 0 }
            else drop.accepted = false
        }
        Rectangle {
            objectName: "transcriptDropOverlay"
            anchors.fill: parent; anchors.margins: win.rs(16)
            visible: transcriptDropArea.containsDrag
            radius: 16
            color: win.dark ? "#ec152a31" : "#ece7faf4"
            border.color: win.accent; border.width: 3
            ColumnLayout {
                anchors.centerIn: parent; width: Math.min(660, parent.width - 80); spacing: win.rs(16)
                LabelText { text: "أفلت تفريغات TXT لإضافتها إلى الطابور"; font.pixelSize: win.fs(24); Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap }
                SubText { text: "تُنسخ الملفات تلقائيًا إلى مجلد العمل لإنتاج Word عبر OpenCode. يُستخدم القالب ومجلد الدرس المحددان في صفحة إضافة مصدر، وتُتجاهل الملفات الأخرى."; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap }
                SubText { objectName: "transcriptDropOptions"; text: "القالب: " + templateBox.currentText + "  ·  المادة: " + sourceFolderCombo.currentText + "\nمجلد النتائج: " + win.savedOutputRoot; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WrapAnywhere }
            }
        }
    }

    Rectangle {
        visible: win.toastText.length > 0
        z: 20
        anchors.bottom: parent.bottom; anchors.bottomMargin: 20; anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(850, win.width - 100); height: toastLabel.implicitHeight + 36
        radius: 10; color: visualTheme.panel; border.color: win.toastError ? visualTheme.warning : visualTheme.success
        Text { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; id: toastLabel; anchors.fill: parent; anchors.margins: 18; color: win.ink; text: win.toastText; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignRight; font.pixelSize: win.fs(14) }
        TapHandler { onTapped: win.toastText = "" }
    }

    AppDialog {
        id: forgetInternetDialog; objectName: "forgetInternetDialog"
        anchors.centerIn: parent; width: Math.min(520, win.width - 80); modal: true
        title: "إلغاء تسجيل هذا الجهاز"
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: LabelText { text: "سيُلغى اعتماد الإنترنت لهذا الكمبيوتر واقترانات هواتفه على البوابة. تستمر مهام ورق المحلية، وستحتاج إلى رمز تسجيل جديد لإعادة ربطه. هل تريد المتابعة؟"; wrapMode: Text.WordWrap; padding: 14 }
        onAccepted: backend.forgetInternetDevice()
    }
    AppDialog {
        id: folderDialog; objectName: "folderDialog"
        anchors.centerIn: parent; width: Math.min(460, win.width - 48); modal: true; title: "مجلد مادة أو درس"
        standardButtons: Dialog.Save | Dialog.Cancel
        onOpened: { standardButton(Dialog.Save).text = "إنشاء"; standardButton(Dialog.Cancel).text = "إلغاء"; folderName.forceActiveFocus() }
        contentItem: WDialogBody {
            bodySpacing: 10
            SubText { text: "اجمع مواضيع المادة أو الدرس في مجلد واحد. سيظهر التنظيم في المكتبة وفي تصدير Word."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            AppField { id: folderName; Layout.fillWidth: true; placeholderText: "اسم المادة أو الدرس"; Accessible.name: "اسم المجلد الجديد" }
        }
        onAccepted: { var ident = backend.createFolder(folderName.text); if (ident) { var items = backend.folders; for (var i=0; i<items.length; i++) if (items[i].id === ident) sourceFolderCombo.currentIndex = i; folderName.text = "" } }
    }
    AppDialog {
        id: jobDialog; objectName: "jobDialog"
        anchors.centerIn: parent; width: Math.min(1100 * win.uiScale, win.width - 48); height: Math.min(760 * win.uiScale, win.height - 48); modal: true
        readonly property bool narrow: availableWidth / win.uiScale < 900
        // Keep keyboard focus inside the visible portion of the scrollable body.
        function revealFocusedItem() {
            if (!jobDialog.opened || !win.activeFocusItem) return
            var focused = win.activeFocusItem
            var target = focused
            var ancestor = focused
            while (ancestor && ancestor !== jobBodyScroll) {
                if (ancestor === logScroll) target = logScroll
                if (ancestor === jobFolderCombo) target = jobFolderCombo
                ancestor = ancestor.parent
            }
            if (ancestor !== jobBodyScroll) return
            var viewport = jobBodyScroll.contentItem
            var point = target.mapToItem(viewport, 0, 0)
            var maximum = Math.max(0, viewport.contentHeight - viewport.height)
            var next = viewport.contentY
            if (point.y < 8) next += point.y - 8
            else if (point.y + target.height > viewport.height - 8)
                next += point.y + target.height - viewport.height + 8
            viewport.contentY = Math.max(0, Math.min(maximum, next))
        }
        Connections {
            target: win
            function onActiveFocusItemChanged() { if (jobDialog.opened) Qt.callLater(jobDialog.revealFocusedItem) }
        }
        title: "تفاصيل المهمة"
        standardButtons: Dialog.Close
        onOpened: { standardButton(Dialog.Close).text = "إغلاق"; standardButton(Dialog.Close).objectName = "jobDialogCloseButton"; jobFolderCombo.currentIndex = Math.max(0, backend.folders.findIndex(function(f) { return f.id === selectedJob.folder_id })) }
        background: Rectangle { color: win.panel; radius: 10; border.color: win.line }
        header: Rectangle {
            color: visualTheme.inset; radius: 10
            implicitHeight: jobHeading.implicitHeight + 28
            ColumnLayout {
                id: jobHeading
                anchors { left: parent.left; right: parent.right; top: parent.top; margins: 14 }
                spacing: 5
                SubText { text: jobDialog.title; color: visualTheme.accent; Layout.fillWidth: true }
                LabelText {
                    objectName: "jobDialogTitle"; text: selectedJob.title || ""; textFormat: Text.PlainText
                    font.pixelSize: win.fs(20); Layout.fillWidth: true; wrapMode: Text.Wrap; elide: Text.ElideNone
                }
                SubText {
                    objectName: "jobDialogStatus"; text: (selectedJob.statusLabel || "") + "  ·  " + (selectedJob.stageLabel || "")
                    Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone
                }
            }
        }
        contentItem: ScrollView {
            id: jobBodyScroll; objectName: "jobDialogBodyScroll"
            clip: true; contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical: AppScroll { policy: ScrollBar.AsNeeded }
            ColumnLayout {
                width: jobBodyScroll.availableWidth
                height: Math.max(implicitHeight, jobBodyScroll.availableHeight)
                spacing: 12
                Rectangle {
                    objectName: "jobDialogStepper"; Layout.fillWidth: true
                    implicitHeight: jobStageRow.implicitHeight + 20
                    color: visualTheme.inset; radius: 8; border.color: win.line
                    RowLayout {
                        id: jobStageRow
                        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 10 }
                        layoutDirection: Qt.RightToLeft; spacing: 6
                        Repeater {
                            model: selectedJob.steps || []
                            RowLayout {
                                required property var modelData
                                required property int index
                                Layout.fillWidth: true; layoutDirection: Qt.RightToLeft; spacing: 6
                                StageBadge { objectName: "jobDialogStage-" + modelData.name; theme: visualTheme; step: modelData; Layout.fillWidth: true }
                                Rectangle { visible: index < (selectedJob.steps || []).length - 1; Layout.preferredWidth: 16 * win.uiScale; Layout.preferredHeight: 1; color: win.line }
                            }
                        }
                    }
                }
                RowLayout {
                    objectName: "jobDialogProgressRow"; visible: selectedJob.status === "running"
                    Layout.fillWidth: true; layoutDirection: Qt.RightToLeft; spacing: 10
                    SubText { text: selectedJob.stageLabel || ""; color: visualTheme.info }
                    AppProgress {
                        objectName: "jobDialogProgress"; Layout.fillWidth: true; from: 0; to: 100
                        value: selectedJob.progress || 0
                        indeterminate: selectedJob.status === "running" && (typeof selectedJob.progress !== "number" || !isFinite(selectedJob.progress) || selectedJob.progress < 0)
                    }
                    SubText {
                        objectName: "jobDialogProgressLabel"; color: visualTheme.info
                        text: typeof selectedJob.progress === "number" && isFinite(selectedJob.progress) && selectedJob.progress >= 0 ? Math.round(selectedJob.progress) + "٪" : "قيد التنفيذ"
                    }
                }
                GridLayout {
                    objectName: "jobDialogMetadata"; columns: jobDialog.availableWidth / win.uiScale >= 680 ? 3 : 1
                    Layout.fillWidth: true; layoutDirection: Qt.RightToLeft; columnSpacing: 10; rowSpacing: 10
                    Rectangle {
                        Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1
                        implicitHeight: jobFolderMeta.implicitHeight + 24; color: visualTheme.inset; radius: 8; border.color: win.line
                        ColumnLayout {
                            id: jobFolderMeta; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 12 } spacing: 8
                            SubText { text: "مجلد المادة / الدرس"; Layout.fillWidth: true }
                            AppCombo { id: jobFolderCombo; objectName: "jobDialogFolderCombo"; model: backend.folders; textRole: "name"; valueRole: "id"; Layout.fillWidth: true; Layout.minimumWidth: 0; onActivated: { backend.assignJobFolder(selectedJob.id, currentValue || ""); selectedJob = backend.jobById(selectedJob.id) } }
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1
                        implicitHeight: jobTemplateMeta.implicitHeight + 24; color: visualTheme.inset; radius: 8; border.color: win.line
                        ColumnLayout {
                            id: jobTemplateMeta; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 12 } spacing: 8
                            SubText { text: "القالب المستخدم"; Layout.fillWidth: true }
                            LabelText { objectName: "jobDialogTemplate"; text: selectedJob.template || "غير محدد"; textFormat: Text.PlainText; Layout.fillWidth: true; wrapMode: Text.Wrap; elide: Text.ElideNone; font.pixelSize: win.fs(13) }
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1
                        implicitHeight: jobSourceMeta.implicitHeight + 24; color: visualTheme.inset; radius: 8; border.color: win.line
                        ColumnLayout {
                            id: jobSourceMeta; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 12 } spacing: 8
                            SubText { text: "المصدر"; Layout.fillWidth: true }
                            LabelText { objectName: "jobDialogSource"; text: selectedJob.input_path || selectedJob.url || "غير متاح"; textFormat: Text.PlainText; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere; elide: Text.ElideNone; font.pixelSize: win.fs(12) }
                        }
                    }
                }
                Rectangle {
                    visible: !!selectedJob.retryAt || !!selectedJob.error
                    Layout.fillWidth: true; implicitHeight: jobNotice.implicitHeight + 24
                    color: visualTheme.inset; radius: 8; border.color: visualTheme.warning
                    ColumnLayout {
                        id: jobNotice; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 12 } spacing: 8
                        SubText {
                            objectName: "jobDialogRetryNotice"; text: selectedJob.retryAt ? "أُعيدت الجدولة تلقائيًا — المحاولة القادمة " + selectedJob.retryAt : ""
                            visible: !!selectedJob.retryAt; color: visualTheme.info; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone; textFormat: Text.PlainText
                        }
                        LabelText {
                            objectName: "jobDialogError"; text: selectedJob.error || ""; visible: !!selectedJob.error; color: visualTheme.warning
                            font.pixelSize: win.fs(13); wrapMode: Text.WrapAnywhere; elide: Text.ElideNone; textFormat: Text.PlainText; Layout.fillWidth: true
                        }
                    }
                }
                GridLayout {
                    objectName: "jobDialogWorkArea"; columns: jobDialog.narrow ? 1 : 2
                    Layout.fillWidth: true; Layout.fillHeight: true; columnSpacing: 12; rowSpacing: 12
                    Rectangle {
                        objectName: "jobDialogLogPane"; Layout.fillWidth: true; Layout.fillHeight: true
                        Layout.minimumHeight: 190 * win.uiScale; Layout.preferredWidth: 700 * win.uiScale
                        color: win.bg; radius: 8; border.color: win.line
                        ColumnLayout {
                            anchors.fill: parent; anchors.margins: 12; spacing: 8
                            LabelText { text: "سجل التنفيذ"; Layout.fillWidth: true; font.pixelSize: win.fs(14) }
                            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: win.line }
                            ScrollView {
                                id: logScroll; objectName: "jobDialogLogScroll"
                                Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                                contentWidth: availableWidth
                                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                                // updateJobLog preserves a normalized scroll position.
                                ScrollBar.vertical: AppScroll { readonly property real positionMaximum: 1.0 }
                                TextArea {
                                    id: jobLog; objectName: "jobDialogLog"
                                    font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight
                                    readOnly: true; selectByMouse: true; color: win.muted; wrapMode: TextEdit.Wrap; font.pixelSize: win.fs(12)
                                    textFormat: TextEdit.PlainText; Accessible.name: "سجل تنفيذ المهمة"
                                    background: Rectangle { color: win.bg }
                                }
                            }
                        }
                    }
                    Rectangle {
                        objectName: "jobDialogActionPane"; Layout.fillWidth: jobDialog.narrow; Layout.fillHeight: !jobDialog.narrow
                        Layout.preferredWidth: 300 * win.uiScale; Layout.minimumWidth: jobDialog.narrow ? 0 : 300 * win.uiScale
                        implicitHeight: jobActions.implicitHeight + 24
                        color: visualTheme.inset; radius: 8; border.color: win.line
                        ColumnLayout {
                            id: jobActions; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 12 } spacing: 10
                            LabelText { text: "إجراءات المهمة"; Layout.fillWidth: true; font.pixelSize: win.fs(14) }
                            SubText {
                                objectName: "jobDialogOutputAvailability"
                                text: "Word: " + (selectedJob.document ? "متاح" : "غير متاح") + " · التفريغ: " + (selectedJob.transcript ? "متاح" : "غير متاح") + " · الصوت: " + (selectedJob.audio ? "متاح" : "غير متاح")
                                Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone
                            }
                            GridLayout {
                                columns: jobDialog.narrow ? 2 : 1; Layout.fillWidth: true; layoutDirection: Qt.RightToLeft; columnSpacing: 8; rowSpacing: 8
                                AppButton { objectName: "jobDialogOpenWord"; text: "فتح Word"; enabled: !!selectedJob.document; primary: true; Layout.fillWidth: true; Layout.minimumWidth: 0; onClicked: backend.openPath(selectedJob.document) }
                                AppButton { objectName: "jobDialogOpenFolder"; text: "فتح المجلد"; Layout.fillWidth: true; Layout.minimumWidth: 0; onClicked: backend.openPath(selectedJob.folder) }
                                AppButton { objectName: "jobDialogRetry"; text: "إعادة المحاولة"; enabled: ["failed","blocked","uncertain","paused","cancelled"].indexOf(selectedJob.status) >= 0 || !!selectedJob.retryAt; Layout.fillWidth: true; Layout.minimumWidth: 0; onClicked: { retryWarning.open() } }
                                AppButton { objectName: "jobDialogCancel"; text: "إلغاء المهمة"; danger: true; enabled: selectedJob.status !== "completed"; Layout.fillWidth: true; Layout.minimumWidth: 0; onClicked: { backend.cancelJob(selectedJob.id); jobDialog.close() } }
                                AppButton { objectName: "jobDialogNewWord"; text: "إعادة OpenCode بنسخة جديدة"; enabled: !!selectedJob.transcript && selectedJob.status !== "running"; Layout.fillWidth: true; Layout.minimumWidth: 0; onClicked: { backend.retry(selectedJob.id,"opencode",true); jobDialog.close() } }
                                AppButton { objectName: "jobDialogNewTranscript"; text: "إعادة التفريغ بنسخة جديدة"; enabled: !!selectedJob.audio && selectedJob.status !== "running"; Layout.fillWidth: true; Layout.minimumWidth: 0; onClicked: { paidRerun.open() } }
                            }
                        }
                    }
                }
            }
        }
    }
    AppDialog {
        id: retryWarning; objectName: "retryWarning"; anchors.centerIn: parent; width: Math.min(500, win.width - 48); modal: true; title: "إعادة المحاولة"; standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "إعادة المحاولة"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; width: parent.width; wrapMode: Text.WordWrap; text: selectedJob.status === "uncertain" && selectedJob.current === "opencode"
                ? "جلسة ChatGPT السابقة لم يُتحقق من توقفها. افتح صفحة العمال واضغط «فحص الجلسة السابقة» للحساب؛ بعد تأكيد توقفها يمكنك إعادة المحاولة."
                : selectedJob.status === "uncertain" ? "الطلب السابق لدى Deepgram قد يكون اكتمل. إعادة الإرسال قد تسبب تكلفة إضافية. هل تريد المحاولة؟"
                : (selectedJob.current === "download" && selectedJob.status === "blocked") ? "توقف التنزيل لأن YouTube رفض الوصول أو لنقص المساحة. أصلح السبب أولًا (الكوكيز أو المساحة) ثم أعد المحاولة؛ سيُرفع التوقف الآلي عن خدمة التنزيل ويُعاد العمل دون المساس بالإيقاف اليدوي."
                : (selectedJob.status === "pending" && selectedJob.retryAt) ? "المهمة مجدولة تلقائيًا حتى المحاولة القادمة. الإعادة اليدوية تعيدها إلى الطابور لكنها لا تتجاوز تهدئة تقييد YouTube."
                : "ستُستأنف المرحلة باستخدام الملفات المكتملة المحفوظة." }
        onAccepted: { backend.retry(selectedJob.id, selectedJob.current, false); jobDialog.close() }
    }
    AppDialog {
        id: bulkRetry; objectName: "bulkRetry"; anchors.centerIn: parent; width: Math.min(530, win.width - 48); modal: true; title: "إعادة محاولة جماعية"; standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "إعادة " + (bulkGroup.ids || []).length + " مهمة"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; width: parent.width; wrapMode: Text.WordWrap
            text: bulkGroup.status === "uncertain" && bulkGroup.stage === "opencode"
                ? "تحتاج جلسات ChatGPT غير المؤكدة إلى إثبات توقفها أولًا من «فحص الجلسة السابقة» في صفحة العمال. ستُعاد فقط المهام التي تأكد توقف جلساتها."
                : bulkGroup.status === "uncertain"
                ? "نتائج " + (bulkGroup.ids || []).length + " طلب تفريغ غير مؤكدة. قد تكون اكتملت لدى Deepgram، وإعادة إرسالها قد تسبب تكلفة إضافية لكل مهمة. هل تريد إعادة المحاولة للجميع؟"
                : "ستُعاد " + (bulkGroup.ids || []).length + " مهمة من مجموعة «" + (bulkGroup.label || "") + "» إلى الطابور باستخدام الملفات المكتملة المحفوظة."
        }
        onAccepted: backend.retryMany(bulkGroup.ids, bulkGroup.stage, bulkGroup.status)
    }
    AppDialog {
        id: paidRerun; objectName: "paidRerun"; anchors.centerIn: parent; width: Math.min(500, win.width - 48); modal: true; title: "تفريغ جديد"; standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "تفريغ جديد"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; width: parent.width; wrapMode: Text.WordWrap; text: "سيُرسل الصوت مجددًا إلى Deepgram وقد تنتج تكلفة جديدة. ستبقى النسخة السابقة محفوظة." }
        onAccepted: { backend.retry(selectedJob.id, "deepgram", true); jobDialog.close() }
    }
    AppDialog {
        id: accountLoginMethodDialog; objectName: "accountLoginMethodDialog"; anchors.centerIn: parent
        width: Math.min(win.width - 40, 620 * win.uiScale); height: Math.min(win.height - 60, 540 * win.uiScale)
        modal: true; title: "اختر طريقة تسجيل الدخول"; standardButtons: Dialog.Cancel
        visible: !!win.accountLoginChoice.accountId
        onOpened: { standardButton(Dialog.Cancel).text = "إلغاء"; standardButton(Dialog.Cancel).objectName = "accountLoginChoiceCancelButton" }
        onRejected: backend.cancelAccountLoginChoice()
        background: Rectangle { color: win.panel; radius: 12; border.color: win.line }
        contentItem: ScrollView {
            id: accountLoginMethodScroll; clip: true; contentWidth: availableWidth
            ColumnLayout {
                width: accountLoginMethodScroll.availableWidth; spacing: win.rs(12)
                LabelText { objectName: "accountLoginChoiceAlias"; textFormat: Text.PlainText; text: win.accountLoginChoice.alias || ""; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone }
                SubText { text: "لن يتغير تسجيل الحساب الحالي حتى تختار طريقة. أكمل التفويض بنفسك في المتصفح واختر الحساب المطلوب صراحةً."; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone }
                SubText { objectName: "accountLoginChoiceLoading"; visible: win.accountLoginChoice.loading === true; text: "جارٍ قراءة طرق الدخول المتاحة في OpenCode…"; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone }
                SubText { objectName: "accountLoginChoiceError"; text: win.accountLoginChoice.error || ""; visible: text.length > 0; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone }
                Repeater {
                    model: win.accountLoginChoice.methods || []
                    ColumnLayout {
                        required property var modelData
                        Layout.fillWidth: true; spacing: win.rs(4)
                        AppButton { objectName: "accountLoginMethodButton"; text: modelData.label; Layout.fillWidth: true; onClicked: backend.chooseAccountLoginMethod(win.accountLoginChoice.accountId, modelData.id); Accessible.name: "تسجيل " + (win.accountLoginChoice.alias || "") + " بواسطة " + text }
                        SubText {
                            objectName: "accountLoginMethodExplanation"; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone
                            text: "أكمل تفويض الحساب في المتصفح. يعرض ورق الحصص التي يعيدها محرك الحسابات المدمج فقط."
                        }
                    }
                }
                AppButton { objectName: "accountLoginChoiceRetryButton"; text: "إعادة قراءة طرق الدخول"; visible: !!win.accountLoginChoice.error; onClicked: backend.loginAccount(win.accountLoginChoice.accountId) }
            }
        }
    }
    AppDialog {
        id: renameAccountDialog; objectName: "renameAccountDialog"; anchors.centerIn: parent; width: 450 * win.uiScale; modal: true
        title: "تغيير اسم الحساب"; standardButtons: Dialog.Save | Dialog.Cancel
        onOpened: { standardButton(Dialog.Save).text = "حفظ"; standardButton(Dialog.Save).objectName = "accountRenameSaveButton"; standardButton(Dialog.Cancel).text = "إلغاء"; accountRename.forceActiveFocus() }
        background: Rectangle { color: win.panel; radius: 12; border.color: win.line }
        contentItem: ColumnLayout {
            LabelText { text: "اسم الحساب"; Layout.alignment: Qt.AlignRight }
            AppField { id: accountRename; objectName: "accountRenameField"; Layout.fillWidth: true; maximumLength: 80; Accessible.name: "اسم الحساب الجديد" }
        }
        onAccepted: backend.renameAccount(win.pendingAccount.id, accountRename.text)
    }
    AppDialog {
        id: removeAccountDialog; objectName: "removeAccountDialog"; anchors.centerIn: parent; width: 480 * win.uiScale; implicitHeight: 230 * win.uiScale; modal: true
        title: "حذف حساب ChatGPT"; standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "حذف الحساب"; standardButton(Dialog.Ok).objectName = "accountRemoveConfirmButton"; standardButton(Dialog.Cancel).text = "إلغاء"; standardButton(Dialog.Cancel).objectName = "accountRemoveCancelButton" }
        background: Rectangle { color: win.panel; radius: 12; border.color: win.line }
        contentItem: Label { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; wrapMode: Text.WordWrap; text: "سيُحذف تسجيل دخول «" + (win.pendingAccount.alias || "") + "» من ورق. يمكنك إضافته وتسجيل دخوله لاحقًا. هل تريد حذفه؟" }
        onAccepted: backend.removeAccount(win.pendingAccount.id)
    }
    AppDialog {
        id: workerDialog; objectName: "workerDialog"; anchors.centerIn: parent; width: Math.min(620, win.width - 48); height: Math.min(workerKind === "deepgram" ? 650 : 610, win.height - 48); modal: true
        property string modelDraft: ""
        property string languageDraft: "auto"
        title: "إعدادات العامل"; standardButtons: Dialog.Save | Dialog.Cancel
        onOpened: { standardButton(Dialog.Save).text = "حفظ"; standardButton(Dialog.Save).objectName = "workerSaveButton"; standardButton(Dialog.Cancel).text = "إلغاء"; Qt.callLater(win.fillWorkerFields); backend.refreshModelCatalogs() }
        background: Rectangle { color: win.panel; radius: 10; border.color: win.line }
        contentItem: WDialogBody {
            bodySpacing: 10
            LabelText { text: "اسم العامل"; Layout.alignment: Qt.AlignRight }
            AppField { id: workerName; Layout.fillWidth: true }
            LabelText { text: "النموذج"; Layout.alignment: Qt.AlignRight }
            GridLayout { columns: win.contentUnits < 850 ? 1 : 2;
                Layout.fillWidth: true
                AppCombo {
                    id: workerModel; objectName: "workerModelBox"
                    Layout.fillWidth: true; editable: true
                    model: workerKind === "deepgram" ? win.deepgramModelOptions(editingWorker.model)
                                                      : win.opencodeModelOptions(workerProvider.currentValue, editingWorker.model)
                    onModelChanged: Qt.callLater(function() { if (workerDialog.opened) workerModel.editText = workerDialog.modelDraft })
                    onActivated: { workerDialog.modelDraft = editText; if (workerKind === "deepgram") Qt.callLater(win.selectWorkerLanguage) }
                    onAccepted: workerDialog.modelDraft = editText
                    Accessible.name: "نموذج العامل"
                }
                AppButton { text: "تحديث القائمة"; enabled: !backend.catalogBusy; onClicked: backend.refreshModelCatalogs() }
            }
            SubText {
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                text: backend.catalogBusy ? "جارٍ تحميل النماذج المتاحة…"
                      : (backend.catalogErrors[workerKind] || (workerKind === "deepgram"
                          ? "نماذج التفريغ ولغاتها من Deepgram. يمكن كتابة معرّف نموذج مخصص."
                          : "النماذج من محرك ورق المضمّن. يمكن كتابة معرّف نموذج مخصص."))
            }
            GridLayout { layoutDirection: Qt.RightToLeft; columns: win.contentUnits < 850 ? 1 : 2;
                visible: workerKind !== "deepgram"
                LabelText { text: "مزود الخدمة" }
                AppCombo { id: workerProvider; objectName: "workerProviderBox"; Layout.fillWidth: true; model: backend.providers; textRole: "name"; valueRole: "id" }
            }
            SubText {
                visible: workerKind !== "deepgram"
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                text: workerProvider.currentValue === "login"
                      ? "يستخدم حسابات الاشتراك المحفوظة داخل محرك ورق المدمج دون مفتاح API. سجّل الدخول من لوحة الحسابات."
                      : "يُشغَّل بمفتاح هذا العامل، وفي مجلد بيانات مستقل حتى لا تتعارض العمليات المتوازية. المعرّف المرسل: " + win.resolvedModel(workerProvider.currentValue, workerModel.editText)
            }
            GridLayout { columns: win.contentUnits < 850 ? 1 : 2;
                Layout.fillWidth: true
                AppCombo {
                    id: workerLanguage; objectName: "workerLanguageBox"
                    visible: workerKind === "deepgram"; Layout.fillWidth: true
                    model: win.deepgramLanguageOptions(workerModel.editText,
                                                        workerModel.editText === editingWorker.model ? editingWorker.language : "")
                    textRole: "name"; valueRole: "id"
                    onModelChanged: Qt.callLater(function() { if (workerDialog.opened && workerKind === "deepgram") win.selectWorkerLanguage() })
                    onActivated: workerDialog.languageDraft = currentValue
                    Accessible.name: "لغة التفريغ"
                }
                AppField { id: workerExtra; visible: workerKind !== "deepgram"; Layout.fillWidth: true }
                LabelText { text: workerKind === "deepgram" ? "لغة التفريغ" : "مستوى التفكير (فارغ = بلا درجة)" }
            }
            GridLayout { layoutDirection: Qt.RightToLeft; columns: win.contentUnits < 850 ? 1 : 2;
                LabelText { text: workerKind === "deepgram" ? "معرّف المشروع المشترك" : "الوكيل" }
                AppField { id: workerProject; Layout.fillWidth: true }
            }
            GridLayout { layoutDirection: Qt.RightToLeft; columns: win.contentUnits < 850 ? 1 : 2;
                visible: workerKind === "deepgram"; Layout.fillWidth: true
                LabelText { text: "دور العامل" }
                AppCombo { id: workerRole; model: ["أساسي", "احتياطي لإعادة التفريغ"]; Layout.fillWidth: true }
            }
            AppCombo { id: endpoint; visible: workerKind === "deepgram"; Layout.fillWidth: true; model: ["https://api.deepgram.com", "https://api.eu.deepgram.com", "https://api.au.deepgram.com"] }
            AppCheck { id: diarize; visible: workerKind === "deepgram"; text: "تمييز المتحدثين"; palette.windowText: win.ink }
            LabelText {
                visible: workerKind === "deepgram" || workerProvider.currentValue !== "login"
                text: workerKind === "deepgram" ? "مفتاح API خاص بهذا العامل" : "مفتاح المزود لهذا العامل"
                Layout.alignment: Qt.AlignRight
            }
            AppField {
                id: apiKey
                visible: workerKind === "deepgram" || workerProvider.currentValue !== "login"
                Layout.fillWidth: true; echoMode: TextInput.Password
                placeholderText: "اتركه فارغًا للاحتفاظ بالمفتاح المحفوظ"
            }
            AppCheck { id: workerEnabled; text: "استقبال مهام جديدة"; checked: editingWorker.enabled || false; palette.windowText: win.ink }
            Item { Layout.fillHeight: true }
        }
        onAccepted: {
            editingWorker.name = workerName.text; editingWorker.model = workerModel.editText.trim(); editingWorker.enabled = workerEnabled.checked
            if(workerKind === "deepgram") {
                editingWorker.language = workerLanguage.currentValue || "auto"; editingWorker.project = workerProject.text; editingWorker.endpoint = endpoint.currentText; editingWorker.diarize = diarize.checked; editingWorker.role = workerRole.currentIndex === 1 ? "backup" : "primary"
                settings.deepgram_workers[workerIndex] = editingWorker
            } else {
                editingWorker.variant = workerExtra.text; editingWorker.agent = workerProject.text
                editingWorker.provider = workerProvider.currentValue || "login"
                settings.opencode_workers[workerIndex] = editingWorker
            }
            var needsKey = workerKind === "deepgram" || editingWorker.provider !== "login"
            if(save() && needsKey && apiKey.text.length > 0) backend.saveKey(editingWorker.id, apiKey.text)
            apiKey.text = ""
        }
        onRejected: apiKey.text = ""
    }
    AppDialog {
        id: forceDialog; objectName: "forceDialog"
        property bool starting: true
        property string modelDraft: ""
        anchors.centerIn: parent; width: Math.min(640, win.width - 48); height: Math.min(460, win.height - 48); modal: true
        title: "فرض إعداد على عمال OpenCode"
        onOpened: backend.refreshModelCatalogs()
        background: Rectangle { color: win.panel; radius: 10; border.color: win.line }
        footer: GridLayout { objectName: "forceDialogActions"; columns: win.contentUnits < 850 ? 1 : 2;
                width: parent.width; columnSpacing: 10
                AppButton {
                    objectName: "forceDefaultButton"
                    text: forceDialog.starting ? "تشغيل بالإعداد الافتراضي للعمال" : "إلغاء الفرض"
                    onClicked: { win.skipForceQuestion = skipForceBox.checked; win.applyForce(false, forceDialog.starting) }
                }
                AppButton {
                    objectName: "forceApplyButton"
                    text: forceDialog.starting ? "تشغيل بالإعداد المفروض" : "تطبيق الفرض"
                    primary: true
                    onClicked: { win.skipForceQuestion = skipForceBox.checked; win.applyForce(true, forceDialog.starting) }
                }
                Item { Layout.fillWidth: true }
                AppButton { text: "رجوع"; onClicked: forceDialog.close() }
            }
        contentItem: WDialogBody {
            bodySpacing: 12
            LabelText {
                objectName: "forceQuestion"
                Layout.fillWidth: true; wrapMode: Text.WordWrap; font.pixelSize: win.fs(17)
                text: forceDialog.starting
                      ? "قبل تشغيل الطابور: هل تفرض مزودًا ونموذجًا ومستوى تفكير على كل عمال OpenCode، أم يبقى كل عامل على إعداده؟"
                      : "اختر الإعداد المفروض على كل عمال OpenCode، أو ألغِ الفرض ليعود كل عامل إلى إعداده."
            }
            GridLayout { layoutDirection: Qt.RightToLeft; columns: win.contentUnits < 850 ? 1 : 2;
                Layout.fillWidth: true
                LabelText { text: "المزود" }
                AppCombo { id: forceProvider; objectName: "forceProviderBox"; Layout.fillWidth: true; model: backend.providers; textRole: "name"; valueRole: "id" }
            }
            GridLayout { layoutDirection: Qt.RightToLeft; columns: win.contentUnits < 850 ? 1 : 2;
                Layout.fillWidth: true
                LabelText { text: "النموذج" }
                AppCombo {
                    id: forceModel; objectName: "forceModelField"; Layout.fillWidth: true; editable: true
                    model: win.opencodeModelOptions(forceProvider.currentValue, "")
                    onModelChanged: Qt.callLater(function() { if (forceDialog.opened) forceModel.editText = forceDialog.modelDraft })
                    onActivated: forceDialog.modelDraft = editText
                    onAccepted: forceDialog.modelDraft = editText
                    Accessible.name: "نموذج OpenCode المفروض"
                }
            }
            GridLayout { layoutDirection: Qt.RightToLeft; columns: win.contentUnits < 850 ? 1 : 2;
                Layout.fillWidth: true
                LabelText { text: "مستوى التفكير" }
                AppField { id: forceVariant; objectName: "forceVariantField"; Layout.fillWidth: true; horizontalAlignment: Text.AlignRight; placeholderText: "high أو max أو اتركه فارغًا" }
            }
            SubText {
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                text: "الفرض لجلسة واحدة ولا يُحفظ. عامل بلا مفتاح للمزود المفروض يُتخطى برسالة واضحة في صفحة العمال."
            }
            AppCheck { id: skipForceBox; objectName: "skipForceBox"; text: "تخطَّ هذا السؤال في هذه الجلسة"; palette.windowText: win.ink; Layout.alignment: Qt.AlignRight }
            Item { Layout.fillHeight: true }

        }
    }
    AppDialog {
        id: providersDialog; objectName: "providersDialog"
        anchors.centerIn: parent; width: Math.min(780, win.width - 48); height: Math.min(620, win.height - 48); modal: true
        title: "مزودو الخدمة"
        background: Rectangle { color: win.panel; radius: 10; border.color: win.line }
        footer: GridLayout { objectName: "providersDialogActions"; columns: win.contentUnits < 850 ? 1 : 2;
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                AppButton { text: "إغلاق"; onClicked: providersDialog.close() }
            }
        contentItem: WDialogBody {
            bodySpacing: 12
            SubText {
                Layout.fillWidth: true; wrapMode: Text.WordWrap
                text: "المزودون المدمجون ثابتون. أضف مزودك بمعرّفه كما يستخدمه OpenCode ومتغير المفتاح الذي يقرأ منه، واترك العزل مفعّلًا ليأخذ كل عامل مجلد بيانات مستقلًا."
            }
            PinList {
                id: providerList
                objectName: "providerList"
                Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: win.rs(8)
                model: backend.providersModel
                ScrollBar.vertical: AppScroll {}
                delegate: Box {
                    width: providerList.width; height: win.rh(56)
                    GridLayout { columns: win.contentUnits < 850 ? 1 : 2;
                        anchors.fill: parent; anchors.margins: win.rs(10); columnSpacing: win.rs(8)
                        AppButton { text: "حذف"; danger: true; implicitHeight: Math.max(22, Math.round(28 * win.density)); visible: !row.builtin; onClicked: { win.pendingProvider = {id: row.id, name: row.name}; deleteProviderDialog.open() } }
                        AppButton {
                            text: "تعديل"; implicitHeight: Math.max(22, Math.round(28 * win.density)); visible: !row.builtin
                            onClicked: { provId.text = row.id; provName.text = row.name; provEnv.text = row.keyEnv; provIsolated.checked = row.isolated }
                        }
                        Item { Layout.fillWidth: true }
                        ColumnLayout {
                            Layout.fillWidth: true
                            LabelText { text: row.name; font.pixelSize: win.fs(16); Layout.fillWidth: true }
                            SubText { text: row.id + (row.keyEnv ? "  ·  " + row.keyEnv : "  ·  بلا مفتاح (تسجيل دخول)") + (row.builtin ? "  ·  مدمج" : "  ·  مضاف يدويًا") }
                        }
                    }
                }
            }
            Box {
                Layout.fillWidth: true; implicitHeight: provForm.implicitHeight + 36
                ColumnLayout {
                    id: provForm; anchors.fill: parent; anchors.margins: 12; spacing: 8
                    GridLayout { layoutDirection: Qt.RightToLeft; columns: win.contentUnits < 850 ? 1 : 2;
                        Layout.fillWidth: true
                        LabelText { text: "المعرّف" }
                        AppField { id: provId; horizontalAlignment: Text.AlignLeft; objectName: "providerIdField"; Layout.fillWidth: true; placeholderText: "groq" }
                    }
                    GridLayout { layoutDirection: Qt.RightToLeft; columns: win.contentUnits < 850 ? 1 : 2;
                        Layout.fillWidth: true
                        LabelText { text: "الاسم المعروض" }
                        AppField { id: provName; Layout.fillWidth: true; placeholderText: "Groq" }
                    }
                    GridLayout { layoutDirection: Qt.RightToLeft; columns: win.contentUnits < 850 ? 1 : 2;
                        Layout.fillWidth: true
                        LabelText { text: "متغير المفتاح" }
                        AppField { id: provEnv; horizontalAlignment: Text.AlignLeft; objectName: "providerEnvField"; Layout.fillWidth: true; placeholderText: "GROQ_API_KEY" }
                    }
                    GridLayout { columns: win.contentUnits < 850 ? 1 : 2;
                        Layout.fillWidth: true; columnSpacing: 10
                        AppCheck { id: provIsolated; text: "عزل ملفات العامل (مستحسن)"; checked: true; palette.windowText: win.ink }
                        Item { Layout.fillWidth: true }
                        AppButton {
                            objectName: "providerSaveButton"
                            text: "حفظ المزود"; primary: true
                            onClicked: {
                                var raw = JSON.stringify({id: provId.text.trim(), name: provName.text.trim(), keyEnv: provEnv.text.trim(), isolated: provIsolated.checked})
                                if (backend.saveProvider(raw)) { provId.text = ""; provName.text = ""; provEnv.text = ""; provIsolated.checked = true }
                            }
                        }
                    }
                }
            }

        }
    }
    AppDialog {
        id: deleteProviderDialog; objectName: "deleteProviderDialog"; anchors.centerIn: parent; width: Math.min(500, win.width - 48); modal: true; title: "حذف مزود"
        standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "حذف"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; width: parent.width; wrapMode: Text.WordWrap; text: "سيُحذف المزود «" + win.pendingProvider.name + "» من القائمة. يمكنك إضافته مجددًا في أي وقت، ولا يُحذف أي عامل يستخدمه." }
        onAccepted: backend.deleteProvider(win.pendingProvider.id)
    }
    AppDialog {
        id: deleteFileDialog; objectName: "deleteFileDialog"; anchors.centerIn: parent; width: Math.min(520, win.width - 48); modal: true; title: "حذف من المكتبة"
        standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "حذف"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; width: parent.width; wrapMode: Text.WordWrap; text: "سيُحذف «" + win.pendingFile.name + "» من مجلد المكتبة. النسخة الأصلية داخل مجلد المهمة تبقى محفوظة، ويمكن إعادة التنظيم لاحقًا." }
        onAccepted: backend.deleteLibraryFile(win.pendingFile.path)
    }
    AppDialog {
        id: advancedDialog; objectName: "advancedDialog"; anchors.centerIn: parent; width: Math.min(850, win.width - 100); height: win.height - 120; modal: true
        title: "الإعدادات المتقدمة — دون مفاتيح API"; standardButtons: Dialog.Save | Dialog.Cancel
        onOpened: { standardButton(Dialog.Save).text = "حفظ"; standardButton(Dialog.Cancel).text = "إلغاء" }
        contentItem: ScrollView { TextArea { color: win.ink; selectionColor: win.accent; selectedTextColor: visualTheme.accentInk; background: Rectangle { color: visualTheme.inset } id: rawSettings; objectName: "rawSettings"; text: backend.settingsJson; selectByMouse: true; wrapMode: TextEdit.Wrap; font.family: visualTheme.monoFontFamily; font.weight: visualTheme.fontWeight; font.pixelSize: win.fs(13) } }
        onAccepted: backend.saveSettings(rawSettings.text)
    }
    AppDialog {
        id: templatesDialog; objectName: "templatesDialog"; anchors.centerIn: parent; width: Math.min(860, win.width - 80); height: win.height - 140; modal: true
        title: "قوالب البرومبت"; standardButtons: Dialog.Save | Dialog.Cancel
        onOpened: { standardButton(Dialog.Save).text = "حفظ القوالب"; standardButton(Dialog.Cancel).text = "إلغاء"; win.reloadTemplates() }
        background: Rectangle { color: win.panel; radius: 10; border.color: win.line }
        contentItem: WDialogBody {
            bodySpacing: 10
            GridLayout { columns: win.contentUnits < 850 ? 1 : 2;
                Layout.fillWidth: true
                AppButton { text: "قالب جديد"; onClicked: {
                    win.commitTemplateFields()
                    var next = win.templ.slice()
                    next.push({id: "tpl" + Date.now(), name: "قالب " + (next.length + 1), text: "{transcript_path}\n{output_path}\n"})
                    win.templ = next
                    tplBox.currentIndex = next.length - 1
                    win.loadTemplateFields()
                } }
                AppButton { text: "حذف المحدد"; enabled: tplBox.currentIndex >= 0; onClicked: {
                    var index = tplBox.currentIndex
                    var next = win.templ.slice()
                    next.splice(index, 1)
                    win.templ = next
                    tplBox.currentIndex = next.length ? Math.min(index, next.length - 1) : -1
                    win.loadTemplateFields()
                } }
                Item { Layout.fillWidth: true }
                LabelText { text: "القوالب المحفوظة"; font.pixelSize: win.fs(16) }
            }
            AppCombo { id: tplBox; objectName: "templateEditorBox"; Layout.fillWidth: true; model: win.templ; textRole: "name"; onActivated: win.loadTemplateFields() }
            GridLayout { layoutDirection: Qt.RightToLeft; columns: win.contentUnits < 850 ? 1 : 2;
                LabelText { text: "اسم القالب"; Layout.preferredWidth: 110 }
                AppField { id: tplName; Layout.fillWidth: true; Accessible.name: "اسم القالب" }
            }
            SubText { text: "يجب أن يحتوي كل قالب {transcript_path} و{output_path}، ويمكن استخدام {title} و{position}."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true
                TextArea { font.family: visualTheme.fontFamily; font.weight: visualTheme.fontWeight; id: tplText; color: win.ink; wrapMode: TextEdit.Wrap; selectByMouse: true; font.pixelSize: win.fs(14); horizontalAlignment: Text.AlignRight; background: Rectangle { color: visualTheme.inset; radius: 8; border.color: win.line } }
            }
        }
        onAccepted: {
            win.commitTemplateFields()
            settings.opencode_templates = win.templ
            if (save()) win.templ = JSON.parse(JSON.stringify(backend.templates))
        }
    }
}
