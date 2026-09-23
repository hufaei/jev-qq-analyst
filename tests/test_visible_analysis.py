"""The live pipeline analyzes every visible peer bubble, never an own bubble."""

import ast
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from conversation_memory import ConversationMemory, visible_incoming


def harness_class():
    tree = ast.parse((ROOT / "src/hud.py").read_text())
    source = next(node for node in tree.body
                  if isinstance(node, ast.ClassDef) and node.name == "HudController")
    methods = [node for node in source.body
               if isinstance(node, ast.FunctionDef)
               and node.name in {"_visible_update", "_visible_worker_loop",
                                 "_push", "applyEpochUpdate_"}]
    for method in methods:
        method.decorator_list = []
    klass = ast.ClassDef(name="Harness", bases=[], keywords=[], body=methods,
                         decorator_list=[])
    scope = {"visible_incoming": visible_incoming, "JUDGE_TURNS": 8, "time": time,
             "_log": lambda *_args: None}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[])),
                 str(ROOT / "src/hud.py"), "exec"), scope)
    return scope["Harness"]


class VisibleAnalysisTests(unittest.TestCase):
    def test_queued_ui_update_from_previous_foreground_epoch_is_ignored(self):
        controller = harness_class()()
        queued = []
        controller.performSelectorOnMainThread_withObject_waitUntilDone_ = (
            lambda selector, payload, wait: queued.append((selector, payload)))
        controller.applyChat_ = Mock()
        controller._foreground_epoch = 1
        controller._push("applyChat:", "old chat")
        controller._foreground_epoch = 2
        controller.applyEpochUpdate_(queued.pop()[1])
        controller.applyChat_.assert_not_called()

        controller._push("applyChat:", "current chat")
        controller.applyEpochUpdate_(queued.pop()[1])
        controller.applyChat_.assert_called_once_with("current chat")

    def test_all_counterpart_rows_only_and_cached_on_revisit(self):
        controller = harness_class()()
        controller.memory = ConversationMemory()
        controller._visible_signature = None
        controller._visible_targets = ()
        controller._visible_epoch = 0
        controller._visible_errors = {}
        controller._visible_event = Mock()
        controller._visible_event.wait.side_effect = [None, StopIteration()]
        controller._push = Mock()
        controller._paused = False
        controller._qq_frontmost = True
        controller._model_lock = threading.Lock()
        controller.judge = Mock()
        controller.judge.judge.return_value = {"intent": "闲聊"}
        rows = [SimpleNamespace(side=side, sender=None, text=text)
                for side, text in (("me", "自己的话"), ("them", "第一句"),
                                   ("unknown", "贴图"), ("them", "第二句"),
                                   ("me", "我的回复"))]
        controller.memory.observe("chat", rows)
        controller._visible_update("chat", rows)
        self.assertEqual([item.text for item, _key in controller._visible_targets],
                         ["第一句", "第二句"])
        with self.assertRaises(StopIteration):
            controller._visible_worker_loop()
        self.assertEqual([call.args[0] for call in controller.judge.judge.call_args_list],
                         ["第一句", "第二句"])
        controller._visible_signature = None  # scrolling away and back
        controller._visible_update("chat", rows)
        self.assertTrue(all(controller.memory.get_verdict(key) is not None
                            for _item, key in controller._visible_targets))
        self.assertEqual(controller._visible_event.set.call_count, 1)


if __name__ == "__main__":
    unittest.main()
