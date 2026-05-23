// AutomatonClient: OkHttp + kotlinx.serialization wrapper.
//
// Surface mirrors automaton/client.py and the Swift AutomatonClient so
// the Android app talks to the same HTTP API the CLI and iOS app do.
// All methods are suspend functions that run on the IO dispatcher.

package com.automaton.client

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.security.MessageDigest
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

sealed class AutomatonError(msg: String) : Exception(msg) {
    class InvalidUrl(url: String) : AutomatonError("Invalid URL: $url")
    class Unauthorized : AutomatonError("Unauthorized — check the bearer token.")
    class NotFound(path: String) : AutomatonError("Not found: $path")
    class Server(status: Int, body: String) : AutomatonError("Server $status: $body")
    class Transport(cause: Throwable) : AutomatonError("Transport: ${cause.message}")
    class Decoding(cause: Throwable) : AutomatonError("Decoding: ${cause.message}")
}

private val JSON = Json {
    ignoreUnknownKeys = true
    coerceInputValues = true
}

class AutomatonClient(
    val baseUrl: String,
    private val token: String?,
    /** SHA-256 fingerprint of the leaf cert (hex). Set when using the
     *  self-signed cert from `automaton tls init` without installing
     *  it as a trusted CA on the device. */
    private val pinnedCertSHA256: String? = null,
) {
    private val http: OkHttpClient by lazy { buildHttp() }

    // ------------------------------------------------------------------ //
    // public methods                                                       //
    // ------------------------------------------------------------------ //

    suspend fun health(): Boolean = withContext(Dispatchers.IO) {
        val body = get<Map<String, Boolean>>("/healthz")
        body["ok"] ?: false
    }

    suspend fun runs(): List<RunSummary> = withContext(Dispatchers.IO) {
        get("/api/runs")
    }

    suspend fun runDetail(runId: Int): RunDetail = withContext(Dispatchers.IO) {
        get("/api/run/$runId")
    }

    /** Synthesize a workflow list from recent runs — server has no GET
     *  /api/workflows endpoint today (write-only). Mirrors the Swift client. */
    suspend fun workflows(): List<String> = withContext(Dispatchers.IO) {
        runs().map { it.workflow }.distinct().sorted()
    }

    suspend fun trigger(workflow: String,
                        payload: Map<String, Any>? = null): Int =
        withContext(Dispatchers.IO) {
            val body = buildMap<String, Any> {
                if (payload != null) put("payload", payload)
            }
            val res: TriggerResult = postJson("/api/trigger/$workflow", body)
            res.runId
        }

    suspend fun signal(runId: Int, name: String,
                       payload: Map<String, Any>? = null): Int =
        withContext(Dispatchers.IO) {
            val body = buildMap<String, Any> {
                if (payload != null) put("payload", payload)
            }
            val res: SignalResult = postJson("/api/signals/$runId/$name", body)
            res.signalId
        }

    suspend fun cancel(runId: Int, reason: String? = null): Boolean =
        withContext(Dispatchers.IO) {
            val body = buildMap<String, Any> {
                if (reason != null) put("reason", reason)
            }
            val res: CancelResult = postJson("/api/run/$runId/cancel", body)
            res.cancelled
        }

    // ------------------------------------------------------------------ //
    // internals                                                            //
    // ------------------------------------------------------------------ //

    private inline fun <reified T> get(path: String): T {
        val req = Request.Builder()
            .url(baseUrl.trimEnd('/') + path)
            .apply { token?.let { addHeader("Authorization", "Bearer $it") } }
            .build()
        return send(req)
    }

    private inline fun <reified T> postJson(path: String,
                                             body: Map<String, Any>): T {
        val jsonBody = JSON.encodeToString(
            kotlinx.serialization.serializer(), body
        ).toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url(baseUrl.trimEnd('/') + path)
            .post(jsonBody)
            .apply { token?.let { addHeader("Authorization", "Bearer $it") } }
            .build()
        return send(req)
    }

    private inline fun <reified T> send(req: Request): T {
        val resp: Response = try {
            http.newCall(req).execute()
        } catch (e: Exception) {
            throw AutomatonError.Transport(e)
        }
        resp.use { r ->
            val text = r.body?.string() ?: ""
            when (r.code) {
                in 200..299 -> Unit
                401 -> throw AutomatonError.Unauthorized()
                404 -> throw AutomatonError.NotFound(req.url.encodedPath)
                else -> throw AutomatonError.Server(r.code, text.take(240))
            }
            return try {
                JSON.decodeFromString(text)
            } catch (e: Exception) {
                throw AutomatonError.Decoding(e)
            }
        }
    }

    private fun buildHttp(): OkHttpClient {
        val builder = OkHttpClient.Builder()
        if (pinnedCertSHA256 != null) {
            val fp = pinnedCertSHA256.lowercase().replace(":", "")
            val tm = object : X509TrustManager {
                override fun checkClientTrusted(chain: Array<X509Certificate>, t: String) = Unit
                override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
                override fun checkServerTrusted(chain: Array<X509Certificate>, t: String) {
                    val observed = chain.firstOrNull()?.let {
                        MessageDigest.getInstance("SHA-256")
                            .digest(it.encoded)
                            .joinToString("") { b -> "%02x".format(b) }
                    } ?: throw Exception("empty cert chain")
                    if (observed != fp) throw Exception(
                        "cert fingerprint mismatch: expected $fp, got $observed"
                    )
                }
            }
            val ctx = SSLContext.getInstance("TLS").apply {
                init(null, arrayOf(tm), null)
            }
            builder
                .sslSocketFactory(ctx.socketFactory, tm)
                .hostnameVerifier { _, _ -> true }   // pinning by fingerprint; hostname check redundant
        }
        return builder.build()
    }
}
