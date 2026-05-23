// Persistent settings. Server URL lives in plain SharedPreferences (not
// secret); the bearer token lives in EncryptedSharedPreferences so it
// isn't readable by other apps in the sandbox and survives reinstalls.

package com.automaton

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.automaton.client.AutomatonClient

class Settings(context: Context) {

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val encrypted = EncryptedSharedPreferences.create(
        context,
        ENC_PREFS_NAME,
        MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    var serverUrl: String
        get() = prefs.getString(KEY_SERVER_URL, "") ?: ""
        set(v) = prefs.edit().putString(KEY_SERVER_URL, v).apply()

    var pinnedFingerprint: String
        get() = prefs.getString(KEY_FINGERPRINT, "") ?: ""
        set(v) = prefs.edit().putString(KEY_FINGERPRINT, v).apply()

    /** Token stored in EncryptedSharedPreferences; never in plain prefs. */
    var token: String
        get() = encrypted.getString(KEY_TOKEN, "") ?: ""
        set(v) = encrypted.edit().putString(KEY_TOKEN, v).apply()

    val hasToken: Boolean get() = token.isNotBlank()

    fun makeClient(): AutomatonClient? {
        val url = serverUrl.trim()
        if (url.isEmpty()) return null
        return AutomatonClient(
            baseUrl = url,
            token = token.ifBlank { null },
            pinnedCertSHA256 = pinnedFingerprint.ifBlank { null },
        )
    }

    companion object {
        private const val PREFS_NAME    = "automaton_prefs"
        private const val ENC_PREFS_NAME = "automaton_encrypted_prefs"
        private const val KEY_SERVER_URL = "server_url"
        private const val KEY_FINGERPRINT = "pinned_fingerprint"
        private const val KEY_TOKEN     = "token"
    }
}
