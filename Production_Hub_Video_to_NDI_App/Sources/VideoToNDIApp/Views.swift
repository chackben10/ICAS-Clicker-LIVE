import AppKit
import SwiftUI
import VideoToNDICore

struct MainView: View {
    @EnvironmentObject private var controller: RouteController
    @State private var editingRoute: VideoRoute?
    @State private var routePendingDeletion: VideoRoute?
    @State private var showingDiagnostics = false

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    runtimeBanner
                    routeHeader
                    if controller.routes.isEmpty {
                        emptyState
                    } else {
                        LazyVGrid(
                            columns: [GridItem(.adaptive(minimum: 410, maximum: 620), spacing: 16)],
                            alignment: .leading,
                            spacing: 16
                        ) {
                            ForEach(controller.routes) { route in
                                RouteCard(
                                    route: route,
                                    snapshot: controller.snapshot(for: route.id),
                                    preview: controller.preview(for: route.id),
                                    inputName: inputName(for: route),
                                    ndiAvailable: controller.ndiStatus.available,
                                    onStart: { controller.start(route.id) },
                                    onStop: { controller.stop(route.id) },
                                    onEdit: { editingRoute = route },
                                    onDelete: { routePendingDeletion = route }
                                )
                            }
                        }
                    }
                    settingsCard
                }
                .padding(24)
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .sheet(item: $editingRoute) { route in
            RouteEditorView(route: route, devices: controller.devices) { updated in
                do {
                    try controller.save(updated)
                    editingRoute = nil
                } catch {
                    controller.lastError = error.localizedDescription
                }
            }
        }
        .sheet(isPresented: $showingDiagnostics) {
            DiagnosticsView()
                .environmentObject(controller)
        }
        .alert(
            "Remove Route?",
            isPresented: Binding(
                get: { routePendingDeletion != nil },
                set: { if !$0 { routePendingDeletion = nil } }
            ),
            presenting: routePendingDeletion
        ) { route in
            Button("Cancel", role: .cancel) { routePendingDeletion = nil }
            Button("Remove", role: .destructive) {
                controller.delete(route.id)
                routePendingDeletion = nil
            }
        } message: { route in
            Text("This removes \(route.name) from the application. It does not change OBS.")
        }
        .alert(
            "Video to NDI",
            isPresented: Binding(
                get: { !controller.lastError.isEmpty },
                set: { if !$0 { controller.clearError() } }
            )
        ) {
            Button("OK") { controller.clearError() }
        } message: {
            Text(controller.lastError)
        }
    }

    private var header: some View {
        HStack(spacing: 14) {
            Image(systemName: "video.badge.waveform.fill")
                .font(.system(size: 31, weight: .semibold))
                .foregroundStyle(.blue)
            VStack(alignment: .leading, spacing: 2) {
                Text("Production Hub - Video to NDI")
                    .font(.title2.weight(.semibold))
                Text("Keep capture devices available as independent NDI sources.")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("Diagnostics") { showingDiagnostics = true }
            Button("Stop All", role: .destructive) { controller.stopAll() }
                .disabled(controller.runningCount == 0)
            Button("Start All") { controller.startAll() }
                .buttonStyle(.borderedProminent)
                .disabled(!controller.ndiStatus.available)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
    }

    @ViewBuilder
    private var runtimeBanner: some View {
        if controller.ndiStatus.available {
            StatusBanner(
                symbol: "checkmark.circle.fill",
                color: .green,
                title: "NDI Runtime \(controller.ndiStatus.version)",
                detail: "Ready to publish. One NDI source can be received by OBS and Production Hub simultaneously."
            )
        } else {
            StatusBanner(
                symbol: "exclamationmark.triangle.fill",
                color: .orange,
                title: "Official NDI Runtime Required",
                detail: controller.ndiStatus.message
            )
        }
    }

    private var routeHeader: some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text("Video Routes").font(.title3.weight(.semibold))
                Text("Each physical device is opened once and may feed multiple NDI senders.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                controller.refreshDevices()
            } label: {
                Label("Refresh Devices", systemImage: "arrow.clockwise")
            }
            Button {
                editingRoute = VideoRoute(
                    name: "New Camera",
                    ndiSourceName: "Production Hub - New Camera"
                )
            } label: {
                Label("Add Route", systemImage: "plus")
            }
            .buttonStyle(.borderedProminent)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "video.slash")
                .font(.system(size: 36))
                .foregroundStyle(.secondary)
            Text("No Video Routes").font(.title3.weight(.semibold))
            Text("Add a camera, capture device, or test pattern to begin.")
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, minHeight: 260)
    }

    private func inputName(for route: VideoRoute) -> String {
        guard route.sourceKind == .camera else { return "Test Pattern" }
        guard !route.deviceUniqueID.isEmpty else { return "Not selected" }
        return controller.devices.first(where: { $0.uniqueID == route.deviceUniqueID })?.displayName
            ?? "Disconnected device"
    }

    private var settingsCard: some View {
        GroupBox("Application") {
            VStack(alignment: .leading, spacing: 12) {
                Toggle(
                    "Launch at login",
                    isOn: Binding(
                        get: { controller.launchAtLoginEnabled },
                        set: { controller.setLaunchAtLogin($0) }
                    )
                )
                Text("Closing the window keeps active NDI outputs running. Use the menu-bar camera icon to reopen it or quit.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 6)
        }
    }
}

private struct StatusBanner: View {
    let symbol: String
    let color: Color
    let title: String
    let detail: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).foregroundStyle(color).font(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                Text(detail).font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(14)
        .background(color.opacity(0.09), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(color.opacity(0.24)))
    }
}

private struct RouteCard: View {
    let route: VideoRoute
    let snapshot: RouteSnapshot
    let preview: NSImage?
    let inputName: String
    let ndiAvailable: Bool
    let onStart: () -> Void
    let onStop: () -> Void
    let onEdit: () -> Void
    let onDelete: () -> Void

    private var isActive: Bool {
        [.starting, .running, .reconnecting].contains(snapshot.state)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top) {
                HStack(spacing: 9) {
                    Circle().fill(stateColor).frame(width: 10, height: 10)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(route.name).font(.headline)
                        Text(snapshot.state.displayName)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(stateColor)
                    }
                }
                Spacer()
                Menu {
                    Button("Edit", action: onEdit)
                    Button("Remove", role: .destructive, action: onDelete)
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
            }

            previewArea

            VStack(alignment: .leading, spacing: 7) {
                Label(route.ndiSourceName, systemImage: "network")
                    .font(.subheadline.weight(.medium))
                detailRow("Input", sourceDescription)
                detailRow("Format", snapshot.negotiatedFormat)
                detailRow("Transform", route.orientationDescription)
                detailRow("Output", "\(String(format: "%.1f", snapshot.effectiveFPS)) fps · \(formatted(snapshot.sentFrames)) frames")
                detailRow("Receivers", String(snapshot.connectionCount))
                if snapshot.droppedFrames > 0 {
                    detailRow("Dropped", formatted(snapshot.droppedFrames))
                }
                Text(snapshot.message)
                    .font(.caption)
                    .foregroundStyle(snapshot.state == .error || snapshot.state == .busy ? .red : .secondary)
                    .lineLimit(3)
            }

            HStack {
                if isActive {
                    Button("Stop", role: .destructive, action: onStop)
                } else {
                    Button("Start", action: onStart)
                        .buttonStyle(.borderedProminent)
                        .disabled(!route.isEnabled || !ndiAvailable)
                }
                Spacer()
                if route.autoStart {
                    Label("Starts automatically", systemImage: "bolt.fill")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(16)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.primary.opacity(0.09)))
    }

    @ViewBuilder
    private var previewArea: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 9).fill(Color.black.opacity(0.88))
            if let preview, route.previewEnabled {
                Image(nsImage: preview)
                    .resizable()
                    .scaledToFit()
                    .clipShape(RoundedRectangle(cornerRadius: 9))
            } else {
                VStack(spacing: 7) {
                    Image(systemName: route.previewEnabled ? "video" : "eye.slash")
                        .font(.title)
                    Text(route.previewEnabled ? "Preview appears when running" : "Preview disabled")
                        .font(.caption)
                }
                .foregroundStyle(.white.opacity(0.65))
            }
        }
        .aspectRatio(16 / 9, contentMode: .fit)
    }

    private var sourceDescription: String {
        inputName
    }

    private func detailRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).foregroundStyle(.secondary).frame(width: 66, alignment: .leading)
            Text(value).textSelection(.enabled).lineLimit(1)
            Spacer()
        }
        .font(.caption)
    }

    private var stateColor: Color {
        switch snapshot.state {
        case .running: return .green
        case .starting, .reconnecting: return .blue
        case .missing, .busy: return .orange
        case .error: return .red
        case .stopped: return .secondary
        }
    }

    private func formatted(_ value: UInt64) -> String {
        value.formatted(.number.grouping(.automatic))
    }
}

private struct RouteEditorView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var draft: VideoRoute
    @State private var validationMessage = ""
    let devices: [CaptureDeviceInfo]
    let onSave: (VideoRoute) -> Void

    init(route: VideoRoute, devices: [CaptureDeviceInfo], onSave: @escaping (VideoRoute) -> Void) {
        _draft = State(initialValue: route)
        self.devices = devices
        self.onSave = onSave
    }

    private var selectedDevice: CaptureDeviceInfo? {
        devices.first { $0.uniqueID == draft.deviceUniqueID }
    }

    private var formatChoices: [CapturePreference] {
        [.automatic] + (selectedDevice?.formats ?? [])
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Video Route").font(.title2.weight(.semibold))
                    Text("Capture one input and publish it continuously over NDI.")
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(22)
            Divider()

            Form {
                TextField("Route name", text: $draft.name)
                Picker("Input type", selection: $draft.sourceKind) {
                    ForEach(VideoSourceKind.allCases) { kind in
                        Text(kind.displayName).tag(kind)
                    }
                }
                if draft.sourceKind == .camera {
                    Picker("Camera / capture device", selection: $draft.deviceUniqueID) {
                        Text("Select a device…").tag("")
                        ForEach(devices) { device in
                            Text(device.displayName).tag(device.uniqueID)
                        }
                    }
                    Picker("Capture format", selection: $draft.capturePreference) {
                        ForEach(formatChoices, id: \.self) { preference in
                            Text(preference.displayName).tag(preference)
                        }
                    }
                } else {
                    Picker("Test format", selection: $draft.capturePreference) {
                        Text("1280×720 @ 30 fps").tag(CapturePreference.automatic)
                        Text("1920×1080 @ 30 fps").tag(CapturePreference(width: 1920, height: 1080, framesPerSecond: 30))
                        Text("1280×720 @ 15 fps").tag(CapturePreference(width: 1280, height: 720, framesPerSecond: 15))
                    }
                }
                Picker("Rotation", selection: $draft.rotation) {
                    ForEach(VideoRotation.allCases) { rotation in
                        Text(rotation.displayName).tag(rotation)
                    }
                }
                .pickerStyle(.segmented)
                Toggle("Flip horizontally", isOn: $draft.flipHorizontal)
                Toggle("Flip vertically", isOn: $draft.flipVertical)
                Text("Rotation and flips are applied to both the preview and NDI output.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("NDI source name", text: $draft.ndiSourceName)
                Toggle("Route enabled", isOn: $draft.isEnabled)
                Toggle("Start automatically when the app launches", isOn: $draft.autoStart)
                Toggle("Show a throttled preview", isOn: $draft.previewEnabled)
                if !validationMessage.isEmpty {
                    Text(validationMessage).foregroundStyle(.red).font(.caption)
                }
            }
            .formStyle(.grouped)

            Divider()
            HStack {
                Button("Cancel") { dismiss() }
                Spacer()
                Button("Save") {
                    validationMessage = ""
                    if draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        validationMessage = "Enter a route name."
                    } else if draft.ndiSourceName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        validationMessage = "Enter an NDI source name."
                    } else if draft.sourceKind == .camera && draft.deviceUniqueID.isEmpty {
                        validationMessage = "Select a camera or capture device."
                    } else {
                        onSave(draft)
                    }
                }
                .buttonStyle(.borderedProminent)
            }
            .padding(18)
        }
        .frame(width: 600, height: 680)
        .onChange(of: draft.deviceUniqueID) { _ in
            draft.capturePreference = .automatic
        }
    }
}

private struct DiagnosticsView: View {
    @EnvironmentObject private var controller: RouteController
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Diagnostics").font(.title2.weight(.semibold))
                Spacer()
                Button("Refresh") { controller.refreshDevices() }
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding(20)
            Divider()
            ScrollView {
                Text(controller.diagnosticsText())
                    .font(.system(.body, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(20)
            }
            Divider()
            HStack {
                Button("Reveal Configuration") {
                    NSWorkspace.shared.open(controller.configurationDirectory())
                }
                Spacer()
                Button("Copy Diagnostics") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(controller.diagnosticsText(), forType: .string)
                }
            }
            .padding(16)
        }
        .frame(width: 780, height: 580)
    }
}

struct MenuBarView: View {
    @EnvironmentObject private var controller: RouteController
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Text(menuSummary)
        if !controller.routes.isEmpty {
            Divider()
            ForEach(controller.routes) { route in
                let snapshot = controller.snapshot(for: route.id)
                Button {
                    if isActive(snapshot.state) {
                        controller.stop(route.id)
                    } else {
                        controller.start(route.id)
                    }
                } label: {
                    Label(
                        routeActionTitle(route: route, snapshot: snapshot),
                        systemImage: isActive(snapshot.state) ? "stop.circle" : "play.circle"
                    )
                }
                .disabled(
                    !isActive(snapshot.state) &&
                    (!route.isEnabled || !controller.ndiStatus.available)
                )
            }
        }
        Divider()
        Button("Open Video to NDI") {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        if controller.routes.count > 1 && controller.runningCount > 0 {
            Button("Stop All", role: .destructive) { controller.stopAll() }
        } else if controller.routes.count > 1 {
            Button("Start All") { controller.startAll() }
                .disabled(!controller.ndiStatus.available)
        }
        Divider()
        Button("Quit") {
            controller.stopAll()
            NSApp.terminate(nil)
        }
    }

    private var menuSummary: String {
        let receiverCount = controller.snapshots.values.reduce(0) { $0 + $1.connectionCount }
        let receiverText = receiverCount == 1 ? "1 receiver" : "\(receiverCount) receivers"
        return "\(controller.runningCount) running · \(receiverText)"
    }

    private func isActive(_ state: RouteState) -> Bool {
        state == .starting || state == .running || state == .reconnecting
    }

    private func routeActionTitle(route: VideoRoute, snapshot: RouteSnapshot) -> String {
        if isActive(snapshot.state) {
            return "Stop \(route.name) — \(String(format: "%.1f", snapshot.effectiveFPS)) fps"
        }
        return "Start \(route.name) — \(snapshot.state.displayName)"
    }
}
