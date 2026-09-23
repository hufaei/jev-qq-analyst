package info.jevqq.analyst

internal data class DisplayRow(
    val message: ChatMessage,
    val key: VerdictKey? = null,
    val context: String? = null,
)

/** One immutable pass over the messages currently visible in the QQ chat. */
internal data class VisiblePlan(
    val chat: String,
    val rows: List<DisplayRow>,
) {
    val targets: List<DisplayRow> get() = rows.filter { it.key != null }

    companion object {
        fun from(snapshot: ChatSnapshot, memory: ConversationMemory): VisiblePlan {
            val chat = snapshot.title?.trim().orEmpty()
            var previousIncoming = ""
            val rows = snapshot.messages.filter { it.side != Side.UNKNOWN }.map { message ->
                if (message.kind == Kind.TEXT && message.side == Side.THEM) {
                    val key = memory.key(chat, message, previousIncoming)
                    val context = memory.context(chat, message, snapshot.messages)
                    previousIncoming = message.text
                    DisplayRow(message, key, context)
                } else {
                    DisplayRow(message)
                }
            }
            memory.observe(chat, snapshot.messages)
            return VisiblePlan(chat, rows)
        }
    }
}
