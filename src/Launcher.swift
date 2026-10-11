// SPDX-License-Identifier: MIT
import AppKit

final class Launcher: NSObject, NSApplicationDelegate {
    var process: Process?
    var log: FileHandle?

    func showFailure(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Battlefield 4 Portable"
        alert.informativeText = message
        alert.runModal()
        NSApp.terminate(nil)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let resources = Bundle.main.resourceURL!
            let location = resources.appendingPathComponent("data-location.txt")
            let data = try String(contentsOf: location, encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let root = URL(fileURLWithPath: data, isDirectory: true)
            let runtime = root.appendingPathComponent("runtime", isDirectory: true)
            try FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true)
            let logURL = runtime.appendingPathComponent("desktop-launch.log")
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
            let output = try FileHandle(forWritingTo: logURL)
            try output.seekToEnd()
            log = output
            let child = Process()
            child.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            child.arguments = ["-B", resources.appendingPathComponent("project/src/bf4.py").path,
                               "--data-dir", data, "play"]
            child.standardOutput = output
            child.standardError = output
            child.terminationHandler = { child in
                DispatchQueue.main.async {
                    if child.terminationStatus == 0 { NSApp.terminate(nil) }
                    else {
                        self.showFailure("Launch failed. Open Manage.command → Diagnose and the private desktop-launch.log. The current game was preserved.")
                    }
                }
            }
            process = child
            try child.run()
            // Keep AppKit processing events while the child starts Wine. A
            // synchronous wait here can leave folder access prompts pending.
        } catch {
            showFailure("Installation is incomplete or its private folder is inaccessible. Run Setup.command or check macOS Files and Folders permissions for Battlefield 4 Portable.")
        }
    }
}

let application = NSApplication.shared
let launcher = Launcher()
application.setActivationPolicy(.accessory)
application.delegate = launcher
application.run()
