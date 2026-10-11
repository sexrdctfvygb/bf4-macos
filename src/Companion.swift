// SPDX-License-Identifier: MIT
import AppKit
import CoreGraphics
import ApplicationServices

let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let runtimeFolder = root.appendingPathComponent("runtime", isDirectory: true)
let runtimeFile = root.appendingPathComponent("wine-environment.json")
let project = root.appendingPathComponent("apps/Battlefield 4 Portable.app/Contents/Resources/project")
let configData = try! Data(contentsOf: root.appendingPathComponent("config.json"))
let config = try! JSONSerialization.jsonObject(with: configData) as! [String: Any]
let monitoring = config["monitoring"] as? Bool ?? false
let mouseEnabled = config["mouse_fix"] as? Bool ?? true

final class StatsView: NSView {
    var data: [String: Any] = [:]
    var bridgeOK = false
    override var isFlipped: Bool { true }
    func text(_ value: String, _ x: CGFloat, _ y: CGFloat, _ size: CGFloat,
              _ color: NSColor = .white, bold: Bool = false) {
        let font = NSFont.monospacedDigitSystemFont(ofSize: size, weight: bold ? .semibold : .regular)
        (value as NSString).draw(at: NSPoint(x: x, y: y), withAttributes: [.font: font, .foregroundColor: color])
    }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.withAlphaComponent(0.82).setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 10, yRadius: 10).fill()
        let seconds = (data["elapsed_seconds"] as? Double) ?? 0
        let count = (data["frames"] as? Int) ?? 0
        let status = data["status"] as? String ?? "waiting_for_frames"
        let live = status == "live"
        let complete = (data["data_complete"] as? Bool) ?? true
        text("BF4  ·  \(String(format: "%02d:%02d", Int(seconds) / 60, Int(seconds) % 60))  ·  \(count) кадров", 13, 9, 10, .lightGray)
        let keys = ["current_fps", "avg_fps", "low_1_fps", "low_01_fps"]
        let labels = ["FPS NOW", "AVG FPS", "1% LOW", "0,1% LOW"]
        for i in 0..<4 {
            let x = CGFloat(13 + i * 140)
            text(labels[i], x, 29, 10, .lightGray)
            let number = (data[keys[i]] as? Double).map { String(format: "%.1f", $0) } ?? "—"
            let color: NSColor = live && complete ? .white : .systemYellow
            text(number, x, 43, 26, color, bold: true)
        }
        let values = data["recent_ms"] as? [Double] ?? []
        if values.count > 1 {
            let rectangle = NSRect(x: 13, y: 83, width: 554, height: 35)
            NSColor.white.withAlphaComponent(0.12).setStroke()
            let guide = NSBezierPath()
            guide.move(to: NSPoint(x: rectangle.minX, y: rectangle.maxY))
            guide.line(to: NSPoint(x: rectangle.maxX, y: rectangle.maxY))
            guide.stroke()
            let ceiling = max(16.67, values.max() ?? 16.67)
            let line = NSBezierPath()
            for (i, value) in values.enumerated() {
                let point = NSPoint(x: rectangle.minX + CGFloat(i) / CGFloat(values.count - 1) * rectangle.width,
                                    y: rectangle.maxY - CGFloat(value / ceiling) * rectangle.height)
                if i == 0 { line.move(to: point) } else { line.line(to: point) }
            }
            NSColor.systemGreen.setStroke()
            line.lineWidth = 1
            line.stroke()
            text(String(format: "%.1f ms", ceiling), 501, 76, 9, .lightGray)
        }
        var hint = "F8 новый замер  ·  F9 скрыть"
        if !live { hint = count == 0 ? "Ожидание покадровых данных" : "Нет новых данных — замер приостановлен" }
        if !complete { hint = "Пропущены данные — результат неполный" }
        else if live && (data["low_01_preliminary"] as? Bool ?? true) { hint += "  ·  0,1% предв." }
        if data["recording"] as? Bool == true { hint += "  ·  CSV" }
        text(hint, 13, 126, 9, .lightGray)
    }
}

final class Controller: NSObject, NSApplicationDelegate {
    let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 580, height: 149),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    let view = StatsView(frame: NSRect(x: 0, y: 0, width: 580, height: 149))
    var item: NSStatusItem!
    var timer: Timer?
    var terminationSignal: DispatchSourceSignal?
    var eventTap: CFMachPort?
    var bridge: Process?
    var lastBridgeAttempt: TimeInterval = 0
    var collector: Process?
    var reopenProcess: Process?
    var bridgeInput: FileHandle?
    var hidden = false
    var pid: Int32 = 0
    var buttonsHeld = Set<Int>()
    var keysConsumed = Set<Int>()
    var navigationKeysHeld: [Int: Int] = [:]
    var tapKind = "unavailable"
    var lastActivationRequest = ""
    var lastTapRetry: TimeInterval = 0
    var frontPID: Int32 = 0
    var frontObserver: NSObjectProtocol?
    var lastInputState: Data?
    var previouslyActive = false
    var recordingItem: NSMenuItem?

    var gameActive: Bool {
        pid > 0 && frontPID == pid
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        // The companion can retain the desktop launcher's application identity
        // after its short-lived launcher exits. LaunchServices then delivers a
        // reopen event here instead of running the launcher a second time.
        guard reopenProcess?.isRunning != true else { return false }
        let logURL = runtimeFolder.appendingPathComponent("desktop-launch.log")
        do {
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
            let log = try FileHandle(forWritingTo: logURL)
            try log.seekToEnd()
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = ["-B", project.appendingPathComponent("src/bf4.py").path, "--data-dir", root.path, "play"]
            process.standardOutput = log
            process.standardError = log
            process.terminationHandler = { [weak self] finished in
                try? log.close()
                DispatchQueue.main.async {
                    if self?.reopenProcess === finished { self?.reopenProcess = nil }
                }
            }
            try process.run()
            reopenProcess = process
        } catch {
            print("Desktop reopen failed: \(error)")
        }
        return false
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        frontPID = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
        frontObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main) { [weak self] note in
                guard let self = self else { return }
                self.frontPID = (note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)?.processIdentifier ?? 0
                self.update()
            }
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.ignoresMouseEvents = true
        // Wine's fullscreen windows use the shielding level; ordinary overlay
        // levels are below them even when NSPanel.isVisible reports true.
        panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.maximumWindow)))
        panel.collectionBehavior = [.canJoinAllSpaces, .canJoinAllApplications, .fullScreenAuxiliary, .stationary]
        panel.title = "BF4 FPS"
        panel.contentView = view
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = monitoring ? "BF4 FPS" : "BF4 Input"
        let menu = NSMenu()
        for (title, action) in [("Новый замер (F8)", #selector(reset)),
                                ("Показать / скрыть (F9)", #selector(toggle)),
                                ("Запись кадров в CSV", #selector(toggleRecording)),
                                ("Завершить помощник", #selector(quit))] {
            let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
            entry.target = self
            menu.addItem(entry)
            if action == #selector(toggleRecording) { recordingItem = entry }
        }
        item.menu = menu
        signal(SIGTERM, SIG_IGN)
        terminationSignal = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        terminationSignal?.setEventHandler { NSApp.terminate(nil) }
        terminationSignal?.resume()
        startCollector()
        // The Windows bridge starts in update() after BF4 is discovered. EA
        // must initialize Wine before another Windows helper joins the session.
        startTap()
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in self?.update() }
        update()
    }

    func startCollector() {
        do {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = ["-B", project.appendingPathComponent("src/telemetry.py").path,
                                 "--data-dir", root.path]
            let logURL = runtimeFolder.appendingPathComponent("collector.log")
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
            let log = try FileHandle(forWritingTo: logURL)
            process.standardOutput = log
            process.standardError = log
            try process.run()
            collector = process
        } catch { print("Frame collector launch failed: \(error)") }
    }

    func startBridge() {
        guard bridge?.isRunning != true else { return }
        lastBridgeAttempt = Date().timeIntervalSince1970
        try? bridgeInput?.close()
        bridgeInput = nil
        do {
            let data = try Data(contentsOf: runtimeFile)
            let settings = try JSONSerialization.jsonObject(with: data) as! [String: String]
            let process = Process()
            process.executableURL = URL(fileURLWithPath: settings["CX_ROOT"]! + "/lib/wine/x86_64-unix/wine")
            process.arguments = [root.appendingPathComponent("tools/MouseBridge.exe").path]
            process.environment = ProcessInfo.processInfo.environment.merging(settings) { _, new in new }
            let input = Pipe()
            process.standardInput = input
            let logURL = runtimeFolder.appendingPathComponent("mouse-bridge.log")
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
            process.standardOutput = try FileHandle(forWritingTo: logURL)
            process.standardError = FileHandle.nullDevice
            try process.run()
            bridge = process
            bridgeInput = input.fileHandleForWriting
        } catch { print("Mouse bridge launch failed: \(error)") }
    }

    func sendButton(_ number: Int, _ down: Bool) {
        guard bridge?.isRunning == true else { return }
        do { try bridgeInput?.write(contentsOf: Data("\(number) \(down ? 1 : 0)\n".utf8)) }
        catch { print("Mouse bridge input unavailable") }
    }

    func startTap() {
        let types: [CGEventType] = [.otherMouseDown, .otherMouseUp, .keyDown, .keyUp, .flagsChanged]
        let mask = types.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << $1.rawValue) }
        let ref = Unmanaged.passUnretained(self).toOpaque()
        eventTap = CGEvent.tapCreate(tap: .cghidEventTap, place: .headInsertEventTap,
                                    options: .defaultTap, eventsOfInterest: mask,
                                    callback: { _, type, event, context in
            let owner = Unmanaged<Controller>.fromOpaque(context!).takeUnretainedValue()
            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let tap = owner.eventTap { CGEvent.tapEnable(tap: tap, enable: true) }
                return Unmanaged.passUnretained(event)
            }
            if type == .keyUp {
                let code = Int(event.getIntegerValueField(.keyboardEventKeycode))
                if let number = owner.navigationKeysHeld.removeValue(forKey: code) {
                    owner.sendButton(number, false)
                    owner.buttonsHeld.remove(number)
                    return nil
                }
                if owner.keysConsumed.remove(code) != nil { return nil }
            }
            if type == .otherMouseUp {
                let number = Int(event.getIntegerValueField(.mouseEventButtonNumber))
                if owner.buttonsHeld.remove(number) != nil {
                    owner.sendButton(number, false)
                    return nil
                }
            }
            guard owner.gameActive else { return Unmanaged.passUnretained(event) }
            if mouseEnabled && type == .flagsChanged && [54,55].contains(Int(event.getIntegerValueField(.keyboardEventKeycode))) {
                // Preserve macOS shortcut key events while keeping its Command
                // modifier transitions out of Wine's Ctrl game binding.
                event.flags = CGEventFlags(rawValue: event.flags.rawValue & ~UInt64(0x00100018))
                return Unmanaged.passUnretained(event)
            }
            if type == .keyDown && event.getIntegerValueField(.keyboardEventAutorepeat) == 0 {
                let code = Int(event.getIntegerValueField(.keyboardEventKeycode))
                if [30,33].contains(code) && event.flags.contains(.maskCommand) && !event.flags.contains(.maskControl) && owner.bridge?.isRunning == true {
                    let number = code == 33 ? 3 : 4
                    owner.sendButton(number, true)
                    owner.buttonsHeld.insert(number)
                    owner.navigationKeysHeld[code] = number
                    return nil
                }
                if monitoring && (code == 100 || code == 101) {
                    if code == 100 { owner.reset() } else { owner.toggle() }
                    owner.keysConsumed.insert(code)
                    return nil
                }
            }
            if type == .otherMouseDown || type == .otherMouseUp {
                let number = Int(event.getIntegerValueField(.mouseEventButtonNumber))
                if (number == 3 || number == 4) && owner.bridge?.isRunning == true {
                    let down = type == .otherMouseDown
                    owner.sendButton(number, down)
                    if down { owner.buttonsHeld.insert(number) }
                    return nil
                }
            }
            return Unmanaged.passUnretained(event)
        }, userInfo: ref)
        if let tap = eventTap {
            tapKind = "HID head"
            let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
            CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
            CGEvent.tapEnable(tap: tap, enable: true)
            print("BF4-only mouse input tap enabled")
        } else {
            print("BF4 mouse input tap unavailable: macOS Accessibility access required")
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
            lastTapRetry = Date().timeIntervalSince1970
        }
        let state: [String: Any] = ["hud_pid": ProcessInfo.processInfo.processIdentifier,
                                   "input_tap_enabled": eventTap != nil,
                                   "input_tap_kind": tapKind,
                                   "bridge_running": bridge?.isRunning == true]
        if let data = try? JSONSerialization.data(withJSONObject: state) {
            try? data.write(to: runtimeFolder.appendingPathComponent("input-state.json"), options: .atomic)
        }
    }

    func update() {
        if let gameData = try? Data(contentsOf: runtimeFolder.appendingPathComponent("game-state.json")),
           let game = try? JSONSerialization.jsonObject(with: gameData) as? [String: Any] {
            pid = (game["bf4_pid"] as? NSNumber)?.int32Value ?? 0
        }
        if monitoring, let data = try? Data(contentsOf: runtimeFolder.appendingPathComponent("stats.json")),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            view.data = object
        }
        if let request = try? String(contentsOf: runtimeFolder.appendingPathComponent("activate-request"), encoding: .utf8),
           request != lastActivationRequest, pid > 0,
           Int32(request.split(separator: " ").first ?? "") == pid {
            lastActivationRequest = request
            NSRunningApplication(processIdentifier: pid)?.activate(options: [.activateAllWindows])
        }
        if eventTap == nil && Date().timeIntervalSince1970 - lastTapRetry > 10 {
            lastTapRetry = Date().timeIntervalSince1970
            if AXIsProcessTrusted() { startTap() }
        }
        // An up event while another app has focus is intentionally not injected
        // by the bridge. Clear any game-side held state when BF4 regains focus.
        if gameActive && !previouslyActive && mouseEnabled {
            sendButton(3, false)
            sendButton(4, false)
        }
        previouslyActive = gameActive
        if mouseEnabled && pid > 0 && bridge?.isRunning != true &&
            Date().timeIntervalSince1970 - lastBridgeAttempt > 2 {
            startBridge()
            sendButton(3, false)
            sendButton(4, false)
        }
        view.bridgeOK = eventTap != nil && bridge?.isRunning == true
        recordingItem?.state = view.data["recording"] as? Bool == true ? .on : .off
        if monitoring && gameActive && !hidden {
            let applicationWasHidden = NSApp.isHidden
            if applicationWasHidden { NSApp.unhideWithoutActivation() }
            let screen = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1920, height: 1080)
            let origin = NSPoint(x: screen.minX + 16, y: screen.maxY - 178)
            if panel.frame.origin != origin { panel.setFrameOrigin(origin) }
            if !panel.isVisible || applicationWasHidden { panel.orderFrontRegardless() }
            view.needsDisplay = true
        } else if panel.isVisible { panel.orderOut(nil) }
        let state: [String: Any] = ["hud_pid": ProcessInfo.processInfo.processIdentifier,
                                   "input_tap_enabled": eventTap != nil,
                                   "input_tap_kind": tapKind,
                                   "bridge_running": bridge?.isRunning == true,
                                   "bf4_pid": pid,
                                   "front_pid": frontPID,
                                   "game_active": gameActive,
                                   "panel_hidden": hidden,
                                   "panel_visible": panel.isVisible]
        if let data = try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]), data != lastInputState {
            do {
                try data.write(to: runtimeFolder.appendingPathComponent("input-state.json"), options: .atomic)
                lastInputState = data
            } catch { print("BF4 focus state write unavailable") }
        }
    }

    @objc func reset() {
        try? Data(String(Date().timeIntervalSince1970).utf8).write(
            to: runtimeFolder.appendingPathComponent("reset-request"), options: .atomic)
    }
    @objc func toggle() { hidden.toggle(); update() }
    @objc func toggleRecording() {
        let enabled = !(view.data["recording"] as? Bool ?? false)
        try? Data((enabled ? "1" : "0").utf8).write(
            to: runtimeFolder.appendingPathComponent("record-frames"), options: .atomic)
    }
    @objc func quit() { NSApp.terminate(nil) }
    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
        if let observer = frontObserver { NSWorkspace.shared.notificationCenter.removeObserver(observer) }
        if let tap = eventTap { CGEvent.tapEnable(tap: tap, enable: false) }
        for number in buttonsHeld { sendButton(number, false) }
        try? bridgeInput?.close()
        if let process = collector, process.isRunning { process.terminate() }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let controller = Controller()
app.delegate = controller
app.run()
