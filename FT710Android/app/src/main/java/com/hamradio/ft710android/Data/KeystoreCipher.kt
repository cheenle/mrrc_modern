package com.hamradio.ft710android.Data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * AndroidKeyStore AES-GCM 加密存储。密钥不进应用进程明文，卸载即失。
 * 密文 + IV 存 SharedPreferences（base64）。
 */
class KeystoreCipher(context: Context) {
    private val prefs = context.getSharedPreferences("secure_creds", Context.MODE_PRIVATE)
    private val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun getOrCreateKey(): SecretKey {
        keyStore.getKey(KEY_ALIAS, null)?.let { return it as SecretKey }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build())
        return gen.generateKey()
    }

    fun putSecret(name: String, value: String) {
        val key = getOrCreateKey()
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key)
        val enc = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        prefs.edit()
            .putString(name, Base64.encodeToString(enc, Base64.NO_WRAP))
            .putString("$name.iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .apply()
    }

    fun getSecret(name: String): String? {
        val encB64 = prefs.getString(name, null) ?: return null
        val ivB64 = prefs.getString("$name.iv", "") ?: return null
        val key = getOrCreateKey()
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, Base64.decode(ivB64, Base64.NO_WRAP)))
        return String(cipher.doFinal(Base64.decode(encB64, Base64.NO_WRAP)), Charsets.UTF_8)
    }

    fun deleteSecret(name: String) {
        prefs.edit().remove(name).remove("$name.iv").apply()
    }

    companion object { private const val KEY_ALIAS = "ft710_creds" }
}
