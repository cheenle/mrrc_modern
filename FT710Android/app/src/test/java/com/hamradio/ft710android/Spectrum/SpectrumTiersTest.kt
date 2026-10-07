package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class SpectrumTiersTest {
    @Test fun `values match the server whitelist exactly`() {
        // 服务端 spectrum_profile.CLIENT_PROFILES == ("high","mid","low")；
        // "listen" 是服务端内部档（listener 角色默认），客户端不得声明。
        assertEquals(listOf("high", "mid", "low"), SpectrumTiers.NAMES)
        assertEquals("high", SpectrumTiers.DEFAULT)
        assertFalse(SpectrumTiers.isValid("listen"))
    }

    @Test fun `every tier has a label and none is empty`() {
        SpectrumTiers.NAMES.forEach {
            assertEquals(true, SpectrumTiers.label(it).isNotBlank())
        }
        // 未知名字回退到默认档的标签，不抛
        assertEquals(SpectrumTiers.label(SpectrumTiers.DEFAULT), SpectrumTiers.label("nope"))
    }

    @Test fun `caps json is exactly what the server parses`() {
        assertEquals("""{"type":"spectrumCaps","profile":"mid"}""", SpectrumTiers.capsJson("mid"))
        assertEquals("""{"type":"spectrumCaps","profile":"high"}""", SpectrumTiers.capsJson("high"))
        assertEquals("""{"type":"spectrumCaps","profile":"low"}""", SpectrumTiers.capsJson("low"))
    }

    @Test fun `caps json refuses to put an unknown name on the wire`() {
        // 宁可声明 high（= 服务端默认，逐字节兼容）也不发一个服务端会忽略的名字：
        // 发了非法值会让人以为"选了低档"，实际却在拿满帧。
        assertEquals(SpectrumTiers.capsJson(SpectrumTiers.DEFAULT), SpectrumTiers.capsJson("turbo"))
        assertEquals(SpectrumTiers.capsJson(SpectrumTiers.DEFAULT), SpectrumTiers.capsJson("listen"))
        assertEquals(SpectrumTiers.capsJson(SpectrumTiers.DEFAULT), SpectrumTiers.capsJson(""))
    }

    @Test fun `normalize coerces anything unknown to the default`() {
        assertEquals("high", SpectrumTiers.normalize("high"))
        assertEquals("mid", SpectrumTiers.normalize("mid"))
        assertEquals("low", SpectrumTiers.normalize("low"))
        assertEquals("high", SpectrumTiers.normalize(""))
        assertEquals("high", SpectrumTiers.normalize("listen"))
        assertEquals("high", SpectrumTiers.normalize(null))
    }
}
