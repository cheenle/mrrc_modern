package com.hamradio.ft710android.App

import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.compose.LocalLifecycleOwner

/** 主界面装配：RX 音频焦点（切走暂停）、保持亮屏、退后台时强制释放 TX。 */
@Composable
fun AppSetup(rxRunning: Boolean, onTxRelease: () -> Unit) {
    val view = LocalView.current
    val context = LocalContext.current

    // 保持亮屏
    DisposableEffect(Unit) {
        val prev = view.keepScreenOn
        view.keepScreenOn = true
        onDispose { view.keepScreenOn = prev }
    }

    // 音频焦点
    val am = remember { context.getSystemService(AudioManager::class.java) }
    val focusReq = remember {
        AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
            .build()
    }
    DisposableEffect(rxRunning) {
        if (rxRunning) am.requestAudioFocus(focusReq) else am.abandonAudioFocusRequest(focusReq)
        onDispose { am.abandonAudioFocusRequest(focusReq) }
    }

    // 生命周期：退后台 → 强制释放 TX（spec §7.3 Layer 6 等价物）
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = object : DefaultLifecycleObserver {
            override fun onStop(owner: LifecycleOwner) { onTxRelease() }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
}
