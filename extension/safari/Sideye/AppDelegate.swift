import Cocoa
import SafariServices

@main
class AppDelegate: NSObject, NSApplicationDelegate {

    var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Force foreground activation (needed without storyboard)
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)

        setupWindow()
    }

    func setupWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 340),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Sideye"
        window.center()

        let view = NSView(frame: window.contentView!.bounds)
        view.autoresizingMask = [.width, .height]

        let titleLabel = NSTextField(labelWithString: "Sideye for Safari")
        titleLabel.font = NSFont.boldSystemFont(ofSize: 22)
        titleLabel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(titleLabel)

        let bodyLabel = NSTextField(wrappingLabelWithString:
            "This app contains the Sideye Safari extension.\n\n" +
            "Setup:\n" +
            "1. In Safari menu bar: Develop → Allow Unsigned Extensions\n" +
            "2. Safari → Settings → Extensions → enable Sideye\n" +
            "3. Grant it permission for github.com\n\n" +
            "The extension connects to your local bot at localhost:8000.\n\n" +
            "Note: If you don't see the Develop menu, enable it in\n" +
            "Safari → Settings → Advanced → Show Develop menu."
        )
        bodyLabel.font = NSFont.systemFont(ofSize: 13)
        bodyLabel.textColor = .secondaryLabelColor
        bodyLabel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(bodyLabel)

        let openButton = NSButton(title: "Open Safari Extensions Settings…", target: self, action: #selector(openSafariPrefs))
        openButton.bezelStyle = .rounded
        openButton.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(openButton)

        NSLayoutConstraint.activate([
            titleLabel.topAnchor.constraint(equalTo: view.topAnchor, constant: 28),
            titleLabel.centerXAnchor.constraint(equalTo: view.centerXAnchor),

            bodyLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 14),
            bodyLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 36),
            bodyLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -36),

            openButton.topAnchor.constraint(equalTo: bodyLabel.bottomAnchor, constant: 18),
            openButton.centerXAnchor.constraint(equalTo: view.centerXAnchor),
        ])

        window.contentView = view
        window.makeKeyAndOrderFront(nil)
    }

    @objc func openSafariPrefs() {
        SFSafariApplication.showPreferencesForExtension(withIdentifier: "com.prreviewbot.safari.Extension") { error in
            if let error = error {
                // Extension not found — likely unsigned extensions not enabled
                DispatchQueue.main.async {
                    let alert = NSAlert()
                    alert.messageText = "Extension Not Found"
                    alert.informativeText = "Safari can't find the extension. Make sure you've enabled:\n\nDevelop → Allow Unsigned Extensions\n\n(Enable the Develop menu in Safari → Settings → Advanced)"
                    alert.alertStyle = .warning
                    alert.addButton(withTitle: "OK")
                    alert.runModal()
                }
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }
}
