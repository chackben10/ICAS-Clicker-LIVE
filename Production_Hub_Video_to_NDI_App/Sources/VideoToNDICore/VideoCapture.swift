import AVFoundation
import CoreImage
import CoreMedia
import CoreVideo
import Darwin
import Foundation

public struct VideoFrame {
    public let pixelBuffer: CVPixelBuffer
    public let width: Int
    public let height: Int
    public let frameRateNumerator: Int
    public let frameRateDenominator: Int
    public let timecode: Int64
    public let capturedAt: Date

    public init(
        pixelBuffer: CVPixelBuffer,
        width: Int,
        height: Int,
        frameRateNumerator: Int = 30000,
        frameRateDenominator: Int = 1001,
        timecode: Int64 = Int64.max,
        capturedAt: Date = Date()
    ) {
        self.pixelBuffer = pixelBuffer
        self.width = width
        self.height = height
        self.frameRateNumerator = frameRateNumerator
        self.frameRateDenominator = frameRateDenominator
        self.timecode = timecode
        self.capturedAt = capturedAt
    }
}

public final class CameraPermissionService {
    public init() {}

    public var authorizationStatus: AVAuthorizationStatus {
        AVCaptureDevice.authorizationStatus(for: .video)
    }

    public func requestIfNeeded(completion: @escaping (Bool) -> Void) {
        switch authorizationStatus {
        case .authorized:
            completion(true)
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video, completionHandler: completion)
        default:
            completion(false)
        }
    }
}

public final class CaptureDeviceDiscovery {
    public init() {}

    public func discover() -> [CaptureDeviceInfo] {
        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.builtInWideAngleCamera, .externalUnknown],
            mediaType: .video,
            position: .unspecified
        )
        return discovery.devices.map { device in
            CaptureDeviceInfo(
                uniqueID: device.uniqueID,
                displayName: device.localizedName,
                modelID: device.modelID,
                isConnected: device.isConnected,
                isSuspended: device.isSuspended,
                formats: Self.formats(for: device)
            )
        }.sorted { $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending }
    }

    private static func formats(for device: AVCaptureDevice) -> [CapturePreference] {
        var values = Set<String>()
        var formats: [CapturePreference] = []
        for format in device.formats {
            let dimensions = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            for range in format.videoSupportedFrameRateRanges {
                let commonRates = [60.0, 59.94, 30.0, 29.97, 25.0, 24.0, 23.98, 15.0]
                let supported = commonRates.filter {
                    $0 >= range.minFrameRate - 0.01 && $0 <= range.maxFrameRate + 0.01
                }
                for rate in supported.isEmpty ? [range.maxFrameRate] : supported {
                    let item = CapturePreference(
                        width: Int(dimensions.width),
                        height: Int(dimensions.height),
                        framesPerSecond: rate
                    )
                    let key = "\(item.width)x\(item.height)@\(String(format: "%.2f", item.framesPerSecond))"
                    if values.insert(key).inserted { formats.append(item) }
                }
            }
        }
        return formats.sorted {
            if $0.width * $0.height != $1.width * $1.height {
                return $0.width * $0.height > $1.width * $1.height
            }
            return $0.framesPerSecond > $1.framesPerSecond
        }
    }
}

public enum CaptureError: LocalizedError {
    case deviceMissing
    case deviceBusy(String)
    case cannotAddInput
    case cannotAddOutput
    case unsupportedFormat
    case cannotCreatePixelBuffer

    public var errorDescription: String? {
        switch self {
        case .deviceMissing: return "The configured camera or capture device is not connected."
        case .deviceBusy(let message): return "The device could not be opened. It may already be in use. \(message)"
        case .cannotAddInput: return "The capture session rejected this video input."
        case .cannotAddOutput: return "The capture session rejected its video output."
        case .unsupportedFormat: return "The requested resolution or frame rate is not supported."
        case .cannotCreatePixelBuffer: return "Could not allocate a test-pattern video frame."
        }
    }
}

final class LatestFramePump {
    private let lock = NSLock()
    private let queue: DispatchQueue
    private let handler: (VideoFrame) -> Void
    private var latestFrame: VideoFrame?
    private var drainScheduled = false
    private var stopped = false
    private var droppedFrames: UInt64 = 0

    init(label: String, handler: @escaping (VideoFrame) -> Void) {
        self.queue = DispatchQueue(label: label, qos: .userInitiated)
        self.handler = handler
    }

    func submit(_ frame: VideoFrame) {
        lock.lock()
        guard !stopped else { lock.unlock(); return }
        if latestFrame != nil { droppedFrames += 1 }
        latestFrame = frame
        if !drainScheduled {
            drainScheduled = true
            queue.async { [weak self] in self?.drain() }
        }
        lock.unlock()
    }

    var droppedFrameCount: UInt64 {
        lock.lock()
        defer { lock.unlock() }
        return droppedFrames
    }

    func stopAndWait() {
        lock.lock()
        stopped = true
        latestFrame = nil
        lock.unlock()
        // RouteEngine calls this from its owner queue, never from `queue`.
        // Waiting here guarantees its NDI sender is not destroyed while a
        // synchronous frame send is still in progress.
        queue.sync {}
    }

    private func drain() {
        while true {
            lock.lock()
            guard !stopped else {
                drainScheduled = false
                lock.unlock()
                return
            }
            guard let frame = latestFrame else {
                drainScheduled = false
                lock.unlock()
                return
            }
            latestFrame = nil
            lock.unlock()
            handler(frame)
        }
    }
}

public final class CaptureSubscription {
    private let cancellation: () -> Void
    private let lock = NSLock()
    private var cancelled = false

    init(cancellation: @escaping () -> Void) { self.cancellation = cancellation }
    deinit { cancel() }

    public func cancel() {
        lock.lock()
        defer { lock.unlock() }
        guard !cancelled else { return }
        cancelled = true
        cancellation()
    }
}

private protocol SharedFrameSource: AnyObject {
    var negotiatedFormat: String { get }
    var subscriberCount: Int { get }
    func start() throws
    func stop()
    func addSubscriber(id: UUID, callback: @escaping (VideoFrame) -> Void)
    func removeSubscriber(id: UUID)
}

private final class CameraFrameSource: NSObject, SharedFrameSource, AVCaptureVideoDataOutputSampleBufferDelegate {
    let deviceID: String
    let preference: CapturePreference
    private let session = AVCaptureSession()
    private let sessionQueue: DispatchQueue
    private let callbackLock = NSLock()
    private var callbacks: [UUID: (VideoFrame) -> Void] = [:]
    private var actualFPS = 30.0
    private(set) var negotiatedFormat = "—"

    init(deviceID: String, preference: CapturePreference) {
        self.deviceID = deviceID
        self.preference = preference
        self.sessionQueue = DispatchQueue(label: "org.icas.video-to-ndi.capture.\(deviceID)")
    }

    var subscriberCount: Int {
        callbackLock.lock(); defer { callbackLock.unlock() }
        return callbacks.count
    }

    func addSubscriber(id: UUID, callback: @escaping (VideoFrame) -> Void) {
        callbackLock.lock(); callbacks[id] = callback; callbackLock.unlock()
    }

    func removeSubscriber(id: UUID) {
        callbackLock.lock(); callbacks.removeValue(forKey: id); callbackLock.unlock()
    }

    func start() throws {
        guard let device = AVCaptureDevice(uniqueID: deviceID) else { throw CaptureError.deviceMissing }
        do {
            let input = try AVCaptureDeviceInput(device: device)
            session.beginConfiguration()
            defer { session.commitConfiguration() }
            guard session.canAddInput(input) else { throw CaptureError.cannotAddInput }
            session.addInput(input)

            try configure(device: device)
            let output = AVCaptureVideoDataOutput()
            output.alwaysDiscardsLateVideoFrames = true
            output.videoSettings = [
                kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32BGRA)
            ]
            output.setSampleBufferDelegate(self, queue: sessionQueue)
            guard session.canAddOutput(output) else { throw CaptureError.cannotAddOutput }
            session.addOutput(output)
            sessionQueue.async { [session] in session.startRunning() }
        } catch let error as CaptureError {
            throw error
        } catch {
            throw CaptureError.deviceBusy(error.localizedDescription)
        }
    }

    private func configure(device: AVCaptureDevice) throws {
        if preference.isAutomatic {
            let dimensions = CMVideoFormatDescriptionGetDimensions(device.activeFormat.formatDescription)
            let frameRate = device.activeVideoMinFrameDuration.isValid && device.activeVideoMinFrameDuration.seconds > 0
                ? 1.0 / device.activeVideoMinFrameDuration.seconds
                : 30.0
            actualFPS = frameRate.isFinite ? frameRate : 30
            negotiatedFormat = "\(dimensions.width)×\(dimensions.height) @ \(String(format: "%.2f", actualFPS)) fps"
            return
        }

        guard let match = device.formats.first(where: { format in
            let dimensions = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            guard dimensions.width == Int32(preference.width), dimensions.height == Int32(preference.height) else {
                return false
            }
            return format.videoSupportedFrameRateRanges.contains {
                preference.framesPerSecond >= $0.minFrameRate - 0.01 &&
                preference.framesPerSecond <= $0.maxFrameRate + 0.01
            }
        }) else { throw CaptureError.unsupportedFormat }

        try device.lockForConfiguration()
        defer { device.unlockForConfiguration() }
        device.activeFormat = match
        let timescale = Int32(60_000)
        let duration = CMTime(value: CMTimeValue(Double(timescale) / preference.framesPerSecond), timescale: timescale)
        device.activeVideoMinFrameDuration = duration
        device.activeVideoMaxFrameDuration = duration
        actualFPS = preference.framesPerSecond
        negotiatedFormat = preference.displayName
    }

    func stop() {
        sessionQueue.sync { [session] in
            if session.isRunning { session.stopRunning() }
        }
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let denominator = 1001
        let numerator = max(1, Int((actualFPS * Double(denominator)).rounded()))
        let frame = VideoFrame(
            pixelBuffer: pixelBuffer,
            width: CVPixelBufferGetWidth(pixelBuffer),
            height: CVPixelBufferGetHeight(pixelBuffer),
            frameRateNumerator: numerator,
            frameRateDenominator: denominator
        )
        callbackLock.lock()
        let currentCallbacks = Array(callbacks.values)
        callbackLock.unlock()
        currentCallbacks.forEach { $0(frame) }
    }
}

private final class TestPatternFrameSource: SharedFrameSource {
    private let width: Int
    private let height: Int
    private let fps: Double
    private let queue = DispatchQueue(label: "org.icas.video-to-ndi.test-pattern", qos: .userInitiated)
    private let callbackLock = NSLock()
    private var callbacks: [UUID: (VideoFrame) -> Void] = [:]
    private var timer: DispatchSourceTimer?
    private var frameIndex: UInt64 = 0
    private let basePixels: Data

    init(preference: CapturePreference) {
        let resolvedWidth = preference.isAutomatic ? 1280 : preference.width
        let resolvedHeight = preference.isAutomatic ? 720 : preference.height
        let resolvedFPS = preference.isAutomatic ? 30 : preference.framesPerSecond
        self.width = resolvedWidth
        self.height = resolvedHeight
        self.fps = resolvedFPS
        let colors: [(UInt8, UInt8, UInt8)] = [
            (235, 235, 235), (16, 235, 235), (235, 235, 16), (16, 235, 16),
            (235, 16, 235), (16, 16, 235), (235, 16, 16), (16, 16, 16)
        ]
        let barWidth = max(1, resolvedWidth / colors.count)
        var pixels = [UInt8](repeating: 0, count: resolvedWidth * resolvedHeight * 4)
        for y in 0..<resolvedHeight {
            for x in 0..<resolvedWidth {
                let color = colors[min(colors.count - 1, x / barWidth)]
                let offset = (y * resolvedWidth + x) * 4
                pixels[offset] = color.0
                pixels[offset + 1] = color.1
                pixels[offset + 2] = color.2
                pixels[offset + 3] = 255
            }
        }
        self.basePixels = Data(pixels)
    }

    var negotiatedFormat: String { "\(width)×\(height) @ \(String(format: "%.2f", fps)) fps" }
    var subscriberCount: Int {
        callbackLock.lock(); defer { callbackLock.unlock() }
        return callbacks.count
    }

    func addSubscriber(id: UUID, callback: @escaping (VideoFrame) -> Void) {
        callbackLock.lock(); callbacks[id] = callback; callbackLock.unlock()
    }

    func removeSubscriber(id: UUID) {
        callbackLock.lock(); callbacks.removeValue(forKey: id); callbackLock.unlock()
    }

    func start() throws {
        guard timer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: 1.0 / fps, leeway: .milliseconds(2))
        timer.setEventHandler { [weak self] in self?.emitFrame() }
        self.timer = timer
        timer.resume()
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    private func emitFrame() {
        var optionalBuffer: CVPixelBuffer?
        let attributes: [CFString: Any] = [
            kCVPixelBufferCGImageCompatibilityKey: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey: true,
            kCVPixelBufferIOSurfacePropertiesKey: [:]
        ]
        guard CVPixelBufferCreate(
            kCFAllocatorDefault,
            width,
            height,
            kCVPixelFormatType_32BGRA,
            attributes as CFDictionary,
            &optionalBuffer
        ) == kCVReturnSuccess, let buffer = optionalBuffer else { return }

        CVPixelBufferLockBaseAddress(buffer, [])
        if let base = CVPixelBufferGetBaseAddress(buffer) {
            let stride = CVPixelBufferGetBytesPerRow(buffer)
            let pointer = base.assumingMemoryBound(to: UInt8.self)
            basePixels.withUnsafeBytes { source in
                guard let sourceBase = source.baseAddress else { return }
                if stride == width * 4 {
                    memcpy(pointer, sourceBase, basePixels.count)
                } else {
                    for y in 0..<height {
                        memcpy(pointer.advanced(by: y * stride), sourceBase.advanced(by: y * width * 4), width * 4)
                    }
                }
            }
            let markerX = Int(frameIndex % UInt64(max(1, width)))
            for y in 0..<height {
                let row = pointer.advanced(by: y * stride)
                for markerOffset in -5...5 {
                    let x = markerX + markerOffset
                    if x >= 0 && x < width {
                        let offset = x * 4
                        row[offset] = 255
                        row[offset + 1] = 255
                        row[offset + 2] = 255
                        row[offset + 3] = 255
                    }
                }
            }
            let centerRow = pointer.advanced(by: (height / 2) * stride)
            for x in 0..<width {
                let offset = x * 4
                centerRow[offset] = 255
                centerRow[offset + 1] = 255
                centerRow[offset + 2] = 255
                centerRow[offset + 3] = 255
            }
        }
        CVPixelBufferUnlockBaseAddress(buffer, [])
        frameIndex += 1
        let denominator = 1001
        let frame = VideoFrame(
            pixelBuffer: buffer,
            width: width,
            height: height,
            frameRateNumerator: Int((fps * Double(denominator)).rounded()),
            frameRateDenominator: denominator
        )
        callbackLock.lock(); let currentCallbacks = Array(callbacks.values); callbackLock.unlock()
        currentCallbacks.forEach { $0(frame) }
    }
}

public final class CaptureHub {
    private let lock = NSLock()
    private var cameraSources: [String: CameraFrameSource] = [:]
    private var testSources: [String: TestPatternFrameSource] = [:]

    public init() {}

    public func subscribe(
        route: VideoRoute,
        callback: @escaping (VideoFrame) -> Void
    ) throws -> (CaptureSubscription, String) {
        let subscriberID = route.id
        lock.lock()
        defer { lock.unlock() }

        switch route.sourceKind {
        case .camera:
            // A physical capture device is always opened once. If multiple NDI
            // routes use it, the first active route owns format negotiation and
            // every additional sender fans out from the same frame stream.
            let key = route.deviceUniqueID
            let source: CameraFrameSource
            if let existing = cameraSources[key] {
                source = existing
            } else {
                source = CameraFrameSource(deviceID: route.deviceUniqueID, preference: route.capturePreference)
                try source.start()
                cameraSources[key] = source
            }
            source.addSubscriber(id: subscriberID, callback: callback)
            return (
                CaptureSubscription { [weak self, weak source] in
                    self?.removeCameraSubscriber(id: subscriberID, key: key, source: source)
                },
                source.negotiatedFormat
            )

        case .testPattern:
            let key = route.capturePreference.displayName
            let source: TestPatternFrameSource
            if let existing = testSources[key] {
                source = existing
            } else {
                source = TestPatternFrameSource(preference: route.capturePreference)
                try source.start()
                testSources[key] = source
            }
            source.addSubscriber(id: subscriberID, callback: callback)
            return (
                CaptureSubscription { [weak self, weak source] in
                    self?.removeTestSubscriber(id: subscriberID, key: key, source: source)
                },
                source.negotiatedFormat
            )
        }
    }

    private func removeCameraSubscriber(id: UUID, key: String, source: CameraFrameSource?) {
        lock.lock(); defer { lock.unlock() }
        guard let source else { return }
        source.removeSubscriber(id: id)
        if source.subscriberCount == 0 {
            source.stop()
            cameraSources.removeValue(forKey: key)
        }
    }

    private func removeTestSubscriber(id: UUID, key: String, source: TestPatternFrameSource?) {
        lock.lock(); defer { lock.unlock() }
        guard let source else { return }
        source.removeSubscriber(id: id)
        if source.subscriberCount == 0 {
            source.stop()
            testSources.removeValue(forKey: key)
        }
    }

    public func stopAll() {
        lock.lock()
        let cameras = Array(cameraSources.values)
        let tests = Array(testSources.values)
        cameraSources.removeAll()
        testSources.removeAll()
        lock.unlock()
        cameras.forEach { $0.stop() }
        tests.forEach { $0.stop() }
    }
}

final class FrameToImageConverter {
    private let context = CIContext(options: [.cacheIntermediates: false])

    func makeCGImage(from buffer: CVPixelBuffer) -> CGImage? {
        let image = CIImage(cvPixelBuffer: buffer)
        return context.createCGImage(image, from: image.extent)
    }
}
