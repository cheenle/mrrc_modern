/*
 * sdr_api_shim -- libsdrplay_api 的一层薄封装，给 sdr_pipe.py 用。
 *
 * 为什么不让 Python 直接 ctypes 调厂商 API：SDRplay 的
 * sdrplay_api_DeviceParamsT / RxChannelParamsT 是嵌套十几层的结构体
 * （RSPduo/RSPdx 各有一段），在 Python 里镜像布局就是给"偏移写错"留门。
 * 这里用厂商头文件编译，只暴露扁平的几个函数；IQ 落在 C 里的环形缓冲，
 * Python 端按块取，回调不穿越语言边界。
 *
 * 构建：gcc -O2 -fPIC -shared -o libsdr_api_shim.so sdr_api_shim.c -lsdrplay_api -lm
 */

#include <math.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "sdrplay_api.h"

#define RING_SAMPLES (1u << 20)          /* 约 0.44 s @ 2.4 MSPS */

static short   g_ring[2 * RING_SAMPLES]; /* 交错 I/Q */
static unsigned g_head, g_tail;          /* 生产者/消费者（带锁） */
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_cond = PTHREAD_COND_INITIALIZER;

static sdrplay_api_DeviceT g_dev;
static sdrplay_api_DeviceParamsT *g_params;
static HANDLE g_handle;
static int  g_started, g_overloads, g_removed;
static char g_info[256] = "";

static const char *err_name(sdrplay_api_ErrT e)
{
    switch (e) {
    case sdrplay_api_Success: return "success";
    case sdrplay_api_Fail: return "fail";
    case sdrplay_api_InvalidParam: return "invalid param";
    case sdrplay_api_OutOfRange: return "out of range";
    case sdrplay_api_GainUpdateError: return "gain update error";
    case sdrplay_api_RfUpdateError: return "rf update error";
    case sdrplay_api_FsUpdateError: return "fs update error";
    case sdrplay_api_HwError: return "hardware error";
    case sdrplay_api_ServiceNotResponding: return "api service not responding";
    case sdrplay_api_StartPending: return "start pending";
    case sdrplay_api_StopPending: return "stop pending";
    case sdrplay_api_InvalidMode: return "invalid mode";
    default: return "unknown";
    }
}

/* 设备回调：把交错 IQ 塞进环形缓冲（满了就丢，绝不阻塞 API 线程） */
static void stream_cb(short *xi, short *xq, sdrplay_api_StreamCbParamsT *params,
                      unsigned int num, unsigned int reset, void *ctx)
{
    (void)params; (void)reset; (void)ctx;
    if (!num) return;
    pthread_mutex_lock(&g_lock);
    for (unsigned int i = 0; i < num; i++) {
        unsigned next = (g_head + 1) % RING_SAMPLES;
        if (next == g_tail) break;                 /* 满：丢，不阻塞 */
        g_ring[2 * g_head]     = xi[i];
        g_ring[2 * g_head + 1] = xq[i];
        g_head = next;
    }
    pthread_cond_signal(&g_cond);
    pthread_mutex_unlock(&g_lock);
}

static void event_cb(sdrplay_api_EventT id, sdrplay_api_TunerSelectT tuner,
                     sdrplay_api_EventParamsT *params, void *ctx)
{
    (void)ctx;
    switch (id) {
    case sdrplay_api_GainChange:
        break;
    case sdrplay_api_PowerOverloadChange:
        g_overloads++;
        sdrplay_api_Update(g_handle, tuner, sdrplay_api_Update_Ctrl_OverloadMsgAck,
                           sdrplay_api_Update_Ext1_None);
        break;
    case sdrplay_api_DeviceRemoved:
        g_removed = 1;
        break;
    default:
        break;
    }
}

int sdrshim_open(void)
{
    sdrplay_api_ErrT err = sdrplay_api_Open();
    if (err != sdrplay_api_Success) {
        snprintf(g_info, sizeof(g_info), "api open: %s", err_name(err));
        return -1;
    }
    sdrplay_api_DeviceT devs[4];
    unsigned int n = 0;
    err = sdrplay_api_GetDevices(devs, &n, 4);
    if (err != sdrplay_api_Success || n == 0) {
        snprintf(g_info, sizeof(g_info), "no device (err=%s, n=%u)", err_name(err), n);
        sdrplay_api_Close();
        return -1;
    }
    g_dev = devs[0];
    err = sdrplay_api_SelectDevice(&g_dev);
    if (err != sdrplay_api_Success) {
        snprintf(g_info, sizeof(g_info), "select: %s", err_name(err));
        sdrplay_api_Close();
        return -1;
    }
    err = sdrplay_api_GetDeviceParams(g_dev.dev, &g_params);
    if (err != sdrplay_api_Success || !g_params) {
        snprintf(g_info, sizeof(g_info), "get params: %s", err_name(err));
        sdrplay_api_ReleaseDevice(&g_dev);
        sdrplay_api_Close();
        return -1;
    }
    g_handle = g_dev.dev;
    snprintf(g_info, sizeof(g_info), "serial=%s hwVer=%u", g_dev.SerNo, (unsigned)g_dev.hwVer);
    return 0;
}

int sdrshim_config(double fs_hz, double rf_hz, int grdb, int lna)
{
    if (!g_params) return -1;
    g_params->devParams->fsFreq.fsHz = fs_hz;
    g_params->devParams->ppm = 0.0;
    g_params->rxChannelA->tunerParams.rfFreq.rfHz = rf_hz;
    g_params->rxChannelA->tunerParams.gain.gRdB = grdb;
    g_params->rxChannelA->tunerParams.gain.LNAstate = (unsigned char)lna;
    g_params->rxChannelA->ctrlParams.agc.enable = sdrplay_api_AGC_DISABLE;
    g_params->rxChannelA->ctrlParams.dcOffset.DCenable = 1;
    g_params->rxChannelA->ctrlParams.dcOffset.IQenable = 1;
    return 0;
}

int sdrshim_start(void)
{
    if (!g_handle) return -1;
    sdrplay_api_CallbackFnsT fns;
    fns.StreamACbFn = stream_cb;
    fns.StreamBCbFn = NULL;
    fns.EventCbFn  = event_cb;
    sdrplay_api_ErrT err = sdrplay_api_Init(g_handle, &fns, NULL);
    if (err != sdrplay_api_Success) {
        snprintf(g_info, sizeof(g_info), "init: %s", err_name(err));
        return -1;
    }
    g_started = 1;
    return 0;
}

/* 取最多 max_samples 个复样本（交错）；timeout_ms 内没有数据就返回 0 */
long sdrshim_read(short *dst, long max_samples, int timeout_ms)
{
    long got = 0;
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += timeout_ms / 1000;
    ts.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
    if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }

    pthread_mutex_lock(&g_lock);
    if (g_head == g_tail && g_started && !g_removed)
        pthread_cond_timedwait(&g_cond, &g_lock, &ts);
    while (got < max_samples && g_tail != g_head) {
        dst[2 * got]     = g_ring[2 * g_tail];
        dst[2 * got + 1] = g_ring[2 * g_tail + 1];
        g_tail = (g_tail + 1) % RING_SAMPLES;
        got++;
    }
    pthread_mutex_unlock(&g_lock);
    return got;
}

int  sdrshim_overloads(void) { return g_overloads; }
int  sdrshim_removed(void)   { return g_removed; }
const char *sdrshim_info(void) { return g_info; }

void sdrshim_stop(void)
{
    if (g_started) {
        sdrplay_api_Uninit(g_handle);
        g_started = 0;
    }
    if (g_handle) {
        sdrplay_api_ReleaseDevice(&g_dev);
        g_handle = NULL;
    }
    sdrplay_api_Close();
    g_params = NULL;
}
