package com.hamradio.ft710android.Audio

/** JNI → libopus：RX 解码 / TX 编码。48kHz 单声道 20ms（960 样本），TX CBR 64kbps。 */
object OpusBridge {
    const val SAMPLE_RATE = 48000
    const val CHANNELS = 1
    const val FRAME_SAMPLES = 960      // 48k * 20ms
    const val BITRATE = 64000          // CBR

    init { System.loadLibrary("opus_jni") }

    external fun encoderCreate(sampleRate: Int, channels: Int, bitrate: Int): Long
    external fun encoderEncode(handle: Long, pcm: ShortArray, out: ByteArray): Int
    external fun decoderCreate(sampleRate: Int, channels: Int): Long
    external fun decoderDecode(handle: Long, opus: ByteArray, len: Int, pcm: ShortArray): Int
    external fun destroyEncoder(handle: Long)
    external fun destroyDecoder(handle: Long)
}
