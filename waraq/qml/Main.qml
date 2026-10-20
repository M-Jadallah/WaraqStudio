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
    property bool dark: settings.theme !== "light"
    readonly property color bg: visualTheme.bg
    readonly property color panel: visualTheme.panel
    readonly property color line: visualTheme.line
    readonly property color ink: visualTheme.ink
    readonly property color muted: visualTheme.muted
    readonly property color accent: visualTheme.accent
    Theme { id: visualTheme; dark: win.dark; scale: win.uiScale }
    readonly property real contentUnits: (width - navRail.width - 32) / uiScale
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
    property string search: ""
    property string sourceSearch: ""
    property string fileKind: "all"
    property var pendingFile: ({})
    property var pendingProvider: ({})
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
    font.family: "Segoe UI"
    font.pixelSize: win.fs(14)

    function matches(job) {
        return (filter === "all" || job.status === filter || (filter === "attention" && ["blocked", "failed", "uncertain"].indexOf(job.status) >= 0))
            && (search.length === 0 || job.title.toLowerCase().indexOf(search.toLowerCase()) >= 0)
    }
    function attentionGroups() {
        var groups = {}
        backend.jobs.forEach(function(job) {
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
        if ((w.variant || "").length > 0) parts.push("تفكير " + w.variant)
        parts.push(!w.keyEnv ? "تسجيل دخول OpenCode" : (w.hasKey ? "المفتاح محفوظ" : "لا مفتاح محفوظ"))
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
        font.family: "Segoe UI"
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
                LabelText { text: group.heading; font.weight: Font.DemiBold; Layout.fillWidth: true; wrapMode: Text.WordWrap }
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
                        LabelText { text: row.name; font.weight: Font.DemiBold; Layout.fillWidth: true }
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
                Layout.preferredHeight: headerContent.implicitHeight + 28
                color: win.bg
                ColumnLayout {
                    id: headerContent
                    anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                    anchors.margins: 16; spacing: 8
                    RowLayout {
                        Layout.fillWidth: true
                        SubText { text: backend.running ? "● المحرك يعمل" : "○ المحرك متوقف"; color: backend.running ? visualTheme.success : win.muted }
                        Item { Layout.fillWidth: true }
                        ColumnLayout {
                            Layout.fillWidth: true; spacing: 2
                            LabelText { text: win.pageNames[win.page]; font.pixelSize: win.fs(22); font.weight: Font.DemiBold; Layout.fillWidth: true }
                            SubText { text: win.pageDescriptions[win.page]; Layout.fillWidth: true }
                        }
                    }
                    ActionRow {
                        AppButton { objectName: "runButton"; text: backend.running ? "إيقاف بعد المهام الحالية" : "تشغيل الطابور"; primary: !backend.running; onClicked: win.runClicked() }
                        AppButton { objectName: "forceButton"; text: "فرض إعداد على عمال OpenCode"; onClicked: win.openForceDialog(false) }
                        AppCheck { objectName: "autoStopBox"; text: "إيقاف التطبيق بعد اكتمال الطابور"; onClicked: backend.setAutoStop(checked); Accessible.name: "إيقاف التطبيق تلقائيًا بعد اكتمال الطابور" }
                    }
                    SubText { objectName: "forceSummary"; text: win.forceSummary(); color: win.lastForce.provider ? win.accent : win.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap; elide: Text.ElideNone; visible: !!win.lastForce.provider }
                }
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.line }
            }

            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 16
                currentIndex: win.page

                // Operations
                ColumnLayout {
                    spacing: 12
                    GridLayout {
                        Layout.fillWidth: true
                        columns: win.contentUnits < 850 ? 2 : 4
                        columnSpacing: 10; rowSpacing: 8
                        Repeater {
                            model: [
                                {label: "تحتاج انتباهك", value: backend.stats.attention, color: visualTheme.warning},
                                {label: "مكتملة", value: backend.stats.done, color: visualTheme.success},
                                {label: "قيد التنفيذ", value: backend.stats.active, color: visualTheme.info},
                                {label: "إجمالي المهام", value: backend.stats.total, color: win.ink}
                            ]
                            delegate: Box {
                                required property var modelData
                                Layout.fillWidth: true
                                Layout.preferredHeight: Math.round(76 * win.uiScale)
                                ColumnLayout {
                                    anchors.fill: parent; anchors.margins: 12; spacing: 3
                                    SubText { text: modelData.label; Layout.alignment: Qt.AlignRight }
                                    LabelText { text: modelData.value; font.pixelSize: win.fs(24); color: win.dark ? modelData.color : win.ink; Layout.alignment: Qt.AlignRight }
                                }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        AppButton { text: "إضافة مصدر"; gold: true; onClicked: win.page = 1 }
                        AppButton { text: "تقرير Excel"; onClicked: backend.exportReport() }
                        Item { Layout.fillWidth: true }
                        AppField { Layout.preferredWidth: Math.min(240, win.width * 0.2); placeholderText: "بحث في المهام"; horizontalAlignment: Text.AlignRight; onTextChanged: win.search = text; Accessible.name: "بحث في المهام" }
                        LabelText { text: "طابور المعالجة"; font.pixelSize: win.fs(18); font.weight: Font.DemiBold }
                    }
                    RowLayout {
                        Layout.alignment: Qt.AlignRight
                        spacing: 8
                        Repeater {
                            model: [{id:"attention",label:"تحتاج إجراء"},{id:"completed",label:"مكتملة"},{id:"running",label:"نشطة"},{id:"all",label:"الكل"}]
                            delegate: AppButton {
                                required property var modelData
                                text: modelData.label; primary: win.filter === modelData.id
                                implicitHeight: 30
                                onClicked: win.filter = modelData.id
                            }
                        }
                    }
                    Flow {
                        Layout.fillWidth: true
                        Layout.preferredHeight: childrenRect.height
                        spacing: 8
                        visible: win.filter === "attention"
                        Repeater {
                            model: win.attentionGroups()
                            delegate: AppButton {
                                required property var modelData
                                text: "إعادة " + modelData.label + " (" + modelData.ids.length + ")"
                                implicitHeight: 30
                                onClicked: { win.bulkGroup = modelData; bulkRetry.open() }
                            }
                        }
                    }
                    Box {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ColumnLayout {
                            anchors.fill: parent; spacing: 0
                            RowLayout {
                                Layout.fillWidth: true; Layout.preferredHeight: 38; Layout.leftMargin: 14; Layout.rightMargin: 14
                                SubText { text: "التفاصيل"; Layout.preferredWidth: 90; horizontalAlignment: Text.AlignLeft }
                                SubText { text: "الحالة"; Layout.preferredWidth: 140 * win.uiScale }
                                SubText { text: "التقدم"; Layout.preferredWidth: 90 * win.uiScale }
                                SubText { text: "مراحل المعالجة"; Layout.preferredWidth: 170 * win.uiScale; visible: win.contentUnits >= 850 }
                                SubText { text: "الفيديو / الملف"; Layout.fillWidth: true }
                            }
                            Rectangle { Layout.fillWidth: true; height: 1; color: win.line }
                            PinList {
                                id: jobsList
                                objectName: "jobsList"
                                Layout.fillWidth: true; Layout.fillHeight: true
                                clip: true
                                model: backend.jobsModel
                                ScrollBar.vertical: AppScroll {}
                                delegate: Rectangle {
                                    width: jobsList.width
                                    height: win.matches(row) ? Math.max(win.rh(settings.ui_compact ? 56 : 64), jobTitle.implicitHeight + win.fs(12) + 22) : 0
                                    visible: height > 0
                                    color: rowMouse.containsMouse ? visualTheme.hover : (index % 2 === 0 ? win.panel : visualTheme.inset)
                                    MouseArea { id: rowMouse; anchors.fill: parent; hoverEnabled: true; onClicked: win.openJob(row.id); cursorShape: Qt.PointingHandCursor }
                                    RowLayout {
                                        anchors.fill: parent; anchors.leftMargin: win.rs(12); anchors.rightMargin: win.rs(12); spacing: win.rs(10)
                                        AppButton { text: "عرض"; implicitWidth: 64; implicitHeight: Math.max(22, Math.round(28 * win.density)); onClicked: win.openJob(row.id) }
                                        ColumnLayout {
                                            Layout.preferredWidth: 140 * win.uiScale
                                            LabelText { text: row.statusLabel; color: win.stateColor(row.status); Layout.alignment: Qt.AlignRight }
                                            SubText { text: row.retryAt ? "محاولة قادمة " + row.retryAt : (row.worker || row.stageLabel); Layout.alignment: Qt.AlignRight }
                                        }
                                        ColumnLayout {
                                            Layout.preferredWidth: 90 * win.uiScale
                                            AppProgress { Layout.fillWidth: true; from: 0; to: 100; value: Math.max(0, row.progress); indeterminate: row.status === "running" && row.progress < 0 }
                                            SubText { text: row.progress >= 0 ? Math.round(row.progress) + "٪" : row.status === "running" ? "جارٍ التنفيذ" : "—"; Layout.fillWidth: true }
                                        }
                                        RowLayout {
                                            visible: win.contentUnits >= 850
                                            Layout.preferredWidth: 170 * win.uiScale; spacing: 6
                                            Repeater {
                                                model: row.steps.slice().reverse()
                                                delegate: Rectangle {
                                                    required property var modelData
                                                    Layout.fillWidth: true; height: Math.max(18, win.rh(26)); radius: 5
                                                    color: modelData.status === "completed" ? (visualTheme.inset) : (visualTheme.inset)
                                                    border.color: modelData.status === "running" ? visualTheme.accent : "transparent"
                                                    LabelText { anchors.centerIn: parent; text: modelData.label; font.pixelSize: win.fs(11); color: win.stateColor(modelData.status) }
                                                }
                                            }
                                        }
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            LabelText { id: jobTitle; text: row.title; Layout.fillWidth: true; font.weight: Font.DemiBold; wrapMode: Text.WrapAnywhere; maximumLineCount: 2 }
                                            SubText { text: win.contentUnits < 850 ? row.steps.map(function(step) { return step.label }).join(" · ") : "#" + String(row.position).padStart(3,"0") + "   ·   " + row.video_id; Layout.fillWidth: true; font.pixelSize: win.fs(11) }
                                        }
                                    }
                                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.line }
                                }
                                ColumnLayout {
                                    anchors.centerIn: parent; spacing: 14; visible: jobsList.count === 0
                                    Rectangle {
                                        Layout.alignment: Qt.AlignHCenter; width: 66; height: 76; radius: 12; color: visualTheme.inset
                                        Column { anchors.centerIn: parent; spacing: 7; Repeater { model: 3; Rectangle { width: 30; height: 3; radius: 2; color: visualTheme.accent } } }
                                    }
                                    LabelText { text: "مساحة عملك جاهزة"; font.pixelSize: win.fs(22); Layout.alignment: Qt.AlignHCenter }
                                    SubText { text: "أضف فيديو أو قائمة تشغيل، وحدد الدروس التي تريد تحويلها إلى Word."; Layout.alignment: Qt.AlignHCenter }
                                    AppButton { text: "إضافة المصدر الأول"; primary: true; Layout.alignment: Qt.AlignHCenter; onClicked: win.page = 1 }
                                }
                                SubText { anchors.centerIn: parent; visible: jobsList.count > 0 && backend.jobs.filter(win.matches).length === 0; text: "لا توجد مهام مطابقة للبحث والتصفية" }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        SubText { text: "Word (.docx)  /  TXT  /  JSON  /  MP3"; font.pixelSize: win.fs(12) }
                        Item { Layout.fillWidth: true }
                        SubText { text: "تنزيل واحد  ·  تفريغ متوازٍ  ·  مستند مستقل لكل مهمة"; font.pixelSize: win.fs(12) }
                    }
                }

                // Source selection: both tabs preserve the same live selection.
                ScrollView {
                    id: sourcePage; objectName: "sourcePage"
                    clip: true; contentWidth: availableWidth
                    GridLayout {
                        width: sourcePage.availableWidth
                        columns: win.contentUnits >= 850 ? 2 : 1
                        layoutDirection: Qt.RightToLeft; columnSpacing: 16; rowSpacing: 16
                        Box {
                            Layout.alignment: Qt.AlignTop
                            Layout.fillWidth: true
                            Layout.preferredWidth: win.contentUnits >= 850 ? 330 * win.uiScale : sourcePage.availableWidth
                            Layout.maximumWidth: win.contentUnits >= 850 ? 390 * win.uiScale : sourcePage.availableWidth
                            Layout.preferredHeight: optionsColumn.implicitHeight + 32
                            ColumnLayout {
                                id: optionsColumn
                                anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                                anchors.margins: 16; spacing: 12
                                LabelText { text: "مصدر جديد"; font.pixelSize: win.fs(18); Layout.fillWidth: true }
                                RowLayout {
                                    Layout.fillWidth: true
                                    AppButton { objectName: "sourceYoutubeTab"; text: "YouTube"; primary: win.sourceTab === 0; Layout.fillWidth: true; onClicked: win.sourceTab = 0 }
                                    AppButton { objectName: "sourceLocalTab"; text: "TXT / صوت"; primary: win.sourceTab === 1; Layout.fillWidth: true; onClicked: win.sourceTab = 1 }
                                }
                                LabelText { text: "رابط فيديو أو قائمة تشغيل"; visible: win.sourceTab === 0; Layout.fillWidth: true }
                                AppField { id: urlField; horizontalAlignment: Text.AlignLeft; objectName: "urlField"; visible: win.sourceTab === 0; Layout.fillWidth: true; placeholderText: "https://www.youtube.com/…"; Accessible.name: "رابط YouTube"; onAccepted: if (!backend.busy) backend.discover(text, forceRefresh.checked) }
                                AppButton { objectName: "discoverButton"; visible: win.sourceTab === 0; text: backend.busy ? "جارٍ القراءة…" : "قراءة المصدر"; primary: true; Layout.fillWidth: true; enabled: !backend.busy && urlField.text.length > 0; onClicked: backend.discover(urlField.text, forceRefresh.checked) }
                                AppCheck { id: forceRefresh; visible: win.sourceTab === 0; text: "تحديث المعلومات المحفوظة"; Layout.fillWidth: true }
                                SubText { visible: win.sourceTab === 1; text: "اختر ملفات صوت أو تفريغات TXT من جهازك. يحدد نوع الملف مرحلة البداية."; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                                AppButton { objectName: "importLocalButton"; visible: win.sourceTab === 1; text: "استيراد صوت أو TXT"; Layout.fillWidth: true; onClicked: backend.importLocal(sourceFolderCombo.currentValue || "") }
                                AppButton { text: "إلغاء القراءة"; visible: backend.busy; Layout.fillWidth: true; onClicked: backend.cancelDiscovery() }
                                Rule {}
                                LabelText { text: "خيارات الإخراج"; font.pixelSize: win.fs(18); Layout.fillWidth: true }
                                LabelText { text: "مجلد المادة / الدرس"; Layout.fillWidth: true }
                                AppCombo { id: sourceFolderCombo; objectName: "sourceFolderCombo"; model: backend.folders; textRole: "name"; valueRole: "id"; Layout.fillWidth: true; Accessible.name: "مجلد المادة أو الدرس" }
                                AppButton { text: "مجلد جديد"; onClicked: folderDialog.open() }
                                LabelText { text: "قالب البرومبت"; visible: templateBox.visible; Layout.fillWidth: true }
                                AppCombo { id: templateBox; objectName: "templateBox"; visible: backend.templates.length > 0; model: backend.templates; textRole: "name"; valueRole: "id"; Layout.fillWidth: true; Accessible.name: "قالب البرومبت" }
                                LabelText { text: "مراحل المعالجة"; Layout.fillWidth: true }
                                AppCombo { id: preset; model: ["الدورة الكاملة حتى Word", "تنزيل وتفريغ فقط", "تنزيل فقط", "القالب المخصص من الإعدادات"]; Layout.fillWidth: true }
                                AppButton { objectName: "enqueueButton"; text: "إضافة إلى الطابور"; gold: true; Layout.fillWidth: true; enabled: backend.hasSourceSelection; onClicked: { backend.enqueue(["full","transcript","download","custom"][preset.currentIndex], backend.templates.length > 0 ? templateBox.currentValue : "", sourceFolderCombo.currentValue || ""); win.page = 0 } }
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.preferredHeight: Math.max(420 * win.uiScale, sourcePage.availableHeight - 4)
                            spacing: 12
                    ActionRow {
                        AppButton { text: "تطبيق النطاق"; onClicked: backend.selectRange(rangeField.text) }
                        AppField { id: rangeField; width: 140; placeholderText: "1-10,15"; Accessible.name: "نطاق الفيديوهات" }
                        AppButton { text: "إلغاء التحديد"; onClicked: backend.selectRange("none") }
                        AppButton { text: "تحديد الكل"; onClicked: backend.selectRange("all") }

                        LabelText { text: backend.sourceTitle || "الفيديوهات المختارة"; width: 230; font.pixelSize: win.fs(18) }
                    }
                    AppField { Layout.fillWidth: true; placeholderText: "بحث في أسماء فيديوهات القائمة"; horizontalAlignment: Text.AlignRight; onTextChanged: win.sourceSearch = text; Accessible.name: "بحث في القائمة" }
                    Box {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        PinList {
                            id: sourceList; objectName: "sourceList"
                            anchors.fill: parent; anchors.margins: win.rs(8); clip: true
                            model: backend.sourceModel
                            ScrollBar.vertical: AppScroll {}
                            delegate: Item {
                                width: sourceList.width; height: win.sourceSearch === "" || row.title.indexOf(win.sourceSearch) >= 0 ? Math.max(win.rh(54), 48 * win.uiScale) : 0
                                visible: height > 0
                                RowLayout {
                                    anchors.fill: parent; anchors.leftMargin: win.rs(8); anchors.rightMargin: win.rs(8); spacing: win.rs(8)
                                    AppButton { text: "↓"; Accessible.name: "نقل للأسفل"; implicitWidth: 32; implicitHeight: Math.max(22, Math.round(28 * win.density)); enabled: index < sourceList.count - 1; onClicked: backend.moveSource(index, 1) }
                                    AppButton { text: "↑"; Accessible.name: "نقل للأعلى"; implicitWidth: 32; implicitHeight: Math.max(22, Math.round(28 * win.density)); enabled: index > 0; onClicked: backend.moveSource(index, -1) }
                                    SubText { text: row.available ? (row.duration ? Math.round(row.duration / 60) + " دقيقة" : "") : "غير متاح"; Layout.preferredWidth: 60 }
                                    LabelText { text: row.title; Layout.fillWidth: true }
                                    SubText { text: "#" + row.position }
                                    AppCheck { checked: row.selected; enabled: row.available; onClicked: backend.selectSource(index, checked); Accessible.name: "تحديد " + row.title }
                                }
                                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.line }
                            }
                            SubText { anchors.centerIn: parent; text: backend.busy ? "تُقرأ المعلومات مع مراعاة فواصل طلبات YouTube…" : "ستظهر قائمة الفيديوهات هنا"; visible: sourceList.count === 0 }
                        }
                    }
                        }
                    }
                }

                // Workers: individual actions remain attached to the original row id.
                ColumnLayout {
                    id: workersPage
                    spacing: 12
                    SubText { text: "كل عامل يعالج مهمة واحدة. إيقاف الاستقبال يترك المهمة الحالية تكتمل."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    ActionRow {
                        AppButton { text: "إضافة عامل OpenCode"; onClicked: { var w = JSON.parse(JSON.stringify(settings.opencode_workers[0])); w.id = "oc" + Date.now(); w.name = "OpenCode " + (settings.opencode_workers.length + 1); w.provider = w.provider || "login"; settings.opencode_workers.push(w); save() } }
                        AppButton { objectName: "providersButton"; text: "المزودون"; onClicked: { providersDialog.open() } }
                        AppButton { text: "إضافة عامل Deepgram"; onClicked: { var w = JSON.parse(JSON.stringify(settings.deepgram_workers[0])); w.id = "dg" + Date.now(); w.name = "Deepgram " + (settings.deepgram_workers.length + 1); w.project = "proj-" + (settings.deepgram_workers.length + 1); settings.deepgram_workers.push(w); save() } }
                    }
                    ScrollView {
                        id: workersScroll
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; contentWidth: availableWidth
                        GridLayout {
                            width: workersScroll.availableWidth
                            columns: win.serviceColumns; columnSpacing: 12; rowSpacing: 12; layoutDirection: Qt.RightToLeft
                            ServiceGroup { stage: "download"; heading: "تنزيل YouTube"; symbol: "download"; Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.preferredHeight: win.serviceColumns === 3 ? Math.max(360, workersScroll.availableHeight - 4) : 380 }
                            ServiceGroup { stage: "deepgram"; heading: "التفريغ الصوتي"; symbol: "audio"; Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.preferredHeight: win.serviceColumns === 3 ? Math.max(360, workersScroll.availableHeight - 4) : 380 }
                            ServiceGroup { stage: "opencode"; heading: "OpenCode / Word"; symbol: "document"; Layout.fillWidth: true; Layout.preferredWidth: 1; Layout.preferredHeight: win.serviceColumns === 3 ? Math.max(360, workersScroll.availableHeight - 4) : 380 }
                        }
                    }
                    Box {
                        Layout.fillWidth: true; Layout.preferredHeight: workerHelp.implicitHeight + 24
                        SubText { id: workerHelp; anchors.fill: parent; anchors.margins: 12; verticalAlignment: Text.AlignVCenter; wrapMode: Text.WordWrap; text: "مفاتيح Deepgram و OpenRouter محفوظة في Windows Credential Manager، مفتاح مستقل لكل عامل. عمال Deepgram ذوو معرّف المشروع نفسه يتقاسمون حد التزامن؛ اجعل المعرّف مطابقًا لمشروع المفتاح. عامل OpenCode بمزود غير «تسجيل الدخول» يعمل في مجلد بيانات مستقل فلا تتعارض العمليات المتوازية." }
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
                                LabelText { text: "مجلدات المواد"; font.weight: Font.DemiBold; Layout.fillWidth: true }
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
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                        contentWidth: availableWidth
                        ColumnLayout {
                            width: parent.width; spacing: 12
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
                                        TextArea { id: promptField; objectName: "promptField"; text: settings.prompt; color: win.ink; wrapMode: TextEdit.Wrap; selectByMouse: true; font.pixelSize: win.fs(14); horizontalAlignment: Text.AlignRight; background: Rectangle { color: visualTheme.inset; radius: 8; border.color: win.line } }
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
                        Text { text: "وَرَق"; color: visualTheme.railInk; font.family: "Segoe UI"; font.pixelSize: win.fs(28); font.weight: Font.Bold; Layout.alignment: Qt.AlignRight }
                        Text { text: "WARAQ STUDIO"; color: visualTheme.railActive; font.pixelSize: win.fs(10); font.letterSpacing: 2; Layout.alignment: Qt.AlignRight }
                    }
                    Text { visible: !railOpen; text: "و"; color: visualTheme.railActive; font.pixelSize: win.fs(24); font.weight: Font.Bold }
                }
                Repeater {
                    model: win.pageNames
                    delegate: Button {
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
                                Layout.fillWidth: true; visible: railOpen
                                text: modelData; font.pixelSize: win.fs(14); font.weight: win.page === index ? Font.DemiBold : Font.Normal
                                color: win.page === index ? visualTheme.railSelectedInk : visualTheme.railInk
                                horizontalAlignment: Text.AlignRight; elide: Text.ElideRight
                            }
                            Image { source: "icons/" + win.pageIcons[index] + (win.page === index ? "-active" : "") + ".svg"; Layout.preferredWidth: 20; Layout.preferredHeight: 20; Layout.alignment: Qt.AlignHCenter }
                        }
                    }
                }
                Item { Layout.fillHeight: true }
                Rectangle { Layout.fillWidth: true; height: 1; color: visualTheme.line; visible: railOpen }
                Text { visible: railOpen; text: "سير عملك، تحت سيطرتك"; color: visualTheme.railMuted; font.pixelSize: win.fs(12); Layout.fillWidth: true; elide: Text.ElideRight; horizontalAlignment: Text.AlignRight; Layout.topMargin: 10 }
                Text { visible: railOpen; text: "نسخة محلية  ·  " + backend.version; color: visualTheme.railMuted; font.pixelSize: win.fs(11); Layout.alignment: Qt.AlignRight; Layout.bottomMargin: 4 }
            }
        }
    }

    Rectangle {
        visible: win.toastText.length > 0
        z: 20
        anchors.top: parent.top; anchors.topMargin: 100; anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(850, win.width - 100); height: toastLabel.implicitHeight + 36
        radius: 10; color: visualTheme.panel; border.color: win.toastError ? visualTheme.warning : visualTheme.success
        Text { id: toastLabel; anchors.fill: parent; anchors.margins: 18; color: win.ink; text: win.toastText; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignRight; font.pixelSize: win.fs(14) }
        TapHandler { onTapped: win.toastText = "" }
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
        anchors.centerIn: parent; width: Math.min(900, win.width - 80); height: Math.min(610, win.height - 48); modal: true
        title: "تفاصيل المهمة"
        standardButtons: Dialog.Close
        onOpened: { standardButton(Dialog.Close).text = "إغلاق"; jobFolderCombo.currentIndex = Math.max(0, backend.folders.findIndex(function(f) { return f.id === selectedJob.folder_id })) }
        background: Rectangle { color: win.panel; radius: 10; border.color: win.line }
        contentItem: WDialogBody {
            bodySpacing: 14
            LabelText { text: selectedJob.title || ""; font.pixelSize: win.fs(20); Layout.fillWidth: true }
            SubText { text: (selectedJob.statusLabel || "") + "  ·  " + (selectedJob.stageLabel || ""); Layout.fillWidth: true }
            SubText { text: selectedJob.retryAt ? "أُعيدت الجدولة تلقائيًا — المحاولة القادمة " + selectedJob.retryAt : ""; visible: !!selectedJob.retryAt; color: visualTheme.info; Layout.fillWidth: true }
            SubText { text: selectedJob.template ? "قالب البرومبت: " + selectedJob.template : ""; visible: !!selectedJob.template; Layout.fillWidth: true }
            GridLayout { columns: win.contentUnits < 850 ? 1 : 2;
                Layout.fillWidth: true
                AppCombo { id: jobFolderCombo; model: backend.folders; textRole: "name"; valueRole: "id"; Layout.preferredWidth: 240; onActivated: { backend.assignJobFolder(selectedJob.id, currentValue || ""); selectedJob = backend.jobById(selectedJob.id) } }
                LabelText { text: "مجلد المادة / الدرس" }
                Item { Layout.fillWidth: true }
            }
            SubText { text: selectedJob.error || ""; visible: !!selectedJob.error; color: visualTheme.warning; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            AppProgress { Layout.fillWidth: true; from: 0; to: 100; value: selectedJob.progress || 0; indeterminate: selectedJob.status === "running" && selectedJob.progress < 0; visible: selectedJob.status === "running" }
            GridLayout { columns: win.contentUnits < 850 ? 1 : 2;
                AppButton { text: "إلغاء المهمة"; danger: true; enabled: selectedJob.status !== "completed"; onClicked: { backend.cancelJob(selectedJob.id); jobDialog.close() } }
                AppButton { text: "إعادة المحاولة"; enabled: ["failed","blocked","uncertain","paused","cancelled"].indexOf(selectedJob.status) >= 0 || !!selectedJob.retryAt; onClicked: { retryWarning.open() } }
                AppButton { text: "فتح المجلد"; onClicked: backend.openPath(selectedJob.folder) }
                AppButton { text: "فتح Word"; enabled: !!selectedJob.document; primary: true; onClicked: backend.openPath(selectedJob.document) }
            }
            GridLayout { columns: win.contentUnits < 850 ? 1 : 2;
                AppButton { text: "إعادة OpenCode بنسخة جديدة"; enabled: !!selectedJob.transcript && selectedJob.status !== "running"; onClicked: { backend.retry(selectedJob.id,"opencode",true); jobDialog.close() } }
                AppButton { text: "إعادة التفريغ بنسخة جديدة"; enabled: !!selectedJob.audio && selectedJob.status !== "running"; onClicked: { paidRerun.open() } }
                Item { Layout.fillWidth: true }
            }
            LabelText { text: "سجل التنفيذ"; Layout.alignment: Qt.AlignRight }
            ScrollView {
                id: logScroll
                Layout.fillWidth: true; Layout.fillHeight: true
                TextArea {
                    id: jobLog
                    readOnly: true; selectByMouse: true; color: win.muted; wrapMode: TextEdit.Wrap; font.pixelSize: win.fs(12)
                    background: Rectangle { color: win.bg; radius: 8 }
                }
            }
        }
    }
    AppDialog {
        id: retryWarning; objectName: "retryWarning"; anchors.centerIn: parent; width: Math.min(500, win.width - 48); modal: true; title: "إعادة المحاولة"; standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "إعادة المحاولة"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { width: parent.width; wrapMode: Text.WordWrap; text: selectedJob.status === "uncertain" ? "الطلب السابق لدى Deepgram قد يكون اكتمل. إعادة الإرسال قد تسبب تكلفة إضافية. هل تريد المحاولة؟"
                : (selectedJob.current === "download" && selectedJob.status === "blocked") ? "توقف التنزيل لأن YouTube رفض الوصول أو لنقص المساحة. أصلح السبب أولًا (الكوكيز أو المساحة) ثم أعد المحاولة؛ سيُرفع التوقف الآلي عن خدمة التنزيل ويُعاد العمل دون المساس بالإيقاف اليدوي."
                : (selectedJob.status === "pending" && selectedJob.retryAt) ? "المهمة مجدولة تلقائيًا حتى المحاولة القادمة. الإعادة اليدوية تعيدها إلى الطابور لكنها لا تتجاوز تهدئة تقييد YouTube."
                : "ستُستأنف المرحلة باستخدام الملفات المكتملة المحفوظة." }
        onAccepted: { backend.retry(selectedJob.id, selectedJob.current, false); jobDialog.close() }
    }
    AppDialog {
        id: bulkRetry; objectName: "bulkRetry"; anchors.centerIn: parent; width: Math.min(530, win.width - 48); modal: true; title: "إعادة محاولة جماعية"; standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "إعادة " + (bulkGroup.ids || []).length + " مهمة"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { width: parent.width; wrapMode: Text.WordWrap
            text: bulkGroup.status === "uncertain"
                ? "نتائج " + (bulkGroup.ids || []).length + " طلب تفريغ غير مؤكدة. قد تكون اكتملت لدى Deepgram، وإعادة إرسالها قد تسبب تكلفة إضافية لكل مهمة. هل تريد إعادة المحاولة للجميع؟"
                : "ستُعاد " + (bulkGroup.ids || []).length + " مهمة من مجموعة «" + (bulkGroup.label || "") + "» إلى الطابور باستخدام الملفات المكتملة المحفوظة."
        }
        onAccepted: backend.retryMany(bulkGroup.ids, bulkGroup.stage, bulkGroup.status)
    }
    AppDialog {
        id: paidRerun; objectName: "paidRerun"; anchors.centerIn: parent; width: Math.min(500, win.width - 48); modal: true; title: "تفريغ جديد"; standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "تفريغ جديد"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { width: parent.width; wrapMode: Text.WordWrap; text: "سيُرسل الصوت مجددًا إلى Deepgram وقد تنتج تكلفة جديدة. ستبقى النسخة السابقة محفوظة." }
        onAccepted: { backend.retry(selectedJob.id, "deepgram", true); jobDialog.close() }
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
                          : "النماذج من OpenCode المثبّت محليًا. يمكن كتابة معرّف نموذج مخصص."))
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
                      ? "يستخدم تسجيل الدخول المحفوظ داخل OpenCode (اشتراك ChatGPT وغيره) دون مفتاح، وفي مجلد بيانات OpenCode المشترك."
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
        Label { width: parent.width; wrapMode: Text.WordWrap; text: "سيُحذف المزود «" + win.pendingProvider.name + "» من القائمة. يمكنك إضافته مجددًا في أي وقت، ولا يُحذف أي عامل يستخدمه." }
        onAccepted: backend.deleteProvider(win.pendingProvider.id)
    }
    AppDialog {
        id: deleteFileDialog; objectName: "deleteFileDialog"; anchors.centerIn: parent; width: Math.min(520, win.width - 48); modal: true; title: "حذف من المكتبة"
        standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "حذف"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { width: parent.width; wrapMode: Text.WordWrap; text: "سيُحذف «" + win.pendingFile.name + "» من مجلد المكتبة. النسخة الأصلية داخل مجلد المهمة تبقى محفوظة، ويمكن إعادة التنظيم لاحقًا." }
        onAccepted: backend.deleteLibraryFile(win.pendingFile.path)
    }
    AppDialog {
        id: advancedDialog; objectName: "advancedDialog"; anchors.centerIn: parent; width: Math.min(850, win.width - 100); height: win.height - 120; modal: true
        title: "الإعدادات المتقدمة — دون مفاتيح API"; standardButtons: Dialog.Save | Dialog.Cancel
        onOpened: { standardButton(Dialog.Save).text = "حفظ"; standardButton(Dialog.Cancel).text = "إلغاء" }
        contentItem: ScrollView { TextArea { color: win.ink; selectionColor: win.accent; selectedTextColor: visualTheme.accentInk; background: Rectangle { color: visualTheme.inset } id: rawSettings; objectName: "rawSettings"; text: backend.settingsJson; selectByMouse: true; wrapMode: TextEdit.Wrap; font.family: "Consolas"; font.pixelSize: win.fs(13) } }
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
                TextArea { id: tplText; color: win.ink; wrapMode: TextEdit.Wrap; selectByMouse: true; font.pixelSize: win.fs(14); horizontalAlignment: Text.AlignRight; background: Rectangle { color: visualTheme.inset; radius: 8; border.color: win.line } }
            }
        }
        onAccepted: {
            win.commitTemplateFields()
            settings.opencode_templates = win.templ
            if (save()) win.templ = JSON.parse(JSON.stringify(backend.templates))
        }
    }
}
