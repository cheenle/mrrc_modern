package com.hamradio.ft710android.UI

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * UI 布局安全门槛（源码级静态检查）。
 *
 * Compose 的 SubcomposeLayout 家族（`BoxWithConstraints` / `Lazy*` / `TabRow` …）
 * **不支持 intrinsic 测量**：一旦被 `Modifier.height(IntrinsicSize.Min)`（或 width/IntrinsicSize）
 * 包住，布局阶段就抛 IllegalStateException —— 现象是"装完打开就退出"，
 * 而 JVM 逻辑测试与 lint 都盖不到（2026-10-05 v1.1.16 真机事故）。
 *
 * 官方异常原文（compose-ui 1.7.6，LayoutNodeSubcompositionsState）：
 * > Asking for intrinsic measurements of SubcomposeLayout layouts is not supported.
 * > This includes components that are built on top of SubcomposeLayout, such as lazy lists,
 * > BoxWithConstraints, TabRow, etc.
 *
 * 想让孩子和某个固定尺寸对齐时，用**显式高度**（本仓做法：S 表尺寸由屏宽算出 meterH，
 * 顶栏 Row 直接 `.height(meterH)`），不要问 intrinsic。
 */
class UiLayoutSafetyTest {
    /** 真实的 intrinsic 用法 token（裸词会命中注释与文档，见 stripComments）。 */
    private val intrinsicTokens = listOf("IntrinsicSize.Min", "IntrinsicSize.Max", "IntrinsicSize.Fixed")

    /** 会走 SubcomposeLayout、因此不支持 intrinsic 测量的组件。 */
    private val subcomposeLayouts = listOf(
        "BoxWithConstraints",
        "LazyColumn",
        "LazyRow",
        "LazyVerticalGrid",
        "LazyHorizontalGrid",
        "LazyVerticalStaggeredGrid",
        "TabRow",
        "ScrollableTabRow",
        "SubcomposeLayout",
    )

    @Test
    fun `no IntrinsicSize wrapper around a SubcomposeLayout child`() {
        val src = File("src/main/java")
        assertTrue("找不到 src/main/java（单元测试工作目录应为 app/）", src.isDirectory)

        val violations = mutableListOf<String>()
        src.walkTopDown().filter { it.extension == "kt" }.forEach { f ->
            composables(stripComments(f.readText())).forEach { (fn, body) ->
                if (intrinsicTokens.none { tok -> body.contains(tok) }) return@forEach
                val hit = subcomposeLayouts.filter { body.contains(it) }
                if (hit.isNotEmpty()) {
                    violations += "${f.name} → $fn()：IntrinsicSize 与 $hit 同时出现"
                }
            }
        }
        assertTrue(
            "IntrinsicSize 不能包住 SubcomposeLayout 家族（会启动即崩）：\n  " +
                violations.joinToString("\n  "),
            violations.isEmpty(),
        )
    }

    /**
     * **收了 `vm` 就必须订阅 `vm.version`**（strong skipping 守卫，2026-10-24 真机事故）。
     *
     * Kotlin 2.0.20 起 Compose 默认 strong skipping：**所有** composable 都可跳过，
     * 不稳定参数按**实例相等**比较。`RadioState` 是可变的普通类、`vm.state` 永远是同一个
     * 对象 —— 子组件只收 `state` 时会被判定「参数没变」而整块跳过，里面的 `state.*`
     * 再也不重读。v1.1.33 表现为：连得上、有声、有频谱、能控制电台，只有频率一直
     * `00.000.00`，转一次屏或点一下才对（`MainScreenRecomposeTest` 复现并盯着修好）。
     *
     * 判据是「**读了 `vm.state`**」：这类组件依赖一个不可观测的可变对象，必须自己订阅
     * 版本号。只订阅 `StateFlow` 的组件（`SpectrumPanel` 读 `vm.waterfall/fft`）不受影响，
     * 它们的重画由各自的流驱动。只扫 `MainScreen.kt`。
     */
    @Test
    fun `every composable that takes the view model also observes its version`() {
        val file = File("src/main/java/com/hamradio/ft710android/UI/MainScreen.kt")
        assertTrue("找不到 MainScreen.kt（单元测试工作目录应为 app/）", file.isFile)
        val src = stripComments(file.readText())
        val offenders = mutableListOf<String>()
        for (m in Regex("""fun\s+([A-Za-z_]\w*)\s*\(""").findAll(src)) {
            val paramsEnd = matchParen(src, m.range.last) ?: continue
            val params = src.substring(m.range.last, paramsEnd)
            if ("vm: MainViewModel" !in params) continue
            var i = paramsEnd + 1
            while (i < src.length && src[i].isWhitespace()) i++
            if (i >= src.length || src[i] != '{') continue
            val bodyEnd = matchBrace(src, i) ?: continue
            val body = src.substring(i, bodyEnd + 1)
            if ("vm.state" in body && "vm.version.collectAsState()" !in body) {
                offenders += m.groupValues[1] + "()"
            }
        }
        assertTrue(
            "这些 composable 收了 vm 却没订阅 vm.version —— strong skipping 会跳过整块，" +
                "屏幕上的读数就冻在第一次组合时的值（v1.1.33「频率 00.000.00」事故）：\n  " +
                offenders.joinToString("\n  "),
            offenders.isEmpty(),
        )
    }

    /**
     * 切出每个 `fun name(...)` 的花括号体（够静态检查用）。
     *
     * 必须先**配对跳过参数列表**再找函数体的 `{`：本仓不少组件的参数默认值就是 lambda
     * （`onToggleFullscreen: () -> Unit = {}`），从第一个 `{` 开始配对会把 `{}` 当成整个函数体，
     * 门槛就形同虚设（2026-10-05 实测踩到）。表达式函数体（`fun x() = …`）直接跳过。
     */
    private fun composables(src: String): List<Pair<String, String>> {
        val out = ArrayList<Pair<String, String>>()
        for (m in Regex("""fun\s+([A-Za-z_]\w*)\s*\(""").findAll(src)) {
            val paramsEnd = matchParen(src, m.range.last) ?: continue
            var i = paramsEnd + 1
            while (i < src.length && src[i].isWhitespace()) i++
            if (i >= src.length || src[i] != '{') continue   // 表达式函数体，跳过
            val bodyEnd = matchBrace(src, i) ?: continue
            out += m.groupValues[1] to src.substring(i, bodyEnd + 1)
        }
        return out
    }

    /** 从 `start`（必须指向 `(`）开始配对括号，返回匹配 `)` 的下标；忽略字符串字面量内的括号。 */
    private fun matchParen(src: String, start: Int): Int? {
        require(src[start] == '(')
        var depth = 0
        var inStr = false
        var escape = false
        var i = start
        while (i < src.length) {
            val c = src[i]
            when {
                escape -> escape = false
                inStr && c == '\\' -> escape = true
                inStr && c == '"' -> inStr = false
                !inStr && c == '"' -> inStr = true
                !inStr && c == '(' -> depth++
                !inStr && c == ')' -> { depth--; if (depth == 0) return i }
            }
            i++
        }
        return null
    }

    /** 从 `start`（必须指向 `{`）开始配对花括号，返回匹配 `}` 的下标。 */
    private fun matchBrace(src: String, start: Int): Int? {
        var depth = 0
        var i = start
        while (i < src.length) {
            when (src[i]) {
                '{' -> depth++
                '}' -> { depth--; if (depth == 0) return i }
            }
            i++
        }
        return null
    }

    /**
     * 把注释替成等长空格（**保持字符偏移**，这样括号配对的下标仍然有效）。
     * 不剥的话，KDoc/行注释里写一句"不能用 IntrinsicSize"就会被判成违规（实测踩到）。
     */
    private fun stripComments(src: String): String {
        val sb = StringBuilder(src)
        var i = 0
        var inStr = false
        var inRaw = false
        var escape = false
        while (i < sb.length) {
            val c = sb[i]
            when {
                inRaw -> {
                    if (sb.startsWith("\"\"\"", i)) { blank(sb, i, i + 3); i += 3; inRaw = false } else i++
                }
                inStr -> when {
                    escape -> { escape = false; i++ }
                    c == '\\' -> { escape = true; i++ }
                    c == '"' -> { inStr = false; i++ }
                    else -> i++
                }
                c == '"' -> {
                    if (sb.startsWith("\"\"\"", i)) { inRaw = true; i += 3 } else { inStr = true; i++ }
                }
                c == '/' && i + 1 < sb.length && sb[i + 1] == '/' -> {
                    val e = sb.indexOf('\n', i).let { if (it < 0) sb.length else it }
                    blank(sb, i, e); i = e
                }
                c == '/' && i + 1 < sb.length && sb[i + 1] == '*' -> {
                    val close = sb.indexOf("*/", i + 2)
                    val e = if (close < 0) sb.length else close + 2
                    blank(sb, i, e); i = e
                }
                else -> i++
            }
        }
        return sb.toString()
    }

    private fun blank(sb: StringBuilder, from: Int, to: Int) {
        for (k in from until minOf(to, sb.length)) if (sb[k] != '\n') sb.setCharAt(k, ' ')
    }
}