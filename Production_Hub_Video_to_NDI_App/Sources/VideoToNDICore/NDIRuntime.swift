import CNDIShim
import CoreVideo
import Foundation

public struct NDIRuntimeStatus: Equatable {
    public var available: Bool
    public var version: String
    public var message: String

    public init(available: Bool, version: String, message: String) {
        self.available = available
        self.version = version
        self.message = message
    }
}

public enum NDISenderError: LocalizedError {
    case runtimeUnavailable(String)
    case senderCreationFailed(String)
    case invalidFrame
    case sendFailed

    public var errorDescription: String? {
        switch self {
        case .runtimeUnavailable(let message): return message
        case .senderCreationFailed(let message): return message
        case .invalidFrame: return "The captured frame is not a valid BGRA pixel buffer."
        case .sendFailed: return "The NDI Runtime could not send the frame."
        }
    }
}

public final class NDIRuntime {
    public static let shared = NDIRuntime()

    private let lock = NSLock()
    private var initialized = false
    private var loaded = false

    private init() {}

    deinit {
        if loaded { ndi_bridge_shutdown() }
    }

    public func initialize(runtimePath: String? = nil) -> NDIRuntimeStatus {
        lock.lock()
        defer { lock.unlock() }
        if initialized { return statusLocked() }

        initialized = true
        if let runtimePath, !runtimePath.isEmpty {
            loaded = runtimePath.withCString { ndi_bridge_initialize($0) }
        } else {
            let bundled = Bundle.main.privateFrameworksPath.map {
                URL(fileURLWithPath: $0).appendingPathComponent("libndi.dylib").path
            }
            if let bundled, FileManager.default.fileExists(atPath: bundled) {
                loaded = bundled.withCString { ndi_bridge_initialize($0) }
            } else {
                loaded = ndi_bridge_initialize(nil)
            }
        }
        return statusLocked()
    }

    public var status: NDIRuntimeStatus {
        lock.lock()
        defer { lock.unlock() }
        return statusLocked()
    }

    private func statusLocked() -> NDIRuntimeStatus {
        if loaded && ndi_bridge_is_loaded() {
            return NDIRuntimeStatus(
                available: true,
                version: String(cString: ndi_bridge_version()),
                message: "Official NDI Runtime loaded"
            )
        }
        let error = String(cString: ndi_bridge_last_error())
        return NDIRuntimeStatus(
            available: false,
            version: "Unavailable",
            message: error.isEmpty ? "Official NDI Runtime is unavailable." : error
        )
    }
}

public protocol NDISending: AnyObject {
    var sourceName: String { get }
    var connectionCount: Int { get }
    func send(_ frame: VideoFrame) throws
    func stop()
}

public protocol NDISenderFactory {
    func makeSender(sourceName: String) throws -> NDISending
}

public struct OfficialNDISenderFactory: NDISenderFactory {
    public init() {}

    public func makeSender(sourceName: String) throws -> NDISending {
        let status = NDIRuntime.shared.initialize()
        guard status.available else { throw NDISenderError.runtimeUnavailable(status.message) }
        return try OfficialNDISender(sourceName: sourceName)
    }
}

public final class OfficialNDISender: NDISending {
    public let sourceName: String
    private let instance: ndi_bridge_sender_t
    private var stopped = false

    public init(sourceName: String) throws {
        self.sourceName = sourceName
        guard let instance = sourceName.withCString({ ndi_bridge_sender_create($0) }) else {
            throw NDISenderError.senderCreationFailed(String(cString: ndi_bridge_last_error()))
        }
        self.instance = instance
    }

    deinit { stop() }

    public var connectionCount: Int {
        stopped ? 0 : Int(ndi_bridge_sender_connection_count(instance))
    }

    public func send(_ frame: VideoFrame) throws {
        guard !stopped else { throw NDISenderError.sendFailed }
        let buffer = frame.pixelBuffer
        guard CVPixelBufferGetPixelFormatType(buffer) == kCVPixelFormatType_32BGRA else {
            throw NDISenderError.invalidFrame
        }

        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let baseAddress = CVPixelBufferGetBaseAddress(buffer) else {
            throw NDISenderError.invalidFrame
        }

        let ok = ndi_bridge_sender_send_bgra(
            instance,
            baseAddress.assumingMemoryBound(to: UInt8.self),
            Int32(frame.width),
            Int32(frame.height),
            Int32(CVPixelBufferGetBytesPerRow(buffer)),
            Int32(frame.frameRateNumerator),
            Int32(frame.frameRateDenominator),
            frame.timecode
        )
        if !ok { throw NDISenderError.sendFailed }
    }

    public func stop() {
        guard !stopped else { return }
        stopped = true
        ndi_bridge_sender_destroy(instance)
    }
}

public final class MockNDISender: NDISending {
    public let sourceName: String
    public private(set) var sentFrameCount = 0
    public private(set) var stopped = false
    public var connectionCount = 0

    public init(sourceName: String) { self.sourceName = sourceName }

    public func send(_ frame: VideoFrame) throws {
        guard !stopped else { throw NDISenderError.sendFailed }
        sentFrameCount += 1
    }

    public func stop() { stopped = true }
}

public struct MockNDISenderFactory: NDISenderFactory {
    public let senderBuilder: (String) -> NDISending

    public init(senderBuilder: @escaping (String) -> NDISending = { MockNDISender(sourceName: $0) }) {
        self.senderBuilder = senderBuilder
    }

    public func makeSender(sourceName: String) throws -> NDISending {
        senderBuilder(sourceName)
    }
}
