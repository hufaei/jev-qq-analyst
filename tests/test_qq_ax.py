"""Synthetic regressions for the macOS QQ AX adapter; no real chat is read."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import qq_ax


class FakeTree:
    def __init__(self):
        self.attrs = {}
        self.rects = {}

    def node(self, name, role, rect=None, children=(), **attrs):
        self.attrs[name, qq_ax.AX.kAXRoleAttribute] = role
        self.attrs[name, qq_ax.AX.kAXChildrenAttribute] = list(children)
        for key, value in attrs.items():
            self.attrs[name, key] = value
        if rect is not None:
            self.rects[name] = rect
        return name

    def attr(self, node, name):
        return self.attrs.get((node, name))


class QQAccessibilityTests(unittest.TestCase):
    def test_live_bottom_uses_final_real_row_not_scroll_overlap(self):
        tree = FakeTree()
        tree.node("old", "AXGroup", (300, 100, 700, 1))
        tree.node("visible", "AXGroup", (300, 400, 700, 200))
        tree.node("container", "AXGroup", (300, 100, 700, 500),
                  children=("old", "visible"))
        tree.node("list", "AXGroup", (300, 100, 700, 500),
                  children=("container",))
        with (patch.object(qq_ax, "_ax_attr", side_effect=tree.attr),
              patch.object(qq_ax, "_ax_rect", side_effect=tree.rects.get)):
            self.assertTrue(qq_ax._at_live_bottom("list"))
            tree.node("later", "AXGroup", (300, 600, 700, 1))
            tree.attrs["container", qq_ax.AX.kAXChildrenAttribute] = ["old", "visible", "later"]
            self.assertFalse(qq_ax._at_live_bottom("list"))

    def test_unicode_and_named_image_emoji_but_not_generic_images(self):
        tree = FakeTree()
        value = qq_ax.AX.kAXValueAttribute
        description = qq_ax.AX.kAXDescriptionAttribute
        tree.node("unicode", "AXStaticText", (350, 200, 90, 20), **{value: "好呀🙂"})
        tree.node("named", "AXImage", (445, 200, 24, 24), **{description: "微笑"})
        tree.node("generic", "AXImage", (470, 200, 24, 24), **{description: "图片"})
        tree.node("row", "AXGroup", (300, 190, 700, 50),
                  children=("unicode", "named", "generic"))
        with (patch.object(qq_ax, "_ax_attr", side_effect=tree.attr),
              patch.object(qq_ax, "_ax_rect", side_effect=tree.rects.get)):
            values = [item[1] for item in qq_ax._static_text_nodes("row")]
        self.assertEqual(values, ["好呀🙂", "[微笑]"])

    def test_walk_reaches_web_area_and_message_list_through_groups(self):
        tree = FakeTree()
        tree.node("list", "AXGroup", description="ignored")
        tree.attrs["list", qq_ax.AX.kAXDescriptionAttribute] = "消息列表"
        tree.node("web", "AXWebArea", children=("g2",))
        tree.attrs["web", "AXURL"] = "app://./renderer/index.html#/main/message"
        tree.node("g2", "AXGroup", children=("list",))
        tree.node("g1", "AXGroup", children=("web",))
        tree.node("window", "AXWindow", children=("g1",))

        with patch.object(qq_ax, "_ax_attr", side_effect=tree.attr):
            web = qq_ax._find_web_area("window")
            self.assertEqual(web, "web")
            self.assertEqual(qq_ax._find_message_list(web), "list")

    def test_parser_uses_only_visible_message_rows_and_classifies_sides(self):
        tree = FakeTree()
        value = qq_ax.AX.kAXValueAttribute
        description = qq_ax.AX.kAXDescriptionAttribute
        tree.node("sender", "AXStaticText", (350, 180, 40, 16), **{value: "小王"})
        tree.node("sender_in", "AXGroup", (350, 180, 40, 16),
                  children=("sender",), **{description: "小王"})
        tree.node("sender_out", "AXGroup", (960, 260, 32, 32),
                  children=("avatar_out",), **{description: "我"})
        tree.node("incoming", "AXStaticText", (350, 202, 180, 22), **{value: "明天十点可以吗"})
        tree.node("outgoing", "AXStaticText", (820, 260, 130, 22), **{value: "可以，明天见"})
        tree.node("avatar_in", "AXImage", (310, 180, 32, 32))
        tree.node("avatar_out", "AXImage", (960, 260, 32, 32))
        tree.node("time", "AXStaticText", (625, 145, 50, 14), **{value: "10:30"})
        tree.node("time2", "AXStaticText", (625, 242, 50, 14), **{value: "10:31"})
        tree.node("offscreen", "AXStaticText", (350, 100, 90, 1), **{value: "旧消息"})
        tree.node("row_time", "AXGroup", (300, 140, 700, 30), children=("time",))
        tree.node("row_in", "AXGroup", (300, 175, 700, 60),
                  children=("avatar_in", "sender_in", "incoming"))
        tree.node("row_out", "AXGroup", (300, 240, 700, 60),
                  children=("time2", "outgoing", "sender_out"))
        tree.node("row_virtual", "AXGroup", (300, 100, 700, 1), children=("offscreen",))
        tree.node("rows", "AXGroup", (300, 100, 700, 500),
                  children=("row_virtual", "row_time", "row_in", "row_out"))
        tree.node("list", "AXGroup", (300, 100, 700, 500), children=("rows",))

        with (patch.object(qq_ax, "_ax_attr", side_effect=tree.attr),
              patch.object(qq_ax, "_ax_rect", side_effect=tree.rects.get)):
            messages = qq_ax._parse_message_list("list", (0, 0, 1000, 800))

        self.assertEqual([(m.side, m.sender, m.text) for m in messages], [
            ("them", "小王", "明天十点可以吗"),
            ("me", None, "可以，明天见"),
        ])
        self.assertTrue(all(0 <= m.x <= 1 and 0 <= m.y <= 1 for m in messages))

    def test_long_left_message_needs_left_avatar_not_text_alignment(self):
        tree = FakeTree()
        value = qq_ax.AX.kAXValueAttribute
        tree.node("avatar", "AXImage", (310, 200, 32, 32))
        tree.node("long", "AXStaticText", (350, 200, 590, 44),
                  **{value: "我来例假了，好难受。痛痛痛痛痛痛痛痛。"})
        tree.node("row", "AXGroup", (300, 190, 700, 65),
                  children=("avatar", "long"))
        tree.node("rows", "AXGroup", (300, 100, 700, 500), children=("row",))
        tree.node("list", "AXGroup", (300, 100, 700, 500), children=("rows",))
        with (patch.object(qq_ax, "_ax_attr", side_effect=tree.attr),
              patch.object(qq_ax, "_ax_rect", side_effect=tree.rects.get)):
            messages = qq_ax._parse_message_list("list", (0, 0, 1000, 800))
        self.assertEqual([message.side for message in messages], ["them"])

    def test_missing_ax_avatar_uses_clear_bubble_alignment(self):
        tree = FakeTree()
        value = qq_ax.AX.kAXValueAttribute
        tree.node("body", "AXStaticText", (350, 200, 180, 22), **{value: "别误判方向"})
        tree.node("row", "AXGroup", (300, 190, 700, 45), children=("body",))
        tree.node("rows", "AXGroup", (300, 100, 700, 500), children=("row",))
        tree.node("list", "AXGroup", (300, 100, 700, 500), children=("rows",))
        with (patch.object(qq_ax, "_ax_attr", side_effect=tree.attr),
              patch.object(qq_ax, "_ax_rect", side_effect=tree.rects.get)):
            messages = qq_ax._parse_message_list("list", (0, 0, 1000, 800))
        self.assertEqual([message.side for message in messages], ["them"])

    def test_near_full_width_without_ax_avatar_is_unknown(self):
        tree = FakeTree()
        value = qq_ax.AX.kAXValueAttribute
        tree.node("body", "AXStaticText", (350, 200, 610, 42),
                  **{value: "这条消息几乎铺满整个区域，不能只靠位置猜发送方"})
        tree.node("row", "AXGroup", (300, 190, 700, 60), children=("body",))
        tree.node("rows", "AXGroup", (300, 100, 700, 500), children=("row",))
        tree.node("list", "AXGroup", (300, 100, 700, 500), children=("rows",))
        with (patch.object(qq_ax, "_ax_attr", side_effect=tree.attr),
              patch.object(qq_ax, "_ax_rect", side_effect=tree.rects.get)):
            messages = qq_ax._parse_message_list("list", (0, 0, 1000, 800))
        self.assertEqual([message.side for message in messages], ["unknown"])

    def test_sender_container_outweighs_quote_name_and_avatar_position(self):
        tree = FakeTree()
        value = qq_ax.AX.kAXValueAttribute
        description = qq_ax.AX.kAXDescriptionAttribute
        tree.node("quote_name", "AXGroup", (360, 200, 60, 18),
                  **{description: "我"})
        tree.node("quote_body", "AXStaticText", (360, 221, 160, 20),
                  **{value: "我之前说的话"})
        tree.node("quote", "AXGroup", (350, 195, 200, 50),
                  children=("quote_name", "quote_body"),
                  **{description: "引用消息"})
        tree.node("incoming", "AXStaticText", (350, 256, 160, 20),
                  **{value: "这次是对方在说"})
        tree.node("sender", "AXGroup", (310, 185, 32, 32),
                  children=("avatar",), **{description: "小王"})
        tree.node("avatar", "AXImage", (310, 185, 32, 32))
        tree.node("row", "AXGroup", (300, 180, 700, 105),
                  children=("quote", "incoming", "sender"))
        tree.node("old", "AXGroup", (300, 100, 700, 1))
        tree.node("later", "AXGroup", (300, 500, 700, 1))
        tree.node("rows", "AXGroup", (300, 100, 700, 500),
                  children=("old", "row", "later"))
        tree.node("list", "AXGroup", (300, 100, 700, 500), children=("rows",))
        with (patch.object(qq_ax, "_ax_attr", side_effect=tree.attr),
              patch.object(qq_ax, "_ax_rect", side_effect=tree.rects.get)):
            messages = qq_ax._parse_message_list("list", (0, 0, 1000, 800))
        self.assertEqual([(m.side, m.sender, m.text, m.quoted_text) for m in messages],
                         [("them", "小王", "这次是对方在说", "我之前说的话")])

    def test_own_sender_container_wins_even_if_avatar_is_on_left(self):
        tree = FakeTree()
        value = qq_ax.AX.kAXValueAttribute
        description = qq_ax.AX.kAXDescriptionAttribute
        tree.node("avatar", "AXImage", (310, 200, 32, 32))
        tree.node("sender", "AXGroup", (310, 200, 32, 32),
                  children=("avatar",), **{description: "我"})
        tree.node("body", "AXStaticText", (350, 200, 250, 22),
                  **{value: "这条仍然是我发的"})
        tree.node("row", "AXGroup", (300, 190, 700, 50),
                  children=("sender", "body"))
        tree.node("old", "AXGroup", (300, 100, 700, 1))
        tree.node("later", "AXGroup", (300, 500, 700, 1))
        tree.node("rows", "AXGroup", (300, 100, 700, 500),
                  children=("old", "row", "later"))
        tree.node("list", "AXGroup", (300, 100, 700, 500), children=("rows",))
        with (patch.object(qq_ax, "_ax_attr", side_effect=tree.attr),
              patch.object(qq_ax, "_ax_rect", side_effect=tree.rects.get)):
            messages = qq_ax._parse_message_list("list", (0, 0, 1000, 800))
        self.assertEqual([(m.side, m.text) for m in messages],
                         [("me", "这条仍然是我发的")])

    def test_quote_sender_name_is_not_used_when_own_row_has_sender(self):
        tree = FakeTree()
        description = qq_ax.AX.kAXDescriptionAttribute
        tree.node("quoted_sender", "AXGroup", **{description: "小王"})
        tree.node("quote", "AXGroup", children=("quoted_sender",),
                  **{description: "引用消息"})
        tree.node("own_sender", "AXGroup", **{description: "我"})
        tree.node("row", "AXGroup", children=("quote", "own_sender"))
        with patch.object(qq_ax, "_ax_attr", side_effect=tree.attr):
            name, element = qq_ax._sender_container("row")
        self.assertEqual((name, element), ("我", "own_sender"))

    def test_parser_never_collects_sidebar_text_outside_message_list(self):
        tree = FakeTree()
        value = qq_ax.AX.kAXValueAttribute
        tree.node("bubble", "AXStaticText", (350, 200, 120, 20), **{value: "列表内消息"})
        tree.node("sidebar", "AXStaticText", (20, 200, 200, 20), **{value: "会话预览"})
        tree.node("row", "AXGroup", (300, 190, 700, 50), children=("bubble",))
        tree.node("rows", "AXGroup", (300, 100, 700, 500), children=("row",))
        tree.node("list", "AXGroup", (300, 100, 700, 500), children=("rows",))
        with (patch.object(qq_ax, "_ax_attr", side_effect=tree.attr),
              patch.object(qq_ax, "_ax_rect", side_effect=tree.rects.get)):
            messages = qq_ax._parse_message_list("list", (0, 0, 1000, 800))
        self.assertEqual([message.text for message in messages], ["列表内消息"])
        self.assertNotIn("会话预览", [message.text for message in messages])


if __name__ == "__main__":
    unittest.main()
