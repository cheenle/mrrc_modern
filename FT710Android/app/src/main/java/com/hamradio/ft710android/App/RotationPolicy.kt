package com.hamradio.ft710android.App

/**
 * 旋转与 PTT 的判定（纯函数，JVM 可测）。
 *
 * 背景（2026-10-22 放开方向锁）：旋转会重建 Activity，`onStop()` 必然被调一次。
 * 如果那里无条件松手，**说着一半把手机转过去 = 立刻断话**，用户只会觉得"断线了"。
 * 但"松手"是安全铁律，不能含糊 —— 所以判定收敛到这里：
 *
 *  - `isChangingConfigurations == true`：系统在重建同一个界面（旋转/深色模式/字体缩放），
 *    用户没走，**不松手**；
 *  - `false`：真正的 `onStop`（切后台、锁屏、被别的界面盖住），**必须松手**。
 *
 * 这个函数只回答"该不该松"。真正松手的实现仍然只有一处
 * （`MainActivity.onStop` → `vm.onPttRelease()`），不允许在别处复制一份。
 */
internal fun shouldReleaseOnStop(isChangingConfigurations: Boolean): Boolean = !isChangingConfigurations
