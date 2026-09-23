package info.jevqq.analyst

internal data class VerdictKey(
    val chat: String,
    val sender: Side,
    val text: String,
    val quote: String,
    val previousIncoming: String,
)

/** Bounded, process-only message context and verdict cache. */
internal class ConversationMemory(
    private val maxChats: Int = 24,
    private val maxRows: Int = 32,
    private val maxVerdicts: Int = 128,
) {
    private val histories = object : LinkedHashMap<String, List<ChatMessage>>(32, .75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, List<ChatMessage>>?) =
            size > maxChats
    }
    private val verdicts = object : LinkedHashMap<VerdictKey, Verdict>(160, .75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<VerdictKey, Verdict>?) =
            size > maxVerdicts
    }

    @Synchronized fun observe(chat: String, visible: List<ChatMessage>) {
        val text = visible.filter { it.kind == Kind.TEXT && it.side != Side.UNKNOWN }
        if (text.isEmpty()) return
        val prior = histories[chat].orEmpty()
        val overlap = (minOf(prior.size, text.size) downTo 1).firstOrNull { length ->
            prior.takeLast(length).map(::signature) == text.take(length).map(::signature)
        } ?: 0
        // A jump without overlap is not evidence that unseen rows followed the last view.
        histories[chat] = (if (overlap > 0) prior + text.drop(overlap) else text).takeLast(maxRows)
    }

    @Synchronized fun context(
        chat: String,
        target: ChatMessage,
        visible: List<ChatMessage>,
        turns: Int = 8,
        maxChars: Int = 2000,
    ): String? {
        val seen = histories[chat].orEmpty()
        val visibleText = visible.filter { it.kind == Kind.TEXT && it.side != Side.UNKNOWN }
        val visibleIndex = visibleText.indexOfFirst { it === target }
        if (visibleIndex < 0) return null
        val matches = if (seen.size >= visibleText.size) {
            (0..seen.size - visibleText.size).filter { start ->
                visibleText.indices.all { index ->
                    signature(seen[start + index]) == signature(visibleText[index])
                }
            }
        } else emptyList()
        // If repeated text admits multiple positions, only the actual visible
        // prefix is safe; an arbitrary match could include future messages.
        val prior = if (matches.size == 1) seen.take(matches.single() + visibleIndex)
            else visibleText.take(visibleIndex)
        return prior.filter { it.kind == Kind.TEXT && it.side != Side.UNKNOWN }
            .takeLast(turns)
            .joinToString("\n") { item ->
                val speaker = if (item.side == Side.ME) "我" else "对方"
                "$speaker: ${if (item.quote.isEmpty()) "" else "[引用：${item.quote}] "}${item.text}"
            }.takeLast(maxChars).ifEmpty { null }
    }

    @Synchronized fun key(chat: String, message: ChatMessage, previousIncoming: String = "") =
        VerdictKey(chat, message.side, message.text, message.quote, previousIncoming)

    @Synchronized fun verdict(key: VerdictKey): Verdict? = verdicts[key]

    @Synchronized fun put(key: VerdictKey, verdict: Verdict) {
        verdicts[key] = verdict
    }

    @Synchronized fun clearChat(chat: String) {
        histories.remove(chat)
        verdicts.keys.removeAll { it.chat == chat }
    }

    @Synchronized fun clear() {
        histories.clear()
        verdicts.clear()
    }

    private fun signature(message: ChatMessage) =
        listOf(message.side, message.text, message.quote)
}
