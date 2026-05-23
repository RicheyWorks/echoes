// AutomatonClient: async/await wrapper over URLSession.
//
// Surface mirrors automaton/client.py so the iOS app talks to the
// same HTTP API the CLI does. Methods all return Codable Sendable
// types so they cross actor boundaries cleanly.

import Foundation

public enum AutomatonError: Error, Sendable {
    case invalidURL
    case unauthorized
    case notFound(String)
    case server(status: Int, body: String)
    case transport(underlying: String)
    case decoding(underlying: String)
}

public struct AutomatonClient: Sendable {
    public let baseURL: URL
    public let token: String?
    /// SHA-256 fingerprint of the leaf cert, hex-encoded. Set this when
    /// the server uses the self-signed cert from `automaton tls init`
    /// and you don't want to install it as a trusted CA on the device.
    public let pinnedCertSHA256: String?

    public init(baseURL: URL, token: String?,
                pinnedCertSHA256: String? = nil) {
        self.baseURL = baseURL
        self.token = token
        self.pinnedCertSHA256 = pinnedCertSHA256
    }

    // MARK: - public methods

    public func health() async throws -> Bool {
        let body: [String: Bool] = try await get("/healthz")
        return body["ok"] ?? false
    }

    public func runs() async throws -> [RunSummary] {
        return try await get("/api/runs")
    }

    public func runDetail(_ runId: Int) async throws -> RunDetail {
        return try await get("/api/run/\(runId)")
    }

    public func workflows() async throws -> [String] {
        // The server doesn't have a /api/workflows GET today (write-only).
        // Synthesize the list from /api/runs' workflow column.
        let recents = try await runs()
        return Array(Set(recents.map(\.workflow))).sorted()
    }

    public func trigger(_ workflow: String,
                         payload: [String: Any]? = nil) async throws -> Int {
        var body: [String: Any] = [:]
        if let payload = payload { body["payload"] = payload }
        let res: TriggerResult = try await postJSON(
            "/api/trigger/\(workflow)", body: body
        )
        return res.runId
    }

    public func signal(_ runId: Int, name: String,
                        payload: [String: Any]? = nil) async throws -> Int {
        var body: [String: Any] = [:]
        if let payload = payload { body["payload"] = payload }
        let res: SignalResult = try await postJSON(
            "/api/signals/\(runId)/\(name)", body: body
        )
        return res.signalId
    }

    public func cancel(_ runId: Int, reason: String? = nil) async throws -> Bool {
        var body: [String: Any] = [:]
        if let reason = reason { body["reason"] = reason }
        let res: CancelResult = try await postJSON(
            "/api/run/\(runId)/cancel", body: body
        )
        return res.cancelled
    }

    // MARK: - internals

    private func get<T: Decodable>(_ path: String) async throws -> T {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        if let token = token {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return try await send(req)
    }

    private func postJSON<T: Decodable>(_ path: String,
                                          body: [String: Any]) async throws -> T {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token = token {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if !body.isEmpty {
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return try await send(req)
    }

    private func send<T: Decodable>(_ req: URLRequest) async throws -> T {
        let (data, resp): (Data, URLResponse)
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw AutomatonError.transport(underlying: String(describing: error))
        }
        guard let http = resp as? HTTPURLResponse else {
            throw AutomatonError.transport(underlying: "non-HTTP response")
        }
        switch http.statusCode {
        case 200..<300: break
        case 401:       throw AutomatonError.unauthorized
        case 404:       throw AutomatonError.notFound(req.url?.path ?? "")
        default:
            let text = String(data: data, encoding: .utf8) ?? ""
            throw AutomatonError.server(status: http.statusCode, body: text)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw AutomatonError.decoding(underlying: String(describing: error))
        }
    }

    /// Lazy URLSession so the (optional) cert-pinning delegate is wired
    /// in exactly once per client instance.
    private var session: URLSession {
        if let fp = pinnedCertSHA256 {
            return URLSession(
                configuration: .default,
                delegate: PinnedCertDelegate(fingerprint: fp),
                delegateQueue: nil
            )
        }
        return .shared
    }
}

// MARK: - cert pinning

private final class PinnedCertDelegate: NSObject, URLSessionDelegate, Sendable {
    let fingerprint: String
    init(fingerprint: String) { self.fingerprint = fingerprint.lowercased() }

    func urlSession(_ session: URLSession,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition,
                                                   URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod
              == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let cert = SecTrustGetCertificateAtIndex(trust, 0) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let data = SecCertificateCopyData(cert) as Data
        let observed = data.sha256HexString()
        if observed == fingerprint {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }
}

import CryptoKit

private extension Data {
    func sha256HexString() -> String {
        let digest = SHA256.hash(data: self)
        return digest.map { String(format: "%02x", $0) }.joined()
    }
}
