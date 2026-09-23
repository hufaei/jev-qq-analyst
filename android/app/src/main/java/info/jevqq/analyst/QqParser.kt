package info.jevqq.analyst

/** A copied accessibility tree. No Android objects or callbacks cross into the parser. */
data class UiBounds(val left: Int, val top: Int, val right: Int, val bottom: Int) {
    val width: Int get() = right - left
    val height: Int get() = bottom - top
    val centerX: Int get() = (left + right) / 2

    fun onScreen(width: Int, height: Int): Boolean =
        this.width > 0 && this.height > 0 && right > 0 && bottom > 0 && left < width && top < height
}

data class UiNode(
    val id: String = "",
    val text: String = "",
    val description: String = "",
    val bounds: UiBounds,
    val visible: Boolean = true,
    val children: List<UiNode> = emptyList(),
    val className: String = "",
)

enum class Side { ME, THEM, UNKNOWN }
enum class Kind { TEXT, IMAGE, STICKER, FILE, ATTACHMENT }

data class ChatMessage(
    val side: Side,
    val text: String,
    val kind: Kind,
    val bounds: UiBounds,
    val quote: String = "",
)

data class ChatSnapshot(val title: String?, val messages: List<ChatMessage>)

/** Rules for QQ's currently materialized accessibility tree; ambiguous rows stay UNKNOWN. */
class QqParser(private val width: Int, private val height: Int) {
    private class Entry(val node: UiNode, val parent: Entry?, val inQuote: Boolean, val order: Int)

    fun parse(root: UiNode): ChatSnapshot? {
        if (width <= 0 || height <= 0) return null
        val entries = ArrayList<Entry>()
        fun walk(node: UiNode, parent: Entry?, quoted: Boolean, depth: Int) {
            if (entries.size >= 5000 || depth > 48 || !node.visible) return
            val entry = Entry(node, parent, quoted || isQuote(node), entries.size)
            entries.add(entry)
            node.children.forEach { walk(it, entry, entry.inQuote, depth + 1) }
        }
        walk(root, null, false, 0)
        // A message-looking node in a list or search result is not proof of an open chat.
        if (entries.none { it.node.id == INPUT_ID && shown(it.node) }) return null
        val title = entries.firstOrNull {
            it.node.id in TITLE_IDS && shown(it.node) && it.node.text.isNotBlank()
        }?.node?.text?.trim()

        val bodies = entries.filter {
            it.node.id == BODY_ID && !it.inQuote && shown(it.node) && it.node.text.isNotBlank()
        }
        val media = entries.filter {
            !it.inQuote && shown(it.node) && mediaKind(it.node) != null && !isAvatar(it.node)
        }.filter { item ->
            val row = rowFor(item, entries)
            // Picker controls also say “图片” or “文件”; require message-row evidence.
            entries.any { candidate ->
                candidate !== item && ancestorOf(row, candidate) && !candidate.inQuote &&
                    shown(candidate.node) && (isSender(candidate.node) || isAvatar(candidate.node) ||
                        candidate.node.id == BODY_ID)
            }
        }
        val items = ArrayList<Pair<Int, ChatMessage>>()
        val mediaRows = media.map { rowFor(it, entries) }.toSet()
        for (body in bodies) {
            val row = rowFor(body, entries)
            if (row in mediaRows) continue // A filename or caption cannot become a text judgment.
            items.add(body.order to ChatMessage(
                side = side(row, body.node.bounds, entries),
                text = body.node.text.trim(),
                kind = Kind.TEXT,
                bounds = body.node.bounds,
                quote = quote(row, entries),
            ))
        }
        // Collapse multiple media nodes in one message row to one fixed display placeholder.
        val seenRows = HashSet<Entry>()
        for (item in media) {
            val row = rowFor(item, entries)
            if (!seenRows.add(row)) continue
            val kind = mediaKind(item.node) ?: continue
            items.add(item.order to ChatMessage(
                side = side(row, item.node.bounds, entries),
                text = placeholder(kind),
                kind = kind,
                bounds = item.node.bounds,
            ))
        }
        return ChatSnapshot(title, items.sortedWith(compareBy<Pair<Int, ChatMessage>> { it.second.bounds.top }
            .thenBy { it.first }).map { it.second })
    }

    private fun shown(node: UiNode): Boolean = node.visible && node.bounds.onScreen(width, height)

    private fun ancestorOf(ancestor: Entry, item: Entry): Boolean {
        var cursor: Entry? = item
        while (cursor != null) {
            if (cursor === ancestor) return true
            cursor = cursor.parent
        }
        return false
    }

    private fun rowFor(item: Entry, entries: List<Entry>): Entry {
        val ancestors = generateSequence(item.parent) { it.parent }.filter {
            if (!shown(it.node) || it.node.bounds.height > height / 4) return@filter false
            val bodies = entries.filter { candidate ->
                candidate.node.id == BODY_ID && !candidate.inQuote && ancestorOf(it, candidate)
            }
            val media = entries.filter { candidate ->
                !candidate.inQuote && !isAvatar(candidate.node) &&
                    mediaKind(candidate.node) != null && ancestorOf(it, candidate)
            }
            if (bodies.size > 1 || media.size > 1) return@filter false
            // A compact parent may contain two adjacent message rows. They cannot
            // be collapsed into a single media row merely because each has one item.
            bodies.isEmpty() || media.isEmpty() ||
                bodies.single().node.bounds.top < media.single().node.bounds.bottom &&
                media.single().node.bounds.top < bodies.single().node.bounds.bottom
        }.toList()
        // The widest still-compact ancestor includes sender, avatar, and quote siblings.
        return ancestors.lastOrNull() ?: item
    }

    private fun side(row: Entry, content: UiBounds, entries: List<Entry>): Side {
        val members = entries.filter { ancestorOf(row, it) && !it.inQuote && shown(it.node) }
        val senders = members.filter { isSender(it.node) }
        if (senders.any { it.node.text.trim() == "我" || it.node.description.trim() == "我" }) return Side.ME
        val senderSides = senders.map { edgeSide(it.node.bounds) }.filter { it != Side.UNKNOWN }.distinct()
        if (senderSides.size == 1) return senderSides.single()
        if (senderSides.size > 1) return Side.UNKNOWN
        val avatars = members.filter { isAvatar(it.node) }.map { edgeSide(it.node.bounds) }
            .filter { it != Side.UNKNOWN }.distinct()
        if (avatars.size == 1) return avatars.single()
        if (avatars.size > 1) return Side.UNKNOWN
        // QQ anchors bubbles by the avatar columns. The center of a long
        // incoming bubble may sit to the right of the screen midpoint.
        val avatarEdge = width * .13
        val leftGap = kotlin.math.abs(content.left - avatarEdge)
        val rightGap = kotlin.math.abs((width - avatarEdge) - content.right)
        val margin = width * .05
        return when {
            leftGap + margin < rightGap -> Side.THEM
            rightGap + margin < leftGap -> Side.ME
            else -> Side.UNKNOWN
        }
    }

    private fun edgeSide(bounds: UiBounds): Side = when {
        bounds.centerX < width * .35 -> Side.THEM
        bounds.centerX > width * .65 -> Side.ME
        else -> Side.UNKNOWN
    }

    private fun quote(row: Entry, entries: List<Entry>): String {
        val quotes = entries.filter { it !== row && it.inQuote && ancestorOf(row, it) && shown(it.node) }
        return quotes.map { it.node.text.trim() }.filter { it.isNotEmpty() && it !in QUOTE_MARKERS }
            .distinct().joinToString("\n")
    }

    private fun isQuote(node: UiNode): Boolean = QUOTE_MARKERS.any {
        node.description.contains(it) || node.id.contains("quote", ignoreCase = true)
    }

    private fun isSender(node: UiNode): Boolean =
        node.id == SENDER_ID || node.id.endsWith(":id/sender") ||
            node.id.endsWith(":id/nickname")

    private fun isAvatar(node: UiNode): Boolean =
        node.id.contains("avatar", ignoreCase = true) || node.description.trim() == "头像" ||
            // Current QQ exposes the tappable avatar as a profile-card node.
            node.description.trim().endsWith("的资料卡")

    private fun mediaKind(node: UiNode): Kind? {
        val id = node.id.substringAfterLast('/')
        val description = node.description.trim()
        return when {
            id == "file" || description == "文件" || description == "文件消息" -> Kind.FILE
            id == "attachment" || description == "附件" -> Kind.ATTACHMENT
            id == "sticker" || description == "表情包" || description == "贴图" -> Kind.STICKER
            id == "image" || description == "图片" || description == "图像" -> Kind.IMAGE
            else -> null
        }
    }

    private fun placeholder(kind: Kind): String = when (kind) {
        Kind.IMAGE -> "【图片】"
        Kind.STICKER -> "【表情】"
        Kind.FILE -> "【文件】"
        Kind.ATTACHMENT -> "【附件】"
        Kind.TEXT -> error("Text has no media placeholder")
    }

    private companion object {
        const val BODY_ID = "com.tencent.mobileqq:id/mjn"
        const val SENDER_ID = "com.tencent.mobileqq:id/mjq"
        val TITLE_IDS = setOf("com.tencent.mobileqq:id/371", "com.tencent.mobileqq:id/3_z",
            "com.tencent.mobileqq:id/3g3")
        const val INPUT_ID = "com.tencent.mobileqq:id/input"
        val QUOTE_MARKERS = setOf("引用消息", "引用的消息", "回复消息", "回复的消息")
    }
}
