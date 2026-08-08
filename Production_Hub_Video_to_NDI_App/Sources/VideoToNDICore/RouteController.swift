import AppKit
import AVFoundation
import Combine
import CoreVideo
import Foundation
import ServiceManagement

private final class RouteEngine {
    let routeID: UUID
    private let route: VideoRoute
    private let sender: NDISending
    private lazy var pump = LatestFramePump(
        label: "org.icas.video-to-ndi.route.\(routeID.uuidString)"
    ) { [weak self] frame in
        self?.process(frame)
    }
    private var subscription: CaptureSubscription?
    private let stateLock = NSLock()
    private var capturedFrames: UInt64 = 0
    private var sentFrames: UInt64 = 0
    private var lastRateDate = Date()
    private var lastRateFrames: UInt64 = 0
    private var lastStatePublishDate = Date.distantPast
    private var lastPreviewDate = Date.distantPast
    private var previewConsumerActive = false
    private let stateHandler: (RouteSnapshot, CGImage?) -> Void
    private var snapshot = RouteSnapshot()
    private var isStopped = false
    private var didReportFailure = false
    private let imageConverter = FrameToImageConverter()
    private let frameTransformer: VideoFrameTransformer
    private let watchdogQueue: DispatchQueue
    private var watchdog: DispatchSourceTimer?
    private var attachedAt = Date()
    var failureHandler: ((Error) -> Void)?

    init(
        route: VideoRoute,
        sender: NDISending,
        stateHandler: @escaping (RouteSnapshot, CGImage?) -> Void
    ) {
        self.route = route
        self.routeID = route.id
        self.sender = sender
        self.stateHandler = stateHandler
        self.frameTransformer = VideoFrameTransformer(route: route)
        self.watchdogQueue = DispatchQueue(label: "org.icas.video-to-ndi.watchdog.\(route.id.uuidString)")
    }

    func attach(subscription: CaptureSubscription, negotiatedFormat: String) {
        self.subscription = subscription
        stateLock.lock()
        guard !isStopped else {
            stateLock.unlock()
            subscription.cancel()
            return
        }
        snapshot.state = .running
        snapshot.message = "Publishing \(sender.sourceName)"
        snapshot.negotiatedFormat = negotiatedFormat
        attachedAt = Date()
        let value = snapshot
        stateLock.unlock()
        publish(value)
        startWatchdog()
    }

    func receive(_ frame: VideoFrame) {
        stateLock.lock()
        guard !isStopped else {
            stateLock.unlock()
            return
        }
        capturedFrames += 1
        snapshot.capturedFrames = capturedFrames
        snapshot.lastFrameDate = frame.capturedAt
        stateLock.unlock()
        pump.submit(frame)
    }

    func setPreviewConsumerActive(_ active: Bool) {
        stateLock.lock()
        previewConsumerActive = active
        stateLock.unlock()
    }

    func fail(_ error: Error) {
        stateLock.lock()
        guard !isStopped, !didReportFailure else {
            stateLock.unlock()
            return
        }
        didReportFailure = true
        snapshot.state = state(for: error)
        snapshot.message = error.localizedDescription
        let value = snapshot
        stateLock.unlock()
        publish(value)
        failureHandler?(error)
    }

    func stop() {
        shutdown(publishStoppedState: true)
    }

    func shutdownAfterFailure() {
        shutdown(publishStoppedState: false)
    }

    private func shutdown(publishStoppedState: Bool) {
        stateLock.lock()
        guard !isStopped else {
            stateLock.unlock()
            return
        }
        isStopped = true
        snapshot.state = .stopped
        snapshot.message = "Stopped"
        snapshot.connectionCount = 0
        let value = snapshot
        stateLock.unlock()

        subscription?.cancel()
        subscription = nil
        watchdog?.cancel()
        watchdog = nil
        pump.stopAndWait()
        sender.stop()
        if publishStoppedState { publish(value) }
    }

    private func process(_ frame: VideoFrame) {
        do {
            let outputFrame = try frameTransformer.transform(frame)
            try sender.send(outputFrame)
            stateLock.lock()
            guard !isStopped else {
                stateLock.unlock()
                return
            }
            sentFrames += 1
            snapshot.sentFrames = sentFrames
            if sentFrames == 1 {
                let fps = Double(outputFrame.frameRateNumerator) / Double(outputFrame.frameRateDenominator)
                snapshot.negotiatedFormat = "\(outputFrame.width)×\(outputFrame.height) @ \(String(format: "%.2f", fps)) fps"
            }
            let now = Date()
            let elapsed = now.timeIntervalSince(lastRateDate)
            if elapsed >= 1 {
                snapshot.effectiveFPS = Double(sentFrames - lastRateFrames) / elapsed
                lastRateFrames = sentFrames
                lastRateDate = now
            }

            let shouldPublishState = sentFrames == 1 || now.timeIntervalSince(lastStatePublishDate) >= 0.5
            let shouldCreatePreview = route.previewEnabled && previewConsumerActive &&
                now.timeIntervalSince(lastPreviewDate) >= 0.5
            if shouldPublishState {
                snapshot.droppedFrames = pump.droppedFrameCount
                snapshot.connectionCount = sender.connectionCount
                lastStatePublishDate = now
            }
            if shouldCreatePreview { lastPreviewDate = now }
            let value = snapshot
            stateLock.unlock()

            var preview: CGImage?
            if shouldCreatePreview {
                preview = imageConverter.makeCGImage(from: outputFrame.pixelBuffer)
            }
            if shouldPublishState || preview != nil {
                publish(value, preview: preview)
            }
        } catch {
            fail(error)
        }
    }

    private func publish(_ value: RouteSnapshot, preview: CGImage? = nil) {
        stateHandler(value, preview)
    }

    private func startWatchdog() {
        let timer = DispatchSource.makeTimerSource(queue: watchdogQueue)
        timer.schedule(deadline: .now() + 5, repeating: 2, leeway: .milliseconds(250))
        timer.setEventHandler { [weak self] in self?.checkFrameHealth() }
        watchdog = timer
        timer.resume()
    }

    private func checkFrameHealth() {
        stateLock.lock()
        guard !isStopped, !didReportFailure else {
            stateLock.unlock()
            return
        }
        let referenceDate = snapshot.lastFrameDate ?? attachedAt
        let frameAge = Date().timeIntervalSince(referenceDate)
        stateLock.unlock()

        if frameAge >= 5 {
            fail(CaptureError.deviceBusy("No video frames have arrived for \(Int(frameAge.rounded())) seconds."))
        }
    }

    private func state(for error: Error) -> RouteState {
        guard let captureError = error as? CaptureError else { return .error }
        switch captureError {
        case .deviceBusy: return .busy
        case .deviceMissing: return .missing
        default: return .error
        }
    }
}

@MainActor
public final class RouteController: ObservableObject {
    @Published public private(set) var routes: [VideoRoute]
    @Published public private(set) var snapshots: [UUID: RouteSnapshot] = [:]
    @Published public private(set) var previews: [UUID: NSImage] = [:]
    @Published public private(set) var devices: [CaptureDeviceInfo] = []
    @Published public private(set) var ndiStatus: NDIRuntimeStatus
    @Published public private(set) var cameraAuthorized = false
    @Published public var lastError = ""
    @Published public private(set) var launchAtLoginEnabled = false

    private let configurationStore: ConfigurationStore
    private let captureHub: CaptureHub
    private let senderFactory: NDISenderFactory
    private let deviceDiscovery: CaptureDeviceDiscovery
    private let permissionService: CameraPermissionService
    private var configuration: AppConfiguration
    private var engines: [UUID: RouteEngine] = [:]
    private var didInitialize = false
    private var mainWindowVisible = false

    public init(
        configurationStore: ConfigurationStore = ConfigurationStore(),
        captureHub: CaptureHub = CaptureHub(),
        senderFactory: NDISenderFactory = OfficialNDISenderFactory(),
        deviceDiscovery: CaptureDeviceDiscovery = CaptureDeviceDiscovery(),
        permissionService: CameraPermissionService = CameraPermissionService()
    ) {
        self.configurationStore = configurationStore
        self.captureHub = captureHub
        self.senderFactory = senderFactory
        self.deviceDiscovery = deviceDiscovery
        self.permissionService = permissionService
        self.configuration = configurationStore.load()
        self.routes = configuration.routes
        self.ndiStatus = NDIRuntime.shared.initialize()
        self.launchAtLoginEnabled = SMAppService.mainApp.status == .enabled
        for route in routes { snapshots[route.id] = RouteSnapshot() }
    }

    public var runningCount: Int {
        snapshots.values.filter { $0.state == .running }.count
    }

    public var overallHealthy: Bool {
        ndiStatus.available && snapshots.values.allSatisfy { $0.state != .error && $0.state != .busy }
    }

    public func initialize() {
        guard !didInitialize else { return }
        didInitialize = true
        permissionService.requestIfNeeded { [weak self] allowed in
            Task { @MainActor in
                guard let self else { return }
                self.cameraAuthorized = allowed
                self.refreshDevices()
                if !allowed {
                    self.lastError = "Camera access is required for physical capture devices. Test patterns remain available."
                }
                self.startAutomaticRoutes()
            }
        }
    }

    public func refreshDevices() {
        devices = deviceDiscovery.discover()
    }

    public func snapshot(for routeID: UUID) -> RouteSnapshot {
        snapshots[routeID] ?? RouteSnapshot()
    }

    public func preview(for routeID: UUID) -> NSImage? { previews[routeID] }

    public func setMainWindowVisible(_ visible: Bool) {
        guard mainWindowVisible != visible else { return }
        mainWindowVisible = visible
        engines.values.forEach { $0.setPreviewConsumerActive(visible) }
    }

    public func start(_ routeID: UUID) {
        guard engines[routeID] == nil, let route = routes.first(where: { $0.id == routeID }) else { return }
        do {
            try RouteValidator.validate(route, among: routes)
            guard route.isEnabled else { return }
            guard ndiStatus.available else {
                throw NDISenderError.runtimeUnavailable(ndiStatus.message)
            }
            var snapshot = snapshots[routeID] ?? RouteSnapshot()
            snapshot.state = .starting
            snapshot.message = "Opening source…"
            snapshots[routeID] = snapshot

            let sender = try senderFactory.makeSender(sourceName: route.ndiSourceName)
            let engine = RouteEngine(route: route, sender: sender) { [weak self] snapshot, preview in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.snapshots[routeID] = snapshot
                    if let preview {
                        self.previews[routeID] = NSImage(cgImage: preview, size: .zero)
                    }
                }
            }
            engine.setPreviewConsumerActive(mainWindowVisible)
            engine.failureHandler = { [weak self, weak engine] error in
                DispatchQueue.main.async {
                    guard let self, let engine, self.engines[routeID] === engine else { return }
                    self.engines.removeValue(forKey: routeID)
                    engine.shutdownAfterFailure()
                    var failed = self.snapshots[routeID] ?? RouteSnapshot()
                    if let captureError = error as? CaptureError {
                        switch captureError {
                        case .deviceBusy: failed.state = .busy
                        case .deviceMissing: failed.state = .missing
                        default: failed.state = .error
                        }
                    } else {
                        failed.state = .error
                    }
                    failed.message = error.localizedDescription
                    self.snapshots[routeID] = failed
                }
            }
            engines[routeID] = engine
            let (subscription, format) = try captureHub.subscribe(route: route) { [weak engine] frame in
                engine?.receive(frame)
            }
            engine.attach(subscription: subscription, negotiatedFormat: format)
        } catch let error as CaptureError {
            var snapshot = snapshots[routeID] ?? RouteSnapshot()
            if case .deviceBusy = error {
                snapshot.state = .busy
            } else if case .deviceMissing = error {
                snapshot.state = .missing
            } else {
                snapshot.state = .error
            }
            snapshot.message = error.localizedDescription
            snapshots[routeID] = snapshot
            engines.removeValue(forKey: routeID)?.shutdownAfterFailure()
        } catch {
            var snapshot = snapshots[routeID] ?? RouteSnapshot()
            snapshot.state = .error
            snapshot.message = error.localizedDescription
            snapshots[routeID] = snapshot
            engines.removeValue(forKey: routeID)?.shutdownAfterFailure()
        }
    }

    public func stop(_ routeID: UUID) {
        engines.removeValue(forKey: routeID)?.stop()
        var snapshot = snapshots[routeID] ?? RouteSnapshot()
        snapshot.state = .stopped
        snapshot.message = "Stopped"
        snapshots[routeID] = snapshot
    }

    public func startAll() {
        routes.filter(\.isEnabled).forEach { start($0.id) }
    }

    public func stopAll() {
        Array(engines.keys).forEach(stop)
        captureHub.stopAll()
    }

    public func startAutomaticRoutes() {
        routes.filter { $0.isEnabled && $0.autoStart }.forEach { start($0.id) }
    }

    public func save(_ route: VideoRoute) throws {
        try RouteValidator.validate(route, among: routes)
        if let index = routes.firstIndex(where: { $0.id == route.id }) {
            let wasRunning = engines[route.id] != nil
            if wasRunning { stop(route.id) }
            routes[index] = route
            if wasRunning && route.isEnabled { start(route.id) }
        } else {
            routes.append(route)
            snapshots[route.id] = RouteSnapshot()
        }
        try persist()
    }

    public func delete(_ routeID: UUID) {
        stop(routeID)
        routes.removeAll { $0.id == routeID }
        snapshots.removeValue(forKey: routeID)
        previews.removeValue(forKey: routeID)
        do { try persist() } catch { lastError = error.localizedDescription }
    }

    public func setLaunchAtLogin(_ enabled: Bool) {
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            launchAtLoginEnabled = SMAppService.mainApp.status == .enabled
        } catch {
            launchAtLoginEnabled = SMAppService.mainApp.status == .enabled
            lastError = "Launch at login could not be changed: \(error.localizedDescription)"
        }
    }

    public func clearError() { lastError = "" }

    public func diagnosticsText() -> String {
        var lines = [
            "Production Hub - Video to NDI",
            "NDI: \(ndiStatus.available ? "Available" : "Unavailable") \(ndiStatus.version)",
            "NDI detail: \(ndiStatus.message)",
            "Camera permission: \(permissionService.authorizationStatus.rawValue)",
            "Devices: \(devices.count)",
            "Routes: \(routes.count), running: \(runningCount)",
            "",
        ]
        for device in devices {
            lines.append("Device: \(device.displayName) [\(device.uniqueID)] formats=\(device.formats.count)")
        }
        for route in routes {
            let snapshot = snapshot(for: route.id)
            lines.append(
                "Route: \(route.name) | \(route.ndiSourceName) | \(snapshot.state.rawValue) | " +
                "\(snapshot.negotiatedFormat) | sent=\(snapshot.sentFrames) dropped=\(snapshot.droppedFrames) " +
                "receivers=\(snapshot.connectionCount) | \(snapshot.message)"
            )
        }
        return lines.joined(separator: "\n")
    }

    public func configurationDirectory() -> URL { configurationStore.directoryURL }

    private func persist() throws {
        configuration.routes = routes
        try configurationStore.save(configuration)
    }
}
