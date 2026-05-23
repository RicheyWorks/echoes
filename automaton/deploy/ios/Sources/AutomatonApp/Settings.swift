// Persistent settings store. Server URL is plain UserDefaults (not
// secret); bearer token lives in the Keychain so it survives app
// reinstalls and isn't readable by other apps in the sandbox.

import Foundation
import Security

@MainActor
public final class Settings: ObservableObject {
    @Published public var serverURL: String {
        didSet { UserDefaults.standard.set(serverURL, forKey: Self.urlKey) }
    }
    @Published public var pinnedFingerprint: String {
        didSet { UserDefaults.standard.set(pinnedFingerprint, forKey: Self.fingerprintKey) }
    }
    @Published public private(set) var hasToken: Bool

    private static let urlKey = "automaton.serverURL"
    private static let fingerprintKey = "automaton.pinnedFingerprint"
    private static let keychainAccount = "automaton.token"

    public init() {
        self.serverURL = UserDefaults.standard.string(forKey: Self.urlKey) ?? ""
        self.pinnedFingerprint = UserDefaults.standard.string(forKey: Self.fingerprintKey) ?? ""
        self.hasToken = Keychain.read(account: Self.keychainAccount) != nil
    }

    public func saveToken(_ token: String) {
        if token.isEmpty {
            Keychain.delete(account: Self.keychainAccount)
            hasToken = false
        } else {
            Keychain.save(account: Self.keychainAccount, value: token)
            hasToken = true
        }
    }

    public func loadToken() -> String? {
        Keychain.read(account: Self.keychainAccount)
    }
}

// Minimal Keychain helper. Items are scoped to this app's bundle via
// the system default (kSecAttrService).
enum Keychain {
    static let service = "io.automaton.ios"

    static func save(account: String, value: String) {
        let data = Data(value.utf8)
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let updates: [String: Any] = [kSecValueData as String: data]
        let status = SecItemUpdate(q as CFDictionary, updates as CFDictionary)
        if status == errSecItemNotFound {
            var add = q
            add[kSecValueData as String] = data
            SecItemAdd(add as CFDictionary, nil)
        }
    }

    static func read(account: String) -> String? {
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecReturnData as String: true,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let s = String(data: data, encoding: .utf8) else {
            return nil
        }
        return s
    }

    static func delete(account: String) {
        let q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(q as CFDictionary)
    }
}
