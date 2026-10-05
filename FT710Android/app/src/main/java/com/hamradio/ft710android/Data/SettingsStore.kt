package com.hamradio.ft710android.Data

import android.content.Context
import androidx.datastore.preferences.core.MutablePreferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.withContext

private val Context.dataStore by preferencesDataStore(name = "settings")

/**
 * host/port 与全部本地偏好存 DataStore；password/token 用 Keystore 加密（AndroidKeyStore AES-GCM），密文存 SharedPreferences。
 * 偏好语义逐条对齐手机端 Web 的 cookie 键（ft710_afVol / ft710_micVol / scopeTheme / scopeFloor / scopeCeil / …）。
 */
class SettingsStore(private val context: Context) {
    private object Keys {
        val host = stringPreferencesKey("host")
        val port = stringPreferencesKey("port")
        val afVol = intPreferencesKey("afVol")
        val micVol = intPreferencesKey("micVol")
        val micGain = intPreferencesKey("micGain")
        val scopeTheme = stringPreferencesKey("scopeTheme")
        val scopeFloor = intPreferencesKey("scopeFloor")
        val scopeCeil = intPreferencesKey("scopeCeil")
        val fftHeight = intPreferencesKey("fftHeight")
        val wfHeight = intPreferencesKey("wfHeight")
        val keepScreenOn = booleanPreferencesKey("keepScreenOn")
        val backgroundRx = booleanPreferencesKey("backgroundRx")
    }
    private val ks = KeystoreCipher(context)

    val host: Flow<String> = context.dataStore.data.map { it[Keys.host] ?: DEFAULT_HOST }
    val port: Flow<String> = context.dataStore.data.map { it[Keys.port] ?: DEFAULT_PORT }
    val afVol: Flow<Int> = context.dataStore.data.map { it[Keys.afVol] ?: 128 }
    val micVol: Flow<Int> = context.dataStore.data.map { it[Keys.micVol] ?: 100 }
    val micGain: Flow<Int?> = context.dataStore.data.map { it[Keys.micGain] }
    val scopeTheme: Flow<String> = context.dataStore.data.map { it[Keys.scopeTheme] ?: "jet" }
    val scopeFloor: Flow<Int> = context.dataStore.data.map { it[Keys.scopeFloor] ?: 5 }
    val scopeCeil: Flow<Int> = context.dataStore.data.map { it[Keys.scopeCeil] ?: 220 }
    val fftHeight: Flow<Int> = context.dataStore.data.map { it[Keys.fftHeight] ?: 40 }
    val wfHeight: Flow<Int> = context.dataStore.data.map { it[Keys.wfHeight] ?: 110 }
    val keepScreenOn: Flow<Boolean> = context.dataStore.data.map { it[Keys.keepScreenOn] ?: true }
    val backgroundRx: Flow<Boolean> = context.dataStore.data.map { it[Keys.backgroundRx] ?: true }

    suspend fun save(host: String, port: String, password: String) = withContext(Dispatchers.IO) {
        context.dataStore.edit { it[Keys.host] = host; it[Keys.port] = port }
        ks.putSecret("password", password)
    }

    suspend fun savedPassword(): String? = withContext(Dispatchers.IO) { ks.getSecret("password") }

    /** 退出登录时清除加密凭据（不删 host/port 与本地偏好）。 */
    suspend fun clearCredentials() = withContext(Dispatchers.IO) { ks.deleteSecret("password") }

    suspend fun putAfVol(v: Int) = edit { it[Keys.afVol] = v.coerceIn(0, 255) }
    suspend fun putMicVol(v: Int) = edit { it[Keys.micVol] = v.coerceIn(0, 200) }
    suspend fun putMicGain(v: Int) = edit { it[Keys.micGain] = v.coerceIn(0, 100) }
    suspend fun putScopeTheme(v: String) = edit { it[Keys.scopeTheme] = v }
    suspend fun putScopeFloor(v: Int) = edit { it[Keys.scopeFloor] = v.coerceIn(0, 200) }
    suspend fun putScopeCeil(v: Int) = edit { it[Keys.scopeCeil] = v.coerceIn(50, 255) }
    suspend fun putFftHeight(v: Int) = edit { it[Keys.fftHeight] = v.coerceIn(20, 120) }
    suspend fun putWfHeight(v: Int) = edit { it[Keys.wfHeight] = v.coerceIn(30, 200) }
    suspend fun putKeepScreenOn(v: Boolean) = edit { it[Keys.keepScreenOn] = v }
    suspend fun putBackgroundRx(v: Boolean) = edit { it[Keys.backgroundRx] = v }

    private suspend fun edit(block: (MutablePreferences) -> Unit) {
        withContext(Dispatchers.IO) { context.dataStore.edit(block) }
    }

    companion object {
        const val DEFAULT_HOST = "radio.vlsc.net"
        const val DEFAULT_PORT = "8888"
    }
}
