package com.hamradio.ft710android.Spectrum

/**
 * 频谱带宽档位（服务端 `spectrum_profile.py` / SDD AD-025）。
 *
 * 一个档位 = 帧形状 × 帧率分频，两者都由服务端决定：
 * - `high` 1701B（wf1+wf2）每个广播 tick —— 与 AD-025 之前逐字节相同
 * - `mid`  851B（仅 wf1）每 2 个 tick
 * - `low`  851B（仅 wf1）每 4 个 tick
 *
 * 值域必须与服务端白名单一致；`listen` 是服务端给只读会话的内部默认档，
 * 客户端声明它会被忽略，所以不在 [NAMES] 里。
 *
 * 对手机来说省下来的是**真的线上字节**：OkHttp 4.12 不提供
 * `permessage-deflate`（其 dex 里有 "Request header not permitted:
 * 'Sec-WebSocket-Extensions'" 守卫），所以 1701B 逐个走出电台。这跟浏览器不一样
 * ——浏览器协商了 deflate，满帧上线只剩 ≈441B，砍掉 wf2 那 850 个零字节几乎不省钱，
 * 只有降帧率有效。两个因子在两个端的权重不同，但对安卓来说形状因子就是全部收益。
 */
object SpectrumTiers {
    /** 与服务端 `spectrum_profile.CLIENT_PROFILES` 逐字一致。 */
    val NAMES = listOf("high", "mid", "low")

    /** 默认不改变任何人的观感：服务端在未收到 caps 时本来也发 high。 */
    const val DEFAULT = "high"

    /** 设置页芯片上显示的字；对齐手机端 Web 的 Full/Half/Quarter。 */
    private val LABELS = mapOf("high" to "Full", "mid" to "Half", "low" to "Quarter")

    fun isValid(name: String): Boolean = name in NAMES

    /** 未知/内部名字一律收敛到默认档，绝不把非法值发上线或落盘。 */
    fun normalize(name: String?): String = if (name != null && isValid(name)) name else DEFAULT

    fun label(name: String): String = LABELS.getValue(normalize(name))

    /**
     * `/WSspectrum` 上的能力声明帧。
     *
     * 老服务端把文本帧当保活直接丢弃（AD-025 之前是 `await ws.receive_text()` 不解析），
     * 所以新 App 连老服务端不会坏，只是拿不到短帧 —— 向前兼容。
     */
    fun capsJson(name: String): String =
        """{"type":"spectrumCaps","profile":"${normalize(name)}"}"""
}
