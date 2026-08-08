import Foundation
import XCTest
@testable import VideoToNDICore

final class ModelTests: XCTestCase {
    func testDefaultRoutesHaveStableOperatorNames() {
        let configuration = AppConfiguration()

        XCTAssertEqual(configuration.routes.map(\.name), ["Audience Cam", "PTZ Camera"])
        XCTAssertEqual(
            configuration.routes.map(\.ndiSourceName),
            ["Production Hub - Audience Cam", "Production Hub - PTZ Camera"]
        )
    }

    func testDuplicateEnabledNDINameIsRejectedCaseInsensitively() {
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

        XCTAssertThrowsError(try RouteValidator.validate(second, among: [first, second])) { error in
            XCTAssertEqual(error as? RouteValidationError, .duplicateNDIName)
        }
    }

    func testDisabledDuplicateNDINameIsAllowed() throws {
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

        XCTAssertNoThrow(try RouteValidator.validate(second, among: [first, second]))
    }

    func testReservedCharactersAreRejected() {
        let route = VideoRoute(
            name: "Invalid",
            sourceKind: .testPattern,
            ndiSourceName: "Production Hub / Audience"
        )

        XCTAssertThrowsError(try RouteValidator.validate(route, among: [route])) { error in
            XCTAssertEqual(error as? RouteValidationError, .invalidNDIName("/"))
        }
    }

    func testCameraRequiresADeviceButTestPatternDoesNot() {
        let camera = VideoRoute(name: "Camera", ndiSourceName: "Camera")
        let pattern = VideoRoute(name: "Pattern", sourceKind: .testPattern, ndiSourceName: "Pattern")

        XCTAssertThrowsError(try RouteValidator.validate(camera, among: [camera])) { error in
            XCTAssertEqual(error as? RouteValidationError, .missingDevice)
        }
        XCTAssertNoThrow(try RouteValidator.validate(pattern, among: [pattern]))
    }

    func testCapturePreferenceDisplayName() {
        XCTAssertEqual(CapturePreference.automatic.displayName, "Automatic")
        XCTAssertEqual(
            CapturePreference(width: 1920, height: 1080, framesPerSecond: 29.97).displayName,
            "1920×1080 @ 29.97 fps"
        )
    }

    func testOrientationSettingsRoundTripThroughJSON() throws {
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

        XCTAssertEqual(decoded, route)
        XCTAssertEqual(decoded.orientationDescription, "270° · Horizontal flip · Vertical flip")
    }

    func testOlderRouteJSONDefaultsToNoOrientationTransform() throws {
        let route = VideoRoute(
            name: "Existing",
            sourceKind: .testPattern,
            ndiSourceName: "Existing"
        )
        let encoded = try JSONEncoder().encode(route)
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        object.removeValue(forKey: "rotation")
        object.removeValue(forKey: "flipHorizontal")
        object.removeValue(forKey: "flipVertical")
        let legacyData = try JSONSerialization.data(withJSONObject: object)

        let decoded = try JSONDecoder().decode(VideoRoute.self, from: legacyData)

        XCTAssertEqual(decoded.rotation, .degrees0)
        XCTAssertFalse(decoded.flipHorizontal)
        XCTAssertFalse(decoded.flipVertical)
    }
}

