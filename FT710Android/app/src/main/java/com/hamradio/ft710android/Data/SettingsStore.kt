package com.hamradio.ft710android.Data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.withContext

private val Context.dataStore by preferencesDataStore(name = "settings")

/** host/port 存 DataStore；password/token 用 Keystore 加密（AndroidKeyStore AES-GCM），密文存 SharedPreferences。 */
class SettingsStore(private val context: Context) {
    private object Keys {
        val host = stringPreferencesKey("host")
        val port = stringPreferencesKey("port")
    }
    private val ks = KeystoreCipher(context)

    val host: Flow<String> = context.dataStore.data.map { it[Keys.host] ?: DEFAULT_HOST }
    val port: Flow<String> = context.dataStore.data.map { it[Keys.port] ?: DEFAULT_PORT }

    suspend fun save(host: String, port: String, password: String) = withContext(Dispatchers.IO) {
        context.dataStore.edit { it[Keys.host] = host; it[Keys.port] = port }
        ks.putSecret("password", password)
    }

    suspend fun savedPassword(): String? = withContext(Dispatchers.IO) { ks.getSecret("password") }

    /** 退出登录时清除加密凭据（不删 host/port）。 */
    suspend fun clearCredentials() = withContext(Dispatchers.IO) { ks.deleteSecret("password") }

    companion object {
        const val DEFAULT_HOST = "radio.vlsc.net"
        const val DEFAULT_PORT = "8888"
    }
}
