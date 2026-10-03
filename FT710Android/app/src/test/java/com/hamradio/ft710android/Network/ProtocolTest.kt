package com.hamradio.ft710android.Network

import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocolTest {
    @Test fun `fullState parsed with bands modes memChannels filterTables`() {
        val text = """{"type":"fullState","data":{"vfo_a_freq":7050000,"mode":1},
            "bands":["160m","80m","40m"],"modes":["LSB","USB","CW"],
            "memChannels":[{"freq":7050000,"mode":"LSB","label":"40m 7.050"},null],
            "filterTables":{"voice":[300,500],"narrow":[3000,5000],"narrowModes":["CW","CW-L"]},
            "atr1000Enabled":false}"""
        val ev = parseWsEvent(text)
        assertTrue(ev is WsEvent.FullState)
        val f = ev as WsEvent.FullState
        assertEquals(3, f.bands.size)
        assertEquals(3, f.modes.size)
        assertEquals(2, f.memChannels.size)
        assertNull(f.memChannels[1])
        assertEquals(listOf(300, 500), f.filterTables!!.voice)
        assertEquals(false, f.atr1000Enabled)
        // data 原样保留，供 RadioState.apply
        assertEquals(7050000, f.data["vfo_a_freq"]!!.jsonPrimitive.int)
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

    @Test fun `fullState exposes recording and cq capability`() {
        val withFeatures = parseWsEvent(
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
}
