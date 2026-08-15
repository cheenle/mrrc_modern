package com.hamradio.ft710android.UI

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.launch

@Composable
fun SettingsScreen(vm: MainViewModel, settings: SettingsStore, onLoggedOut: () -> Unit) {
    val scope = rememberCoroutineScope()
    // 订阅 version 版本号触发重组，然后读 vm.state.* 拿到最新值
    vm.version.collectAsState()
    val state = vm.state

    Column(Modifier.fillMaxSize().padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Text("设置", style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(16.dp))
        Text("RF 功率: ${state.rfPower} W")
        Slider(value = state.rfPower.toFloat(), onValueChange = { vm.setRfPower(it.toInt()) }, valueRange = 5f..100f)
        Spacer(Modifier.height(8.dp))
        Text("Scope Span")
        Row {
            listOf("50k", "100k", "1M").forEachIndexed { i, label ->
                FilterChip(selected = state.scopeSpan == i, onClick = { vm.setScopeSpan(i) }, label = { Text(label) })
                Spacer(Modifier.width(6.dp))
            }
        }
        Spacer(Modifier.weight(1f))
        TextButton(onClick = { vm.connectionManager.reconnectAll() }) { Text("重连") }
        Button(onClick = {
            scope.launch { vm.logout(); settings.clearCredentials(); onLoggedOut() }
        }) { Text("退出登录") }
    }
}
