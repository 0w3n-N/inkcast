import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Inkcast bar widget: an icon that shows whether the whiteboard is live, and a
// popup to start it, pair an iPad by QR code, open the share window, and
// toggle AirPlay mirroring.
//
// All work happens in the bundled `bin/inkcast` CLI, run with argument
// arrays (never through a shell). The widget only reads two small status
// files the CLI writes to $XDG_RUNTIME_DIR/inkcast/.
Panel {
  id: root
  moduleName: "io.github.0w3n-n.inkcast"
  ipcTarget: "io.github.0w3n-n.inkcast"
  manageIpc: false

  readonly property string pluginDir: decodeURIComponent(String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")).replace(/\/$/, "")
  readonly property string cli: pluginDir + "/bin/inkcast"
  readonly property string runDir: Quickshell.env("XDG_RUNTIME_DIR") + "/inkcast"

  readonly property int port: Model.toInt(setting("port", 8770), 1024, 65535, 8770)
  readonly property string airplayName: Model.airplayName(setting("airplayName", "Omarchy"))
  readonly property bool airplayAudio: setting("airplayAudio", "on") !== "off"

  property real nowSec: Date.now() / 1000
  property string statusRaw: ""
  readonly property var st: Model.parseStatus(statusRaw, nowSec)
  property var doctor: Model.parseDoctor("")
  property var mirrorInfo: Model.parseMirror("")
  property string pendingAction: ""
  property string errorText: ""

  property bool cursorActive: false
  property int cursorIndex: 0

  readonly property bool mirroring: mirrorProc.running
  readonly property bool serverOn: pendingAction === "start" || (st.running && pendingAction !== "stop")
  readonly property bool live: (st.running && st.pads > 0) || mirroring
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color barIconColor: live ? Color.accent : (st.running ? barForeground : Qt.darker(barForeground, 1.55))

  // Rows the keyboard cursor walks, top to bottom.
  readonly property var rows: st.running ? ["open", "mirror", "setup"] : ["start", "mirror", "setup"]

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function runCli(action) {
    if (actionProc.running) return
    errorText = ""
    pendingAction = action
    actionProc.command = ["python3", root.cli, action, "--port", String(root.port)]
    actionProc.running = true
  }

  function toggleServer() {
    runCli(st.running ? "stop" : "start")
  }

  function openWindow() {
    runCli("open")
  }

  function toggleMirror() {
    errorText = ""
    if (mirrorProc.running) {
      mirrorProc.running = false
      return
    }
    if (!doctor.uxplay) {
      runSetup()
      return
    }
    var cmd = ["python3", root.cli, "mirror", "--name", root.airplayName]
    if (!root.airplayAudio) cmd.push("--no-audio")
    mirrorProc.command = cmd
    mirrorProc.running = true
  }

  function runSetup() {
    // The setup script asks before every change, so it runs in a terminal.
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation",
                             Model.shellQuote(root.pluginDir + "/scripts/setup")])
    root.close()
  }

  function refresh() {
    nowSec = Date.now() / 1000
    statusFile.reload()
    if (!doctorProc.running) doctorProc.running = true
  }

  function activate(name) {
    if (name === "start") toggleServer()
    else if (name === "open") openWindow()
    else if (name === "mirror") toggleMirror()
    else if (name === "setup") runSetup()
  }

  function moveCursor(dy) {
    if (!cursorActive) {
      cursorActive = true
      return
    }
    cursorIndex = Math.max(0, Math.min(rows.length - 1, cursorIndex + dy))
  }

  onOpenedChanged: if (opened) {
    cursorActive = false
    cursorIndex = 0
    refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Component.onCompleted: refresh()

  // ---------------------------------------------------------------- state

  FileView {
    id: statusFile
    path: root.runDir + "/status.json"
    watchChanges: true
    printErrors: false
    onLoaded: root.statusRaw = text()
    onFileChanged: reload()
    onLoadFailed: root.statusRaw = ""
  }

  FileView {
    id: mirrorFile
    path: root.runDir + "/mirror.json"
    watchChanges: true
    printErrors: false
    onLoaded: root.mirrorInfo = Model.parseMirror(text())
    onFileChanged: reload()
    onLoadFailed: root.mirrorInfo = Model.parseMirror("")
  }

  // The server rewrites status.json every 10s; a stale file means it died.
  Timer {
    interval: root.opened ? 2000 : 6000
    running: true
    repeat: true
    onTriggered: {
      root.nowSec = Date.now() / 1000
      statusFile.reload()
    }
  }

  Process {
    id: actionProc
    command: []
    stderr: StdioCollector {
      id: actionErr
      waitForEnd: true
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        var msg = Model.singleLine(String(actionErr.text || "").trim().split("\n").pop(), 200)
        root.errorText = msg || "Inkcast could not " + root.pendingAction + " the whiteboard"
      }
      root.pendingAction = ""
      root.refresh()
    }
  }

  Process {
    id: doctorProc
    command: ["python3", root.cli, "doctor", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.doctor = Model.parseDoctor(text)
    }
  }

  Process {
    id: mirrorProc
    command: []
    stderr: StdioCollector {
      id: mirrorErr
      waitForEnd: true
    }
    onRunningChanged: if (running) mirrorFile.reload()
    onExited: function(exitCode) {
      // 0, SIGTERM (we stopped it) and SIGINT are normal ways to end.
      if (exitCode !== 0 && exitCode !== 143 && exitCode !== 130 && exitCode !== 15) {
        var msg = Model.singleLine(String(mirrorErr.text || "").trim().split("\n").pop(), 200)
        root.errorText = msg || "AirPlay mirroring stopped unexpectedly"
      }
    }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function board(): void { root.openWindow() }
    function start(): void { if (!root.st.running) root.toggleServer() }
    function stop(): void { if (root.st.running) root.toggleServer() }
    function mirror(): void { root.toggleMirror() }
    function status(): string { return Model.statusLine(root.st, root.mirroring) }
  }

  // ---------------------------------------------------------------- bar icon

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    tooltipText: "Inkcast · " + Model.statusLine(root.st, root.mirroring)
      + (root.st.running ? "\nRight-click: open the share window" : "\nRight-click: start and open the share window")
    iconComponent: Component {
      Item {
        InkcastIcon {
          anchors.centerIn: parent
          iconSize: Style.bar.iconCanvas
          color: root.barIconColor
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.openWindow()
      else if (buttonCode === Qt.MiddleButton) root.toggleMirror()
      else root.toggle()
    }
  }

  // ---------------------------------------------------------------- popup

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.moveCursor(dy) }
      onActivateRequested: if (root.cursorActive) root.activate(root.rows[root.cursorIndex])
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        var k = t.toLowerCase()
        if (k === "s") root.toggleServer()
        else if (k === "o") root.openWindow()
        else if (k === "m") root.toggleMirror()
      }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(12)

        PanelHero {
          id: hero
          width: parent.width
          title: "Inkcast"
          meta: root.pendingAction === "start" ? "Starting…"
            : root.pendingAction === "stop" ? "Stopping…"
            : Model.statusLine(root.st, root.mirroring)
          foreground: root.foreground
          fontFamily: root.fontFamily
          iconComponent: Component {
            InkcastIcon {
              iconSize: Style.font.display
              color: root.live ? Color.accent : root.foreground
            }
          }
          trailingControl: Component {
            ToggleSwitch {
              id: serverSwitch
              checked: root.serverOn
              busy: root.pendingAction !== ""
              foreground: hero.foreground
              onToggled: root.toggleServer()

              PanelToolTip {
                visible: serverSwitch.containsMouse
                text: root.st.running ? "Stop the whiteboard" : "Start the whiteboard"
                fontFamily: hero.fontFamily
              }
            }
          }
        }

        Text {
          textFormat: Text.PlainText
          visible: root.errorText !== ""
          width: parent.width
          text: root.errorText
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.Wrap
        }

        // Pairing: QR code drawn natively from the server's matrix.
        Row {
          visible: root.st.running && root.st.qrSize > 0
          width: parent.width
          spacing: Style.space(14)

          Rectangle {
            id: qrCanvas
            readonly property int moduleSize: root.st.qrSize > 0 ? Math.max(3, Math.floor(Style.space(132) / root.st.qrSize)) : 0
            width: root.st.qrSize * moduleSize
            height: width
            color: "white"
            radius: Style.cornerRadius

            Grid {
              anchors.fill: parent
              columns: root.st.qrSize

              Repeater {
                model: root.opened ? root.st.qrSize * root.st.qrSize : 0

                Rectangle {
                  required property int index
                  width: qrCanvas.moduleSize
                  height: qrCanvas.moduleSize
                  color: root.st.qr[Math.floor(index / root.st.qrSize)].charAt(index % root.st.qrSize) === "1" ? "#111111" : "transparent"
                }
              }
            }
          }

          Column {
            width: parent.width - qrCanvas.width - parent.spacing
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(6)

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: "Scan with the iPad's Camera, or open the link in Safari"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.Wrap
            }
            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: Model.urlForDisplay(root.st.url)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: Model.detailLine(root.st)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }
        }

        Text {
          textFormat: Text.PlainText
          visible: root.st.running && root.st.url === ""
          width: parent.width
          text: "No Wi-Fi or Ethernet address found. Connect to the same network as the iPad."
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.Wrap
        }

        ActionRow {
          name: root.st.running ? "open" : "start"
          glyph: "󰍹"
          title: root.st.running ? "Open the share window" : "Start the whiteboard"
          subtitle: root.st.running
            ? "A clean window to share in Zoom, Teams or Meet"
            : "Then scan the QR code with your iPad"
          onActivated: root.st.running ? root.openWindow() : root.toggleServer()
        }

        PanelSeparator { foreground: root.foreground }

        PanelSectionHeader {
          text: "SCREEN MIRRORING"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        ActionRow {
          name: "mirror"
          glyph: "󰄘"
          title: "AirPlay receiver"
          showSwitch: root.doctor.uxplay
          switchOn: root.mirroring
          subtitle: root.mirroring
            ? "On the iPad: Control Center › Screen Mirroring › " + root.airplayName
              + (root.mirrorInfo.pin !== "" ? "  ·  PIN " + root.mirrorInfo.pin : "")
            : root.doctor.known && !root.doctor.uxplay
              ? "Needs UxPlay. Select to run setup"
              : "Mirror any iPad app into a window"
          onActivated: root.toggleMirror()
        }

        PanelSeparator { foreground: root.foreground }

        ActionRow {
          name: "setup"
          glyph: "󰒓"
          title: "Setup"
          subtitle: !root.doctor.known ? "Install helpers and open the firewall port"
            : Model.needsSetup(root.doctor)
              ? (root.doctor.firewall === "needs-setup" ? "The firewall may block your iPad. Select to fix" : "Some helpers are missing. Select to install")
              : "Everything is ready"
          onActivated: root.runSetup()
        }
      }
    }
  }

  component ActionRow: CursorSurface {
    id: row

    property string name: ""
    property string glyph: ""
    property string title: ""
    property string subtitle: ""
    property bool showSwitch: false
    property bool switchOn: false

    signal activated()

    width: parent ? parent.width : 0
    hasCursor: root.cursorActive && root.rows[root.cursorIndex] === name
    foreground: root.foreground
    implicitHeight: rowLayout.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: {
        var i = root.rows.indexOf(row.name)
        if (i >= 0) {
          root.cursorActive = true
          root.cursorIndex = i
        }
      }
      onClicked: row.activated()
    }

    RowLayout {
      id: rowLayout
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(10)

      Text {
        textFormat: Text.PlainText
        text: row.glyph
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.heading
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: row.title
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: row.subtitle
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }
      }

      ToggleSwitch {
        visible: row.showSwitch
        checked: row.switchOn
        interactive: false
        foreground: root.foreground
        Layout.alignment: Qt.AlignVCenter
      }
    }
  }
}
