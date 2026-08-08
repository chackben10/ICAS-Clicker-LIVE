import Foundation
import XCTest
@testable import VideoToNDICore

final class ConfigurationStoreTests: XCTestCase {
    private var temporaryDirectory: URL!

    override func setUpWithError() throws {
        temporaryDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("VideoToNDITests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: temporaryDirectory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        if let temporaryDirectory {
            try? FileManager.default.removeItem(at: temporaryDirectory)
        }
        temporaryDirectory = nil
    }

    func testMissingConfigurationLoadsDefaults() {
        let store = ConfigurationStore(directoryURL: temporaryDirectory)
        XCTAssertEqual(store.load(), AppConfiguration())
    }

    func testSaveAndLoadRoundTrip() throws {
        let store = ConfigurationStore(directoryURL: temporaryDirectory)
        let expected = AppConfiguration(routes: [
            VideoRoute(
                name: "Test",
                sourceKind: .testPattern,
                ndiSourceName: "Production Hub - Test",
                autoStart: true,
                previewEnabled: false
            )
        ])

        try store.save(expected)

        XCTAssertEqual(store.load(), expected)
        XCTAssertTrue(FileManager.default.fileExists(atPath: store.configurationURL.path))
    }

    func testSecondSaveKeepsPreviousConfigurationAsBackup() throws {
        let store = ConfigurationStore(directoryURL: temporaryDirectory)
        let original = AppConfiguration(routes: [
            VideoRoute(name: "Original", sourceKind: .testPattern, ndiSourceName: "Original")
        ])
        let replacement = AppConfiguration(routes: [
            VideoRoute(name: "Replacement", sourceKind: .testPattern, ndiSourceName: "Replacement")
        ])

        try store.save(original)
        try store.save(replacement)

        let backupData = try Data(contentsOf: store.backupURL)
        let backup = try JSONDecoder().decode(AppConfiguration.self, from: backupData)
        XCTAssertEqual(backup, original)
        XCTAssertEqual(store.load(), replacement)
    }

    func testCorruptPrimaryFallsBackToBackup() throws {
        let store = ConfigurationStore(directoryURL: temporaryDirectory)
        let original = AppConfiguration(routes: [
            VideoRoute(name: "Original", sourceKind: .testPattern, ndiSourceName: "Original")
        ])
        let replacement = AppConfiguration(routes: [
            VideoRoute(name: "Replacement", sourceKind: .testPattern, ndiSourceName: "Replacement")
        ])
        try store.save(original)
        try store.save(replacement)
        try Data("not json".utf8).write(to: store.configurationURL, options: .atomic)

        XCTAssertEqual(store.load(), original)
    }
}
