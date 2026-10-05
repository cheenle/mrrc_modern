package com.hamradio.ft710android.Network

import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocolTest {
    @Test fun `real fullState shape parses bands as objects and filter tables as pairs`() {
        val text = """
        {"type":"fullState",
         "data":{"vfo_a_freq":7050000,"mode":2,"tx_status":0,"scope_span":6},
         "bands":[{"name":"40m","start":7000000,"end":7300000,"bsr":3,"default_freq":7050000}],
         "modes":["LSB","USB","CW-U"],
         "memChannels":[{"freq":7050000,"mode":"LSB","label":"M1"},null],
         "filterTables":{"voice":[[1,300],[2,400],[13,2400]],"narrow":[[1,50]],"narrowModes":["CW-U"]},
         "recording":{"recording":false,"freq_hz":0,"started_at":null,"duration":0.0,"name":null,"bytes":0,"dropped":0},
         "cq":{"state":"idle","duration_s":0.0,"elapsed_s":0.0,"frames_total":0,"frames_sent":0,"started_by":null,"reason":null,"ready":false},
         "radioModel":"ft710","radioDisplayName":"Yaesu FT-710",
         "capabilities":{"model_name":"ft710","display_name":"Yaesu FT-710","verified":true,"tx_gated":false,
           "has_atu":true,"has_auto_notch":true,"has_vd_id_meters":true,"filter_model":"width_table",
           "att_steps":[0,6,12,18],"preamp_steps":["OFF","AMP1","AMP2"],"scope_type":"ft4222",
           "scope_spans":{"6":{"name":"100 kHz","freq":100000}},
           "scope_speeds":["1","2","3","4","5"],"audio_gain_boost":10.0},
         "atr1000Enabled":true}
        """.trimIndent()
        val ev = parseWsEvent(text)
        assertTrue(ev is WsEvent.FullState)
        val f = ev as WsEvent.FullState
        assertEquals(1, f.bands.size)
        assertEquals("40m", f.bands[0].name)
        assertEquals(7050000L, f.bands[0].defaultFreq)
        assertEquals(3, f.modes.size)
        assertEquals(2, f.memChannels.size)
        assertNull(f.memChannels[1])
        assertEquals(listOf(listOf(1, 300), listOf(2, 400), listOf(13, 2400)), f.filterTables!!.voice)
        assertEquals(listOf("CW-U"), f.filterTables!!.narrowModes)
        assertEquals("Yaesu FT-710", f.radioDisplayName)
        assertEquals("width_table", f.capabilities!!.filterModel)
        assertEquals(listOf(0, 6, 12, 18), f.capabilities!!.attSteps)
        assertEquals(100000L, f.capabilities!!.scopeSpans.getValue("6").freq)
        assertTrue(f.atr1000Enabled)
        // data 原样保留，供 RadioState.apply
        assertEquals(7050000, f.data["vfo_a_freq"]!!.jsonPrimitive.int)
    }

    @Test fun `legacy payload without capabilities still parses`() {
        val ev = parseWsEvent(
            """{"type":"fullState","data":{"mode":1},"bands":[{"name":"20m","default_freq":14270000}]}"""
        ) as WsEvent.FullState
        assertEquals(1, ev.bands.size)
        assertEquals(14270000L, ev.bands[0].defaultFreq)
        assertNull(ev.capabilities)
        assertNull(ev.radioDisplayName)
    }

    @Test fun `stateUpdate parsed with fields and dirty`() {
        val text = """{"type":"stateUpdate","fields":{"tx_status":1,"s_meter":9},"dirty":["tx_status","s_meter"]}"""
        val ev = parseWsEvent(text) as WsEvent.StateUpdate
        assertEquals(setOf("tx_status", "s_meter"), ev.dirty.toSet())
        assertEquals(1, ev.fields["tx_status"]!!.jsonPrimitive.int)
    }

    @Test fun `memChannels and error and pong parsed`() {
        assertTrue(parseWsEvent("""{"type":"memChannels","channels":[null,null]}""") is WsEvent.MemChannels)
        val err = parseWsEvent("""{"type":"error","message":"Radio not connected"}""")
        assertTrue(err is WsEvent.ErrorEvent)
        assertEquals("Radio not connected", (err as WsEvent.ErrorEvent).message)
        assertTrue(parseWsEvent("""{"type":"pong"}""") is WsEvent.Pong)
    }

    @Test fun `commands serialize exactly`() {
        assertEquals("""{"type":"set","field":"freq","value":7050000}""", WsCommands.setNumber("freq", 7050000))
        assertEquals("""{"type":"set","field":"mode","value":"USB"}""", WsCommands.setString("mode", "USB"))
        assertEquals("""{"type":"set","field":"ptt","value":true}""", WsCommands.setBool("ptt", true))
        assertEquals("""{"type":"ping"}""", WsCommands.ping())
        assertEquals("""{"type":"get","field":"fullState"}""", WsCommands.getFullState())
    }

    @Test fun `fullState with missing optional keys still parses`() {
        val ev = parseWsEvent("""{"type":"fullState","data":{"mode":1}}""")
        assertTrue(ev is WsEvent.FullState)
        assertTrue((ev as WsEvent.FullState).bands.isEmpty())
        assertNull(ev.filterTables)
    }

    @Test fun `parses recordingState`() {
        val ev = parseWsEvent(
            """{"type":"recordingState","recording":{"recording":true,"freq_hz":7050000,"started_at":"2026-10-03T12:00:00","duration":12.5,"name":"7050000kHz_20261003_120000.mp3","bytes":123456,"dropped":0}}"""
        )
        assertTrue(ev is WsEvent.RecordingState)
        val status = (ev as WsEvent.RecordingState).status
        assertEquals(7050000L, status.freqHz)
        assertEquals(true, status.recording)
        assertEquals(12.5, status.duration, 0.001)
    }

    @Test fun `parses cqState`() {
        val ev = parseWsEvent(
            """{"type":"cqState","cq":{"state":"calling","duration_s":6.1,"elapsed_s":2.0,"frames_total":305,"frames_sent":100,"started_by":"abc123","reason":null,"ready":true}}"""
        )
        assertTrue(ev is WsEvent.CqState)
        assertEquals("calling", (ev as WsEvent.CqState).status.state)
        assertEquals(305, ev.status.framesTotal)
    }

    @Test fun `fullState exposes recording and cq capability`() {        val withFeatures = parseWsEvent(
            """{"type":"fullState","data":{},"bands":[],"modes":[],"memChannels":[],"recording":{"recording":false},"cq":{"state":"idle"},"radioModel":"ft710"}"""
        ) as WsEvent.FullState
        assertEquals(false, withFeatures.recording?.recording)
        assertEquals("idle", withFeatures.cq?.state)
        assertEquals("ft710", withFeatures.radioModel)
        val without = parseWsEvent(
            """{"type":"fullState","data":{},"bands":[],"modes":[],"memChannels":[]}"""
        ) as WsEvent.FullState
        assertNull(without.recording)
        assertNull(without.cq)
    }

    @Test fun `atr state and tune result parse`() {
        val st = parseAtrEvent(
            """{"type":"atrState","connected":true,"power":12.5,"swr":1.4,"sw":1,"ind":3,"cap":7,"tuning":false,"tx":false,"freq":7050000,"last_update":1.0}"""
        ) as AtrEvent.State
        assertEquals(12.5, st.s.power, 0.001)
        assertEquals(1, st.s.sw)
        assertEquals(3, st.s.ind)
        val tr = parseAtrEvent(
            """{"type":"atrTuneResult","phase":"auto_success","swr_before":3.2,"swr_after":1.3,"auto":true}"""
        ) as AtrEvent.TuneResult
        assertEquals("auto_success", tr.r.phase)
        assertEquals(3.2, tr.r.swrBefore ?: 0.0, 0.001)
    }

    @Test fun `atr error parses and unknown ATR payload is null`() {
        val err = parseAtrEvent("""{"type":"error","message":"ATR1000 not connected"}""")
        assertEquals("ATR1000 not connected", (err as AtrEvent.Error).message)
        assertNull(parseAtrEvent("""{"type":"pong"}"""))
    }

    @Test fun `atr tune text follows the web copy`() {
        assertEquals("ATR 连续 3 次无改善，已放弃该频点自动调谐",
            AtrText.result(AtrTuneResultDto(phase = "auto_giveup")))
        assertEquals("ATR 调谐完成: SWR 3.2 → 1.3",
            AtrText.result(AtrTuneResultDto(phase = "success", swrBefore = 3.2, swrAfter = 1.3)))
    }
}
