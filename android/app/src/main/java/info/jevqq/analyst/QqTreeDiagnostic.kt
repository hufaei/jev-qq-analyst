package info.jevqq.analyst

import org.json.JSONArray
import org.json.JSONObject

/** A one-shot structural report. Never serializes node text or content descriptions. */
internal object QqTreeDiagnostic {
    fun report(root: UiNode, width: Int, height: Int, model: String,
               sdk: Int, qqVersion: String): String {
        val nodes = JSONArray()
        var truncated = false
        fun walk(node: UiNode, parent: Int, depth: Int) {
            if (nodes.length() >= 5000) {
                truncated = true
                return
            }
            val index = nodes.length()
            nodes.put(JSONObject().apply {
                put("n", index)
                put("p", parent)
                put("d", depth)
                put("id", node.id)
                put("class", node.className)
                put("b", JSONArray().put(node.bounds.left).put(node.bounds.top)
                    .put(node.bounds.right).put(node.bounds.bottom))
                put("v", node.visible)
                put("t", node.text.isNotBlank())
                put("a", node.description.isNotBlank())
                put("children", node.children.size)
            })
            node.children.forEach { walk(it, index, depth + 1) }
        }
        walk(root, -1, 0)
        return JSONObject().apply {
            put("format", "jev-qq-ui-tree-v1")
            put("device", model)
            put("sdk", sdk)
            put("qq", qqVersion)
            put("screen", JSONArray().put(width).put(height))
            put("truncated", truncated)
            put("nodes", nodes)
        }.toString()
    }
}
