import Foundation

public enum PopoverLayout {
    public static func height(availableHeight: Double) -> Double {
        min(720, max(160, availableHeight - 24))
    }
}
