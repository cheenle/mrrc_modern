package com.hamradio.ft710android.Spectrum

/**
 * 瀑布环形缓冲的下标数学（纯函数，JVM 可测）。
 *
 * 背景：`SpectrumProcessor` 每帧 `addLast(newRow)` + 超限时 `removeFirst()`，
 * 所以**整个列表每帧位移一格**。旧画法因此每帧重建整幅位图
 * （850×120 = 102,000 次查表 + 408KB 分配 + 整幅 setPixels，全在主线程 draw 阶段），
 * 中端机（荣耀）直接卡到音频线程被饿死。
 *
 * 改成环形：位图固定 850×capacity，新行只写自己那一格，绘制时用两段 drawImage
 * 把"环序"展开成"最旧在上、最新在下"。每帧成本从 102,000 像素降到 850 像素。
 */
object WaterfallRing {
    /**
     * 相对上一次的列表，**尾部**新增了几行。
     *
     * `IntArray` 没有覆写 equals，所以 `contains` 就是引用相等 —— 正好用来认"这行上次画过没有"。
     * 正常每帧返回 1；丢帧/批量到达时 >1；首次或换 LUT（主题/Floor/Ceil 变了）后返回全部。
     */
    fun newRowCount(prev: List<IntArray>?, rows: List<IntArray>): Int {
        if (prev == null || prev.isEmpty()) return rows.size
        var i = rows.size - 1
        // 从尾部往前找第一行"上次也有的"，它后面全是新行
        while (i >= 0 && !prev.contains(rows[i])) i--
        return rows.size - 1 - i
    }

    /** 显示顺序（最旧在 display y=0）对应的环起点。 */
    fun firstRingIndex(head: Int, filled: Int, capacity: Int): Int {
        require(capacity > 0) { "capacity must be > 0" }
        return if (filled >= capacity) head % capacity else ((head - filled) % capacity + capacity) % capacity
    }

    /**
     * 两段绘制的行数：第一段从 [first] 到环尾，第二段从环首回绕。
     * 返回 (第一段行数, 第二段行数)，两者之和 = filled。
     */
    fun segments(first: Int, filled: Int, capacity: Int): Pair<Int, Int> {
        val n1 = minOf(filled, capacity - first)
        return n1 to (filled - n1)
    }
}
