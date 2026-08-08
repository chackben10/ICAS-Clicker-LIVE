import Foundation

public enum VideoSourceKind: String, Codable, CaseIterable, Identifiable {
    case camera
    case testPattern

    public var id: String { rawValue }

    public var displayName: String {
        switch self {
        case .camera: return "Camera / Capture Device"
        case .testPattern: return "Test Pattern"
        }
    }
}

public struct CapturePreference: Codable, Equatable, Hashable {
    public var width: Int
    public var height: Int
    public var framesPerSecond: Double

    public init(width: Int = 0, height: Int = 0, framesPerSecond: Double = 0) {
        self.width = width
        self.height = height
        self.framesPerSecond = framesPerSecond
    }

    public static let automatic = CapturePreference()

    public var isAutomatic: Bool { width <= 0 || height <= 0 || framesPerSecond <= 0 }

    public var displayName: String {
        isAutomatic ? "Automatic" : "\(width)×\(height) @ \(formattedFPS) fps"
    }

    private var formattedFPS: String {
        framesPerSecond.rounded() == framesPerSecond
            ? String(Int(framesPerSecond))
            : String(format: "%.2f", framesPerSecond)
    }
}

public enum VideoRotation: Int, Codable, CaseIterable, Identifiable {
    case degrees0 = 0
    case degrees90 = 90
    case degrees180 = 180
    case degrees270 = 270

    public var id: Int { rawValue }
    public var displayName: String { "\(rawValue)°" }
    public var swapsDimensions: Bool { self == .degrees90 || self == .degrees270 }
}

public struct VideoRoute: Codable, Identifiable, Equatable {
    public var id: UUID
    public var name: String
    public var sourceKind: VideoSourceKind
    public var deviceUniqueID: String
    public var ndiSourceName: String
    public var capturePreference: CapturePreference
    public var rotation: VideoRotation
    public var flipHorizontal: Bool
    public var flipVertical: Bool
    public var isEnabled: Bool
    public var autoStart: Bool
    public var previewEnabled: Bool

    public init(
        id: UUID = UUID(),
        name: String,
        sourceKind: VideoSourceKind = .camera,
        deviceUniqueID: String = "",
        ndiSourceName: String,
        capturePreference: CapturePreference = .automatic,
        rotation: VideoRotation = .degrees0,
        flipHorizontal: Bool = false,
        flipVertical: Bool = false,
        isEnabled: Bool = true,
        autoStart: Bool = false,
        previewEnabled: Bool = true
    ) {
        self.id = id
        self.name = name
        self.sourceKind = sourceKind
        self.deviceUniqueID = deviceUniqueID
        self.ndiSourceName = ndiSourceName
        self.capturePreference = capturePreference
        self.rotation = rotation
        self.flipHorizontal = flipHorizontal
        self.flipVertical = flipVertical
        self.isEnabled = isEnabled
        self.autoStart = autoStart
        self.previewEnabled = previewEnabled
    }

    public var orientationDescription: String {
        var values = [rotation.displayName]
        if flipHorizontal { values.append("Horizontal flip") }
        if flipVertical { values.append("Vertical flip") }
        return values.joined(separator: " · ")
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case name
        case sourceKind
        case deviceUniqueID
        case ndiSourceName
        case capturePreference
        case rotation
        case flipHorizontal
        case flipVertical
        case isEnabled
        case autoStart
        case previewEnabled
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        sourceKind = try container.decode(VideoSourceKind.self, forKey: .sourceKind)
        deviceUniqueID = try container.decode(String.self, forKey: .deviceUniqueID)
        ndiSourceName = try container.decode(String.self, forKey: .ndiSourceName)
        capturePreference = try container.decode(CapturePreference.self, forKey: .capturePreference)
        rotation = try container.decodeIfPresent(VideoRotation.self, forKey: .rotation) ?? .degrees0
        flipHorizontal = try container.decodeIfPresent(Bool.self, forKey: .flipHorizontal) ?? false
        flipVertical = try container.decodeIfPresent(Bool.self, forKey: .flipVertical) ?? false
        isEnabled = try container.decode(Bool.self, forKey: .isEnabled)
        autoStart = try container.decode(Bool.self, forKey: .autoStart)
        previewEnabled = try container.decode(Bool.self, forKey: .previewEnabled)
    }

    public static let audienceDefault = VideoRoute(
        name: "Audience Cam",
        ndiSourceName: "Production Hub - Audience Cam"
    )

    public static let ptzDefault = VideoRoute(
        name: "PTZ Camera",
        ndiSourceName: "Production Hub - PTZ Camera"
    )
}

public struct AppConfiguration: Codable, Equatable {
    public var schemaVersion: Int
    public var routes: [VideoRoute]
    public var keepRunningWhenWindowCloses: Bool
    public var showMenuBarItem: Bool

    public init(
        schemaVersion: Int = 1,
        routes: [VideoRoute] = [.audienceDefault, .ptzDefault],
        keepRunningWhenWindowCloses: Bool = true,
        showMenuBarItem: Bool = true
    ) {
        self.schemaVersion = schemaVersion
        self.routes = routes
        self.keepRunningWhenWindowCloses = keepRunningWhenWindowCloses
        self.showMenuBarItem = showMenuBarItem
    }
}

public enum RouteState: String, Codable {
    case stopped
    case starting
    case running
    case reconnecting
    case missing
    case busy
    case error

    public var displayName: String { rawValue.capitalized }
}

public struct RouteSnapshot: Equatable {
    public var state: RouteState = .stopped
    public var message: String = "Ready"
    public var negotiatedFormat: String = "—"
    public var effectiveFPS: Double = 0
    public var capturedFrames: UInt64 = 0
    public var sentFrames: UInt64 = 0
    public var droppedFrames: UInt64 = 0
    public var connectionCount: Int = 0
    public var lastFrameDate: Date?

    public init() {}

    public var lastFrameAge: TimeInterval? {
        lastFrameDate.map { Date().timeIntervalSince($0) }
    }
}

public struct CaptureDeviceInfo: Identifiable, Equatable {
    public var id: String { uniqueID }
    public var uniqueID: String
    public var displayName: String
    public var modelID: String
    public var isConnected: Bool
    public var isSuspended: Bool
    public var formats: [CapturePreference]

    public init(
        uniqueID: String,
        displayName: String,
        modelID: String = "",
        isConnected: Bool = true,
        isSuspended: Bool = false,
        formats: [CapturePreference] = []
    ) {
        self.uniqueID = uniqueID
        self.displayName = displayName
        self.modelID = modelID
        self.isConnected = isConnected
        self.isSuspended = isSuspended
        self.formats = formats
    }
}

public enum RouteValidationError: LocalizedError, Equatable {
    case emptyName
    case emptyNDIName
    case invalidNDIName(String)
    case duplicateNDIName
    case missingDevice

    public var errorDescription: String? {
        switch self {
        case .emptyName: return "Route name cannot be empty."
        case .emptyNDIName: return "NDI source name cannot be empty."
        case .invalidNDIName(let characters): return "NDI source name contains reserved characters: \(characters)"
        case .duplicateNDIName: return "Each enabled route must have a unique NDI source name."
        case .missingDevice: return "Select a camera or capture device."
        }
    }
}

public enum RouteValidator {
    public static let invalidNDICharacters = CharacterSet(charactersIn: "\\/:*?\"<>|")

    public static func validate(_ route: VideoRoute, among routes: [VideoRoute]) throws {
        if route.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw RouteValidationError.emptyName
        }
        let ndiName = route.ndiSourceName.trimmingCharacters(in: .whitespacesAndNewlines)
        if ndiName.isEmpty { throw RouteValidationError.emptyNDIName }
        let invalidScalars = ndiName.unicodeScalars.filter { invalidNDICharacters.contains($0) }
        if !invalidScalars.isEmpty {
            throw RouteValidationError.invalidNDIName(String(String.UnicodeScalarView(invalidScalars)))
        }
        if route.sourceKind == .camera && route.deviceUniqueID.isEmpty {
            throw RouteValidationError.missingDevice
        }
        let duplicate = routes.contains {
            $0.id != route.id && $0.isEnabled && route.isEnabled &&
            $0.ndiSourceName.caseInsensitiveCompare(ndiName) == .orderedSame
        }
        if duplicate { throw RouteValidationError.duplicateNDIName }
    }
}
