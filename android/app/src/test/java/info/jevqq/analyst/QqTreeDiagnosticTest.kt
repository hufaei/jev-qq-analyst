package info.jevqq.analyst

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class QqTreeDiagnosticTest {
    @Test fun reportKeepsStructureWithoutSerializingConversationOrKey() {
        val root = UiNode(bounds = UiBounds(0, 0, 1000, 1800), children = listOf(
            UiNode(id = "com.tencent.mobileqq:id/new_title", text = "私人会话",
                description = "私人备注", bounds = UiBounds(100, 90, 500, 160),
                className = "android.widget.TextView"),
            UiNode(id = "com.tencent.mobileqq:id/new_body", text = "apikey_secret",
                bounds = UiBounds(130, 300, 500, 400)),
        ))
        val report = QqTreeDiagnostic.report(root, 1000, 1800, "V2520A", 36, "9.3.55")
        assertFalse(report.contains("私人会话"))
        assertFalse(report.contains("私人备注"))
        assertFalse(report.contains("apikey_secret"))
        val json = JSONObject(report)
        assertEquals("jev-qq-ui-tree-v1", json.getString("format"))
        assertEquals("V2520A", json.getString("device"))
        assertEquals(3, json.getJSONArray("nodes").length())
        val title = json.getJSONArray("nodes").getJSONObject(1)
        assertEquals("com.tencent.mobileqq:id/new_title", title.getString("id"))
        assertTrue(title.getBoolean("t"))
        assertTrue(title.getBoolean("a"))
        assertEquals(0, title.getInt("p"))
    }
}
