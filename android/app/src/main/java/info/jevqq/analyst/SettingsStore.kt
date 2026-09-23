package info.jevqq.analyst

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Non-secret settings in private preferences; the API key is encrypted by Android Keystore. */
internal class SettingsStore(context: Context) {
    private val prefs = context.getSharedPreferences("connection_settings", Context.MODE_PRIVATE)
    private val keyAlias = "${context.packageName}.jev_api_key"

    fun load(): ConnectionSettings {
        val mode = runCatching {
            EndpointMode.valueOf(prefs.getString("mode", EndpointMode.JEV.name)!!)
        }.getOrDefault(EndpointMode.JEV)
        val encrypted = prefs.getString("api_key_encrypted", null)
        val apiKey = if (encrypted == null) "" else try {
            val iv = prefs.getString("api_key_iv", null) ?: error("API Key IV 缺失")
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, decode(iv)))
            String(cipher.doFinal(decode(encrypted)), Charsets.UTF_8)
        } catch (error: Exception) {
            throw IllegalStateException("已保存的 API Key 无法读取，请重新配置", error)
        }
        return ConnectionSettings(
            mode,
            urlFor(mode),
            prefs.getString("model", "jev-latest") ?: "jev-latest",
            apiKey,
        )
    }

    fun urlFor(mode: EndpointMode): String = prefs.getString(
        if (mode == EndpointMode.JEV) "jev_url" else "gateway_url",
        if (mode == EndpointMode.JEV) JEV_URL else GATEWAY_URL,
    ) ?: if (mode == EndpointMode.JEV) JEV_URL else GATEWAY_URL

    fun save(settings: ConnectionSettings) {
        settings.endpoint()
        val editor = prefs.edit()
            .putString("mode", settings.mode.name)
            .putString(if (settings.mode == EndpointMode.JEV) "jev_url" else "gateway_url",
                settings.url.trim())
            .putString("model", settings.model.trim())
        if (settings.apiKey.isBlank()) {
            editor.remove("api_key_encrypted").remove("api_key_iv")
        } else {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.ENCRYPT_MODE, secretKey())
            editor.putString("api_key_iv", encode(cipher.iv))
                .putString("api_key_encrypted", encode(cipher.doFinal(settings.apiKey.toByteArray(Charsets.UTF_8))))
        }
        editor.apply()
    }

    @Synchronized private fun secretKey(): SecretKey {
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        val existing = keyStore.getEntry(keyAlias, null) as? KeyStore.SecretKeyEntry
        if (existing != null) return existing.secretKey
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(KeyGenParameterSpec.Builder(keyAlias,
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build())
        return generator.generateKey()
    }

    private fun encode(bytes: ByteArray): String = Base64.encodeToString(bytes, Base64.NO_WRAP)
    private fun decode(value: String): ByteArray = Base64.decode(value, Base64.NO_WRAP)

    companion object {
        const val JEV_URL = "https://api.typesafe.ai/v1/systemone"
        const val GATEWAY_URL = "http://127.0.0.1:8080/v1/systemone"
    }
}
