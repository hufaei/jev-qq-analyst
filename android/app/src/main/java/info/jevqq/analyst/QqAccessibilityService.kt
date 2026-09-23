package info.jevqq.analyst

import android.accessibilityservice.AccessibilityService
import android.graphics.Point
import android.graphics.Rect
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONObject
import java.util.concurrent.Executors

/** Observes only the foreground QQ tree. It never performs an accessibility action. */
class QqAccessibilityService : AccessibilityService() {
    private val main = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private val memory = ConversationMemory()
    private val inFlight = HashSet<VerdictKey>()
    private var overlay: AnalysisOverlay? = null
    private var spec: JSONObject? = null
    private var visiblePlan: VisiblePlan? = null
    private var cachedSettings: ConnectionSettings? = null
    private var fingerprint = ""
    private var lastQqEvent = false
    private var lastStatus = ""
    @Volatile private var generation = 0L
    private var captureScheduled = false
    private val capture = Runnable {
        captureScheduled = false
        captureForeground()
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        recordStatus("无障碍服务已连接，等待 QQ 页面事件")
        spec = JSONObject(assets.open("decision_questions.json").bufferedReader().use { it.readText() })
        overlay = AnalysisOverlay(this)
        scheduleCapture()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val packageName = event?.packageName?.toString()
        if (packageName == QQ_PACKAGE) {
            lastQqEvent = true
            if (visiblePlan == null) recordStatus("已收到 QQ 页面事件，正在读取活动窗口")
        }
        if (packageName == QQ_PACKAGE ||
            event?.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED ||
            event?.eventType == AccessibilityEvent.TYPE_WINDOWS_CHANGED
        ) scheduleCapture()
    }

    override fun onInterrupt() = clearVisible()

    override fun onDestroy() {
        main.removeCallbacks(capture)
        clearVisible()
        overlay?.dispose()
        overlay = null
        worker.shutdownNow()
        super.onDestroy()
    }

    private fun scheduleCapture() {
        if (captureScheduled) return
        captureScheduled = true
        main.postDelayed(capture, 280)
    }

    private fun captureForeground() {
        val root = rootInActiveWindow
        if (root?.packageName?.toString() != QQ_PACKAGE) {
            if (lastQqEvent) recordStatus(if (root == null)
                "已收到 QQ 事件，但无障碍服务无法读取活动窗口"
            else "已收到 QQ 事件，但活动窗口不是 QQ")
            clearVisible()
            return
        }
        lastQqEvent = false
        val screen = Point()
        @Suppress("DEPRECATION")
        (getSystemService(WINDOW_SERVICE) as WindowManager).defaultDisplay.getRealSize(screen)
        val tree = copyTree(root, 0, intArrayOf(0))
        val snapshot = QqParser(screen.x, screen.y).parse(tree)
        if (snapshot == null) {
            recordStatus("QQ 已在前台，但未识别到聊天输入框；请进入具体聊天页")
            clearVisible()
            return
        }
        val diagnostic = getSharedPreferences("capture_diagnostic", MODE_PRIVATE)
        if (diagnostic.getBoolean("capture_requested", false)) {
            val qqVersion = runCatching {
                @Suppress("DEPRECATION")
                packageManager.getPackageInfo(QQ_PACKAGE, 0).versionName.orEmpty()
            }.getOrDefault("unknown")
            diagnostic.edit().putString("ui_tree", QqTreeDiagnostic.report(
                tree, screen.x, screen.y, Build.MODEL, Build.VERSION.SDK_INT, qqVersion,
            )).putBoolean("capture_requested", false).apply()
        }
        val currentFingerprint = buildString {
            append(snapshot.title)
            snapshot.messages.forEach {
                append('|').append(it.side).append(':').append(it.kind).append(':')
                    .append(it.text).append(':').append(it.quote)
            }
        }
        if (currentFingerprint == fingerprint) return
        fingerprint = currentFingerprint
        generation++
        inFlight.clear()
        val settingsResult = runCatching { SettingsStore(this).load() }
        val settings = settingsResult.getOrNull()
        if (settings != null && settings != cachedSettings) {
            memory.clear()
            cachedSettings = settings
        }
        val plan = VisiblePlan.from(snapshot, memory)
        visiblePlan = plan
        if (snapshot.title.isNullOrBlank()) {
            recordStatus("已识别聊天页，但当前 QQ 的会话标题节点未识别")
            overlay?.render(plan, memory, "未识别到会话标题，暂不分析")
            return
        }
        if (settings == null) {
            recordStatus("已识别聊天页，但接口设置读取失败")
            overlay?.render(plan, memory, "读取接口设置失败")
            return
        }
        if (settings.mode == EndpointMode.JEV && settings.apiKey.isBlank()) {
            recordStatus("已识别聊天页，但尚未保存 Jev API Key")
            overlay?.render(plan, memory, "请先在 Jev · QQ 中填写 API Key")
            return
        }
        overlay?.render(plan, memory)
        recordStatus(when {
            plan.rows.isEmpty() -> "已识别聊天页，但没有可见消息节点"
            plan.targets.isEmpty() -> "已识别聊天页，但没有可判断的对方文字"
            else -> "已识别聊天页，悬浮面板已显示；对方文字 ${plan.targets.size} 条"
        })
        analyze(plan, generation, settings)
    }

    private fun analyze(plan: VisiblePlan, token: Long, settings: ConnectionSettings) {
        val currentSpec = spec ?: return
        val targets = plan.targets.filter { row -> row.key?.let { memory.verdict(it) == null } ?: false }
        if (targets.isEmpty()) return
        try { settings.endpoint() }
        catch (error: IllegalArgumentException) {
            overlay?.render(plan, memory, error.message ?: "接口 URL 无效")
            return
        }
        val client = DecisionClient(settings, currentSpec)
        for (row in targets) {
            val key = row.key ?: continue
            if (!inFlight.add(key)) continue
            worker.execute {
                if (token != generation) return@execute
                val outcome = runCatching {
                    client.judge(row.message.text, row.context, row.message.quote)
                }
                main.post {
                    if (token != generation || visiblePlan !== plan) return@post
                    inFlight.remove(key)
                    outcome.onSuccess { memory.put(key, it) }
                    val error = outcome.exceptionOrNull()?.let { failure ->
                        when (failure) {
                            is IllegalArgumentException, is IllegalStateException -> failure.message
                            else -> "网络连接失败"
                        }
                    }
                    overlay?.render(plan, memory, error)
                }
            }
        }
    }

    private fun clearVisible() {
        main.removeCallbacks(capture)
        captureScheduled = false
        generation++
        fingerprint = ""
        visiblePlan = null
        inFlight.clear()
        overlay?.hide()
    }

    private fun recordStatus(status: String) {
        if (status == lastStatus) return
        lastStatus = status
        getSharedPreferences("capture_diagnostic", MODE_PRIVATE).edit()
            .putString("last_qq_status", status).apply()
    }

    private fun copyTree(node: AccessibilityNodeInfo, depth: Int, count: IntArray): UiNode {
        val rect = Rect()
        node.getBoundsInScreen(rect)
        val children = ArrayList<UiNode>()
        if (depth < 48 && count[0]++ < 5000) {
            for (index in 0 until node.childCount) {
                if (count[0] >= 5000) break
                val child = node.getChild(index) ?: continue
                children.add(copyTree(child, depth + 1, count))
            }
        }
        return UiNode(
            id = node.viewIdResourceName.orEmpty(),
            text = node.text?.toString().orEmpty(),
            description = node.contentDescription?.toString().orEmpty(),
            bounds = UiBounds(rect.left, rect.top, rect.right, rect.bottom),
            visible = node.isVisibleToUser,
            children = children,
            className = node.className?.toString().orEmpty(),
        )
    }

    private companion object {
        const val QQ_PACKAGE = "com.tencent.mobileqq"
    }
}
