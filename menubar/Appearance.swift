import AppKit

// Three choices, matching the owner's call: System is where a new install starts.
enum AppearanceMode: String, CaseIterable {
    case system, light, dark

    var title: String {
        switch self {
        case .system: return "System"
        case .light: return "Light"
        case .dark: return "Dark"
        }
    }

    var detail: String {
        switch self {
        case .system: return "Follow macOS."
        case .light: return "Always light."
        case .dark: return "Always dark."
        }
    }

    func apply() {
        switch self {
        case .system: NSApp.appearance = nil
        case .light: NSApp.appearance = NSAppearance(named: .aqua)
        case .dark: NSApp.appearance = NSAppearance(named: .darkAqua)
        }
    }

    static var current: AppearanceMode {
        // A stored default value from 0.2.245's first cut no longer parses, so it reads as System.
        guard let value = ConfigStore.shared.config.appearance else { return .system }
        return AppearanceMode(rawValue: value) ?? .system
    }

    static func set(_ mode: AppearanceMode) {
        ConfigStore.shared.update { $0.appearance = mode.rawValue }
        mode.apply()
    }
}
