"""Session-only context, scroll gating and verdict cache regressions."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from conversation_memory import ConversationMemory, visible_incoming


def msg(side, text, sender=None):
    return SimpleNamespace(side=side, text=text, sender=sender)


class ConversationMemoryTests(unittest.TestCase):
    def test_only_visible_counterpart_bubbles_are_targets(self):
        rows = [msg("me", "自己的话"), msg("unknown", "表情包"),
                msg("them", "第一句"), msg("them", "第二句"), msg("me", "我的回复")]
        self.assertEqual([m.text for m in visible_incoming(rows)], ["第一句", "第二句"])

    def test_history_scroll_does_not_become_new_live_tail(self):
        memory = ConversationMemory()
        live = [msg("them", "一"), msg("me", "二"), msg("them", "三")]
        self.assertTrue(memory.observe("chat", live))
        self.assertFalse(memory.observe("chat", [msg("them", "更早的消息")]))
        self.assertTrue(memory.observe("chat", live))
        self.assertTrue(memory.observe("chat", live + [msg("me", "四"), msg("them", "五")]))
        self.assertIn("我: 四", memory.context("chat", msg("them", "五"), live, 8))

    def test_bounded_context_and_verdict_lru(self):
        memory = ConversationMemory(max_chats=2, max_rows=3, max_verdicts=2)
        rows = [msg("me", "a" * 100), msg("them", "b" * 100), msg("me", "c" * 100)]
        memory.observe("chat", rows)
        memory.observe("chat", rows + [msg("them", "d")])
        context = memory.context("chat", msg("them", "d"), rows, 8, max_chars=20)
        self.assertLessEqual(len(context), 20)
        for i in range(3):
            memory.put_verdict((i,), {"n": i})
        self.assertIsNone(memory.get_verdict((0,)))
        self.assertEqual(memory.get_verdict((2,)), {"n": 2})


if __name__ == "__main__":
    unittest.main()
