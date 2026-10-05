package com.hamradio.ft710android.UI

import android.content.Intent
import android.media.MediaPlayer
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.FileProvider
import com.hamradio.ft710android.Network.RecordingRow
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.io.File

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun RecordingPanel(vm: MainViewModel, onClose: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    vm.version.collectAsState()
    val rec by vm.recordingState.collectAsState()
    val rows by vm.recordings.collectAsState()
    val listenOnly by vm.listenOnly.collectAsState()
    val count by vm.recordingsCount.collectAsState()
    val totalBytes by vm.recordingsBytes.collectAsState()

    var playing by remember { mutableStateOf<String?>(null) }
    var playbackPos by remember { mutableStateOf(0) }
    var playbackDur by remember { mutableStateOf(0) }
    var pendingDelete by remember { mutableStateOf<RecordingRow?>(null) }
    var busy by remember { mutableStateOf(false) }
    val player = remember { MediaPlayer() }

    LaunchedEffect(Unit) { vm.refreshRecordings() }
    DisposableEffect(Unit) { onDispose { player.release() } }
    LaunchedEffect(playing) {
        while (playing != null) {
            playbackPos = runCatching { player.currentPosition }.getOrDefault(0)
            playbackDur = runCatching { player.duration }.getOrDefault(0)
            delay(500)
        }
    }

    fun stopPlayback() {
        runCatching { player.stop() }
        playing = null
    }

    fun play(row: RecordingRow) {
        scope.launch {
            busy = true
            val file = vm.downloadRecording(row.name, File(context.cacheDir, "recordings"))
            busy = false
            if (file == null) { vm.showError("下载失败：${row.name}"); return@launch }
            runCatching {
                player.reset()
                player.setDataSource(file.absolutePath)
                player.prepare()
                player.start()
                playing = row.name
                playbackPos = 0
            }.onFailure { vm.showError("播放失败：${it.message}") }
        }
    }

    fun export(row: RecordingRow) {
        scope.launch {
            busy = true
            val file = vm.downloadRecording(row.name, File(context.cacheDir, "recordings"))
            busy = false
            if (file == null) { vm.showError("下载失败：${row.name}"); return@launch }
            val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
            val intent = Intent(Intent.ACTION_SEND).apply {
                type = "audio/mpeg"
                putExtra(Intent.EXTRA_STREAM, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            context.startActivity(Intent.createChooser(intent, "导出录音"))
        }
    }

    Dialog(onDismissRequest = onClose, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
            Column(Modifier.fillMaxSize().padding(16.dp)) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("录音", style = MaterialTheme.typography.titleLarge)
                    Spacer(Modifier.weight(1f))
                    TextButton(onClick = { vm.refreshRecordings() }) { Text("刷新") }
                    TextButton(onClick = onClose) { Text("关闭") }
                }
                HorizontalDivider()
                Text("$count 条 · 共 ${fmtBytes(totalBytes)}", fontSize = 11.sp,
                    color = Color(0xFF6B7280), modifier = Modifier.padding(vertical = 4.dp))
                Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    if (rec.recording) {
                        Text("● 录制中 ${fmtSeconds(rec.duration)} · ${fmtBytes(rec.bytes)}", color = Color(0xFFE53935), fontSize = 13.sp)
                    } else {
                        Text("未在录音", fontSize = 13.sp)
                    }
                    Spacer(Modifier.weight(1f))
                    if (!listenOnly) {
                        Button(onClick = { if (rec.recording) vm.stopRecording() else vm.startRecording() }) {
                            Text(if (rec.recording) "停止" else "开始录音")
                        }
                    }
                }
                playing?.let { name ->
                    var seekLocal by remember { mutableStateOf<Float?>(null) }
                    Column(Modifier.fillMaxWidth()) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("▶ ${fmtSeconds(playbackPos / 1000.0)} / ${fmtSeconds(playbackDur / 1000.0)}", fontSize = 12.sp)
                            Spacer(Modifier.width(8.dp))
                            Text(name, fontSize = 11.sp, modifier = Modifier.weight(1f))
                            TextButton(onClick = { if (player.isPlaying) player.pause() else player.start() }) {
                                Text(if (player.isPlaying) "暂停" else "继续")
                            }
                            TextButton(onClick = { stopPlayback() }) { Text("停止") }
                        }
                        val dur = playbackDur.toFloat().coerceAtLeast(1f)
                        Slider(
                            value = (seekLocal ?: playbackPos.toFloat()).coerceIn(0f, dur),
                            onValueChange = { seekLocal = it },
                            onValueChangeFinished = {
                                seekLocal?.let { player.seekTo(it.toInt()) }
                                seekLocal = null
                            },
                            valueRange = 0f..dur,
                        )
                    }
                }
                if (busy) Text("处理中…", fontSize = 12.sp, color = Color(0xFFE67E22))
                LazyColumn(Modifier.weight(1f)) {
                    items(rows, key = { it.name }) { row ->
                        Column(
                            Modifier.fillMaxWidth()
                                .combinedClickable(
                                    onClick = { if (playing == row.name) stopPlayback() else play(row) },
                                    onLongClick = { if (!row.recording) pendingDelete = row },
                                )
                                .padding(vertical = 8.dp)
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text("${row.freqHz / 1000} kHz", fontSize = 14.sp, modifier = Modifier.weight(1f))
                                Text(fmtStartedAt(row.startedAt), fontSize = 12.sp)
                                Spacer(Modifier.width(8.dp))
                                Text(fmtSeconds(row.duration), fontSize = 12.sp)
                                Spacer(Modifier.width(8.dp))
                                Text(fmtBytes(row.bytes), fontSize = 12.sp)
                                if (row.recording) {
                                    Text("  ●", color = Color(0xFFE53935), fontSize = 12.sp)
                                } else {
                                    TextButton(onClick = { export(row) }) { Text("导出", fontSize = 12.sp) }
                                }
                            }
                            Text("长按删除", fontSize = 10.sp, color = Color(0xFF6B7280))
                        }
                        HorizontalDivider()
                    }
                }
            }
        }
    }

    pendingDelete?.let { row ->
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("删除录音") },
            text = { Text("确定删除 ${row.name} ？服务端文件将被永久删除。") },
            confirmButton = {
                TextButton(onClick = { vm.deleteRecording(row.name); pendingDelete = null }) { Text("删除") }
            },
            dismissButton = { TextButton(onClick = { pendingDelete = null }) { Text("取消") } },
        )
    }
}
