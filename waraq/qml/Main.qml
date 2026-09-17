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
    readonly property color bg: dark ? "#0d1422" : "#f3f6fa"
    readonly property color panel: dark ? "#151f30" : "#ffffff"
    readonly property color line: dark ? "#29364b" : "#d9e1eb"
    readonly property color ink: dark ? "#e7edf7" : "#16263d"
    readonly property color muted: dark ? "#a3b2c8" : "#51647c"
    readonly property color accent: "#42d9b6"
    property string toastText: ""
    property bool toastError: false
    property var selectedJob: ({})
    property var editingWorker: ({})
    property string workerKind: ""
    property int workerIndex: 0
    property string filter: "all"
    property string search: ""
    property string sourceSearch: ""
    property string librarySearch: ""
    property var pageNames: ["مركز العمليات", "إضافة مصدر", "العمال والخدمات", "مكتبة النتائج", "الإعدادات"]
    color: bg
    palette.window: panel
    palette.windowText: ink
    palette.text: ink
    palette.base: dark ? "#0f192a" : "#f7f9fc"
    palette.button: dark ? "#233149" : "#eaf0f7"
    palette.buttonText: ink
    palette.highlight: "#2b9d89"
    palette.highlightedText: "#ffffff"
    onClosing: function(close) { close.accepted = backend.prepareClose() }
    font.family: "Segoe UI"
    font.pixelSize: 14

    function matches(job) {
        return (filter === "all" || job.status === filter || (filter === "attention" && ["blocked", "failed", "uncertain"].indexOf(job.status) >= 0))
            && (search.length === 0 || job.title.toLowerCase().indexOf(search.toLowerCase()) >= 0)
    }
    function stateColor(status) {
        if (status === "completed") return dark ? "#61ddbb" : "#08765b"
        if (status === "running") return dark ? "#87b7ff" : "#215cc4"
        if (["failed", "blocked", "uncertain"].indexOf(status) >= 0) return dark ? "#ffba82" : "#9a4409"
        return muted
    }
    function save() { return backend.saveSettings(JSON.stringify(settings)) }
    function editWorker(worker, kind) {
        workerKind = kind
        var arr = kind === "deepgram" ? settings.deepgram_workers : settings.opencode_workers
        workerIndex = arr.findIndex(function(w) { return w.id === worker.id })
        editingWorker = JSON.parse(JSON.stringify(arr[workerIndex]))
        workerName.text = editingWorker.name
        workerModel.text = editingWorker.model
        workerExtra.text = kind === "deepgram" ? editingWorker.language : editingWorker.variant
        workerProject.text = kind === "deepgram" ? editingWorker.project : editingWorker.agent
        endpoint.currentIndex = Math.max(0, ["https://api.deepgram.com", "https://api.eu.deepgram.com", "https://api.au.deepgram.com"].indexOf(editingWorker.endpoint))
        diarize.checked = !!editingWorker.diarize
        apiKey.text = ""
        workerDialog.open()
    }
    function openJob(job) { selectedJob = job; jobDialog.open() }
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
                var currentJob = backend.jobs.find(function(j){return j.id === selectedJob.id})
                if(currentJob) selectedJob = currentJob
            }
        }
    }
    Timer { id: toastTimer; interval: 9000; onTriggered: toastText = "" }
    Timer { id: autoStopTimer; interval: 2500; onTriggered: Qt.quit() }

    component LabelText: Text {
        color: win.ink
        font.family: "Segoe UI"
        font.pixelSize: 14
        horizontalAlignment: Text.AlignRight
        elide: Text.ElideRight
    }
    component SubText: LabelText { color: win.muted; font.pixelSize: 13 }
    component Box: Rectangle { color: win.panel; radius: 12; border.color: win.line }
    component AppButton: WButton { foreground: win.ink; surface: win.dark ? "#1c2a40" : "#eaf0f7" }
    component AppField: Field { fieldBackground: win.dark ? "#0f192a" : "#f7f9fc"; fieldText: win.ink }

    RowLayout {
        anchors.fill: parent
        spacing: 0
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 94
                color: win.bg
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 32
                    anchors.rightMargin: 32
                    spacing: 12
                    AppButton {
                        objectName: "runButton"
                        text: backend.running ? "إيقاف بعد المهام الحالية" : "تشغيل الطابور"
                        primary: !backend.running
                        onClicked: backend.toggleRunning()
                    }
                    Rectangle { width: 7; height: 7; radius: 4; color: backend.running ? win.accent : win.muted }
                    SubText { text: backend.running ? "المحرك يعمل" : "المحرك متوقف" }
                    CheckBox {
                        objectName: "autoStopBox"
                        text: "إيقاف التطبيق بعد اكتمال الطابور"
                        palette.windowText: win.muted
                        onClicked: backend.setAutoStop(checked)
                        Accessible.name: "إيقاف التطبيق تلقائيًا بعد اكتمال الطابور"
                    }
                    Item { Layout.fillWidth: true }
                    ColumnLayout {
                        Layout.alignment: Qt.AlignRight
                        spacing: 3
                        LabelText { text: win.pageNames[win.page]; font.pixelSize: 26; font.weight: Font.DemiBold; Layout.alignment: Qt.AlignRight }
                        SubText { text: "من الفيديو إلى مستند يستحق القراءة"; Layout.alignment: Qt.AlignRight }
                    }
                }
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.line }
            }

            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 28
                currentIndex: win.page

                // Operations
                ColumnLayout {
                    spacing: 22
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 14
                        Repeater {
                            model: [
                                {label: "تحتاج انتباهك", value: backend.stats.attention, color: "#f4b880"},
                                {label: "مكتملة", value: backend.stats.done, color: "#42d9b6"},
                                {label: "قيد التنفيذ", value: backend.stats.active, color: "#87b7ff"},
                                {label: "إجمالي المهام", value: backend.stats.total, color: win.ink}
                            ]
                            delegate: Box {
                                required property var modelData
                                Layout.fillWidth: true
                                Layout.preferredHeight: 104
                                ColumnLayout {
                                    anchors.fill: parent; anchors.margins: 18; spacing: 6
                                    SubText { text: modelData.label; Layout.alignment: Qt.AlignRight }
                                    LabelText { text: modelData.value; font.pixelSize: 30; color: win.dark ? modelData.color : win.ink; Layout.alignment: Qt.AlignRight }
                                }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        AppButton { text: "إضافة مصدر"; onClicked: win.page = 1 }
                        AppButton { text: "تقرير Excel"; onClicked: backend.exportReport() }
                        Item { Layout.fillWidth: true }
                        AppField { Layout.preferredWidth: 240; placeholderText: "بحث في المهام"; horizontalAlignment: Text.AlignRight; onTextChanged: win.search = text; Accessible.name: "بحث في المهام" }
                        LabelText { text: "طابور المعالجة"; font.pixelSize: 18; font.weight: Font.DemiBold }
                    }
                    RowLayout {
                        Layout.alignment: Qt.AlignRight
                        spacing: 8
                        Repeater {
                            model: [{id:"attention",label:"تحتاج إجراء"},{id:"completed",label:"مكتملة"},{id:"running",label:"نشطة"},{id:"all",label:"الكل"}]
                            delegate: AppButton {
                                required property var modelData
                                text: modelData.label; primary: win.filter === modelData.id
                                implicitHeight: 34
                                onClicked: win.filter = modelData.id
                            }
                        }
                    }
                    Box {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        ColumnLayout {
                            anchors.fill: parent; spacing: 0
                            RowLayout {
                                Layout.fillWidth: true; Layout.preferredHeight: 48; Layout.leftMargin: 20; Layout.rightMargin: 20
                                SubText { text: "التفاصيل"; Layout.preferredWidth: 90; horizontalAlignment: Text.AlignLeft }
                                SubText { text: "الحالة"; Layout.preferredWidth: 160 }
                                SubText { text: "مراحل المعالجة"; Layout.preferredWidth: 230 }
                                SubText { text: "الفيديو / الملف"; Layout.fillWidth: true }
                            }
                            Rectangle { Layout.fillWidth: true; height: 1; color: win.line }
                            ListView {
                                id: jobsList
                                objectName: "jobsList"
                                Layout.fillWidth: true; Layout.fillHeight: true
                                clip: true
                                model: backend.jobs
                                ScrollBar.vertical: ScrollBar {}
                                delegate: Rectangle {
                                    required property var modelData
                                    width: jobsList.width
                                    height: win.matches(modelData) ? 89 : 0
                                    visible: height > 0
                                    color: rowMouse.containsMouse ? (win.dark ? "#1a293d" : "#f3f7fb") : "transparent"
                                    MouseArea { id: rowMouse; anchors.fill: parent; hoverEnabled: true; onClicked: win.openJob(modelData); cursorShape: Qt.PointingHandCursor }
                                    RowLayout {
                                        anchors.fill: parent; anchors.leftMargin: 20; anchors.rightMargin: 20; spacing: 14
                                        AppButton { text: "عرض"; implicitWidth: 76; implicitHeight: 34; onClicked: win.openJob(modelData) }
                                        ColumnLayout {
                                            Layout.preferredWidth: 155
                                            LabelText { text: modelData.statusLabel; color: win.stateColor(modelData.status); Layout.alignment: Qt.AlignRight }
                                            SubText { text: modelData.retryAt ? "محاولة قادمة " + modelData.retryAt : (modelData.worker || modelData.stageLabel); Layout.alignment: Qt.AlignRight }
                                        }
                                        RowLayout {
                                            Layout.preferredWidth: 230; spacing: 6
                                            Repeater {
                                                model: modelData.steps.slice().reverse()
                                                delegate: Rectangle {
                                                    required property var modelData
                                                    Layout.fillWidth: true; height: 30; radius: 6
                                                    color: modelData.status === "completed" ? (win.dark ? "#163b35" : "#e0f5ed") : (win.dark ? "#202d42" : "#eaf0f6")
                                                    border.color: modelData.status === "running" ? "#639ff2" : "transparent"
                                                    LabelText { anchors.centerIn: parent; text: modelData.label; font.pixelSize: 11; color: win.stateColor(modelData.status) }
                                                }
                                            }
                                        }
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            LabelText { text: modelData.title; Layout.fillWidth: true; font.weight: Font.DemiBold }
                                            SubText { text: "#" + String(modelData.position).padStart(3,"0") + "   ·   " + modelData.video_id; Layout.fillWidth: true; font.pixelSize: 11 }
                                        }
                                    }
                                    Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.line }
                                }
                                ColumnLayout {
                                    anchors.centerIn: parent; spacing: 14; visible: backend.jobs.length === 0
                                    Rectangle {
                                        Layout.alignment: Qt.AlignHCenter; width: 66; height: 76; radius: 12; color: win.dark ? "#1d3c3d" : "#dcf4ed"
                                        Column { anchors.centerIn: parent; spacing: 7; Repeater { model: 3; Rectangle { width: 30; height: 3; radius: 2; color: "#42b99f" } } }
                                    }
                                    LabelText { text: "مساحة عملك جاهزة"; font.pixelSize: 22; Layout.alignment: Qt.AlignHCenter }
                                    SubText { text: "أضف فيديو أو قائمة تشغيل، وحدد الدروس التي تريد تحويلها إلى Word."; Layout.alignment: Qt.AlignHCenter }
                                    AppButton { text: "إضافة المصدر الأول"; primary: true; Layout.alignment: Qt.AlignHCenter; onClicked: win.page = 1 }
                                }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        SubText { text: "Word (.docx)  /  TXT  /  JSON  /  MP3"; font.pixelSize: 12 }
                        Item { Layout.fillWidth: true }
                        SubText { text: "تنزيل واحد  ·  تفريغ متوازٍ  ·  مستند مستقل لكل مهمة"; font.pixelSize: 12 }
                    }
                }

                // Source selection
                ColumnLayout {
                    spacing: 18
                    Box {
                        Layout.fillWidth: true; Layout.preferredHeight: 188
                        ColumnLayout {
                            anchors.fill: parent; anchors.margins: 22; spacing: 12
                            LabelText { text: "ما الذي تريد تحويله اليوم؟"; font.pixelSize: 21; Layout.alignment: Qt.AlignRight }
                            SubText { text: "رابط فيديو واحد أو قائمة تشغيل. ستختار الفيديوهات قبل إضافتها إلى الطابور."; Layout.alignment: Qt.AlignRight }
                            RowLayout {
                                Layout.fillWidth: true; spacing: 10
                                AppButton { objectName: "discoverButton"; text: backend.busy ? "جارٍ القراءة…" : "قراءة المصدر"; primary: true; enabled: !backend.busy && urlField.text.length > 0; onClicked: backend.discover(urlField.text, forceRefresh.checked) }
                                AppField { id: urlField; objectName: "urlField"; Layout.fillWidth: true; placeholderText: "https://www.youtube.com/playlist?list=…"; Accessible.name: "رابط YouTube"; onAccepted: if (!backend.busy) backend.discover(text, forceRefresh.checked) }
                            }
                            RowLayout {
                                AppButton { text: "استيراد صوت أو TXT"; implicitHeight: 32; onClicked: backend.importLocal() }
                                AppButton { text: "إلغاء القراءة"; visible: backend.busy; implicitHeight: 32; onClicked: backend.cancelDiscovery() }
                                Item { Layout.fillWidth: true }
                                CheckBox { id: forceRefresh; text: "تحديث المعلومات المحفوظة"; palette.windowText: win.muted }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        AppButton { text: "تطبيق النطاق"; onClicked: backend.selectRange(rangeField.text) }
                        AppField { id: rangeField; Layout.preferredWidth: 140; placeholderText: "1-10,15"; Accessible.name: "نطاق الفيديوهات" }
                        AppButton { text: "إلغاء التحديد"; onClicked: backend.selectRange("none") }
                        AppButton { text: "تحديد الكل"; onClicked: backend.selectRange("all") }
                        Item { Layout.fillWidth: true }
                        LabelText { text: backend.sourceTitle || "الفيديوهات المختارة"; Layout.maximumWidth: 280; font.pixelSize: 18 }
                    }
                    AppField { Layout.fillWidth: true; placeholderText: "بحث في أسماء فيديوهات القائمة"; horizontalAlignment: Text.AlignRight; onTextChanged: win.sourceSearch = text; Accessible.name: "بحث في القائمة" }
                    Box {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        ListView {
                            id: sourceList
                            anchors.fill: parent; anchors.margins: 8; clip: true
                            model: backend.source
                            ScrollBar.vertical: ScrollBar {}
                            delegate: Item {
                                required property var modelData
                                required property int index
                                width: sourceList.width; height: win.sourceSearch === "" || modelData.title.indexOf(win.sourceSearch) >= 0 ? 64 : 0
                                visible: height > 0
                                RowLayout {
                                    anchors.fill: parent; anchors.leftMargin: 12; anchors.rightMargin: 12; spacing: 12
                                    AppButton { text: "للأسفل"; implicitWidth: 68; implicitHeight: 32; enabled: index < backend.source.length - 1; onClicked: backend.moveSource(index, 1) }
                                    AppButton { text: "للأعلى"; implicitWidth: 68; implicitHeight: 32; enabled: index > 0; onClicked: backend.moveSource(index, -1) }
                                    SubText { text: modelData.available ? (modelData.duration ? Math.round(modelData.duration / 60) + " دقيقة" : "") : "غير متاح"; Layout.preferredWidth: 85 }
                                    LabelText { text: modelData.title; Layout.fillWidth: true }
                                    SubText { text: "#" + modelData.position }
                                    CheckBox { checked: modelData.selected; enabled: modelData.available; onClicked: backend.selectSource(index, checked); Accessible.name: "تحديد " + modelData.title }
                                }
                                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: win.line }
                            }
                            SubText { anchors.centerIn: parent; text: backend.busy ? "تُقرأ المعلومات مع مراعاة فواصل طلبات YouTube…" : "ستظهر قائمة الفيديوهات هنا"; visible: backend.source.length === 0 }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        AppButton { text: "إضافة إلى الطابور"; primary: true; enabled: backend.source.some(function(x){return x.selected}); onClicked: { backend.enqueue(["full","transcript","download","custom"][preset.currentIndex], backend.templates.length > 0 ? templateBox.currentValue : ""); win.page = 0 } }
                        Item { Layout.fillWidth: true }
                        ComboBox { id: templateBox; objectName: "templateBox"; visible: backend.templates.length > 0; model: backend.templates; textRole: "name"; valueRole: "id"; Layout.preferredWidth: 200; Accessible.name: "قالب البرومبت" }
                        LabelText { text: "قالب البرومبت"; visible: templateBox.visible }
                        ComboBox { id: preset; model: ["الدورة الكاملة حتى Word", "تنزيل وتفريغ فقط", "تنزيل فقط", "القالب المخصص من الإعدادات"]; Layout.preferredWidth: 230 }
                        LabelText { text: "مراحل المعالجة" }
                    }
                }

                // Workers
                ColumnLayout {
                    spacing: 18
                    SubText { text: "كل عامل يعمل على مهمة واحدة. إيقاف الاستقبال يسمح للمهمة الحالية بالاكتمال."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    RowLayout {
                        Layout.fillWidth: true
                        AppButton { text: "إضافة عامل OpenCode"; onClicked: { var w = JSON.parse(JSON.stringify(settings.opencode_workers[0])); w.id = "oc" + Date.now(); w.name = "OpenCode " + (settings.opencode_workers.length + 1); settings.opencode_workers.push(w); save() } }
                        AppButton { text: "إضافة عامل Deepgram"; onClicked: { var w = JSON.parse(JSON.stringify(settings.deepgram_workers[0])); w.id = "dg" + Date.now(); w.name = "Deepgram " + (settings.deepgram_workers.length + 1); settings.deepgram_workers.push(w); save() } }
                        Item { Layout.fillWidth: true }
                        LabelText { text: "إدارة السعة"; font.pixelSize: 18 }
                    }
                    ListView {
                        id: workerList
                        Layout.fillWidth: true; Layout.fillHeight: true; spacing: 12; clip: true
                        model: backend.workers
                        ScrollBar.vertical: ScrollBar {}
                        delegate: Box {
                            required property var modelData
                            width: workerList.width; height: modelData.error ? 126 : 102
                            ColumnLayout {
                                anchors.fill: parent; anchors.margins: 18; spacing: 8
                                RowLayout {
                                    Layout.fillWidth: true; spacing: 12
                                    AppButton { text: "إعدادات"; visible: modelData.stage !== "download"; onClicked: win.editWorker(modelData, modelData.stage) }
                                    AppButton { text: modelData.paused ? "استئناف الخدمة" : "إيقاف الخدمة"; onClicked: backend.pauseStage(modelData.stage, !modelData.paused) }
                                    AppButton { text: "إعادة فحص العامل"; visible: !!modelData.error; onClicked: backend.resetWorker(modelData.id) }
                                    Item { Layout.fillWidth: true }
                                    ColumnLayout {
                                        Layout.alignment: Qt.AlignRight
                                        LabelText { text: modelData.name; font.pixelSize: 17; font.weight: Font.DemiBold; Layout.alignment: Qt.AlignRight }
                                        SubText { text: modelData.job_id ? "يعالج " + modelData.job_id + "  ·  " + modelData.elapsed + " ثانية" : (!modelData.enabled ? "معطل" : modelData.paused ? "الاستقبال متوقف" : modelData.stageLabel + "  ·  جاهز لاستقبال مهمة"); Layout.alignment: Qt.AlignRight }
                                    }
                                    Rectangle { width: 10; height: 10; radius: 5; color: modelData.error ? "#f4b880" : modelData.job_id ? win.accent : win.muted }
                                }
                                SubText { text: modelData.error || ""; color: win.dark ? "#f4b880" : "#9a4409"; visible: !!modelData.error; Layout.fillWidth: true }
                            }
                        }
                    }
                    Box {
                        Layout.fillWidth: true; Layout.preferredHeight: 72
                        SubText { anchors.fill: parent; anchors.margins: 18; verticalAlignment: Text.AlignVCenter; wrapMode: Text.WordWrap; text: "مفاتيح Deepgram محفوظة في Windows Credential Manager. تعدد المفاتيح لا يزيد حصة المشروع تلقائيًا؛ يُطبق حد تزامن مشترك لكل مشروع." }
                    }
                }

                // Library
                ColumnLayout {
                    spacing: 16
                    RowLayout {
                        AppButton { text: "فتح مجلد النتائج"; onClicked: backend.openPath(settings.output_root) }
                        AppButton { text: "تقرير Excel"; onClicked: backend.exportReport() }
                        Item { Layout.fillWidth: true }
                        LabelText { text: backend.stats.documents + " مستند Word"; font.pixelSize: 20 }
                    }
                    AppField { Layout.fillWidth: true; placeholderText: "بحث في عناوين النتائج"; horizontalAlignment: Text.AlignRight; onTextChanged: win.librarySearch = text; Accessible.name: "بحث في النتائج" }
                    ListView {
                        id: libraryList
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 12
                        model: backend.jobs.filter(function(j){return (j.document || j.transcript || j.audio) && (win.librarySearch === "" || j.title.indexOf(win.librarySearch) >= 0)})
                        ScrollBar.vertical: ScrollBar {}
                        delegate: Box {
                            required property var modelData
                            width: libraryList.width; height: 100
                            RowLayout {
                                anchors.fill: parent; anchors.margins: 18; spacing: 12
                                AppButton { text: "فتح Word"; primary: true; enabled: !!modelData.document; onClicked: backend.openPath(modelData.document) }
                                AppButton { text: "التفريغ"; enabled: !!modelData.transcript; onClicked: backend.openPath(modelData.transcript) }
                                AppButton { text: "الصوت"; enabled: !!modelData.audio; onClicked: backend.openPath(modelData.audio) }
                                AppButton { text: "التفاصيل"; onClicked: win.openJob(modelData) }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    LabelText { text: modelData.title; font.pixelSize: 17; Layout.fillWidth: true }
                                    SubText { text: modelData.document ? "DOCX · تم التحقق من بنية المستند والنص" : "نتائج وسيطة محفوظة"; Layout.fillWidth: true }
                                }
                            }
                        }
                        SubText { anchors.centerIn: parent; visible: libraryList.count === 0; text: "تظهر هنا الملفات الناتجة فور حفظها." }
                    }
                }

                // Settings
                ColumnLayout {
                    spacing: 16
                    RowLayout {
                        AppButton { text: "حفظ الإعدادات"; primary: true; onClicked: { settings.prompt = promptField.text; save() } }
                        AppButton { objectName: "templatesButton"; text: "قوالب البرومبت"; onClicked: templatesDialog.open() }
                        AppButton { text: "استيراد إعدادات السكربت"; onClicked: backend.importLegacy() }
                        AppButton { text: "فحص البيئة"; onClicked: backend.checkEnvironment() }
                        Item { Layout.fillWidth: true }
                        AppButton { text: win.dark ? "المظهر الفاتح" : "المظهر الداكن"; onClicked: { settings.theme = win.dark ? "light" : "dark"; save() } }
                    }
                    ScrollView {
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                        contentWidth: availableWidth
                        ColumnLayout {
                            width: parent.width; spacing: 18
                            Box {
                                Layout.fillWidth: true; implicitHeight: storageColumn.implicitHeight + 40
                                ColumnLayout {
                                    id: storageColumn; anchors.fill: parent; anchors.margins: 20; spacing: 12
                                    LabelText { text: "الملفات والأدوات"; font.pixelSize: 18; Layout.alignment: Qt.AlignRight }
                                    Repeater {
                                        model: [{key:"output_root",label:"مجلد النتائج",kind:"folder"},{key:"cookies_file",label:"ملف كوكيز Netscape",kind:"file"},{key:"opencode_executable",label:"ملف OpenCode التنفيذي",kind:"file"},{key:"opencode_context",label:"مجلد المشروع السابق",kind:"folder"}]
                                        delegate: RowLayout {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            AppButton { text: "اختيار"; implicitHeight: 40; onClicked: { var path = backend.choosePath(modelData.kind); if(path) { settings[modelData.key] = path; pathField.text = path } } }
                                            AppField { id: pathField; Layout.fillWidth: true; text: settings[modelData.key]; onEditingFinished: settings[modelData.key] = text; Accessible.name: modelData.label }
                                            LabelText { text: modelData.label; Layout.preferredWidth: 185 }
                                        }
                                    }
                                }
                            }
                            Box {
                                Layout.fillWidth: true; implicitHeight: limitsColumn.implicitHeight + 40
                                ColumnLayout {
                                    id: limitsColumn; anchors.fill: parent; anchors.margins: 20; spacing: 12
                                    LabelText { text: "السعة والترتيب"; font.pixelSize: 18; Layout.alignment: Qt.AlignRight }
                                    Repeater {
                                        model: [{key:"project_concurrency",label:"حد تزامن Deepgram لكل مشروع"},{key:"max_pending_audio",label:"أقصى ملفات صوت بانتظار التفريغ"},{key:"youtube_sleep_min",label:"أقل انتظار بين التنزيلات — ثانية"},{key:"youtube_sleep_max",label:"أقصى انتظار بين التنزيلات — ثانية"},{key:"chunk_seconds",label:"طول الجزء الصوتي — ثانية"},{key:"opencode_timeout",label:"مهلة OpenCode — ثانية"}]
                                        delegate: RowLayout {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            AppField { Layout.preferredWidth: 160; text: String(settings[modelData.key]); validator: IntValidator {bottom:1;top:43200} onEditingFinished: settings[modelData.key] = Number(text); Accessible.name: modelData.label }
                                            Item { Layout.fillWidth: true }
                                            LabelText { text: modelData.label }
                                        }
                                    }
                                    CheckBox { text: "ترتيب صارم: تنتظر المهمة عبور المهام السابقة من المرحلة نفسها"; checked: settings.strict_order; onClicked: settings.strict_order = checked; palette.windowText: win.ink; Layout.alignment: Qt.AlignRight }
                                    RowLayout {
                                        Layout.alignment: Qt.AlignRight
                                        CheckBox { text: "OpenCode / Word"; checked: settings.opencode_enabled; onClicked: settings.opencode_enabled = checked; palette.windowText: win.ink }
                                        CheckBox { text: "Deepgram"; checked: settings.deepgram_enabled; onClicked: settings.deepgram_enabled = checked; palette.windowText: win.ink }
                                        CheckBox { text: "التنزيل"; checked: settings.download_enabled; onClicked: settings.download_enabled = checked; palette.windowText: win.ink }
                                        LabelText { text: "القالب المخصص" }
                                    }
                                }
                            }
                            Box {
                                Layout.fillWidth: true; implicitHeight: 310
                                ColumnLayout {
                                    anchors.fill: parent; anchors.margins: 20; spacing: 10
                                    LabelText { text: "تعليمات إنتاج مستند Word"; font.pixelSize: 18; Layout.alignment: Qt.AlignRight }
                                    SubText { text: "المتغيرات: {transcript_path}   {output_path}   {title}   {position}"; Layout.fillWidth: true }
                                    ScrollView {
                                        Layout.fillWidth: true; Layout.fillHeight: true
                                        TextArea { id: promptField; text: settings.prompt; color: win.ink; wrapMode: TextEdit.Wrap; selectByMouse: true; font.pixelSize: 14; horizontalAlignment: Text.AlignRight; background: Rectangle { color: win.dark ? "#0f192a" : "#f7f9fc"; radius: 8; border.color: win.line } }
                                    }
                                }
                            }
                            AppButton { text: "تحرير جميع الإعدادات المتقدمة (JSON)"; Layout.alignment: Qt.AlignRight; onClicked: { rawSettings.text = backend.settingsJson; advancedDialog.open() } }
                            SubText { text: "الإعدادات الجديدة تُحفظ للمهام الجديدة. مفاتيح العمال ومسارات الاتصال قابلة للإصلاح للمهام المنتظرة."; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                        }
                    }
                }
            }
        }

        // Right navigation rail
        Rectangle {
            Layout.preferredWidth: 220
            Layout.fillHeight: true
            color: "#111d30"
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 20; spacing: 8
                ColumnLayout {
                    Layout.fillWidth: true; Layout.topMargin: 14; Layout.bottomMargin: 34; spacing: 0
                    Text { text: "وَرَق"; color: "#e9f2fb"; font.family: "Segoe UI"; font.pixelSize: 40; font.weight: Font.Bold; Layout.alignment: Qt.AlignRight }
                    Text { text: "WARAQ STUDIO"; color: "#71d9c1"; font.pixelSize: 11; font.letterSpacing: 2; Layout.alignment: Qt.AlignRight }
                }
                Repeater {
                    model: win.pageNames
                    delegate: Button {
                        required property string modelData
                        required property int index
                        objectName: "nav" + index
                        Layout.fillWidth: true; implicitHeight: 48
                        onClicked: win.page = index
                        Accessible.name: modelData
                        background: Rectangle { radius: 8; color: win.page === index ? "#203b48" : (parent.hovered ? "#1b2b42" : "transparent"); border.color: parent.activeFocus ? "#42d9b6" : "transparent" }
                        contentItem: Text { text: modelData; color: win.page === index ? "#80ecd0" : "#b5c3d6"; font.pixelSize: 15; font.weight: win.page === index ? Font.DemiBold : Font.Normal; horizontalAlignment: Text.AlignRight; verticalAlignment: Text.AlignVCenter; rightPadding: 14 }
                    }
                }
                Item { Layout.fillHeight: true }
                Rectangle { Layout.fillWidth: true; height: 1; color: "#29384b" }
                Text { text: "سير عملك، تحت سيطرتك"; color: "#a5b6cd"; font.pixelSize: 13; Layout.alignment: Qt.AlignRight; Layout.topMargin: 14 }
                Text { text: "نسخة محلية  ·  " + backend.version; color: "#91a3bc"; font.pixelSize: 11; Layout.alignment: Qt.AlignRight; Layout.bottomMargin: 8 }
            }
        }
    }

    Rectangle {
        visible: win.toastText.length > 0
        z: 20
        anchors.top: parent.top; anchors.topMargin: 100; anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(850, win.width - 100); height: toastLabel.implicitHeight + 36
        radius: 10; color: win.toastError ? "#432e25" : "#173b35"; border.color: win.toastError ? "#9b6847" : "#377f70"
        Text { id: toastLabel; anchors.fill: parent; anchors.margins: 18; color: "#f1f5f9"; text: win.toastText; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignRight; font.pixelSize: 14 }
        TapHandler { onTapped: win.toastText = "" }
    }

    Dialog {
        id: jobDialog
        anchors.centerIn: parent; width: Math.min(900, win.width - 80); height: 610; modal: true
        title: "تفاصيل المهمة"
        standardButtons: Dialog.Close
        onOpened: standardButton(Dialog.Close).text = "إغلاق"
        background: Rectangle { color: win.panel; radius: 12; border.color: win.line }
        contentItem: ColumnLayout {
            spacing: 14
            LabelText { text: selectedJob.title || ""; font.pixelSize: 20; Layout.fillWidth: true }
            SubText { text: (selectedJob.statusLabel || "") + "  ·  " + (selectedJob.stageLabel || ""); Layout.fillWidth: true }
            SubText { text: selectedJob.retryAt ? "أُعيدت الجدولة تلقائيًا — المحاولة القادمة الساعة " + selectedJob.retryAt : ""; visible: !!selectedJob.retryAt; color: win.dark ? "#87b7ff" : "#215cc4"; Layout.fillWidth: true }
            SubText { text: selectedJob.template ? "قالب البرومبت: " + selectedJob.template : ""; visible: !!selectedJob.template; Layout.fillWidth: true }
            SubText { text: selectedJob.error || ""; visible: !!selectedJob.error; color: win.dark ? "#ffbc88" : "#98450b"; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            ProgressBar { Layout.fillWidth: true; from: 0; to: 100; value: selectedJob.progress || 0; indeterminate: selectedJob.status === "running" && selectedJob.progress < 0; visible: selectedJob.status === "running" }
            RowLayout {
                AppButton { text: "إلغاء المهمة"; danger: true; enabled: selectedJob.status !== "completed"; onClicked: { backend.cancelJob(selectedJob.id); jobDialog.close() } }
                AppButton { text: "إعادة المحاولة"; enabled: ["failed","blocked","uncertain","paused","cancelled"].indexOf(selectedJob.status) >= 0 || !!selectedJob.retryAt; onClicked: { retryWarning.open() } }
                AppButton { text: "فتح المجلد"; onClicked: backend.openPath(selectedJob.folder) }
                AppButton { text: "فتح Word"; enabled: !!selectedJob.document; primary: true; onClicked: backend.openPath(selectedJob.document) }
            }
            RowLayout {
                AppButton { text: "إعادة OpenCode بنسخة جديدة"; enabled: !!selectedJob.transcript && selectedJob.status !== "running"; onClicked: { backend.retry(selectedJob.id,"opencode",true); jobDialog.close() } }
                AppButton { text: "إعادة التفريغ بنسخة جديدة"; enabled: !!selectedJob.audio && selectedJob.status !== "running"; onClicked: { paidRerun.open() } }
                Item { Layout.fillWidth: true }
            }
            LabelText { text: "سجل التنفيذ"; Layout.alignment: Qt.AlignRight }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true
                TextArea {
                    text: backend.logs.filter(function(e){return e.job_id === selectedJob.id}).map(function(e){return e.time + "  " + e.message}).join("\n\n")
                    readOnly: true; selectByMouse: true; color: win.muted; wrapMode: TextEdit.Wrap; font.pixelSize: 12
                    background: Rectangle { color: win.bg; radius: 8 }
                }
            }
        }
    }
    Dialog {
        id: retryWarning; anchors.centerIn: parent; width: 500; modal: true; title: "إعادة المحاولة"; standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "إعادة المحاولة"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { width: parent.width; wrapMode: Text.WordWrap; text: selectedJob.status === "uncertain" ? "الطلب السابق لدى Deepgram قد يكون اكتمل. إعادة الإرسال قد تسبب تكلفة إضافية. هل تريد المحاولة؟" : "ستُستأنف المرحلة باستخدام الملفات المكتملة المحفوظة." }
        onAccepted: { backend.retry(selectedJob.id, selectedJob.current, false); jobDialog.close() }
    }
    Dialog {
        id: paidRerun; anchors.centerIn: parent; width: 500; modal: true; title: "تفريغ جديد"; standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: { standardButton(Dialog.Ok).text = "تفريغ جديد"; standardButton(Dialog.Cancel).text = "إلغاء" }
        Label { width: parent.width; wrapMode: Text.WordWrap; text: "سيُرسل الصوت مجددًا إلى Deepgram وقد تنتج تكلفة جديدة. ستبقى النسخة السابقة محفوظة." }
        onAccepted: { backend.retry(selectedJob.id, "deepgram", true); jobDialog.close() }
    }
    Dialog {
        id: workerDialog; anchors.centerIn: parent; width: 620; height: workerKind === "deepgram" ? 650 : 450; modal: true
        title: "إعدادات العامل"; standardButtons: Dialog.Save | Dialog.Cancel
        onOpened: { standardButton(Dialog.Save).text = "حفظ"; standardButton(Dialog.Cancel).text = "إلغاء" }
        background: Rectangle { color: win.panel; radius: 12; border.color: win.line }
        contentItem: ColumnLayout {
            spacing: 10
            LabelText { text: "اسم العامل"; Layout.alignment: Qt.AlignRight }
            AppField { id: workerName; Layout.fillWidth: true }
            LabelText { text: "النموذج"; Layout.alignment: Qt.AlignRight }
            AppField { id: workerModel; Layout.fillWidth: true }
            RowLayout {
                AppField { id: workerExtra; Layout.fillWidth: true }
                LabelText { text: workerKind === "deepgram" ? "اللغة: ar أو auto" : "درجة التفكير" }
            }
            RowLayout {
                AppField { id: workerProject; Layout.fillWidth: true }
                LabelText { text: workerKind === "deepgram" ? "معرّف المشروع المشترك" : "الوكيل" }
            }
            ComboBox { id: endpoint; visible: workerKind === "deepgram"; Layout.fillWidth: true; model: ["https://api.deepgram.com", "https://api.eu.deepgram.com", "https://api.au.deepgram.com"] }
            CheckBox { id: diarize; visible: workerKind === "deepgram"; text: "تمييز المتحدثين"; palette.windowText: win.ink }
            LabelText { text: "مفتاح API خاص بهذا العامل"; visible: workerKind === "deepgram"; Layout.alignment: Qt.AlignRight }
            AppField { id: apiKey; visible: workerKind === "deepgram"; Layout.fillWidth: true; echoMode: TextInput.Password; placeholderText: "اتركه فارغًا للاحتفاظ بالمفتاح المحفوظ" }
            CheckBox { id: workerEnabled; text: "استقبال مهام جديدة"; checked: editingWorker.enabled || false; palette.windowText: win.ink }
            Item { Layout.fillHeight: true }
        }
        onAccepted: {
            editingWorker.name = workerName.text; editingWorker.model = workerModel.text; editingWorker.enabled = workerEnabled.checked
            if(workerKind === "deepgram") {
                editingWorker.language = workerExtra.text; editingWorker.project = workerProject.text; editingWorker.endpoint = endpoint.currentText; editingWorker.diarize = diarize.checked
                settings.deepgram_workers[workerIndex] = editingWorker
            } else {
                editingWorker.variant = workerExtra.text; editingWorker.agent = workerProject.text; settings.opencode_workers[workerIndex] = editingWorker
            }
            if(save() && workerKind === "deepgram" && apiKey.text.length > 0) backend.saveKey(editingWorker.id, apiKey.text)
            apiKey.text = ""
        }
        onRejected: apiKey.text = ""
    }
    Dialog {
        id: advancedDialog; anchors.centerIn: parent; width: Math.min(850, win.width - 100); height: win.height - 120; modal: true
        title: "الإعدادات المتقدمة — دون مفاتيح API"; standardButtons: Dialog.Save | Dialog.Cancel
        onOpened: { standardButton(Dialog.Save).text = "حفظ"; standardButton(Dialog.Cancel).text = "إلغاء" }
        contentItem: ScrollView { TextArea { id: rawSettings; text: backend.settingsJson; selectByMouse: true; wrapMode: TextEdit.Wrap; font.family: "Consolas"; font.pixelSize: 13 } }
        onAccepted: backend.saveSettings(rawSettings.text)
    }
    Dialog {
        id: templatesDialog; anchors.centerIn: parent; width: Math.min(860, win.width - 80); height: win.height - 140; modal: true
        title: "قوالب البرومبت"; standardButtons: Dialog.Save | Dialog.Cancel
        onOpened: { standardButton(Dialog.Save).text = "حفظ القوالب"; standardButton(Dialog.Cancel).text = "إلغاء"; win.reloadTemplates() }
        background: Rectangle { color: win.panel; radius: 12; border.color: win.line }
        contentItem: ColumnLayout {
            spacing: 10
            RowLayout {
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
                LabelText { text: "القوالب المحفوظة"; font.pixelSize: 16 }
            }
            ComboBox { id: tplBox; objectName: "templateEditorBox"; Layout.fillWidth: true; model: win.templ; textRole: "name"; onActivated: win.loadTemplateFields() }
            RowLayout {
                AppField { id: tplName; Layout.fillWidth: true; Accessible.name: "اسم القالب" }
                LabelText { text: "اسم القالب"; Layout.preferredWidth: 110 }
            }
            SubText { text: "يجب أن يحتوي كل قالب {transcript_path} و{output_path}، ويمكن استخدام {title} و{position}."; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true
                TextArea { id: tplText; color: win.ink; wrapMode: TextEdit.Wrap; selectByMouse: true; font.pixelSize: 14; horizontalAlignment: Text.AlignRight; background: Rectangle { color: win.dark ? "#0f192a" : "#f7f9fc"; radius: 8; border.color: win.line } }
            }
        }
        onAccepted: {
            win.commitTemplateFields()
            settings.opencode_templates = win.templ
            if (save()) win.templ = JSON.parse(JSON.stringify(backend.templates))
        }
    }
}
