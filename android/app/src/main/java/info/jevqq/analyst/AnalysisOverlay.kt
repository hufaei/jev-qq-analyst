package info.jevqq.analyst

import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Point
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.io.File
import java.util.Locale
import kotlin.math.roundToInt

/** Compact, touch-contained view of the current chat. Never places controls inside QQ. */
internal class AnalysisOverlay(private val context: Context) {
    private val manager = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
    private val saved = context.getSharedPreferences("overlay_layout", Context.MODE_PRIVATE)
    private val screen = Point().also {
        @Suppress("DEPRECATION")
        manager.defaultDisplay.getRealSize(it)
    }
    private var panelWidth = saved.getInt("width", dp(240)).coerceIn(dp(180), minOf(dp(350), screen.x - dp(16)))
    private var bodyHeight = saved.getInt("height", dp(240)).coerceIn(dp(140), (screen.y * .65).roundToInt())
    private val params = WindowManager.LayoutParams(
        panelWidth, WindowManager.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
            WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
        PixelFormat.TRANSLUCENT,
    ).apply {
        gravity = Gravity.TOP or Gravity.LEFT
        x = saved.getInt("x", screen.x - panelWidth - dp(8)).coerceIn(0, screen.x - panelWidth)
        y = saved.getInt("y", dp(72)).coerceIn(0, screen.y - dp(90))
    }
    private val overlayFont = runCatching {
        Typeface.createFromFile(File("/system/fonts/NotoSansCJK-Regular.ttc"))
    }.getOrElse { Typeface.create("sans-serif", Typeface.NORMAL) }
    private val surface = LinearLayout(context).apply {
        orientation = LinearLayout.VERTICAL
        background = rounded(PAPER, 20)
        elevation = dp(14).toFloat()
    }
    private val title = text("Jev · QQ", 15f, INK, true)
    private val subtitle = text("当前会话", 11f, MUTED)
    private val toggle = text("−", 24f, INK, true)
    private val resize = text("拖动调整大小  ↘", 10f, MUTED)
    private val body = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
    private val scroll = ScrollView(context).apply {
        isFillViewport = false
        addView(body)
    }
    private var attached = false
    private var collapsed = false

    init {
        val header = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(14), dp(10), dp(10), dp(10))
            setOnTouchListener(dragListener())
        }
        val labels = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            addView(title)
            addView(subtitle)
        }
        header.addView(labels, LinearLayout.LayoutParams(0, -2, 1f))
        toggle.gravity = Gravity.CENTER
        toggle.setOnClickListener {
            collapsed = !collapsed
            scroll.visibility = if (collapsed) View.GONE else View.VISIBLE
            resize.visibility = if (collapsed) View.GONE else View.VISIBLE
            toggle.text = if (collapsed) "+" else "−"
        }
        header.addView(toggle, LinearLayout.LayoutParams(dp(36), dp(36)))
        surface.addView(header)
        surface.addView(scroll, LinearLayout.LayoutParams(-1, bodyHeight))
        resize.gravity = Gravity.END
        resize.setPadding(dp(12), dp(10), dp(12), dp(14))
        resize.setOnTouchListener(resizeListener())
        surface.addView(resize)
    }

    fun render(plan: VisiblePlan, memory: ConversationMemory, error: String? = null) {
        title.text = plan.chat.ifBlank { "Jev · QQ" }
        val ready = plan.targets.count { row -> row.key?.let(memory::verdict) != null }
        subtitle.text = if (plan.targets.isEmpty()) "当前窗口没有可分析的对方文字"
            else "对方 ${plan.targets.size} 条 · 已分析 $ready 条"
        body.removeAllViews()
        if (error != null) {
            body.addView(text(error, 12f, DANGER).apply {
                setPadding(dp(14), dp(6), dp(14), dp(10))
            })
        }
        if (plan.rows.isEmpty()) {
            body.addView(text("当前聊天没有可分析的可见消息", 12f, MUTED).apply {
                setPadding(dp(14), dp(18), dp(14), dp(18))
            })
        }
        var peerIndex = 0
        plan.rows.forEach { row ->
            if (row.key != null) peerIndex++
            addRow(row, row.key?.let(memory::verdict), error != null,
                peerIndex)
        }
        scroll.layoutParams = (scroll.layoutParams as LinearLayout.LayoutParams).apply {
            height = if (plan.rows.isEmpty()) dp(78) else bodyHeight
        }
        show()
    }

    fun hide() {
        if (!attached) return
        manager.removeView(surface)
        attached = false
    }

    fun dispose() = hide()

    private fun show() {
        if (attached) return
        manager.addView(surface, params)
        attached = true
    }

    private fun dragListener() = object : View.OnTouchListener {
        private var downX = 0f
        private var downY = 0f
        private var startX = 0
        private var startY = 0

        override fun onTouch(view: View, event: MotionEvent): Boolean {
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = event.rawX
                    downY = event.rawY
                    startX = params.x
                    startY = params.y
                }
                MotionEvent.ACTION_MOVE -> {
                    params.x = (startX + (event.rawX - downX).roundToInt()).coerceIn(0, screen.x - panelWidth)
                    params.y = (startY + (event.rawY - downY).roundToInt())
                        .coerceIn(0, (screen.y - surface.height).coerceAtLeast(0))
                    if (attached) manager.updateViewLayout(surface, params)
                }
                MotionEvent.ACTION_UP -> saveLayout()
            }
            return true
        }
    }

    private fun resizeListener() = object : View.OnTouchListener {
        private var downX = 0f
        private var downY = 0f
        private var startWidth = 0
        private var startHeight = 0

        override fun onTouch(view: View, event: MotionEvent): Boolean {
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = event.rawX
                    downY = event.rawY
                    startWidth = panelWidth
                    startHeight = bodyHeight
                }
                MotionEvent.ACTION_MOVE -> {
                    panelWidth = (startWidth + (event.rawX - downX).roundToInt())
                        .coerceIn(dp(180), minOf(dp(350), screen.x - dp(16)))
                    bodyHeight = (startHeight + (event.rawY - downY).roundToInt())
                        .coerceIn(dp(140), (screen.y * .65).roundToInt())
                    params.width = panelWidth
                    params.x = params.x.coerceAtMost(screen.x - panelWidth)
                    scroll.layoutParams = (scroll.layoutParams as LinearLayout.LayoutParams).apply {
                        height = bodyHeight
                    }
                    if (attached) manager.updateViewLayout(surface, params)
                }
                MotionEvent.ACTION_UP -> saveLayout()
            }
            return true
        }
    }

    private fun saveLayout() {
        saved.edit().putInt("x", params.x).putInt("y", params.y)
            .putInt("width", panelWidth).putInt("height", bodyHeight).apply()
    }

    private fun addRow(row: DisplayRow, verdict: Verdict?, unavailable: Boolean, peerIndex: Int) {
        val message = row.message
        val own = message.side == Side.ME
        val card = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(11), dp(8), dp(11), dp(9))
            background = rounded(if (own) OWN else WHITE, 12,
                if (own) OWN_EDGE else EDGE)
        }
        card.addView(text(when {
            own && message.kind == Kind.TEXT -> "我 · 上下文"
            own -> "我 · 仅显示"
            message.kind != Kind.TEXT -> "对方 · 仅显示"
            else -> "对方 · $peerIndex"
        }, 10f, if (own) OWN_TEXT else MUTED, true))
        if (message.quote.isNotBlank()) {
            card.addView(text("引用 · ${message.quote.take(120)}", 10f, MUTED).apply {
                setPadding(0, dp(4), 0, 0)
            })
        }
        card.addView(text(message.text.take(260), 13f, if (own) OWN_TEXT else INK).apply {
            setPadding(0, dp(5), 0, 0)
            maxLines = if (row.key != null) 3 else 2
            ellipsize = android.text.TextUtils.TruncateAt.END
        })
        if (row.key != null) {
            if (verdict == null) {
                card.addView(text(if (unavailable) "暂不可用" else "分析中…", 11f, MUTED).apply {
                    setPadding(0, dp(10), 0, 0)
                })
            } else {
                addVerdict(card, verdict)
            }
        }
        val margins = LinearLayout.LayoutParams(-1, -2).apply {
            setMargins(if (own) dp(24) else dp(10), dp(4), dp(10), dp(4))
        }
        body.addView(card, margins)
    }

    private fun addVerdict(card: LinearLayout, verdict: Verdict) {
        val emotion = verdict.emotion.takeIf { verdict.emotionConfidence >= .45 && it.isNotBlank() }
            ?: "难判断"
        card.addView(text("${verdict.intent} · $emotion", 14f, INK, true).apply {
            setPadding(0, dp(11), 0, dp(3))
        })
        val risk = verdict.risk.roundToInt()
        card.addView(text("回复风险 $risk/9", 11f,
            if (risk <= 3) GREEN else if (risk <= 6) AMBER else DANGER, true))

        val reply = verdict.replyProbability
        val replyColor = when {
            reply == null -> PROB_LOW_BG
            reply < .40 -> PROB_HIGH_BG
            reply <= .60 -> REPLY_WAIT_BG
            else -> REPLY_YES_BG
        }
        val replyText = if (reply == null) "是否值得回复 · 待判断"
            else "是否值得回复 · ${(reply * 100).roundToInt()}%"
        card.addView(pill(replyText, replyColor,
            if (reply == null) MUTED else if (reply < .40) PROB_HIGH_TEXT
            else if (reply <= .60) AMBER else GREEN).apply {
            layoutParams = LinearLayout.LayoutParams(-1, -2).apply {
                topMargin = dp(9)
            }
        })

        card.addView(text("意图可能", 10f, MUTED).apply { setPadding(0, dp(11), 0, dp(3)) })
        verdict.rankedIntents.take(3).forEach { intent ->
            val ink = when {
                intent.probability >= .60 -> PROB_HIGH_TEXT
                intent.probability >= .25 -> PROB_MID_TEXT
                else -> MUTED
            }
            val bg = when {
                intent.probability >= .60 -> PROB_HIGH_BG
                intent.probability >= .25 -> PROB_MID_BG
                else -> PROB_LOW_BG
            }
            card.addView(pill("${intent.label} ${(intent.probability * 100).roundToInt()}%", bg, ink).apply {
                layoutParams = LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(3) }
            })
        }

        val behavior = verdict.behavior.takeIf { verdict.behaviorConfidence >= .45 && it.isNotBlank() } ?: "—"
        val need = verdict.need.takeIf { verdict.needConfidence >= .45 && it.isNotBlank() } ?: "—"
        card.addView(text("行为 · $behavior    需要 · $need", 10f, OWN_TEXT).apply {
            setPadding(0, dp(11), 0, 0)
        })
        if (verdict.signals.isNotEmpty()) card.addView(text(
            verdict.signals.take(2).joinToString(" · "), 10f, MUTED).apply {
            setPadding(0, dp(6), 0, 0)
        })

        card.addView(text("下一步", 10f, MUTED, true).apply {
            setPadding(0, dp(11), 0, dp(3))
        })
        if (verdict.rankedActions.isEmpty()) {
            card.addView(text("暂无可靠建议", 10f, MUTED))
        } else verdict.rankedActions.take(3).forEachIndexed { index, action ->
            val row = LinearLayout(context).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(8), dp(5), dp(8), dp(5))
                background = rounded(ACTION_BG, 6)
                addView(text("%02d".format(Locale.ROOT, index + 1), 10f, MUTED),
                    LinearLayout.LayoutParams(dp(25), -2))
                addView(text(action.label, 10f, INK), LinearLayout.LayoutParams(0, -2, 1f))
                addView(text("%.1f/4".format(Locale.ROOT, action.score), 10f,
                    if (action.score >= 3) GREEN else if (action.score >= 2) AMBER else MUTED, true))
            }
            card.addView(row, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(3) })
        }
    }

    private fun pill(value: String, background: Int, ink: Int) = text(value, 10f, ink, true).apply {
        setPadding(dp(9), dp(5), dp(9), dp(5))
        this.background = rounded(background, 8)
    }

    private fun text(value: String, size: Float, color: Int, bold: Boolean = false) = TextView(context).apply {
        text = value
        textSize = size * .85f
        includeFontPadding = false
        setTextColor(color)
        typeface = Typeface.create(overlayFont, if (bold) Typeface.BOLD else Typeface.NORMAL)
    }

    private fun rounded(color: Int, radius: Int, border: Int? = null) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(radius).toFloat()
        if (border != null) setStroke(dp(1), border)
    }

    private fun dp(value: Int) = (value * context.resources.displayMetrics.density).roundToInt()

    private companion object {
        val PAPER = Color.argb(228, 244, 245, 245)
        val WHITE = Color.argb(245, 255, 255, 255)
        val OWN = Color.argb(241, 239, 241, 242)
        val OWN_EDGE = Color.rgb(226, 230, 232)
        val EDGE = Color.rgb(220, 224, 226)
        val INK = Color.rgb(35, 39, 43)
        val OWN_TEXT = Color.rgb(88, 99, 106)
        val MUTED = Color.rgb(114, 121, 126)
        val GREEN = Color.rgb(57, 130, 105)
        val AMBER = Color.rgb(155, 111, 45)
        val DANGER = Color.rgb(178, 73, 82)
        val PROB_HIGH_BG = Color.rgb(226, 237, 242)
        val PROB_HIGH_TEXT = Color.rgb(49, 91, 112)
        val PROB_MID_BG = Color.rgb(234, 240, 243)
        val PROB_MID_TEXT = Color.rgb(82, 109, 123)
        val PROB_LOW_BG = Color.rgb(241, 243, 244)
        val REPLY_YES_BG = Color.rgb(227, 240, 233)
        val REPLY_WAIT_BG = Color.rgb(245, 235, 220)
        val ACTION_BG = Color.rgb(245, 246, 247)
    }
}
