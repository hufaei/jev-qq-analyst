"""Bounded, process-only memory for visible QQ text and completed decisions.

QQ's accessibility tree does not expose a stable message ID.  We use the ordered
visible rows to recognise scrolls, and retain a small verdict cache for revisits.
Nothing is written to disk.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock


def _is_text(message) -> bool:
    return getattr(message, "kind", "text") == "text"


def signature(message) -> tuple[str, str, str, str]:
    return (message.side, message.sender or "", message.text,
            getattr(message, "quoted_text", ""))


def visible_incoming(messages: list) -> list:
    """Only counterpart bubbles are analysis targets; own text is context only."""
    return [message for message in messages
            if _is_text(message) and message.side == "them" and message.text.strip()]


class ConversationMemory:
    def __init__(self, max_chats: int = 24, max_rows: int = 32,
                 max_verdicts: int = 128):
        self.max_chats = max_chats
        self.max_rows = max_rows
        self.max_verdicts = max_verdicts
        self._views: OrderedDict[str, tuple] = OrderedDict()
        self._history: OrderedDict[str, list] = OrderedDict()
        self._verdicts: OrderedDict[tuple, dict] = OrderedDict()
        self._lock = Lock()

    def observe(self, chat: str, messages: list, at_bottom: bool | None = None) -> bool:
        """Return whether this view still reaches the live tail of this chat.

        A changed viewport lacking the previously known tail is a history scroll,
        not evidence of a new incoming message.  A newly opened chat establishes
        its first visible tail as the baseline.
        """
        # Media placeholders belong in the HUD, but never advance the text tail
        # or enter the conversation history used for model context.
        messages = [message for message in messages if _is_text(message)]
        current = tuple(signature(message) for message in messages)
        if not current:
            return False
        with self._lock:
            prior = self._views.get(chat)
            if prior is None:
                live = at_bottom is not False
                history = list(messages)
            else:
                anchor = prior[-min(2, len(prior)):]
                anchor_at = next((i for i in range(len(current) - len(anchor) + 1)
                                  if current[i:i + len(anchor)] == anchor), None)
                live = at_bottom if at_bottom is not None else anchor_at is not None
                history = self._history.get(chat, [])
                if live:
                    # Keep one copy of each overlapping row and append only rows
                    # actually following our previous live tail.
                    if anchor_at is None:
                        # QQ may jump several rows at once. A measured bottom is
                        # stronger evidence than a missing two-row overlap, but we
                        # cannot safely bridge the unseen gap in the context.
                        history = list(messages)
                    else:
                        new_after = anchor_at + len(anchor)
                        history.extend(messages[new_after:])
                # On a history scroll, retain the known live tail and context.
            if live:
                self._views[chat] = current
                self._views.move_to_end(chat)
                self._history[chat] = history[-self.max_rows:]
                self._history.move_to_end(chat)
                while len(self._views) > self.max_chats:
                    old_chat, _ = self._views.popitem(last=False)
                    self._history.pop(old_chat, None)
            return live

    def reset_view(self, chat: str) -> None:
        """Explicit reanalysis can establish the current viewport as a new baseline."""
        with self._lock:
            self._views.pop(chat, None)
            self._history.pop(chat, None)

    def invalidate_verdicts(self, chat: str) -> None:
        """The explicit re-analyze command bypasses this chat's judgment cache."""
        with self._lock:
            for key in list(self._verdicts):
                if key[0] == chat:
                    del self._verdicts[key]

    def context(self, chat: str, newest, visible: list, turns: int = 8,
                max_chars: int = 2000) -> str | None:
        """Use recent observed turns before the target, bounded for one Jev call."""
        if not _is_text(newest):
            return None
        target = signature(newest)
        with self._lock:
            history = list(self._history.get(chat, []))
        match = next((i for i in range(len(history) - 1, -1, -1)
                      if signature(history[i]) == target), None)
        visible_match = next((i for i, message in enumerate(visible)
                              if message is newest), None)
        if visible_match is None:
            visible_match = next((i for i, message in enumerate(visible)
                                  if signature(message) == target), len(visible))
        prior = history[:match] if match is not None else visible[:visible_match]
        prior = [message for message in prior if _is_text(message)]
        lines = [f"{m.sender or ('我' if m.side == 'me' else '对方')}: "
                 + (f"[引用：{m.quoted_text}] " if getattr(m, "quoted_text", "") else "")
                 + m.text for m in prior[-turns:]]
        result = "\n".join(lines)
        return result[-max_chars:] or None

    @staticmethod
    def verdict_key(chat: str, newest, previous_incoming: str = "") -> tuple:
        # The previous incoming text disambiguates common repeats like “好的”.
        # If it is absent at a viewport edge, the cache safely misses.
        return (chat, newest.sender or "", newest.text,
                getattr(newest, "quoted_text", ""), previous_incoming)

    def get_verdict(self, key: tuple) -> dict | None:
        with self._lock:
            value = self._verdicts.get(key)
            if value is not None:
                self._verdicts.move_to_end(key)
            return value

    def put_verdict(self, key: tuple, verdict: dict) -> None:
        with self._lock:
            self._verdicts[key] = verdict
            self._verdicts.move_to_end(key)
            while len(self._verdicts) > self.max_verdicts:
                self._verdicts.popitem(last=False)
