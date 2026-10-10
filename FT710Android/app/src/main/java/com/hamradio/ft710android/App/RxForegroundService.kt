package com.hamradio.ft710android.App

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.hamradio.ft710android.R

/**
 * 后台 RX 前台服务（L1）：连接建立且「后台接收」开启时常驻，进程不被回收、Doze 不掐网。
 * 只保接收：TX 的释放仍在 MainActivity.onStop → forceRelease()，本服务不碰 PTT。
 * 通知动作「断开」= 与顶栏 ⏻ 相同（VM.disconnect）。
 */
class RxForegroundService : Service() {
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_DISCONNECT) {
            ServiceLocator.vmFactory().disconnect()
            stopSelf()
            return START_NOT_STICKY
        }
        startForeground(NOTIF_ID, buildNotification())
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        val mgr = getSystemService(NotificationManager::class.java)
        if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
            mgr.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "后台接收", NotificationManager.IMPORTANCE_LOW))
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val off = PendingIntent.getService(
            this, 1,
            Intent(this, RxForegroundService::class.java).setAction(ACTION_DISCONNECT),
            PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("MRRC Modern")
            .setContentText("正在接收（后台 RX）")
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(0, "断开", off)
            .build()
    }

    companion object {
        const val CHANNEL_ID = "rx"
        const val NOTIF_ID = 1001
        const val ACTION_DISCONNECT = "com.hamradio.ft710android.DISCONNECT"

        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, RxForegroundService::class.java))
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, RxForegroundService::class.java))
        }
    }
}
