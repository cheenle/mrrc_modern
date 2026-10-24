package com.hamradio.ft710android.App

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 旋转与 PTT 的判定门槛（2026-10-22 放开方向锁）。
 *
 * 放开方向锁之后 `onStop()` 会在每次旋转时被调一次。曾经它在**任何**情况下都无条件
 * `vm.onPttRelease()` —— 也就是说"说着一半把手机转过去"会立刻断话，而用户只会觉得
 * 连接断了。修法不是删掉松手（那是安全铁律），而是把判定收敛到
 * [shouldReleaseOnStop]：配置变更 ≠ 用户离开。
 *
 * 这条测试盯的就是这条红线：**只有真正的 onStop 才松手**。
 */
class RotationPolicyTest {
    @Test
    fun `rotation keeps the key down`() {
        assertFalse(
            "旋转（配置变更）不能松 PTT —— 否则转一下手机就断话",
            shouldReleaseOnStop(isChangingConfigurations = true),
        )
    }

    @Test
    fun `a real stop always releases`() {
        // 切后台 / 锁屏 / 被别的界面盖住：无条件释放，这条不许被"优化"
        assertTrue(
            "真正的退后台必须松 PTT（安全铁律）",
            shouldReleaseOnStop(isChangingConfigurations = false),
        )
    }
}
