package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertEquals
import org.junit.Test

/** Web `WF_PALETTES.jet` 的关键锚点（逐段边界的颜色）。 */
class JetPaletteTest {
    @Test fun `web jet anchors match`() {
        assertEquals(0xFF000080.toInt(), jetArgb(0f))
        assertEquals(0xFF0000FF.toInt(), jetArgb(0.125f))
        assertEquals(0xFF00FFFF.toInt(), jetArgb(0.375f))
        assertEquals(0xFFFFFF00.toInt(), jetArgb(0.625f))
        assertEquals(0xFFFF0000.toInt(), jetArgb(0.875f))
        assertEquals(0xFF7F0000.toInt(), jetArgb(1f))
    }

    @Test fun `out of range clamps`() {
        assertEquals(jetArgb(0f), jetArgb(-1f))
        assertEquals(jetArgb(1f), jetArgb(2f))
    }
}
