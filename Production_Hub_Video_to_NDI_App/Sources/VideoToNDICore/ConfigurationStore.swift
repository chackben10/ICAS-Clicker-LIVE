import Foundation

public final class ConfigurationStore {
    public let directoryURL: URL
    public let configurationURL: URL
    public let backupURL: URL

    public init(directoryURL: URL? = nil) {
        let base = directoryURL ?? FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first!.appendingPathComponent("Production Hub - Video to NDI", isDirectory: true)
        self.directoryURL = base
        self.configurationURL = base.appendingPathComponent("routes.json")
        self.backupURL = base.appendingPathComponent("routes.backup.json")
    }

    public func load() -> AppConfiguration {
        if let configuration = decode(at: configurationURL) { return configuration }
        if let backup = decode(at: backupURL) { return backup }
        return AppConfiguration()
    }

    public func save(_ configuration: AppConfiguration) throws {
        let fileManager = FileManager.default
        try fileManager.createDirectory(at: directoryURL, withIntermediateDirectories: true)

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        let data = try encoder.encode(configuration)

        if fileManager.fileExists(atPath: configurationURL.path) {
            if fileManager.fileExists(atPath: backupURL.path) {
                try fileManager.removeItem(at: backupURL)
            }
            try fileManager.copyItem(at: configurationURL, to: backupURL)
        }
        try data.write(to: configurationURL, options: .atomic)
    }

    private func decode(at url: URL) -> AppConfiguration? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(AppConfiguration.self, from: data)
    }
}
