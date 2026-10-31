import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

WDialog {
    id: control
    objectName: "accountTransferDialog"
    property QtObject controller
    property string mode: "export"
    property string importPath: ""
    property var choices: []
    property var selected: ({})
    readonly property int selectedCount: Object.keys(selected).length
    readonly property var transfer: controller ? controller.accountTransfer : ({status: "idle", message: ""})
    readonly property bool working: transfer.status === "working"
    readonly property bool succeeded: transfer.status === "success"
    readonly property int passwordLength: Array.from(transferPassword.text).length
    readonly property bool validPassword: passwordLength >= 12 && passwordLength <= 256
    readonly property bool canSubmit: !working && !succeeded && validPassword
        && (mode === "import" || (selectedCount > 0 && transferPassword.text === transferConfirmation.text))
    title: mode === "export" ? "تصدير حسابات ChatGPT" : "استيراد حسابات ChatGPT"
    anchors.centerIn: parent
    width: Math.min(650 * theme.scale, parent.width - 32 * theme.scale)
    height: Math.min((mode === "export" ? 720 : 560) * theme.scale, parent.height - 32 * theme.scale)
    closePolicy: working ? Popup.NoAutoClose : Popup.CloseOnEscape

    function clearSecrets() {
        transferPassword.clear()
        transferConfirmation.clear()
        passwordVisibility.checked = false
    }
    function revealField(field) {
        Qt.callLater(function() {
            var flick = transferBody.contentItem
            var position = field.mapToItem(flick.contentItem, 0, 0)
            var padding = 8 * control.theme.scale
            if (position.y < flick.contentY + padding) flick.contentY = Math.max(0, position.y - padding)
            else if (position.y + field.height > flick.contentY + flick.height - padding)
                flick.contentY = Math.max(0, Math.min(flick.contentHeight - flick.height, position.y + field.height - flick.height + padding))
        })
    }
    function openExport() {
        clearSecrets()
        mode = "export"
        choices = controller.accountExportChoices
        selected = ({})
        controller.resetAccountTransfer()
        open()
    }
    function openImport() {
        clearSecrets()
        var path = controller.chooseAccountImportFile()
        if (!path) return
        mode = "import"
        importPath = path
        controller.resetAccountTransfer()
        open()
    }
    function selectAccount(ident, checked) {
        var next = Object.assign({}, selected)
        if (checked) next[ident] = true
        else delete next[ident]
        selected = next
    }
    function submit() {
        if (!canSubmit) return
        if (mode === "export") controller.exportAccounts(Object.keys(selected), transferPassword.text)
        else controller.importAccounts(importPath, transferPassword.text)
        clearSecrets()
    }
    onOpened: transferPassword.forceActiveFocus()
    onClosed: { clearSecrets(); importPath = ""; choices = []; selected = ({}) }
    Connections {
        target: control.controller
        function onAccountTransferChanged() {
            if (control.transfer.status !== "working") control.clearSecrets()
            if (control.visible && control.transfer.status === "error") transferPassword.forceActiveFocus()
        }
    }
    component TransferText: Label {
        color: control.theme.ink
        font.family: control.theme.fontFamily
        font.weight: control.theme.fontWeight
        font.pixelSize: control.theme.fs(14)
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignRight
        Layout.fillWidth: true
    }
    contentItem: WDialogBody {
        id: transferBody
        objectName: "accountTransferBody"
        bodySpacing: 12 * control.theme.scale
        TransferText {
            text: control.mode === "export" ? "اختر الحسابات التي تريد حفظ بيانات دخولها في الملف المشفّر."
                : "أدخل كلمة مرور الملف لإضافة حساباته. تُتخطّى الحسابات الموجودة مع الاحتفاظ ببياناتها الحالية."
        }
        WCheckBox {
            objectName: "accountTransferSelectAll"
            theme: control.theme
            text: "تحديد الجميع · " + control.selectedCount + " من " + control.choices.length
            visible: control.mode === "export"
            enabled: !control.working && !control.succeeded && control.choices.length > 0
            checked: control.choices.length > 0 && control.selectedCount === control.choices.length
            Layout.fillWidth: true
            onClicked: {
                var next = ({})
                if (checked) for (var i = 0; i < control.choices.length; i++) next[control.choices[i].id] = true
                control.selected = next
            }
        }
        Rectangle {
            visible: control.mode === "export"
            color: control.theme.inset; border.color: control.theme.line; radius: 6
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(180, Math.max(52, control.choices.length * 52)) * control.theme.scale
            ScrollView {
                anchors.fill: parent; anchors.margins: 6 * control.theme.scale; clip: true
                ListView {
                    id: choiceList; objectName: "accountTransferChoices"
                    model: control.choices; spacing: 4 * control.theme.scale; clip: true
                    delegate: RowLayout {
                        required property var modelData
                        width: choiceList.width; spacing: 8 * control.theme.scale
                        TransferText {
                            text: modelData.alias + (modelData.email ? "\n" + modelData.email : "")
                            font.pixelSize: control.theme.fs(13)
                        }
                        WCheckBox {
                            objectName: "accountTransferChoice"
                            theme: control.theme; text: ""
                            checked: control.selected[modelData.id] === true
                            enabled: !control.working && !control.succeeded
                            Accessible.name: "تصدير " + modelData.alias + (modelData.email ? " " + modelData.email : "")
                            onClicked: control.selectAccount(modelData.id, checked)
                        }
                    }
                }
            }
        }
        TransferText {
            visible: control.mode === "export" && control.choices.length === 0
            text: "لا توجد حسابات ذات بيانات دخول محفوظة قابلة للتصدير."
            color: control.theme.warning
        }
        TransferText {
            objectName: "accountTransferFileName"; visible: control.mode === "import"
            text: "الملف: " + control.importPath.split(/[\\/]/).pop()
        }
        TransferText { text: control.mode === "export" ? "كلمة مرور هذا الملف" : "كلمة مرور الملف" }
        Field {
            id: transferPassword; objectName: "accountTransferPassword"
            theme: control.theme; uiScale: control.theme.scale; Layout.fillWidth: true
            maximumLength: 512; enabled: !control.working && !control.succeeded
            echoMode: passwordVisibility.checked ? TextInput.Normal : TextInput.Password
            inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
            Accessible.name: control.mode === "export" ? "كلمة مرور ملف تصدير الحسابات" : "كلمة مرور ملف استيراد الحسابات"
            onActiveFocusChanged: if (activeFocus) control.revealField(this)
            onAccepted: { if (control.mode === "export") transferConfirmation.forceActiveFocus(); else control.submit() }
        }
        TransferText { text: "تأكيد كلمة المرور"; visible: control.mode === "export" }
        Field {
            id: transferConfirmation; objectName: "accountTransferConfirmation"
            theme: control.theme; uiScale: control.theme.scale; Layout.fillWidth: true
            maximumLength: 512; visible: control.mode === "export"; enabled: !control.working && !control.succeeded
            echoMode: passwordVisibility.checked ? TextInput.Normal : TextInput.Password
            inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
            Accessible.name: "تأكيد كلمة مرور ملف تصدير الحسابات"
            onActiveFocusChanged: if (activeFocus) control.revealField(this)
            onAccepted: control.submit()
        }
        WCheckBox {
            id: passwordVisibility; objectName: "accountTransferShowPassword"
            theme: control.theme; text: "إظهار كلمة المرور"; enabled: !control.working && !control.succeeded
            Layout.fillWidth: true
            onActiveFocusChanged: if (activeFocus) control.revealField(this)
        }
        TransferText {
            text: "من 12 إلى 256 محرفًا. تُحسب العربية والمسافات كما تكتبها."
            color: control.theme.muted; font.pixelSize: control.theme.fs(12)
        }
        TransferText {
            objectName: "accountTransferValidation"
            text: control.passwordLength > 0 && !control.validPassword ? "أدخل كلمة مرور من 12 إلى 256 محرفًا."
                : control.mode === "export" && transferConfirmation.text.length > 0 && transferConfirmation.text !== transferPassword.text ? "كلمتا المرور غير متطابقتين." : ""
            visible: text.length > 0; color: control.theme.danger
        }
        TransferText {
            text: "احتفظ بكلمة المرور؛ لا يمكن استعادة الملف دونها. قد تتطلب جلسات الدخول إعادة تسجيل الدخول إذا انتهت صلاحيتها لدى الخدمة."
            color: control.theme.muted; font.pixelSize: control.theme.fs(12)
        }
    }
    footer: ColumnLayout {
        spacing: 8 * control.theme.scale
        RowLayout {
            visible: control.working || !!control.transfer.message
            Layout.fillWidth: true; Layout.leftMargin: 16 * control.theme.scale; Layout.rightMargin: 16 * control.theme.scale
            BusyIndicator {
                running: control.working; visible: control.working
                Layout.preferredWidth: 32 * control.theme.scale; Layout.preferredHeight: 32 * control.theme.scale
            }
            TransferText {
                objectName: "accountTransferMessage"
                text: control.transfer.message || ""
                color: control.transfer.status === "error" ? control.theme.danger : control.transfer.status === "success" ? control.theme.success : control.theme.ink
                Accessible.name: text
            }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8 * control.theme.scale
            Item { Layout.preferredWidth: 8 * control.theme.scale }
            WButton {
                objectName: "accountTransferCloseButton"
                theme: control.theme; uiScale: control.theme.scale
                text: control.succeeded ? "إغلاق" : "إلغاء"; enabled: !control.working
                onClicked: control.close()
            }
            Item { Layout.fillWidth: true }
            WButton {
                objectName: "accountTransferSubmitButton"
                theme: control.theme; uiScale: control.theme.scale; primary: true
                text: control.mode === "export" ? "تشفير وحفظ الملف" : "استيراد الحسابات"
                visible: !control.succeeded; enabled: control.canSubmit
                onClicked: control.submit()
            }
            Item { Layout.preferredWidth: 8 * control.theme.scale }
        }
    }
}
