import Foundation
import Testing
@testable import VideoToNDICore

@Suite(.serialized)
final class ConfigurationStoreTests {
    private let temporaryDirectory: URL

    init() throws {
        temporaryDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("VideoToNDITests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: temporaryDirectory, withIntermediateDirectories: true)
    }

    deinit {
        try? FileManager.default.removeItem(at: temporaryDirectory)
    }

    @Test func missingConfigurationLoadsDefaults() {
        let store = ConfigurationStore(directoryURL: temporaryDirectory)
        #expect(store.load() == AppConfiguration())
    }

    @Test func saveAndLoadRoundTrip() throws {
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

        #expect(store.load() == expected)
        #expect(FileManager.default.fileExists(atPath: store.configurationURL.path))
    }

    @Test func secondSaveKeepsPreviousConfigurationAsBackup() throws {
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
        #expect(backup == original)
        #expect(store.load() == replacement)
    }

    @Test func corruptPrimaryFallsBackToBackup() throws {
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

        #expect(store.load() == original)
    }
}
