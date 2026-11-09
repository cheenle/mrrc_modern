package com.hamradio.ft710android.PTT

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 硬件 PTT 路由的纯逻辑门槛。
 *
 * 这些用例盯的是"空中事故"级别的不变量：学习时不许发射、按下/松开必须成对、
 * 同一次物理按下被两条捕获路径各收到一次也只算一次。
 */
class HardwarePttRouterTest {

    private class Harness {
        var presses = 0
        var releases = 0
        var keyed = false
        val router = HardwarePttRouter(
            isKeyed = { keyed },
            press = { presses++; keyed = true },
            release = { releases++; keyed = false },
        )

        fun enable(keyCode: Int = 131, mode: HardwarePttMode = HardwarePttMode.Hold) {
            router.config = HardwarePttConfig(enabled = true, keyCode = keyCode, mode = mode)
        }
    }

    @Test fun `disabled key is not consumed and never keys`() {
        val h = Harness()
        assertFalse(h.router.onKeyDown(131, 0, 0))
        assertEquals(0, h.presses)
    }

    @Test fun `enabled but unlearned key does nothing`() {
        val h = Harness()
        h.router.config = HardwarePttConfig(enabled = true, keyCode = 0)
        assertFalse(h.router.onKeyDown(131, 0, 0))
        assertEquals(0, h.presses)
    }

    @Test fun `hold mode presses once and releases once`() {
        val h = Harness(); h.enable()
        assertTrue(h.router.onKeyDown(131, 0, 0))
        assertEquals(1, h.presses)
        assertTrue(h.router.onKeyUp(131, 0))
        assertEquals(1, h.releases)
        assertTrue(h.router.onKeyUp(131, 0)) // 多出来的 UP：仍属这颗键，吞掉但**不重复释放**
        assertEquals(1, h.releases)
    }

    @Test fun `auto repeat does not re-press`() {
        val h = Harness(); h.enable()
        h.router.onKeyDown(131, 0, 0)
        h.router.onKeyDown(131, 0, 1)
        h.router.onKeyDown(131, 0, 2)
        assertEquals(1, h.presses)
        assertTrue(h.router.onKeyUp(131, 0))
        assertEquals(1, h.releases)
    }

    @Test fun `duplicate down from second capture path is ignored`() {
        // Activity 与无障碍服务会各收到一次同一个物理按下
        val h = Harness(); h.enable()
        assertTrue(h.router.onKeyDown(131, 0, 0))
        assertTrue(h.router.onKeyDown(131, 0, 0))
        assertEquals(1, h.presses)
        h.router.onKeyUp(131, 0) // 第一条路径的 UP
        assertEquals(1, h.releases)
        h.router.onKeyUp(131, 0) // 第二条路径的 UP
        assertEquals(1, h.releases)
    }

    @Test fun `other keys are passed through`() {
        val h = Harness(); h.enable(keyCode = 131)
        assertFalse(h.router.onKeyDown(24, 0, 0)) // VOLUME_UP
        assertEquals(0, h.presses)
    }

    @Test fun `tap mode toggles instead of holding`() {
        val h = Harness(); h.enable(mode = HardwarePttMode.Tap)
        h.router.onKeyDown(131, 0, 0)
        assertEquals(1, h.presses)
        h.router.onKeyUp(131, 0)
        assertEquals(0, h.releases)          // 点按模式：松手不释放
        h.router.onKeyDown(131, 0, 0)
        assertEquals(1, h.releases)          // 第二下才释放
        h.router.onKeyUp(131, 0)
    }

    @Test fun `tap mode ignores duplicate down from the second path`() {
        val h = Harness(); h.enable(mode = HardwarePttMode.Tap)
        h.router.onKeyDown(131, 0, 0)
        h.router.onKeyDown(131, 0, 0) // 同一次物理按下，另一条路径
        assertEquals(1, h.presses)
        assertEquals(0, h.releases)
    }

    @Test fun `learning captures the key and never keys`() {
        val h = Harness()
        var captured: Pair<Int, Int>? = null
        h.router.startLearn { k, sc -> captured = k to sc }
        assertTrue(h.router.learning)
        assertTrue(h.router.onKeyDown(228, 87, 0))
        assertEquals(228 to 87, captured)
        assertEquals(0, h.presses)
        assertFalse(h.router.learning)
        // 学习结束后同一个键就是 PTT
        h.enable(keyCode = 228)
        h.router.onKeyDown(228, 87, 0)
        assertEquals(1, h.presses)
    }

    @Test fun `learning ignores back home and app switch so the dialog stays usable`() {
        val h = Harness()
        var captured: Int? = null
        h.router.startLearn { k, _ -> captured = k }
        assertFalse(h.router.onKeyDown(4, 0, 0))   // BACK 照常下发
        assertNull(captured)
        assertTrue(h.router.learning)
        assertEquals(true, h.router.onKeyDown(131, 0, 0))
        assertEquals(131, captured)
    }

    @Test fun `learning while holding releases first`() {
        val h = Harness(); h.enable()
        h.router.onKeyDown(131, 0, 0)
        h.router.startLearn { _, _ -> }
        assertEquals(1, h.releases)   // 切进学习前先松手，否则那颗键永远不松
        assertEquals(1, h.presses)
    }

    @Test fun `reset releases a held key`() {
        val h = Harness(); h.enable()
        h.router.onKeyDown(131, 0, 0)
        h.router.reset()
        assertEquals(1, h.releases)
    }

    @Test fun `reset without holding does not send a stray release`() {
        val h = Harness(); h.enable()
        h.router.reset()
        assertEquals(0, h.releases)
    }

    @Test fun `mode parse defaults to hold`() {
        assertEquals(HardwarePttMode.Hold, HardwarePttMode.parse(null))
        assertEquals(HardwarePttMode.Hold, HardwarePttMode.parse("hold"))
        assertEquals(HardwarePttMode.Hold, HardwarePttMode.parse("HOLD"))
        assertEquals(HardwarePttMode.Tap, HardwarePttMode.parse("tap"))
        assertEquals("tap", HardwarePttMode.Tap.key)
        assertEquals("hold", HardwarePttMode.Hold.key)
    }
}
