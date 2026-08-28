import Foundation
import Testing
@testable import VideoToNDICore

@Suite
struct ModelTests {
    @Test func defaultRoutesHaveStableOperatorNames() {
        let configuration = AppConfiguration()

        #expect(configuration.routes.map(\.name) == ["Audience Cam"])
        #expect(configuration.routes.map(\.ndiSourceName) == ["Production Hub - Audience Cam"])
    }

    @Test func duplicateEnabledNDINameIsRejectedCaseInsensitively() {
        let first = VideoRoute(
            name: "Audience",
            sourceKind: .testPattern,
            ndiSourceName: "Production Hub - Audience Cam"
        )
        let second = VideoRoute(
            name: "Duplicate",
            sourceKind: .testPattern,
            ndiSourceName: "production hub - audience cam"
        )

        do {
            try RouteValidator.validate(second, among: [first, second])
            Issue.record("Expected a duplicate NDI name error")
        } catch {
            #expect(error as? RouteValidationError == .duplicateNDIName)
        }
    }

    @Test func disabledDuplicateNDINameIsAllowed() throws {
        let first = VideoRoute(
            name: "Audience",
            sourceKind: .testPattern,
            ndiSourceName: "Production Hub - Audience Cam"
        )
        let second = VideoRoute(
            name: "Spare",
            sourceKind: .testPattern,
            ndiSourceName: "Production Hub - Audience Cam",
            isEnabled: false
        )

        try RouteValidator.validate(second, among: [first, second])
    }

    @Test func reservedCharactersAreRejected() {
        let route = VideoRoute(
            name: "Invalid",
            sourceKind: .testPattern,
            ndiSourceName: "Production Hub / Audience"
        )

        do {
            try RouteValidator.validate(route, among: [route])
            Issue.record("Expected an invalid NDI name error")
        } catch {
            #expect(error as? RouteValidationError == .invalidNDIName("/"))
        }
    }

    @Test func cameraRequiresADeviceButTestPatternDoesNot() throws {
        let camera = VideoRoute(name: "Camera", ndiSourceName: "Camera")
        let pattern = VideoRoute(name: "Pattern", sourceKind: .testPattern, ndiSourceName: "Pattern")

        do {
            try RouteValidator.validate(camera, among: [camera])
            Issue.record("Expected a missing device error")
        } catch {
            #expect(error as? RouteValidationError == .missingDevice)
        }
        try RouteValidator.validate(pattern, among: [pattern])
    }

    @Test func capturePreferenceDisplayName() {
        #expect(CapturePreference.automatic.displayName == "Automatic")
        #expect(CapturePreference(width: 1920, height: 1080, framesPerSecond: 29.97).displayName == "1920×1080 @ 29.97 fps")
    }

    @Test func orientationSettingsRoundTripThroughJSON() throws {
        let route = VideoRoute(
            name: "Rotated",
            sourceKind: .testPattern,
            ndiSourceName: "Rotated",
            rotation: .degrees270,
            flipHorizontal: true,
            flipVertical: true
        )

        let data = try JSONEncoder().encode(route)
        let decoded = try JSONDecoder().decode(VideoRoute.self, from: data)

        #expect(decoded == route)
        #expect(decoded.orientationDescription == "270° · Horizontal flip · Vertical flip")
    }

    @Test func olderRouteJSONDefaultsToNoOrientationTransform() throws {
        let route = VideoRoute(
            name: "Existing",
            sourceKind: .testPattern,
            ndiSourceName: "Existing"
        )
        let encoded = try JSONEncoder().encode(route)
        var object = try #require(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        object.removeValue(forKey: "rotation")
        object.removeValue(forKey: "flipHorizontal")
        object.removeValue(forKey: "flipVertical")
        let legacyData = try JSONSerialization.data(withJSONObject: object)

        let decoded = try JSONDecoder().decode(VideoRoute.self, from: legacyData)

        #expect(decoded.rotation == .degrees0)
        #expect(!decoded.flipHorizontal)
        #expect(!decoded.flipVertical)
    }
}
