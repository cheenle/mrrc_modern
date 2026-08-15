package com.hamradio.ft710android.UI

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.Network.AuthResult
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.launch

@Composable
fun LoginScreen(vm: MainViewModel, settings: SettingsStore, onLoggedIn: () -> Unit) {
    val scope = rememberCoroutineScope()
    var host by remember { mutableStateOf(SettingsStore.DEFAULT_HOST) }
    var port by remember { mutableStateOf(SettingsStore.DEFAULT_PORT) }
    var password by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var initialized by remember { mutableStateOf(false) }

    val savedHost by settings.host.collectAsState(initial = SettingsStore.DEFAULT_HOST)
    val savedPort by settings.port.collectAsState(initial = SettingsStore.DEFAULT_PORT)

    LaunchedEffect(savedHost, savedPort) {
        host = savedHost; port = savedPort
        val saved = settings.savedPassword()
        if (!saved.isNullOrEmpty()) password = saved
        initialized = true
    }

    Column(Modifier.fillMaxSize().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Spacer(Modifier.height(80.dp))
        Text("FT-710 Control", style = MaterialTheme.typography.headlineMedium)
        Text("服务器证书未验证（自签）", style = MaterialTheme.typography.bodySmall)
        Spacer(Modifier.height(24.dp))
        OutlinedTextField(host, { host = it }, label = { Text("服务器") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(port, { port = it }, label = { Text("端口") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(password, { password = it }, label = { Text("密码") }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth())
        Spacer(Modifier.height(24.dp))
        Button(onClick = {
            if (busy || !initialized) return@Button
            busy = true; error = null
            scope.launch {
                val res = vm.connect(host.trim(), port.trim(), password)
                when (res) {
                    is AuthResult.Success -> { settings.save(host.trim(), port.trim(), password); onLoggedIn() }
                    is AuthResult.Failure -> {
                        error = when (res.status) {
                            401 -> "密码错误"
                            429 -> "尝试过于频繁，请稍后再试"
                            else -> "连接失败：${res.message.take(80)}"
                        }
                        busy = false
                    }
                }
            }
        }, enabled = !busy, modifier = Modifier.fillMaxWidth()) {
            Text(if (busy) "连接中…" else "连接")
        }
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
    }
}
