import AppKit

// Matches Update Studio KH and Studio Launcher KH's four appearance choices.
enum AppearanceMode: String, CaseIterable {
    case system, light, dark
    case appDefault = "default"

    var title: String {
        switch self {
        case .system: return "System"
        case .light: return "Light"
        case .dark: return "Dark"
        case .appDefault: return "Default"
        }
    }

    var detail: String {
        switch self {
        case .system: return "Follow macOS."
        case .light: return "Always light."
        case .dark: return "Always dark."
        case .appDefault: return "The app's own palette."
        }
    }

    func apply() {
        switch self {
        case .system: NSApp.appearance = nil
        case .light: NSApp.appearance = NSAppearance(named: .aqua)
        case .dark: NSApp.appearance = NSAppearance(named: .darkAqua)
        case .appDefault: NSApp.appearance = nil
        }
    }

    static var current: AppearanceMode {
        guard let value = ConfigStore.shared.config.appearance else { return .system }
        return AppearanceMode(rawValue: value) ?? .system
    }

    static func set(_ mode: AppearanceMode) {
        ConfigStore.shared.update { $0.appearance = mode.rawValue }
        mode.apply()
    }
}
