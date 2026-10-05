package com.hamradio.ft710android.UI

import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.delay

/**
 * S6 Cloud Hub 向导 —— 与 static/modules/cloud_hub.js 同流程：
 * 未申请→表单；已申请未接入→轮询/粘贴登记口令；已接入→入口/证书/隧道 + 必要时重启。
 */
@Composable
fun CloudHubDialog(vm: MainViewModel, onClose: () -> Unit) {
    val state by vm.cloud.collectAsState()
    val msg by vm.cloudMsg.collectAsState()
    var callsign by remember { mutableStateOf("") }
    var contact by remember { mutableStateOf("") }
    var secret by remember { mutableStateOf("") }
    var pendingSecret by remember { mutableStateOf("") }

    LaunchedEffect(Unit) {
        vm.cloudRefresh()
        // 打开期间每 20 秒问一次（服务端自己也在轮询，这里只是刷新显示）
        while (true) {
            delay(20_000)
            vm.cloudRefresh()
        }
    }

    val s = state
    val applied = s != null && s.callsign.isNotEmpty() && s.hasToken
    AlertDialog(
        onDismissRequest = { vm.clearCloudMsg(); onClose() },
        title = { Text("接入云端（Cloud Hub）") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                if (s == null) Text("读取状态…", fontSize = 13.sp)

                if (s != null && !applied) {
                    Text("呼号", fontSize = 12.sp, color = MrrcColors.TextSecondary)
                    OutlinedTextField(value = callsign, onValueChange = { callsign = it }, singleLine = true)
                    Spacer(Modifier.height(6.dp))
                    Text("联系方式（可选）", fontSize = 12.sp, color = MrrcColors.TextSecondary)
                    OutlinedTextField(value = contact, onValueChange = { contact = it }, singleLine = true)
                    Spacer(Modifier.height(6.dp))
                    Text("登记口令（运维已批准时填）", fontSize = 12.sp, color = MrrcColors.TextSecondary)
                    OutlinedTextField(value = secret, onValueChange = { secret = it }, singleLine = true)
                    Spacer(Modifier.height(8.dp))
                    Button(onClick = { vm.cloudApply(callsign, contact, secret) }) { Text("申请 / 接入") }
                }

                if (s != null && applied && !s.connected) {
                    Text("呼号：${s.callsign}", fontSize = 13.sp, fontWeight = FontWeight.Bold)
                    Text("已提交，等待运维批准；本机会自动重试。", fontSize = 12.sp, color = MrrcColors.TextSecondary)
                    s.autoconnect?.let { auto ->
                        val when_ = if (auto.at > 0) "上次询问状态：${auto.status}" else "还没问过"
                        Text("自动接入：$when_${auto.error?.let { " — $it" } ?: ""}",
                            fontSize = 11.sp, color = MrrcColors.TextMuted)
                    }
                    Spacer(Modifier.height(8.dp))
                    Text("运维给的登记口令", fontSize = 12.sp, color = MrrcColors.TextSecondary)
                    OutlinedTextField(value = pendingSecret, onValueChange = { pendingSecret = it }, singleLine = true)
                    Row {
                        Button(onClick = { vm.cloudApply(s.callsign, "", pendingSecret) }) { Text("用口令接入") }
                        Spacer(Modifier.height(4.dp))
                        TextButton(onClick = { vm.cloudRefresh() }) { Text("刷新") }
                    }
                }

                if (s != null && s.connected) {
                    Text("已接入 ✓", color = MrrcColors.Success, fontSize = 14.sp, fontWeight = FontWeight.Bold)
                    Text("入口：${s.entry}", fontSize = 12.sp)
                    Text("证书：${s.cert.ifEmpty { "—" }}", fontSize = 12.sp, color = MrrcColors.TextSecondary)
                    Text(
                        "隧道：" + if (s.tunnelRunning) "已连接" else (s.tunnelError.ifEmpty { "未运行（稍候会自动重试）" }),
                        fontSize = 12.sp,
                        color = if (s.tunnelRunning) MrrcColors.Success else MrrcColors.Warning,
                    )
                    if (s.certReloadRequired) {
                        Spacer(Modifier.height(8.dp))
                        Button(onClick = { vm.cloudRestart() }) { Text("重启以启用新证书") }
                        Text("不重启时入口会 502。", fontSize = 11.sp, color = MrrcColors.Warning)
                    }
                    TextButton(onClick = { vm.cloudRefresh() }) { Text("刷新") }
                }

                msg?.let {
                    Spacer(Modifier.height(8.dp))
                    BoxedMessage(it)
                }
            }
        },
        confirmButton = { TextButton(onClick = { vm.clearCloudMsg(); onClose() }) { Text("关闭") } },
    )
}

@Composable
private fun BoxedMessage(text: String) {
    Row(
        Modifier.fillMaxWidth()
            .border(1.dp, MrrcColors.Border, RoundedCornerShape(6.dp))
            .padding(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(text, fontSize = 12.sp, color = MrrcColors.Accent)
    }
}
