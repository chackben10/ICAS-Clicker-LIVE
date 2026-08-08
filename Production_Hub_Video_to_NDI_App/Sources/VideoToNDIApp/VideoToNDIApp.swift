import AppKit
import SwiftUI
import VideoToNDICore

@MainActor
final class VideoToNDIAppDelegate: NSObject, NSApplicationDelegate {
    var terminationHandler: (() -> Void)?

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationWillTerminate(_ notification: Notification) {
        terminationHandler?()
    }
}

@main
struct VideoToNDIApp: App {
    @NSApplicationDelegateAdaptor(VideoToNDIAppDelegate.self) private var appDelegate
    @StateObject private var controller = RouteController()

    var body: some Scene {
        WindowGroup("Production Hub - Video to NDI", id: "main") {
            MainView()
                .environmentObject(controller)
                .frame(minWidth: 920, minHeight: 660)
                .onAppear {
                    appDelegate.terminationHandler = { controller.stopAll() }
                    controller.initialize()
                }
        }
        .windowStyle(.titleBar)
        .defaultSize(width: 1120, height: 760)

        MenuBarExtra("Production Hub - Video to NDI", systemImage: menuBarSymbol) {
            MenuBarView()
                .environmentObject(controller)
        }
        .menuBarExtraStyle(.menu)
    }

    private var menuBarSymbol: String {
        controller.runningCount > 0 ? "video.fill" : "video"
    }
}
