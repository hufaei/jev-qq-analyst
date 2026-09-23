package info.jevqq.analyst

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class VisiblePlanTest {
    private fun message(side: Side, text: String, kind: Kind = Kind.TEXT) =
        ChatMessage(side, text, kind, UiBounds(100, 100, 300, 140))

    @Test fun onlyCounterpartTextBecomesJudgmentTarget() {
        val snapshot = ChatSnapshot("小王", listOf(
            message(Side.ME, "我先查查"),
            message(Side.THEM, "能今天好吗"),
            message(Side.UNKNOWN, "方向不明"),
            message(Side.THEM, "【图片】", Kind.IMAGE),
            message(Side.THEM, "谢谢"),
        ))
        val plan = VisiblePlan.from(snapshot, ConversationMemory())
        assertEquals(listOf("我先查查", "能今天好吗", "【图片】", "谢谢"),
            plan.rows.map { it.message.text })
        assertEquals(listOf("能今天好吗", "谢谢"), plan.targets.map { it.message.text })
        assertNull(plan.rows[2].key)
        assertEquals("能今天好吗", plan.targets[1].key!!.previousIncoming)
    }
}
