import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Bar widget + popup for Philips Hue Bluetooth bulbs.
//
// All Bluetooth work lives in the Python daemon (`omarchy-hue`), which keeps
// the BLE connection and writes a JSON snapshot to
// $XDG_RUNTIME_DIR/omarchy-hue/state.json. This panel watches that file for
// state and shells out to the CLI for actions, so the UI never touches BLE
// directly and stays responsive while the bulb is busy.
Panel {
  id: root
  moduleName: "mylonz.hue"
  ipcTarget: "mylonz.hue"

  readonly property string home: Quickshell.env("HOME")
  readonly property string cli: home + "/.local/bin/omarchy-hue"
  readonly property string statePath: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/omarchy-hue/state.json"

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color accent: Color.accent
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  property var state: null
  property string selectedId: ""
  property string message: ""
  property bool messageIsError: false
  property var discovered: []
  property bool scanning: false
  property bool pairing: false
  property bool showPair: false
  property bool pairManual: false
  property real dragBrightness: -1
  property real dragKelvin: -1
  property real dragSpeed: -1

  readonly property bool effectActive: !!root.bulb && !!root.bulb.effect
    && root.bulb.effect.toLowerCase() !== "none"

  readonly property var bulbs: state && state.bulbs ? state.bulbs : []
  readonly property var bulb: {
    var list = root.bulbs
    for (var i = 0; i < list.length; i++) if (list[i].id === root.selectedId) return list[i]
    return list.length > 0 ? list[0] : null
  }
  readonly property bool anyOn: {
    var list = root.bulbs
    for (var i = 0; i < list.length; i++) if (list[i].power) return true
    return false
  }
  readonly property bool connected: !!root.bulb && root.bulb.connected === true
  readonly property bool busy: root.scanning || root.pairing || command.running

  readonly property var presets: [
    { "name": "Warm", "hex": "#ffd6a5" },
    { "name": "Cool", "hex": "#cfe8ff" },
    { "name": "Red", "hex": "#ff3b30" },
    { "name": "Orange", "hex": "#ff9500" },
    { "name": "Yellow", "hex": "#ffd60a" },
    { "name": "Green", "hex": "#34c759" },
    { "name": "Teal", "hex": "#00c7be" },
    { "name": "Blue", "hex": "#0a84ff" },
    { "name": "Purple", "hex": "#af52de" },
    { "name": "Pink", "hex": "#ff2d92" }
  ]

  readonly property var effectNames: [
    "None", "Candle", "Fireplace", "Prism", "Sparkle",
    "Opal", "Glisten", "Underwater", "Cosmos", "Sunbeam", "Enchant"
  ]

  // ------------------------------------------------------------- helpers
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }
  function pct(v) { return Math.round(root.clamp(Number(v || 0), 0, 254) / 254 * 100) }
  function kelvinOf(b) { return b && b.mireds ? Math.round(1000000 / b.mireds) : 2700 }
  function minKelvin(b) { return Math.round(1000000 / (b && b.maxMireds ? b.maxMireds : 500)) }
  function maxKelvin(b) { return Math.round(1000000 / (b && b.minMireds ? b.minMireds : 153)) }

  // Tri-state: while the daemon has not polled the bulb yet the capability
  // is unknown, and we show the control rather than flashing it away.
  function supports(name) {
    var b = root.bulb
    if (!b || !b.capabilities) return true
    var value = b.capabilities[name]
    return value === null || value === undefined ? true : value === true
  }

  // Effects to offer for the selected bulb: the per-bulb allowlist the daemon
  // learned/received, or the full catalogue when none is set. "None" is always
  // available so an active effect can be cleared.
  function effectChoices() {
    var b = root.bulb
    var allow = b && b.capabilities ? b.capabilities.effectList : null
    if (allow && allow.length > 0) {
      var out = ["None"]
      for (var i = 0; i < allow.length; i++) {
        var name = String(allow[i])
        out.push(name.charAt(0).toUpperCase() + name.slice(1).toLowerCase())
      }
      return out
    }
    return root.effectNames
  }

  // Effect speed (Hue "effect_speed", 0-254); 128 when the bulb has not
  // reported one. Only meaningful while an effect is running.
  function speedOf(b) {
    var v = b ? Number(b.effectSpeed) : NaN
    return isFinite(v) && v > 0 ? v : 128
  }

  function speedPct(v) {
    return Math.round(root.clamp(Number(v || 0), 0, 254) / 254 * 100)
  }

  function applyEffect(name, speed) {
    if (!root.selectedId) return
    root.runCommand(
      ["set", root.selectedId, "effect", name, String(Math.round(speed))], "set")
  }

  function setMessage(text, isError) {
    root.message = String(text || "")
    root.messageIsError = !!isError
  }

  function heroStatus() {
    if (!root.state) return "starting"
    if (root.bulbs.length === 0) return "no bulbs"
    var b = root.bulb
    if (!b) return ""
    if (b.error) return "error"
    if (!b.connected) return "offline"
    var parts = [b.power ? "on" : "off"]
    if (b.power) parts.push(root.pct(b.brightness) + "%")
    if (b.effect) parts.push(b.effect.toLowerCase())
    return parts.join(" · ")
  }

  function ensureSelection() {
    var list = root.bulbs
    if (list.length === 0) { root.selectedId = ""; return }
    for (var i = 0; i < list.length; i++) if (list[i].id === root.selectedId) return
    root.selectedId = list[0].id
  }

  function absorb(raw) {
    var text = String(raw || "")
    if (text.length === 0) return
    try {
      var parsed = JSON.parse(text)
      if (parsed && typeof parsed === "object") root.state = parsed
    } catch (e) {
      retryTimer.restart()  // torn read while the daemon rewrites the file
    }
  }

  function parseScan(output) {
    var rows = []
    var lines = String(output || "").split("\n")
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim()
      if (line.length === 0) continue
      var parts = line.split("\t")
      if (!parts[0]) continue
      rows.push({ address: parts[0], label: parts[1] || parts[0] })
    }
    root.discovered = rows
    if (rows.length === 0) root.setMessage("No Hue lights found. Put the bulb in pairing mode and retry.", true)
    else root.setMessage("", false)
  }

  function runCommand(args, purpose) {
    if (command.running) return false
    command.args = args
    command.purpose = purpose || "set"
    command.running = true
    return true
  }

  function togglePower() {
    if (!root.selectedId) return
    root.runCommand(["set", root.selectedId, "power", "toggle"], "set")
  }

  function nudgeBrightness(delta) {
    if (!root.bulb) return
    var next = root.clamp(Number(root.bulb.brightness || 128) + delta, 1, 254)
    root.dragBrightness = next
    root.runCommand(["set", root.selectedId, "brightness", String(Math.round(next))], "set")
    settleTimer.restart()
  }

  function startScan() {
    root.scanning = true
    root.discovered = []
    if (!root.runCommand(["scan"], "scan")) root.scanning = false
  }

  function pairLight(address, label) {
    root.pairing = true
    if (!root.runCommand(["pair", address, label || ""], "pair")) root.pairing = false
  }

  // ------------------------------------------------------------ state I/O
  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.absorb(text())
    onLoadFailed: root.state = null
  }

  Timer {
    id: retryTimer
    interval: 250
    repeat: false
    onTriggered: stateFile.reload()
  }

  // Watching a path the daemon creates at first start is not guaranteed, so
  // poll it as a slow safety net; the watcher still makes normal updates
  // instant.
  Timer {
    interval: 4000
    running: true
    repeat: true
    onTriggered: stateFile.reload()
  }

  Timer {
    id: settleTimer
    interval: 1600
    repeat: false
    onTriggered: {
    root.dragBrightness = -1
    root.dragKelvin = -1
    root.dragSpeed = -1
  }
  }

  Process {
    id: ensureProcess
    command: [root.cli, "ensure"]
  }

  Process {
    id: command
    property var args: []
    property string purpose: ""
    command: root.cli ? [root.cli].concat(args) : []
    stdout: StdioCollector { id: commandOut; waitForEnd: true }
    stderr: StdioCollector { id: commandErr; waitForEnd: true }
    onExited: function(code) {
      var out = String(commandOut.text || "").trim()
      var err = String(commandErr.text || "").trim()
      var purpose = command.purpose
      command.purpose = ""
      root.scanning = false
      root.pairing = false
      if (code !== 0) {
        root.setMessage(err || out || ("omarchy-hue failed (" + code + ")"), true)
      } else if (purpose === "scan") {
        root.parseScan(out)
      } else if (purpose === "pair") {
        var label = out.split("\t")[2] || out
        root.setMessage("Paired " + label, false)
        root.showPair = false
        root.pairManual = false
        root.discovered = []
      } else if (purpose === "set") {
        root.message = ""
      }
      stateFile.reload()
    }
  }

  Component.onCompleted: {
    ensureProcess.running = true
    stateFile.reload()
  }

  onStateChanged: {
    root.ensureSelection()
    // A bulb is controllable: collapse the pairing section unless the user
    // deliberately opened it. Empty: show it so there is a way forward.
    if (root.pairManual) return
    root.showPair = root.bulbs.length === 0
  }

  // ------------------------------------------------------------- bar icon
  // The bar sizes each widget slot from the entry point's implicit size. The
  // button is anchored to fill this item, so without these the slot collapses
  // to zero width and the widget never appears.
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.anyOn ? "󰛨" : "󰌵"
    // A couple of px larger than the stock glyph so the bulb reads at a glance
    // against a busy or transparent bar.
    fontSize: Style.bar.iconFont + 2
    tooltipText: {
      if (root.bulbs.length === 0) return "Hue Bluetooth — no bulbs"
      var b = root.bulb
      if (!b) return "Hue Bluetooth"
      return "Hue — " + (b.label || b.id) + (b.power ? " · on" : " · off")
        + "\nClick to control, right-click to toggle"
    }
    opacity: root.bulbs.length === 0 ? 0.6 : (root.connected ? 1.0 : 0.65)
    onPressed: function(b) {
      if (b === Qt.RightButton) root.togglePower()
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
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: root.togglePower()
      onTextKey: function(t) {
        if (t === "p" || t === "P") root.togglePower()
        else if (t === "r" || t === "R") stateFile.reload()
        else if (t === "+" || t === "=") root.nudgeBrightness(16)
        else if (t === "-" || t === "_") root.nudgeBrightness(-16)
      }

      Column {
        id: column
        anchors.fill: parent
        spacing: Style.spacing.lg

        // -------- Hero --------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, powerSwitch.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: root.anyOn ? "󰛨" : "󰌵"
            color: root.foreground
            opacity: root.connected || root.anyOn ? 1.0 : 0.5
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
          }

          ToggleSwitch {
            id: powerSwitch
            visible: !!root.bulb
            checked: root.bulb ? root.bulb.power === true : false
            busy: root.busy
            foreground: root.foreground
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            onToggled: root.togglePower()
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: powerSwitch.visible ? powerSwitch.left : parent.right
            anchors.rightMargin: Style.space(12)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: root.bulb ? (root.bulb.label || root.bulb.id) : "No bulbs paired"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: root.heroStatus().toUpperCase()
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
            }
          }
        }

        // -------- Bulb switch (only with more than one) --------
        Flow {
          width: parent.width
          spacing: Style.spacing.sm
          visible: root.bulbs.length > 1

          Repeater {
            model: root.bulbs

            delegate: Button {
              required property var modelData
              text: modelData.label || modelData.id
              selected: modelData.id === root.selectedId
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.bodySmall
              onClicked: root.selectedId = modelData.id
            }
          }
        }

        // -------- Error --------
        Item {
          width: parent.width
          visible: !!root.bulb && !!root.bulb.error
          implicitHeight: visible ? errorText.implicitHeight + Style.spacing.sm * 2 : 0

          Rectangle {
            anchors.fill: parent
            radius: Style.cornerRadius
            color: Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.12)
          }

          Text {
            id: errorText
            anchors.left: parent.left
            anchors.right: retryButton.left
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.space(10)
            anchors.rightMargin: Style.space(8)
            textFormat: Text.PlainText
            text: root.bulb ? root.bulb.error : ""
            color: root.urgent
            wrapMode: Text.WordWrap
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Button {
            id: retryButton
            anchors.right: parent.right
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            text: "Retry"
            foreground: root.urgent
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            onClicked: {
              if (!root.selectedId) return
              root.dragBrightness = -1
              root.runCommand(["refresh", root.selectedId], "set")
            }
          }
        }

        // -------- Brightness --------
        Column {
          width: parent.width
          spacing: Style.spacing.sm
          visible: !!root.bulb && root.supports("brightness")
          enabled: root.connected
          opacity: root.connected ? 1.0 : 0.45

          SectionRow {
            title: "Brightness"
            value: root.bulb ? root.pct(root.bulb.brightness) + "%" : ""
          }

          PanelSlider {
            width: parent.width
            bar: root.bar
            minimum: 1
            maximum: 254
            integer: true
            value: root.dragBrightness >= 0
              ? root.dragBrightness
              : (root.bulb && root.bulb.brightness ? root.bulb.brightness : 128)
            onMoved: root.dragBrightness = value
            onReleased: {
              root.dragBrightness = value
              root.runCommand(["set", root.selectedId, "brightness", String(Math.round(value))], "set")
              settleTimer.restart()
            }
          }
        }

        // -------- Colour temperature --------
        Column {
          width: parent.width
          spacing: Style.spacing.sm
          visible: !!root.bulb && root.supports("colourTemp")
          enabled: root.connected
          opacity: root.connected ? 1.0 : 0.45

          SectionRow {
            title: "Colour temperature"
            value: root.bulb ? root.kelvinOf(root.bulb) + " K" : ""
          }

          PanelSlider {
            width: parent.width
            bar: root.bar
            minimum: root.bulb ? root.minKelvin(root.bulb) : 2000
            maximum: root.bulb ? root.maxKelvin(root.bulb) : 6500
            integer: true
            value: root.dragKelvin >= 0 ? root.dragKelvin : root.kelvinOf(root.bulb)
            onMoved: root.dragKelvin = value
            onReleased: {
              root.dragKelvin = value
              root.runCommand(["set", root.selectedId, "temp", String(Math.round(value))], "set")
              settleTimer.restart()
            }
          }
        }

        // -------- Colour presets --------
        Column {
          width: parent.width
          spacing: Style.spacing.sm
          visible: !!root.bulb && root.supports("colourXy")
          enabled: root.connected
          opacity: root.connected ? 1.0 : 0.45

          SectionRow { title: "Colour" }

          Flow {
            width: parent.width
            spacing: Style.spacing.sm

            Repeater {
              model: root.presets

              delegate: Rectangle {
                required property var modelData
                width: Style.space(26)
                height: width
                radius: width / 2
                color: modelData.hex
                border.width: 1
                border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.3)

                MouseArea {
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.runCommand(["set", root.selectedId, "color", modelData.hex], "set")
                }

                PanelToolTip {
                  visible: parentColorArea.containsMouse
                  text: modelData.name
                  fontFamily: root.fontFamily
                }

                MouseArea {
                  id: parentColorArea
                  anchors.fill: parent
                  hoverEnabled: true
                  acceptedButtons: Qt.NoButton
                }
              }
            }
          }
        }

        // -------- Effects --------
        Column {
          width: parent.width
          spacing: Style.spacing.sm
          // Shown for any bulb that reports effect support. The chips come from
          // the per-bulb allowlist when one is configured (see
          // `omarchy-hue effects-set`), otherwise the full catalogue.
          visible: !!root.bulb && root.supports("effects")
          enabled: root.connected
          opacity: root.connected ? 1.0 : 0.45

          SectionRow { title: "Effects" }

          Flow {
            width: parent.width
            spacing: Style.spacing.sm

            Repeater {
              model: root.effectChoices()

              delegate: Button {
                required property var modelData
                text: modelData
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                selected: root.bulb && root.bulb.effect
                  ? root.bulb.effect.toLowerCase() === modelData.toLowerCase()
                  : modelData.toLowerCase() === "none"
                onClicked: root.applyEffect(modelData, root.speedOf(root.bulb))
              }
            }
          }

          // Speed only means something while an effect is running.
          Column {
            width: parent.width
            spacing: Style.spacing.sm
            visible: root.effectActive

            SectionRow {
              title: "Speed"
              value: root.speedPct(root.dragSpeed >= 0 ? root.dragSpeed : root.speedOf(root.bulb)) + "%"
            }

            PanelSlider {
              width: parent.width
              bar: root.bar
              minimum: 1
              maximum: 254
              integer: true
              value: root.dragSpeed >= 0 ? root.dragSpeed : root.speedOf(root.bulb)
              onMoved: root.dragSpeed = value
              onReleased: {
                root.dragSpeed = value
                root.applyEffect(root.bulb && root.bulb.effect ? root.bulb.effect : "none", value)
                settleTimer.restart()
              }
            }
          }
        }

        // -------- Pairing --------
        PanelSeparator { foreground: root.foreground }

        Column {
          width: parent.width
          spacing: Style.spacing.sm

          Button {
            width: parent.width
            text: root.showPair
              ? "Hide pairing"
              : (root.bulbs.length > 0 ? "Pair another light…" : "Pair a light…")
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: {
              root.showPair = !root.showPair
              root.pairManual = root.showPair
              if (root.showPair && root.discovered.length === 0) root.startScan()
            }
          }

          Column {
            width: parent.width
            spacing: Style.spacing.sm
            visible: root.showPair

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: "Put the bulb in pairing mode first: Hue app → Settings → Voice assistants → Make discoverable."
              color: root.dim
              wrapMode: Text.WordWrap
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Row {
              spacing: Style.spacing.sm

              Button {
                text: root.scanning ? "Scanning…" : "Scan for lights"
                foreground: root.foreground
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                enabled: !root.busy
                onClicked: root.startScan()
              }
            }

            Repeater {
              model: root.discovered

              delegate: Item {
                required property var modelData
                width: parent.width
                implicitHeight: Math.max(pairLabel.implicitHeight, pairButton.implicitHeight) + Style.spacing.sm

                Column {
                  id: pairLabel
                  anchors.left: parent.left
                  anchors.right: pairButton.left
                  anchors.rightMargin: Style.spacing.sm
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(1)

                  Text {
                    textFormat: Text.PlainText
                    width: parent.width
                    text: modelData.label
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    elide: Text.ElideRight
                  }

                  Text {
                    textFormat: Text.PlainText
                    width: parent.width
                    text: modelData.address
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }
                }

                Button {
                  id: pairButton
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  text: root.pairing ? "Pairing…" : "Pair"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  enabled: !root.busy
                  onClicked: root.pairLight(modelData.address, modelData.label)
                }
              }
            }

            Text {
              width: parent.width
              visible: root.message !== "" && root.showPair
              textFormat: Text.PlainText
              text: root.message
              color: root.messageIsError ? root.urgent : root.dim
              wrapMode: Text.WordWrap
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }
    }
  }

  // ------------------------------------------------------------ components
  component SectionRow: Item {
    property string title: ""
    property string value: ""

    implicitHeight: Math.max(titleText.implicitHeight, valueText.implicitHeight)
    width: parent ? parent.width : implicitWidth

    Text {
      id: titleText
      textFormat: Text.PlainText
      text: parent.title
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      topPadding: Math.ceil(font.pixelSize * 0.15)
    }

    Text {
      id: valueText
      textFormat: Text.PlainText
      visible: text !== ""
      text: parent.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
    }
  }
}
