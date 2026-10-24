package com.hamradio.ft710android.Network

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **跨语言契约测试**：服务端代码生成的真实 fullState payload，必须被 App 自己的解析器吃下去。
 *
 * 为什么需要它：`ProtocolTest` 用的是**手写 fixture**。手写 fixture 只能证明解析器
 * 认得出我们自己想象的样子；服务端一旦多一个字段、换一个类型（或某个 backend 的
 * capabilities 长歪），`runCatching` 会把它吞成 `WsEvent.Unknown`，整条 fullState 被丢，
 * 用户看到的就是「连上了，但频率全是 00.000.00」——没有任何报错。
 *
 * 载荷由 `backends/<model>/backend.py` 与 `radio_state.py` 真实构造（见 tests/README 的生成命令）：
 *   python -c "…create_backend(model).capabilities.to_dict()…"
 */
class FullStateContractTest {
    private fun payload(name: String): String =
        checkNotNull(javaClass.classLoader?.getResourceAsStream(name)) { "缺少 $name" }
            .reader().readText()

    @Test
    fun `ft-710 fullState from the server decodes into the typed event`() {
        val ev = parseWsEvent(payload("fullstate-ft710.json"))
        assertTrue("服务端 fullState 被解析成了 ${ev::class.simpleName}（应为 FullState）", ev is WsEvent.FullState)
        val fs = ev as WsEvent.FullState
        assertEquals("ft710", fs.capabilities?.modelName)
        assertTrue("bands 必须解析出来", fs.bands.isNotEmpty())
        assertTrue("modes 必须解析出来", fs.modes.isNotEmpty())
    }

    @Test
    fun `ic-7300 fullState from the server decodes into the typed event`() {
        val ev = parseWsEvent(payload("fullstate-ic7300.json"))
        assertTrue(ev is WsEvent.FullState)
        val fs = ev as WsEvent.FullState
        assertEquals("ic7300", fs.capabilities?.modelName)
        // Icom 的 filterTables 带 model/filDefaults 两个额外键：多余键必须被忽略
        assertEquals("fil123", fs.filterTables?.model)
    }
}
