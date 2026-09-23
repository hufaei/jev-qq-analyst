package info.jevqq.analyst

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class QqParserTest {
    private val parser = QqParser(1000, 1800)

    private fun box(l: Int, t: Int, r: Int, b: Int) = UiBounds(l, t, r, b)
    private fun node(
        id: String = "", text: String = "", description: String = "",
        bounds: UiBounds = box(0, 0, 1000, 1800), visible: Boolean = true,
        vararg children: UiNode,
    ) = UiNode(id, text, description, bounds, visible, children.toList())

    private fun chat(vararg children: UiNode): UiNode = node(
        children = arrayOf(node(id = "com.tencent.mobileqq:id/input", bounds = box(0, 1600, 1000, 1700)), *children),
    )

    @Test fun requiresChatMarkerEvenWhenBubbleIdExists() {
        val bubble = node(id = "com.tencent.mobileqq:id/mjn", text = "聊天列表预览", bounds = box(130, 200, 300, 240))
        assertNull(parser.parse(node(children = arrayOf(bubble))))
        assertEquals(emptyList<ChatMessage>(), parser.parse(chat())!!.messages)
    }

    @Test fun readsVisibleBodyNodesInScreenOrder() {
        val later = node(id = "com.tencent.mobileqq:id/mjn", text = "第二条", bounds = box(130, 500, 300, 540))
        val earlier = node(id = "com.tencent.mobileqq:id/mjn", text = "第一条", bounds = box(130, 300, 300, 340))
        val hidden = node(id = "com.tencent.mobileqq:id/mjn", text = "旧消息", bounds = box(130, -300, 300, -260))
        val invisible = node(id = "com.tencent.mobileqq:id/mjn", text = "未显示", bounds = box(130, 400, 300, 440), visible = false)
        val title = node(id = "com.tencent.mobileqq:id/371", text = "小王", bounds = box(400, 80, 600, 120))
        val snapshot = parser.parse(chat(later, hidden, earlier, invisible, title))!!
        assertEquals("小王", snapshot.title)
        assertEquals(listOf("第一条", "第二条"), snapshot.messages.map { it.text })
        assertEquals(listOf(Side.THEM, Side.THEM), snapshot.messages.map { it.side })
    }

    @Test fun qq9365TitleDoesNotConfuseSecondaryHeaderText() {
        val title = node(id = "com.tencent.mobileqq:id/3g3", text = "小王",
            bounds = box(103, 134, 238, 196))
        val secondary = node(id = "com.tencent.mobileqq:id/j64", text = "在线状态",
            bounds = box(103, 201, 423, 233))
        val body = node(id = "com.tencent.mobileqq:id/mjn", text = "你好",
            bounds = box(140, 1573, 651, 1728))
        val snapshot = QqParser(1080, 2376).parse(chat(title, secondary, body))!!
        assertEquals("小王", snapshot.title)
        assertEquals(listOf("你好"), snapshot.messages.map { it.text })
    }

    @Test fun rowSenderEvidenceBeatsWideBubbleAndMisleadingAvatar() {
        val incoming = node(bounds = box(70, 200, 960, 290), children = arrayOf(
            node(id = "com.tencent.mobileqq:id/mjq", text = "小王", bounds = box(120, 205, 180, 225)),
            node(id = "com.tencent.mobileqq:id/mjn", text = "很长很长的一条消息", bounds = box(130, 230, 950, 280)),
            node(id = "avatar", description = "头像", bounds = box(940, 205, 980, 245)),
        ))
        val own = node(bounds = box(20, 350, 970, 430), children = arrayOf(
            node(id = "com.tencent.mobileqq:id/mjq", text = "我", bounds = box(40, 355, 70, 375)),
            node(id = "com.tencent.mobileqq:id/mjn", text = "我发的", bounds = box(700, 380, 940, 420)),
        ))
        assertEquals(listOf(Side.THEM, Side.ME), parser.parse(chat(incoming, own))!!.messages.map { it.side })
    }

    @Test fun avatarCanResolveWideBodyButAmbiguousWideBodyStaysUnknown() {
        val withAvatar = node(bounds = box(70, 200, 960, 290), children = arrayOf(
            node(id = "avatar", description = "头像", bounds = box(80, 210, 120, 250)),
            node(id = "com.tencent.mobileqq:id/mjn", text = "横跨屏幕", bounds = box(140, 220, 950, 270)),
        ))
        val uncertain = node(id = "com.tencent.mobileqq:id/mjn", text = "没有可信方向", bounds = box(100, 400, 900, 450))
        assertEquals(listOf(Side.THEM, Side.UNKNOWN), parser.parse(chat(withAvatar, uncertain))!!.messages.map { it.side })
    }

    @Test fun qqProfileCardAvatarResolvesLongIncomingBubble() {
        val longIncoming = node(id = "com.tencent.mobileqq:id/root", bounds = box(0, 1182, 1220, 1718), children = arrayOf(
            node(id = "com.tencent.mobileqq:id/wam", bounds = box(36, 1310, 158, 1432), children = arrayOf(
                node(description = "联系人的资料卡", bounds = box(36, 1310, 158, 1432)),
            )),
            node(id = "com.tencent.mobileqq:id/p1j", bounds = box(158, 1286, 1026, 1718), children = arrayOf(
                node(id = "com.tencent.mobileqq:id/mjn", text = "很长的消息".repeat(12),
                    bounds = box(158, 1286, 1026, 1718)),
            )),
        ))
        val snapshot = QqParser(1220, 2656).parse(chat(longIncoming))!!
        assertEquals(Side.THEM, snapshot.messages.single().side)
    }

    @Test fun separatesExplicitQuoteAndIgnoresQuotedSender() {
        val row = node(bounds = box(650, 200, 960, 380), children = arrayOf(
            node(description = "引用消息", bounds = box(700, 215, 940, 290), children = arrayOf(
                node(id = "com.tencent.mobileqq:id/mjq", text = "小王", bounds = box(710, 220, 760, 240)),
                node(text = "昨天的话", bounds = box(710, 245, 850, 270)),
            )),
            node(id = "com.tencent.mobileqq:id/mjq", text = "我的昵称", bounds = box(900, 200, 955, 220)),
            node(id = "com.tencent.mobileqq:id/mjn", text = "这次新说的话", bounds = box(720, 310, 940, 350)),
        ))
        val message = parser.parse(chat(row))!!.messages.single()
        assertEquals(Side.ME, message.side)
        assertEquals("这次新说的话", message.text)
        assertEquals("小王\n昨天的话", message.quote)
    }

    @Test fun mediaUsesFixedPlaceholderAndNeverFilenameOrAvatar() {
        val row = node(bounds = box(50, 200, 450, 300), children = arrayOf(
            node(id = "avatar", description = "头像", bounds = box(60, 210, 100, 250)),
            node(id = "com.tencent.mobileqq:id/file", description = "文件", bounds = box(130, 210, 400, 280)),
            node(id = "com.tencent.mobileqq:id/mjn", text = "private-contract.pdf", bounds = box(150, 230, 380, 260)),
        ))
        val message = parser.parse(chat(row))!!.messages.single()
        assertEquals(Kind.FILE, message.kind)
        assertEquals("【文件】", message.text)
        assertEquals(Side.THEM, message.side)
    }

    @Test fun mediaInQuoteIsNotNewMessage() {
        val row = node(bounds = box(50, 200, 450, 380), children = arrayOf(
            node(description = "引用消息", bounds = box(130, 210, 400, 280), children = arrayOf(
                node(id = "com.tencent.mobileqq:id/image", description = "图片", bounds = box(150, 220, 240, 270)),
            )),
            node(id = "com.tencent.mobileqq:id/mjn", text = "新正文", bounds = box(140, 300, 300, 340)),
        ))
        val message = parser.parse(chat(row))!!.messages.single()
        assertEquals(Kind.TEXT, message.kind)
        assertEquals("新正文", message.text)
    }

    @Test fun adjacentMediaRowsRemainSeparate() {
        val group = node(bounds = box(50, 200, 500, 420), children = arrayOf(
            node(bounds = box(50, 210, 450, 290), children = arrayOf(
                node(id = "avatar", description = "头像", bounds = box(60, 220, 100, 260)),
                node(id = "com.tencent.mobileqq:id/image", description = "图片", bounds = box(140, 220, 250, 280)),
            )),
            node(bounds = box(50, 310, 450, 390), children = arrayOf(
                node(id = "avatar", description = "头像", bounds = box(60, 320, 100, 360)),
                node(id = "com.tencent.mobileqq:id/file", description = "文件", bounds = box(140, 320, 250, 380)),
            )),
        ))
        val messages = parser.parse(chat(group))!!.messages
        assertEquals(listOf(Kind.IMAGE, Kind.FILE), messages.map { it.kind })
        assertEquals(listOf("【图片】", "【文件】"), messages.map { it.text })
    }

    @Test fun adjacentTextAndMediaRowsRemainSeparate() {
        val group = node(bounds = box(50, 200, 500, 430), children = arrayOf(
            node(bounds = box(50, 210, 450, 290), children = arrayOf(
                node(id = "com.tencent.mobileqq:id/mjn", text = "前一条文字", bounds = box(140, 220, 350, 270)),
            )),
            node(bounds = box(50, 320, 450, 400), children = arrayOf(
                node(id = "avatar", description = "头像", bounds = box(60, 330, 100, 370)),
                node(id = "com.tencent.mobileqq:id/image", description = "图片", bounds = box(140, 330, 250, 390)),
            )),
        ))
        val messages = parser.parse(chat(group))!!.messages
        assertEquals(listOf("前一条文字", "【图片】"), messages.map { it.text })
        assertEquals(listOf(Kind.TEXT, Kind.IMAGE), messages.map { it.kind })
    }

    @Test fun attachmentPickerButtonIsNotAChatMessage() {
        val picker = node(bounds = box(20, 1350, 200, 1440), children = arrayOf(
            node(id = "com.tencent.mobileqq:id/image", description = "图片", bounds = box(40, 1370, 120, 1420)),
        ))
        assertEquals(emptyList<ChatMessage>(), parser.parse(chat(picker))!!.messages)
    }
}
