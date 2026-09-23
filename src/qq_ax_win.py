"""Read the current Windows QQ conversation from the UI Automation tree.

Windows NTQQ is the same Electron build family as the macOS client and exposes
its accessibility tree through UI Automation.  This adapter mirrors qq_ax.py's
read-only contract: no screenshots, no OCR, no database access, no injection,
and only nodes QQ has already rendered for the focused chat window.

Rows scrolled out of view keep their tree nodes with zero-size rectangles; the
zero-rect filter below is what keeps analysis limited to visible content.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import re
import time
from dataclasses import dataclass, field

import psutil
import uiautomation as uia


@dataclass
class Message:
    """A visible QQ row; non-text kinds carry display placeholders only."""

    text: str
    side: str  # them | me | unknown
    y: float   # normalized, top origin
    conf: float
    h: float = 0.0
    sender: str | None = None
    quoted_text: str = ""
    lines: list[str] = field(default_factory=list)
    x: float = 0.0
    w: float = 0.0
    kind: str = "text"  # text | image | sticker | file | attachment


QQ_WINDOW_CLASS = "Chrome_WidgetWin_1"
LIST_NAME = "消息列表"
LIST_ROOT_AID = "ml-root"
MAX_NODES = 50_000
MAX_DEPTH = 64
MIN_WINDOW_W, MIN_WINDOW_H = 400, 300

_GENERIC_IMAGE_LABELS = {"图片", "图像", "表情", "表情包", "动画表情", "image", "emoji", "gif"}
_STICKER_LABELS = {"表情", "表情包", "动画表情", "贴图", "贴纸", "emoji", "sticker", "gif"}
_FILE_LABELS = {"文件", "文件消息", "发送文件", "接收文件", "下载文件", "file", "file attachment"}
_ATTACHMENT_LABELS = {"附件", "attachment"}
_MEDIA_PLACEHOLDERS = {
    "image": "【图片】", "sticker": "【表情】",
    "file": "【文件】", "attachment": "【附件】",
}
_TIME_PATTERN = re.compile(
    r"^(?:\d{1,2}:\d{2}|昨天 \d{1,2}:\d{2}|\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月\d{1,2}日|星期[一二三四五六日] \d{1,2}:\d{2})$")


def has_accessibility() -> bool:
    """UIA needs no macOS-style grant when reading a same-integrity process."""
    return True


def request_accessibility() -> bool:
    return True


_PID_TTL = 5.0
_pid_cache: tuple[float, set[int]] = (0.0, set())


def _qq_pids() -> set[int]:
    global _pid_cache
    now = time.monotonic()
    if now - _pid_cache[0] < _PID_TTL:
        return _pid_cache[1]
    pids = set()
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info["name"] or "").lower().startswith("qq"):
                pids.add(process.pid)
        except Exception:
            continue
    _pid_cache = (now, pids)
    return pids


def frontmost_app_is_qq() -> bool | None:
    """Whether QQ is the foreground process (pure user32, no COM)."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value in _qq_pids()


def _rect(control):
    try:
        r = control.BoundingRectangle
    except Exception:  # stale COM element mid-read (window/chat teardown race)
        return None
    if r is None:
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    return (r.left, r.top, w, h)


def _children(control):
    try:
        return control.GetChildren()
    except Exception:
        return []


def _walk(roots, max_nodes: int = MAX_NODES, max_depth: int = MAX_DEPTH):
    """Breadth-first, bounded UIA walk yielding ``(control, depth)``."""
    queue = [(root, 0) for root in roots]
    visited = 0
    while queue and visited < max_nodes:
        control, depth = queue.pop(0)
        visited += 1
        yield control, depth
        if depth >= max_depth:
            continue
        for child in _children(control):
            queue.append((child, depth + 1))


def _qq_windows():
    pids = _qq_pids()
    windows = []
    for window in uia.GetRootControl().GetChildren():
        if window.ProcessId not in pids:
            continue
        rect = _rect(window)
        if (window.ClassName == QQ_WINDOW_CLASS and rect
                and rect[2] >= MIN_WINDOW_W and rect[3] >= MIN_WINDOW_H):
            windows.append(window)
    return windows


def _ensure_accessibility(window) -> None:
    """Keep a UIA event listener registered while we run.

    NTQQ's Chromium collapses its UIA tree when it believes no assistive
    client is listening (``UiaClientsAreListening``), and a collapsed tree
    cannot be woken by walking alone.  Registering a cheap event handler for
    the window keeps this process counted as a listening client for the whole
    renderer session, which keeps the tree exposed in the product's steady
    state.  Idempotent; handlers live for the process lifetime.
    """
    if _a11y_listening:
        return
    try:
        import comtypes
        from uiautomation import uiautomation as _raw

        client = _raw._AutomationClient.instance()
        core = client.UIAutomationCore

        class _Handler(comtypes.COMObject):
            _com_interfaces_ = core.IUIAutomationEventHandler,

            def IUIAutomationEventHandler_HandleAutomationEvent(self, sender, eventId):
                pass

        _a11y_listening.append(_Handler())
        _a11y_listening.append(client.IUIAutomation)  # keep the COM client alive
        client.IUIAutomation.AddAutomationEventHandler(
            11000, window.Element, 1, None, _a11y_listening[0])  # ToolTipOpened, TreeScope_Element
    except Exception:
        pass  # reading still works whenever QQ exposes the tree
    time.sleep(0.25)  # let the registration reach the provider


_a11y_listening: list = []


def ensure_listener() -> None:
    """Register the UIA keep-alive from a long-lived caller thread (the HUD
    reader supervisor); short-lived per-cycle readers skip re-registration."""
    if _a11y_listening:
        return
    try:
        windows = _qq_windows()
        if windows:
            _ensure_accessibility(windows[0])
    except Exception:
        pass


def reset_session() -> None:
    """Rebuild the UIA session on the next use.

    After heavy DOM churn (rapid chat switching), NTQQ's Chromium provider can
    keep serving a stale subtree snapshot to an existing UIA session while
    fresh sessions see the live tree.  Dropping the apartment-bound library
    singleton (and the keep-alive handler registered in that apartment) makes
    the next reader thread build a clean session.  Call only when no reader
    thread is inside a UIA call.
    """
    _a11y_listening.clear()
    try:
        from uiautomation import uiautomation as _raw
        _raw._AutomationClient._instance = None
    except Exception:
        pass


def _find_message_list(window):
    """The named 消息列表 region, same landmark the macOS adapter uses."""
    for control, _depth in _walk([window], max_nodes=8000, max_depth=12):
        if (control.ControlTypeName == "WindowControl" and control.Name == LIST_NAME):
            return control
    return None


def _row_container(message_list):
    """The virtualised group owning message rows directly.

    Rows are GroupControls whose AutomationId is a bare numeric message id;
    QQ also parents ``md-tips__<id>`` hints here, which we ignore.
    """
    best, best_count = None, 0
    for control, depth in _walk([message_list], max_nodes=2000, max_depth=4):
        if depth == 0 or control.ControlTypeName != "GroupControl":
            continue
        rows = [child for child in _children(control)
                if child.ControlTypeName == "GroupControl"
                and (child.AutomationId or "").isdigit()]
        if len(rows) > best_count:
            best, best_count = control, len(rows)
    return best


def _classify_named_media(label: str) -> str | None:
    label = (label or "").strip()
    if label in _FILE_LABELS:
        return "file"
    if label in _ATTACHMENT_LABELS:
        return "attachment"
    if label in _STICKER_LABELS:
        return "sticker"
    if label in _GENERIC_IMAGE_LABELS:
        return "image"
    return None


def _classify_image(name: str, w: int, h: int) -> str | None:
    return _classify_named_media(name) or (
        "image" if not (name or "").strip() and w >= 64 and h >= 48 else None)


def _parse_row(row, viewport) -> Message | None:
    (vx, vy, vw, vh) = viewport
    row_rect = _rect(row)
    if row_rect is None:
        return None  # virtualised out of view
    rx, ry, rw, rh = row_rect
    if ry + rh < vy or ry > vy + vh:
        return None
    center_x = vx + vw / 2

    sender: str | None = None
    side = "unknown"
    texts: list[tuple[str, tuple]] = []
    media: list[tuple[str, tuple]] = []
    avatar_rect = None

    for control, _depth in _walk([row], max_nodes=600, max_depth=12):
        kind_name = control.ControlTypeName
        name = (control.Name or "").strip()
        rect = _rect(control)
        if rect is None:
            continue
        x, y, w, h = rect
        if kind_name == "GroupControl" and name and w <= 64 and h <= 64 and abs(w - h) <= 12:
            # avatars sit flush at the row edge; stickers share the shape but
            # live inside the bubble, and carry a media label instead of a nickname
            at_edge = abs(x - vx) < 48 or abs(x + w - (vx + vw)) < 48
            if at_edge and _classify_named_media(name) is None and avatar_rect is None:
                avatar_rect, sender = rect, name
            else:
                media_kind = _classify_named_media(name)
                if media_kind:
                    media.append((media_kind, rect))
        elif kind_name == "TextControl" and name:
            if _TIME_PATTERN.match(name) and abs(x + w / 2 - center_x) < vw * 0.2:
                continue  # centered time separator
            texts.append((name, rect))
        elif kind_name == "ImageControl":
            kind = _classify_image(name, w, h)
            if kind:
                media.append((kind, rect))

    if avatar_rect is not None:
        side = "them" if avatar_rect[0] < center_x - 40 else "me"
    body_texts = [item for item in texts if not (sender and item[0] == sender)]
    for name, rect in body_texts:
        if side == "unknown":
            side = "them" if rect[0] + rect[2] / 2 < center_x else "me"
            break
    if side == "unknown" and media:
        rect = media[-1][1]
        side = "them" if rect[0] + rect[2] / 2 < center_x else "me"

    if media and not body_texts:
        kind = media[-1][0]
        text = _MEDIA_PLACEHOLDERS[kind]
    elif media and body_texts:
        kind = media[-1][0]
        text = _MEDIA_PLACEHOLDERS[kind] + "".join(t for t, _r in body_texts)
    else:
        kind = "text"
        text = "".join(t for t, _r in body_texts)
    if not text.strip():
        return None

    return Message(
        text=text, side=side,
        y=max(0.0, min(1.0, (ry - vy) / vh)), conf=1.0,
        h=rh / vh, sender=sender if side == "them" else None,
        lines=[text], x=(rx - vx) / vw, w=rw / vw, kind=kind,
    )


def _chat_title(window, message_list, viewport) -> str:
    """Best-effort chat title from the header strip above the message list."""
    (vx, vy, _vw, _vh) = viewport
    candidates = []
    for control, _depth in _walk([window], max_nodes=8000, max_depth=12):
        if control.ControlTypeName not in ("TextControl", "ButtonControl"):
            continue
        name = (control.Name or "").strip()
        rect = _rect(control)
        if not name or rect is None:
            continue
        x, y, w, h = rect
        # header lives directly above the list, on its left half (right half is toolbar)
        if x >= vx and x < vx + viewport[2] * 0.5 and vy - 64 <= y and y + h <= vy + 3:
            distance = abs(x - (vx + 16)) + abs((y + h / 2) - (vy - 24))
            candidates.append((distance, name))
    return min(candidates, default=(0, ""))[1]


def _window_dict(control, rect) -> dict:
    try:
        wid = int(control.NativeWindowHandle)
    except Exception:
        wid = 0
    x, y, w, h = rect
    return {"wid": wid, "pid": int(control.ProcessId), "title": "QQ",
            "x": float(x), "y": float(y), "w": float(w), "h": float(h)}


def _at_live_bottom(rows, viewport) -> bool | None:
    """Visible rows reach the viewport bottom and no virtualised row follows."""
    if not rows:
        return None
    last = rows[-1]
    rect = _rect(last)
    if rect is None:
        return False
    bottom_gap = abs((rect[1] + rect[3]) - (viewport[1] + viewport[3]))
    return bottom_gap <= 12


def _landmarks(window) -> tuple | None:
    """Locate the message list and its row container with a fresh walk.

    No caching: after a chat switch, detached UIA elements keep serving the
    old subtree with valid rects, which froze reads on the previous
    conversation.  A fresh walk costs ~0.3 s more and is always current.
    """
    _ensure_accessibility(window)
    message_list = _find_message_list(window)
    if message_list is None:
        return None
    return message_list, _row_container(message_list)


def read_conversation(max_messages: int | None = None, previous_wid: int | None = None,
                      prev_fingerprint: bytes | None = None, prev_layout=None) -> dict:
    """Read the current, visible QQ message rows from the foreground QQ window."""
    del previous_wid
    started = time.perf_counter()
    windows = _qq_windows()
    if not windows:
        return {"ok": False, "error": "没找到 QQ 窗口", "messages": []}

    for window in windows:
        window_rect = _rect(window)
        landmarks = _landmarks(window)
        if landmarks is None:
            continue
        message_list, container = landmarks
        viewport = _rect(message_list)
        if viewport is None:
            continue
        rows = [child for child in _children(container or message_list)
                if child.ControlTypeName == "GroupControl"
                and (child.AutomationId or "").isdigit()]
        if not rows:
            return {"ok": False, "error": "当前 QQ 窗口里没找到消息列表", "messages": []}
        messages = []
        for row in rows:
            message = _parse_row(row, viewport)
            if message is not None:
                messages.append(message)
        if max_messages:
            messages = messages[-max_messages:]
        title = _chat_title(window, message_list, viewport)
        window_info = _window_dict(window, window_rect)
        layout = (window_info["wid"], *window_rect, viewport)
        digest = hashlib.sha256(repr((title, [(m.side, m.sender, m.text, m.quoted_text, m.kind)
                                              for m in messages])).encode()).digest()
        elapsed = (time.perf_counter() - started) * 1000
        unchanged = digest == prev_fingerprint and layout == prev_layout
        return {
            "ok": True, "unchanged": unchanged,
            "layout": layout, "fingerprint": digest,
            "chat_title": title, "window": window_info,
            "at_bottom": _at_live_bottom(rows, viewport),
            "messages": [] if unchanged else messages,
            "n_blocks": sum(len(message.lines) for message in messages),
            "timing_ms": {"capture": 0.0, "ocr": 0.0, "total": elapsed,
                          "capture_path": "uia"},
        }
    return {"ok": False, "error": "当前 QQ 窗口里没找到消息列表", "messages": []}


if __name__ == "__main__":
    result = read_conversation()
    if not result.get("ok"):
        print("ERROR:", result.get("error", "unknown"))
        raise SystemExit(1)
    # Diagnostics intentionally contain no conversation title, sender, or message text.
    print({
        "chat_title_found": bool(result["chat_title"]),
        "window_id": result["window"]["wid"],
        "window_size": (result["window"]["w"], result["window"]["h"]),
        "message_count": len(result["messages"]),
        "sides": [message.side for message in result["messages"]],
        "kinds": [message.kind for message in result["messages"]],
        "at_bottom": result["at_bottom"],
        "timing_ms": round(result["timing_ms"]["total"], 1),
    })
