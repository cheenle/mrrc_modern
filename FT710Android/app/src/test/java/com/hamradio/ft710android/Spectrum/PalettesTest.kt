package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertEquals
import org.junit.Test

class PalettesTest {
    @Test fun `jet keeps the legacy anchors`() {
        assertEquals(0xFF000080.toInt(), Palettes.argb("jet", 0f))
        assertEquals(0xFF0000FF.toInt(), Palettes.argb("jet", 0.125f))
        assertEquals(0xFF00FFFF.toInt(), Palettes.argb("jet", 0.375f))
        assertEquals(0xFFFFFF00.toInt(), Palettes.argb("jet", 0.625f))
        assertEquals(0xFFFF0000.toInt(), Palettes.argb("jet", 0.875f))
    }

    @Test fun `all six palettes hit their web anchors`() {
        assertEquals(0xFF000000.toInt(), Palettes.argb("hot", 0f))
        assertEquals(0xFFFF0000.toInt(), Palettes.argb("hot", 0.33f))
        assertEquals(0xFFFFFFFE.toInt(), Palettes.argb("hot", 1f)) // JS: u*255 = 254.999… → 254
        assertEquals(0xFF000028.toInt(), Palettes.argb("cold", 0f))
        assertEquals(0xFF00C8FF.toInt(), Palettes.argb("cold", 0.5f))
        assertEquals(0xFFFFFFFF.toInt(), Palettes.argb("cold", 1f))
        assertEquals(0xFFC80000.toInt(), Palettes.argb("thermal", 0.25f))
        assertEquals(0xFFFFB400.toInt(), Palettes.argb("thermal", 0.5f))
        assertEquals(0xFFFFFFC8.toInt(), Palettes.argb("thermal", 0.75f))
        assertEquals(0xFF000080.toInt(), Palettes.argb("night", 0.33f))
        assertEquals(0xFFB400FF.toInt(), Palettes.argb("night", 0.66f))
        assertEquals(0xFFFFC7FF.toInt(), Palettes.argb("night", 1f))
        assertEquals(0xFF7F7F7F.toInt(), Palettes.argb("gray", 0.5f))
    }

    @Test fun `names match the web selector`() {
        assertEquals(listOf("jet", "hot", "cold", "thermal", "night", "gray"), Palettes.NAMES)
    }

    @Test fun `unknown theme falls back to jet`() {
        assertEquals(Palettes.argb("jet", 0.25f), Palettes.argb("nope", 0.25f))
    }

    @Test fun `lut applies floor and ceiling`() {
        val lut = Palettes.lut("gray", floor = 5, ceil = 220)
        assertEquals(0xFF000000.toInt(), lut[0])
        assertEquals(0xFF000000.toInt(), lut[5])
        assertEquals(0xFFFFFFFF.toInt(), lut[220])
        assertEquals(0xFFFFFFFF.toInt(), lut[255])
    }
}
