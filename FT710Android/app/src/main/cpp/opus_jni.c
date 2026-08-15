#include <jni.h>
#include <opus.h>
#include <stdlib.h>
#include <stdint.h>

static OpusEncoder* enc(jlong h) { return (OpusEncoder*)(intptr_t)h; }
static OpusDecoder* dec(jlong h) { return (OpusDecoder*)(intptr_t)h; }

JNIEXPORT jlong JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_encoderCreate(
    JNIEnv* env, jobject thiz, jint rate, jint ch, jint bitrate) {
    int err;
    OpusEncoder* e = opus_encoder_create(rate, ch, OPUS_APPLICATION_VOIP, &err);
    if (!e) return 0;
    opus_encoder_ctl(e, OPUS_SET_BITRATE(bitrate));
    opus_encoder_ctl(e, OPUS_SET_VBR(0));            /* CBR */
    opus_encoder_ctl(e, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
    return (jlong)(intptr_t)e;
}

JNIEXPORT jint JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_encoderEncode(
    JNIEnv* env, jobject thiz, jlong handle, jshortArray pcm, jbyteArray out) {
    jsize inLen = (*env)->GetArrayLength(env, pcm);
    jsize outLen = (*env)->GetArrayLength(env, out);
    jshort* in = (*env)->GetShortArrayElements(env, pcm, NULL);
    jbyte* ob = (*env)->GetByteArrayElements(env, out, NULL);
    int n = opus_encode(enc(handle), (const opus_int16*)in, (int)(inLen / 2),
                        (unsigned char*)ob, (opus_int32)outLen);
    (*env)->ReleaseShortArrayElements(env, pcm, in, JNI_ABORT);
    (*env)->ReleaseByteArrayElements(env, out, ob, 0);
    return n;
}

JNIEXPORT jlong JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_decoderCreate(
    JNIEnv* env, jobject thiz, jint rate, jint ch) {
    int err;
    OpusDecoder* d = opus_decoder_create(rate, ch, &err);
    return d ? (jlong)(intptr_t)d : 0;
}

JNIEXPORT jint JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_decoderDecode(
    JNIEnv* env, jobject thiz, jlong handle, jbyteArray opus, jint len, jshortArray pcm) {
    jsize outCap = (*env)->GetArrayLength(env, pcm);
    jbyte* in = (*env)->GetByteArrayElements(env, opus, NULL);
    jshort* ob = (*env)->GetShortArrayElements(env, pcm, NULL);
    int n = opus_decode(dec(handle), (const unsigned char*)in, (opus_int32)len,
                        (opus_int16*)ob, (int)(outCap / 2), 0);
    (*env)->ReleaseByteArrayElements(env, opus, in, JNI_ABORT);
    (*env)->ReleaseShortArrayElements(env, pcm, ob, 0);
    return n; /* 解码样本数，负值=错误 */
}

JNIEXPORT void JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_destroyEncoder(
    JNIEnv* env, jobject thiz, jlong handle) {
    if (handle) opus_encoder_destroy(enc(handle));
}

JNIEXPORT void JNICALL
Java_com_hamradio_ft710android_Audio_OpusBridge_destroyDecoder(
    JNIEnv* env, jobject thiz, jlong handle) {
    if (handle) opus_decoder_destroy(dec(handle));
}
