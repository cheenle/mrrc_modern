package com.hamradio.ft710android.Network

import java.util.concurrent.ConcurrentHashMap

/**
 * 通道在线标志集合。
 *
 * 4+1 路 WebSocket 的 OkHttp 回调线程会并发 add/remove；早期实现用普通
 * `mutableSetOf`，丢一个 add 就再也不会出现（聚合永不成立）——真机表现为
 * "控制通、频谱通，但 RX 无声且 PTT 不键控"（2026-10-05）。这里用并发集合
 * 固定这个不变量。
 */
class ChannelFlags {
    private val flags = ConcurrentHashMap.newKeySet<String>()

    fun add(path: String) { flags.add(path) }
    fun remove(path: String) { flags.remove(path) }
    fun clear() { flags.clear() }
    fun has(path: String): Boolean = flags.contains(path)
    fun hasAll(required: Set<String>): Boolean = flags.containsAll(required)
    fun snapshot(): Set<String> = flags.toSet()
}
