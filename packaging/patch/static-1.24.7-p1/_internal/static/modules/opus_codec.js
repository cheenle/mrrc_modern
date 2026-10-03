/**
 * opus_codec.js - OpusEncoder and OpusDecoder classes
 *
 * Extracted from controls.js for modularity.
 *
 * These classes depend on the following globals provided by the Opus WASM module
 * (must be loaded before this script):
 *   _opus_encoder_create, _opus_encode, _opus_encode_float, _opus_encoder_destroy,
 *   _opus_encoder_ctl, _opus_decoder_create, _opus_decode, _opus_decode_float,
 *   _opus_decoder_destroy, _malloc, _free, allocate, ALLOC_STACK,
 *   getValue, setValue, HEAP16, HEAPU8, HEAPF32, Opus
 */

var OpusEncoder = (function () {
    function OpusEncoder(sampling_rate, channels, app, frame_duration) {
        if (frame_duration === void 0) { frame_duration = 20; }
        this.handle = 0;
        this.frame_size = 0;
        this.in_ptr = 0;
        this.in_off = 0;
        this.out_ptr = 0;
        if (!Opus.validFrameDuration(frame_duration))
            throw 'invalid frame duration';
        this.frame_size = sampling_rate * frame_duration / 1000;
        var err_ptr = allocate(4, 'i32', ALLOC_STACK);
        this.handle = _opus_encoder_create(sampling_rate, channels, app, err_ptr);
        if (getValue(err_ptr, 'i32') != 0 /* OK */)
            throw 'opus_encoder_create failed: ' + getValue(err_ptr, 'i32');
        this.in_ptr = _malloc(this.frame_size * channels * 4);
        this.in_len = this.frame_size * channels;
        this.in_i16 = HEAP16.subarray(this.in_ptr >> 1, (this.in_ptr >> 1) + this.in_len);
        this.in_f32 = HEAPF32.subarray(this.in_ptr >> 2, (this.in_ptr >> 2) + this.in_len);
        this.out_bytes = Opus.getMaxFrameSize();
        this.out_ptr = _malloc(this.out_bytes);
        this.out_buf = HEAPU8.subarray(this.out_ptr, this.out_ptr + this.out_bytes);

        // ========== Opus WebRTC 最佳实践优化 ==========
        // 参考: https://wiki.xiph.org/Opus_Recommended_Settings
        // 参考: https://opus-codec.org/docs/opus_api-1.5/group__opus__encoderctls.html

        // OPUS_SET_COMPLEXITY_REQUEST = 4010
        // 编码复杂度 (0-10, 默认 10)
        // TX 编码跑在 Worker；complexity=5 在音质和移动端 CPU 间折中。
        var complexity_ptr = allocate(4, 'i32', ALLOC_STACK);
        setValue(complexity_ptr, 5, 'i32');
        _opus_encoder_ctl(this.handle, 4010, complexity_ptr);

        // OPUS_SET_BITRATE_REQUEST = 4002
        // 目标比特率 (默认自动)
        // TX 优先发射保真，64kbps mono 对语音留足余量。
        var bitrate_ptr = allocate(4, 'i32', ALLOC_STACK);
        setValue(bitrate_ptr, 64000, 'i32');  // 64kbps
        _opus_encoder_ctl(this.handle, 4002, bitrate_ptr);

        // OPUS_SET_MAX_BANDWIDTH_REQUEST = 4004, OPUS_BANDWIDTH_FULLBAND = 1105
        var bandwidth_ptr = allocate(4, 'i32', ALLOC_STACK);
        setValue(bandwidth_ptr, 1105, 'i32');
        _opus_encoder_ctl(this.handle, 4004, bandwidth_ptr);

        // OPUS_SET_VBR_REQUEST = 4006
        // TX 发射优先稳定包间隔和包大小；64kbps CBR 避免复杂语音段码率波动。
        var vbr_ptr = allocate(4, 'i32', ALLOC_STACK);
        setValue(vbr_ptr, 0, 'i32');  // 0 = 关闭 VBR
        _opus_encoder_ctl(this.handle, 4006, vbr_ptr);

        // OPUS_SET_INBAND_FEC_REQUEST = 4012
        // 前向纠错。TX 走 WebSocket/TCP：不会乱序丢 UDP 包，FEC 只会增加
        // 码率和编码负担，不能修复 TCP 队头阻塞。因此关闭。
        var fec_ptr = allocate(4, 'i32', ALLOC_STACK);
        setValue(fec_ptr, 0, 'i32');  // 0 = 关闭 FEC
        _opus_encoder_ctl(this.handle, 4012, fec_ptr);

        // OPUS_SET_PACKET_LOSS_PERC_REQUEST = 4014
        // 预期丢包率 (默认 0%)
        // 配合 FEC 使用；FEC 关闭时保持 0。
        var loss_ptr = allocate(4, 'i32', ALLOC_STACK);
        setValue(loss_ptr, 0, 'i32');
        _opus_encoder_ctl(this.handle, 4014, loss_ptr);

        // OPUS_SET_DTX_REQUEST = 4016
        // DTX 会让静音段停止出包，下一句话起音更容易重新 prime TX jitter
        // buffer；发射听感优先于再省几 kbps，因此关闭。
        var dtx_ptr = allocate(4, 'i32', ALLOC_STACK);
        setValue(dtx_ptr, 0, 'i32');  // 0 = 关闭 DTX
        _opus_encoder_ctl(this.handle, 4016, dtx_ptr);

        // OPUS_SET_SIGNAL_REQUEST = 4024
        // 信号类型提示 (自动检测)
        // OPUS_SIGNAL_VOICE = 3001 语音优化
        var signal_ptr = allocate(4, 'i32', ALLOC_STACK);
        setValue(signal_ptr, 3001, 'i32');  // VOICE
        _opus_encoder_ctl(this.handle, 4024, signal_ptr);

        // OPUS_SET_LSB_DEPTH_REQUEST = 4036
        // 输入是 16-bit 等效麦克风 PCM/float32，告知编码器有效位深。
        var lsb_ptr = allocate(4, 'i32', ALLOC_STACK);
        setValue(lsb_ptr, 16, 'i32');
        _opus_encoder_ctl(this.handle, 4036, lsb_ptr);

        console.log('🎵 Opus TX: complexity=5, bitrate=64kbps, CBR, fullband, FEC=OFF, DTX=OFF');
    }
    OpusEncoder.prototype.encode = function (pcm) {
        var output = [];
        var pcm_off = 0;
        while (pcm.length - pcm_off >= this.in_len - this.in_off) {
            if (this.in_off > 0) {
                this.in_i16.set(pcm.subarray(pcm_off, pcm_off + this.in_len - this.in_off), this.in_off);
                pcm_off += this.in_len - this.in_off;
                this.in_off = 0;
            }
            else {
                this.in_i16.set(pcm.subarray(pcm_off, pcm_off + this.in_len));
                pcm_off += this.in_len;
            }
            var ret = _opus_encode(this.handle, this.in_ptr, this.frame_size, this.out_ptr, this.out_bytes);
            if (ret <= 0)
                throw 'opus_encode failed: ' + ret;
            var packet = new ArrayBuffer(ret);
            new Uint8Array(packet).set(this.out_buf.subarray(0, ret));
            output.push(packet);
        }
        if (pcm_off < pcm.length) {
            this.in_i16.set(pcm.subarray(pcm_off));
            this.in_off = pcm.length - pcm_off;
        }
        return output;
    };
    OpusEncoder.prototype.encode_float = function (pcm) {
        var output = [];
        var pcm_off = 0;
        while (pcm.length - pcm_off >= this.in_len - this.in_off) {
            if (this.in_off > 0) {
                this.in_f32.set(pcm.subarray(pcm_off, pcm_off + this.in_len - this.in_off), this.in_off);
                pcm_off += this.in_len - this.in_off;
                this.in_off = 0;
            }
            else {
                this.in_f32.set(pcm.subarray(pcm_off, pcm_off + this.in_len));
                pcm_off += this.in_len;
            }
            var ret = _opus_encode_float(this.handle, this.in_ptr, this.frame_size, this.out_ptr, this.out_bytes);
            if (ret <= 0)
                throw 'opus_encode failed: ' + ret;
            var packet = new ArrayBuffer(ret);
            new Uint8Array(packet).set(this.out_buf.subarray(0, ret));
            output.push(packet);
        }
        if (pcm_off < pcm.length) {
            this.in_f32.set(pcm.subarray(pcm_off));
            this.in_off = pcm.length - pcm_off;
        }
        return output;
    };
    OpusEncoder.prototype.encode_final = function () {
        if (this.in_off == 0)
            return new ArrayBuffer(0);
        for (var i = this.in_off; i < this.in_len; ++i)
            this.in_i16[i] = 0;
        var ret = _opus_encode(this.handle, this.in_ptr, this.frame_size, this.out_ptr, this.out_bytes);
        if (ret <= 0)
            throw 'opus_encode failed: ' + ret;
        var packet = new ArrayBuffer(ret);
        new Uint8Array(packet).set(this.out_buf.subarray(0, ret));
        return packet;
    };
    OpusEncoder.prototype.encode_float_final = function () {
        if (this.in_off == 0)
            return new ArrayBuffer(0);
        for (var i = this.in_off; i < this.in_len; ++i)
            this.in_f32[i] = 0;
        var ret = _opus_encode_float(this.handle, this.in_ptr, this.frame_size, this.out_ptr, this.out_bytes);
        if (ret <= 0)
            throw 'opus_encode failed: ' + ret;
        var packet = new ArrayBuffer(ret);
        new Uint8Array(packet).set(this.out_buf.subarray(0, ret));
        return packet;
    };
    OpusEncoder.prototype.destroy = function () {
        if (!this.handle)
            return;
        _opus_encoder_destroy(this.handle);
        _free(this.in_ptr);
        this.handle = this.in_ptr = 0;
    };
    return OpusEncoder;
})();

var OpusDecoder = (function () {
    function OpusDecoder(sampling_rate, channels) {
        this.handle = 0;
        this.in_ptr = 0;
        this.out_ptr = 0;
        this.channels = channels;
        var err_ptr = allocate(4, 'i32', ALLOC_STACK);
        this.handle = _opus_decoder_create(sampling_rate, channels, err_ptr);
        if (getValue(err_ptr, 'i32') != 0 /* OK */)
            throw 'opus_decoder_create failed: ' + getValue(err_ptr, 'i32');
        this.in_ptr = _malloc(Opus.getMaxFrameSize(channels));
        this.in_buf = HEAPU8.subarray(this.in_ptr, this.in_ptr + Opus.getMaxFrameSize(channels));
        this.out_len = Opus.getMaxSamplesPerChannel(sampling_rate);
        var out_bytes = this.out_len * channels * 4;
        this.out_ptr = _malloc(out_bytes);
        this.out_i16 = HEAP16.subarray(this.out_ptr >> 1, (this.out_ptr + out_bytes) >> 1);
        this.out_f32 = HEAPF32.subarray(this.out_ptr >> 2, (this.out_ptr + out_bytes) >> 2);
    }
    OpusDecoder.prototype.decode = function (packet) {
        this.in_buf.set(new Uint8Array(packet));
        var ret = _opus_decode(this.handle, this.in_ptr, packet.byteLength, this.out_ptr, this.out_len, 0);
        if (ret < 0)
            throw 'opus_decode failed: ' + ret;
        var samples = new Int16Array(ret * this.channels);
        samples.set(this.out_i16.subarray(0, samples.length));
        return samples;
    };
    OpusDecoder.prototype.decode_float = function (packet) {
        this.in_buf.set(new Uint8Array(packet));
        var ret = _opus_decode_float(this.handle, this.in_ptr, packet.byteLength, this.out_ptr, this.out_len, 0);
        if (ret < 0)
            throw 'opus_decode failed: ' + ret;
        var samples = new Float32Array(ret * this.channels);
        samples.set(this.out_f32.subarray(0, samples.length));
        return samples;
    };
    // ========== Opus PLC (Packet Loss Concealment) ==========
    // 当检测到丢包时调用，使用前一帧信息生成补偿帧
    // 参考: https://opus-codec.org/docs/opus_api-1.5/group__opus__decoder.html
    // 传入 null/空数据时，Opus 解码器会自动进行 PLC
    OpusDecoder.prototype.decode_plc = function () {
        // decode_fec: 0 = 正常解码, 1 = 解码 FEC 帧
        // 这里传入 0，但 packet 长度为 0，触发 PLC
        var ret = _opus_decode(this.handle, 0, 0, this.out_ptr, this.out_len, 0);
        if (ret < 0)
            throw 'opus_decode_plc failed: ' + ret;
        var samples = new Int16Array(ret * this.channels);
        samples.set(this.out_i16.subarray(0, samples.length));
        return samples;
    };
    OpusDecoder.prototype.decode_plc_float = function () {
        var ret = _opus_decode_float(this.handle, 0, 0, this.out_ptr, this.out_len, 0);
        if (ret < 0)
            throw 'opus_decode_plc_float failed: ' + ret;
        var samples = new Float32Array(ret * this.channels);
        samples.set(this.out_f32.subarray(0, samples.length));
        return samples;
    };
    OpusDecoder.prototype.destroy = function () {
        if (!this.handle)
            return;
        _opus_decoder_destroy(this.handle);
        _free(this.in_ptr);
        _free(this.out_ptr);
        this.handle = this.in_ptr = this.out_ptr = 0;
    };
    return OpusDecoder;
})();
