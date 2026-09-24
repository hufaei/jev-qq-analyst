"""Synthetic Windows UIA checks; no QQ process or credentials are accessed."""

import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if "uiautomation" not in sys.modules:
    sys.modules["uiautomation"] = types.ModuleType("uiautomation")
if "psutil" not in sys.modules:
    sys.modules["psutil"] = types.ModuleType("psutil")
import qq_ax_win as win
import hud_win as hud
from jev_client import validated_endpoint


class Node:
    def __init__(self, kind="GroupControl", name="", rect=(0, 0, 100, 40),
                 children=(), handle=0, aid=""):
        self.ControlTypeName = kind
        self.Name = name
        self.AutomationId = aid
        self.HelpText = ""
        self.NativeWindowHandle = handle
        self.ClassName = win.QQ_WINDOW_CLASS
        self.ProcessId = 42
        self._rect = rect
        self._children = list(children)

    def GetChildren(self):
        return self._children


class WindowsAdapterTests(unittest.TestCase):
    def test_only_foreground_qq_window_is_read(self):
        background = Node(handle=10)
        foreground = Node(handle=20)
        row = Node(aid="123")
        message_list = Node()
        with (patch.object(win, "_foreground_qq_handle", return_value=20),
              patch.object(win, "_qq_windows", return_value=[background, foreground]),
              patch.object(win, "_rect", side_effect=lambda node: node._rect),
              patch.object(win, "_landmarks", return_value=(message_list, Node(children=[row]))),
              patch.object(win, "_parse_row", return_value=win.Message("你好", "them", 0, 1)),
              patch.object(win, "_chat_title", return_value="当前会话"),
              patch.object(win, "_window_dict", return_value={"wid": 20}),
              patch.object(win, "_at_live_bottom", return_value=True)):
            result = win.read_conversation()
        self.assertTrue(result["ok"])
        self.assertEqual(result["window"]["wid"], 20)

    def test_foreground_without_chat_does_not_fall_back_to_other_window(self):
        background = Node(handle=10)
        foreground = Node(handle=20)
        with (patch.object(win, "_foreground_qq_handle", return_value=20),
              patch.object(win, "_qq_windows", return_value=[background, foreground]),
              patch.object(win, "_rect", side_effect=lambda node: node._rect),
              patch.object(win, "_landmarks", return_value=None)):
            result = win.read_conversation()
        self.assertFalse(result["ok"])

    def test_quote_is_separate_from_new_body(self):
        quoted = Node("TextControl", "旧消息", (150, 120, 80, 20))
        quote_group = Node(name="引用消息", rect=(140, 115, 120, 30), children=[quoted])
        body = Node("TextControl", "新的回复", (150, 160, 100, 25))
        row = Node(rect=(100, 100, 500, 100), children=[quote_group, body])
        with patch.object(win, "_rect", side_effect=lambda node: node._rect):
            message = win._parse_row(row, (100, 80, 500, 400))
        self.assertEqual(message.text, "新的回复")
        self.assertEqual(message.quoted_text, "旧消息")

    def test_media_never_displays_embedded_filename(self):
        file_icon = Node("ImageControl", "文件", (150, 130, 80, 60))
        filename = Node("TextControl", "私人资料.pdf", (240, 130, 160, 20))
        row = Node(rect=(100, 100, 500, 100), children=[file_icon, filename])
        with patch.object(win, "_rect", side_effect=lambda node: node._rect):
            message = win._parse_row(row, (100, 80, 500, 400))
        self.assertEqual((message.kind, message.text), ("file", "【文件】"))

    def test_external_http_is_rejected_for_both_modes(self):
        for mode in ("jev", "gateway"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validated_endpoint(mode, "http://example.com/v1/systemone")
        self.assertEqual(validated_endpoint("gateway", "http://127.0.0.1:8080"),
                         "http://127.0.0.1:8080/v1/systemone")

    def test_unknown_chat_title_clears_targets_without_judging(self):
        app = hud.HudApp.__new__(hud.HudApp)
        app._read_failure_gate = Mock(return_value=True)
        app._shown = True
        app._follow_qq = Mock()
        app._judge_epoch = 3
        app._visible_signature = ("old",)
        app._judge_lock = threading.Lock()
        app._chat = "旧会话"
        app._targets = [(win.Message("旧消息", "them", 0, 1), ("old",))]
        app._messages = [win.Message("旧消息", "them", 0, 1)]
        app._display_rows = list(app._targets)
        app.title_label = Mock()
        app.cards = Mock()
        app.cards.winfo_children.return_value = []
        app._set_status = Mock()
        app._apply_read({"ok": True, "chat_title": "", "messages": app._messages})
        self.assertEqual((app._chat, app._targets, app._messages), ("", [], []))
        self.assertEqual(app._judge_epoch, 4)

    def test_saving_settings_drops_old_verdicts_and_in_flight_epoch(self):
        app = hud.HudApp.__new__(hud.HudApp)
        old_memory = app.memory = object()
        app._judge_epoch = 5
        app._judge_event = Mock()
        app._judge_lock = threading.Lock()
        app._chat, app._targets, app._messages = "会话", [("消息", "key")], ["消息"]
        app._display_rows = [("消息", "key")]
        app._expanded_keys = {("key",)}
        app._errors = {"key": "旧错误"}
        app.cards = Mock()
        app.cards.winfo_children.return_value = []
        app._set_status = Mock()
        app.judge = object()
        with patch.object(hud, "settings_complete", return_value=True):
            app._settings_saved()
        self.assertIsNot(app.memory, old_memory)
        self.assertEqual(app._judge_epoch, 6)
        self.assertEqual((app._chat, app._targets, app._messages), ("", [], []))
        self.assertIsNone(app.judge)


if __name__ == "__main__":
    unittest.main()
