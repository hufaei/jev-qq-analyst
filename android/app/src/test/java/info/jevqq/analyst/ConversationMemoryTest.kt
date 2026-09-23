package info.jevqq.analyst

import org.junit.Assert.*
import org.junit.Test

class ConversationMemoryTest {
    private fun message(side: Side, text: String, kind: Kind = Kind.TEXT) =
        ChatMessage(side, text, kind, UiBounds(100, 100, 300, 140))

    @Test fun contextUsesPriorTextOnlyAndBoundsTurnsAndCharacters() {
        val rows = (1..10).map { message(if (it % 2 == 0) Side.ME else Side.THEM, "m$it") }
        val media = message(Side.THEM, "【文件】", Kind.FILE)
        val target = message(Side.THEM, "现在呢")
        val visible = rows + media + target
        val memory = ConversationMemory()
        memory.observe("chat", visible)
        val context = memory.context("chat", target, visible)
        assertEquals(8, context!!.lines().size)
        assertFalse(context.lines().any { it.endsWith("m1") })
        assertFalse(context.contains("【文件】"))
        assertTrue(context.contains("我: m10"))
        assertEquals(12, memory.context("chat", target, visible, maxChars = 12)!!.length)
    }

    @Test fun cacheKeySeparatesRepeatedShortReplyByPreviousIncoming() {
        val message = message(Side.THEM, "好的")
        val memory = ConversationMemory(maxVerdicts = 2)
        val first = memory.key("chat", message, "第一次")
        val second = memory.key("chat", message, "第二次")
        assertNotEquals(first, second)
        val verdict = Verdict("闲聊", .8, 2.0, "", 0.0, "", 0.0, "", 0.0,
            emptyList(), null, emptyList(), emptyList(), "jev-latest")
        memory.put(first, verdict)
        assertNull(memory.verdict(second))
        assertEquals(verdict, memory.verdict(first))
    }

    @Test fun earlierDuplicateDoesNotReceiveLaterContext() {
        val first = message(Side.THEM, "好的")
        val future = message(Side.ME, "下一步")
        val last = message(Side.THEM, "好的")
        val memory = ConversationMemory()
        memory.observe("chat", listOf(first, future, last))
        assertNull(memory.context("chat", first, listOf(first)))
    }
}
