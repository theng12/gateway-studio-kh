import Foundation

/// The hold token the updater sends with its drain request.
///
/// Own file first (0.2.306): the estate file is copied into this app's own Application Support
/// once, the copy is said out loud, and the legacy path is never read again. The guard here is the
/// same one `gateway/maintenance.py` applies to the request — a token readable by anyone else, or
/// shorter than 64 hex, is a failed check and not something to copy or use.
enum HoldToken {
    /// `~/…` from Info.plist, expanded with this user's home. Never an absolute `/Users/…` path.
    static func path() -> URL? {
        guard let raw = Bundle.main.object(forInfoDictionaryKey: "KHHoldTokenFile") as? String,
              !raw.isEmpty else { return nil }
        return URL(fileURLWithPath: (raw as NSString).expandingTildeInPath)
    }

    private static func legacy() -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/KHUpdater/state/hold-token")
    }

    private static func token(at url: URL) -> String? {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              attributes[.type] as? FileAttributeType == .typeRegular,
              (attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600,
              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              let text = try? String(contentsOf: url, encoding: .ascii) else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.count == 64 && trimmed.allSatisfy(\.isHexDigit) ? trimmed : nil
    }

    /// Copy the estate token into this app's own file, once. Returns the line to log, or nil.
    /// The same copy lives in `gateway/maintenance.py`, because the server can run without this app.
    /// Both are idempotent (the own file is created exclusively), so whichever runs first wins.
    static func migrateIfNeeded() -> String? {
        guard let own = path(),
              !FileManager.default.fileExists(atPath: own.path) else { return nil }
        let old = legacy()
        guard FileManager.default.fileExists(atPath: old.path) else { return nil }
        guard let token = token(at: old) else {
            return "hold token NOT migrated: \(old.path) is not a 0600 regular file of 64 hex;"
                + " updates stay blocked until \(own.path) exists"
        }
        do {
            try FileManager.default.createDirectory(at: own.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            try (token + "\n").write(to: own, atomically: false, encoding: .ascii)
            try FileManager.default.setAttributes([.posixPermissions: 0o600],
                                                  ofItemAtPath: own.path)
        } catch {
            return "hold token NOT migrated: cannot write \(own.path): \(error.localizedDescription)"
        }
        return "hold token migrated: copied \(old.path) to \(own.path);"
            + " the legacy path is no longer read"
    }

    /// The sentence to show a person when the updater cannot hold, or nil when the token is usable.
    static func problem() -> String? {
        guard let own = path() else {
            return "update blocked: this app has no hold token path in its Info.plist"
        }
        guard FileManager.default.fileExists(atPath: own.path) else {
            return "update blocked: this app's hold token is missing at \(own.path)"
        }
        guard token(at: own) != nil else {
            return "update blocked: this app's hold token at \(own.path) is not a 0600 file of 64 hex"
        }
        return nil
    }
}
