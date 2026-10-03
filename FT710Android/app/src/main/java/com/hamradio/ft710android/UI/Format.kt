package com.hamradio.ft710android.UI

import java.time.LocalDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

fun fmtSeconds(s: Double): String {
    val total = s.toInt().coerceAtLeast(0)
    return "%d:%02d".format(Locale.US, total / 60, total % 60)
}

fun fmtBytes(bytes: Long): String =
    if (bytes >= 1_048_576) "%.1f MB".format(Locale.US, bytes / 1_048_576.0)
    else "%.0f KB".format(Locale.US, bytes / 1024.0)

/** 服务端 started_at 是 `2026-10-03T12:00:00`（recorder.py isoformat(timespec="seconds")）。 */
fun fmtStartedAt(iso: String): String = runCatching {
    LocalDateTime.parse(iso).format(DateTimeFormatter.ofPattern("MM-dd HH:mm"))
}.getOrDefault(iso)
