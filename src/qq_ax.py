"""Read the current macOS QQ conversation from the Accessibility tree.

This adapter deliberately does not capture the screen, OCR pixels, inspect QQ's
database, inject code, or scroll history.  It reads only the nodes QQ has already
materialised for the focused chat window and returns the visible message rows.

NTQQ is Electron-based.  On current builds ``AXWindows`` may be empty until manual
accessibility is enabled, while ``AXFocusedWindow`` and ``AXMainWindow`` still work.
The useful nodes are deep below ``AXGroup -> AXWebArea``; consequently every walk here
is recursive/bounded and never treats groups as leaves.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import deque
from dataclasses import dataclass, field

import AppKit
import ApplicationServices as AX
import Quartz


@dataclass
class Message:
    """A text message visible in the current QQ accessibility tree."""

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


QQ_BUNDLE_ID = "com.tencent.qq"
QQ_NAMES = ("QQ",)
MESSAGE_LIST_DESCRIPTION = "消息列表"
QQ_MESSAGE_ROUTE = "/main/message"
MAX_NODES = 50_000
MAX_DEPTH = 64


def has_accessibility() -> bool:
    try:
        return bool(AX.AXIsProcessTrusted())
    except Exception:
        return False


def request_accessibility() -> bool:
    """Show macOS's Accessibility permission prompt when permission is absent."""
    try:
        opts = {AX.kAXTrustedCheckOptionPrompt: True}
        return bool(AX.AXIsProcessTrustedWithOptions(opts))
    except Exception:
        return has_accessibility()


def _qq_app():
    """Return the main QQ process, identified strictly by bundle id."""
    try:
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            QQ_BUNDLE_ID)
        return apps[0] if apps else None
    except Exception:
        return None


def frontmost_app_is_qq() -> bool | None:
    """Whether QQ is the app currently receiving user input."""
    try:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return bool(app and app.bundleIdentifier() == QQ_BUNDLE_ID)
    except Exception:
        return None


def _ax_attr(element, name):
    """Read one AX attribute.  Missing/transient attributes are ordinary misses."""
    try:
        error, value = AX.AXUIElementCopyAttributeValue(element, name, None)
        return value if error == 0 else None
    except Exception:
        return None


def _ax_text(element) -> str:
    for name in (AX.kAXValueAttribute, AX.kAXDescriptionAttribute,
                 AX.kAXTitleAttribute):
        value = _ax_attr(element, name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _ax_rect(element):
    try:
        raw_position = _ax_attr(element, AX.kAXPositionAttribute)
        raw_size = _ax_attr(element, AX.kAXSizeAttribute)
        ok_position, position = AX.AXValueGetValue(
            raw_position, AX.kAXValueCGPointType, None)
        ok_size, size = AX.AXValueGetValue(raw_size, AX.kAXValueCGSizeType, None)
        if ok_position and ok_size:
            return (float(position.x), float(position.y),
                    float(size.width), float(size.height))
    except Exception:
        pass
    return None


def _same_element(left, right) -> bool:
    try:
        return bool(left == right)
    except Exception:
        return left is right


def _enable_manual_accessibility(app_element) -> None:
    # Chromium accepts this on builds where AXWindows initially appears empty.  Failure is
    # harmless: VoiceOver/Accessibility Inspector may already have activated the tree.
    try:
        AX.AXUIElementSetAttributeValue(app_element, "AXManualAccessibility", True)
    except Exception:
        pass


def _window_roots(app_element) -> list:
    """Focused/main window first, then AXWindows as a compatibility fallback."""
    _enable_manual_accessibility(app_element)
    result = []
    for name in (AX.kAXFocusedWindowAttribute, AX.kAXMainWindowAttribute):
        window = _ax_attr(app_element, name)
        if window is not None and not any(_same_element(window, item) for item in result):
            result.append(window)
    for window in list(_ax_attr(app_element, AX.kAXWindowsAttribute) or []):
        if not any(_same_element(window, item) for item in result):
            result.append(window)
    return result


def _walk(roots, max_nodes: int = MAX_NODES, max_depth: int = MAX_DEPTH):
    """Breadth-first, bounded AX walk yielding ``(element, depth)``."""
    queue = deque((root, 0) for root in roots)
    visited = 0
    while queue and visited < max_nodes:
        element, depth = queue.popleft()
        visited += 1
        yield element, depth
        if depth >= max_depth:
            continue
        for child in list(_ax_attr(element, AX.kAXChildrenAttribute) or []):
            queue.append((child, depth + 1))


def _find_web_area(window):
    fallback = None
    for element, _depth in _walk([window]):
        if _ax_attr(element, AX.kAXRoleAttribute) != "AXWebArea":
            continue
        fallback = fallback or element
        url = _ax_attr(element, "AXURL")
        if QQ_MESSAGE_ROUTE in str(url or ""):
            return element
    return fallback


def _find_message_list(web_area):
    if web_area is None:
        return None
    for element, _depth in _walk([web_area]):
        if (_ax_attr(element, AX.kAXRoleAttribute) == "AXGroup"
                and _ax_attr(element, AX.kAXDescriptionAttribute)
                == MESSAGE_LIST_DESCRIPTION):
            return element
    return None


def _intersects(left, right) -> bool:
    if left is None or right is None:
        return False
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    return (min(lx + lw, rx + rw) > max(lx, rx)
            and min(ly + lh, ry + rh) > max(ly, ry))


def _union(rects):
    rects = [rect for rect in rects if rect is not None]
    if not rects:
        return None
    x1 = min(rect[0] for rect in rects)
    y1 = min(rect[1] for rect in rects)
    x2 = max(rect[0] + rect[2] for rect in rects)
    y2 = max(rect[1] + rect[3] for rect in rects)
    return (x1, y1, x2 - x1, y2 - y1)


def _row_container(message_list):
    """Find the virtualised group's direct row list without depending on a fixed depth."""
    viewport = _ax_rect(message_list)
    best = None
    best_score = -1
    for element, depth in _walk([message_list], max_nodes=4000, max_depth=5):
        if depth == 0 or _ax_attr(element, AX.kAXRoleAttribute) != "AXGroup":
            continue
        children = list(_ax_attr(element, AX.kAXChildrenAttribute) or [])
        rect = _ax_rect(element)
        if (not children or not _intersects(rect, viewport)
                or rect[2] < viewport[2] * .8 or rect[3] < viewport[3] * .8
                or any(_ax_attr(child, AX.kAXRoleAttribute) != "AXGroup"
                       for child in children)):
            continue
        # QQ's list content group owns every virtualised row directly.  Child count is
        # more stable across versions than its anonymous AXGroup depth.
        score = len(children) * 100 + depth
        if score > best_score:
            best, best_score = element, score
    return best


def _at_live_bottom(message_list) -> bool | None:
    """Whether QQ's final virtual row reaches the visible bottom of the list.

    QQ leaves one-point placeholders for unloaded rows.  A real final row flush
    with the viewport bottom means the live tail is present; a placeholder after
    the visible rows means we are browsing older content.  Return None when this
    version of QQ does not expose enough geometry.
    """
    viewport = _ax_rect(message_list)
    container = _row_container(message_list)
    children = list(_ax_attr(container, AX.kAXChildrenAttribute) or []) if container else []
    if viewport is None or not children:
        return None
    last = _ax_rect(children[-1])
    if last is None:
        return None
    if last[3] <= 2 or not _intersects(last, viewport):
        return False
    return abs((last[1] + last[3]) - (viewport[1] + viewport[3])) <= 5


_GENERIC_IMAGE_LABELS = {"图片", "图像", "表情", "表情包", "动画表情", "image", "emoji", "gif"}


def _static_text_nodes(root):
    """Readable text plus named emoji images; never infer pixels or generic pictures."""
    nodes = []
    for element, _depth in _walk([root], max_nodes=2000, max_depth=16):
        role = _ax_attr(element, AX.kAXRoleAttribute)
        if role not in {"AXStaticText", "AXImage"}:
            continue
        text = _ax_text(element)
        if role == "AXImage":
            if (not text or text.casefold() in _GENERIC_IMAGE_LABELS
                    or len(text) > 32):
                continue
            text = text if text.startswith("[") else f"[{text}]"
        rect = _ax_rect(element)
        if text and rect is not None and rect[2] > 0 and rect[3] > 0:
            nodes.append((element, text, rect))
    nodes.sort(key=lambda item: (item[2][1], item[2][0]))
    return nodes


def _avatar_side(row, texts, viewport):
    """Use an AX avatar when QQ exposes one beside the message text."""
    vx, _vy, vw, _vh = viewport
    text_rect = _union([rect for element, _text, rect in texts
                        if _ax_attr(element, AX.kAXRoleAttribute) == "AXStaticText"])
    if text_rect is None:
        return "unknown", None
    gutter = min(72.0, vw * .14)
    left, right = [], []
    for element, _depth in _walk([row], max_nodes=2000, max_depth=16):
        if _ax_attr(element, AX.kAXRoleAttribute) != "AXImage":
            continue
        rect = _ax_rect(element)
        if (rect is None or not _intersects(rect, viewport)
                or not (28 <= rect[2] <= 72 and 28 <= rect[3] <= 72)
                or abs(rect[2] - rect[3]) > 8):
            continue
        if (rect[0] + rect[2] <= text_rect[0] - 4
                and rect[0] + rect[2] / 2 <= vx + gutter):
            left.append((rect[0], element))
        if (rect[0] >= text_rect[0] + text_rect[2] + 4
                and rect[0] + rect[2] / 2 >= vx + vw - gutter):
            right.append((rect[0] + rect[2], element))
    if left and not right:
        return "them", min(left, key=lambda item: item[0])[1]
    if right and not left:
        return "me", max(right, key=lambda item: item[0])[1]
    return "unknown", None


def _bubble_side(text_rect, viewport):
    """Fallback for QQ builds that draw avatars but omit them from the AX tree."""
    vx, _vy, vw, _vh = viewport
    if text_rect[2] >= vw * .85:
        return "unknown"
    left_gap = max(0.0, text_rect[0] - vx)
    right_gap = max(0.0, vx + vw - text_rect[0] - text_rect[2])
    margin = vw * .05
    if left_gap + margin < right_gap:
        return "them"
    if right_gap + margin < left_gap:
        return "me"
    return "unknown"


_QUOTE_MARKERS = ("引用消息", "引用的消息", "回复消息", "回复的消息")
_NON_SENDER_LABELS = {
    "消息", "消息内容", "聊天消息", "头像", "图片", "图像", "表情", "表情包",
    "更多", "转发", "撤回", "删除", "复制", "回复", "引用", "时间",
}


def _is_quote_container(element) -> bool:
    metadata = " ".join(str(_ax_attr(element, name) or "") for name in (
        AX.kAXDescriptionAttribute, AX.kAXTitleAttribute, "AXName"))
    return any(marker in metadata for marker in _QUOTE_MARKERS)


def _sender_container(row):
    """Read a row's own named sender container, not names in its message text.

    QQ exposes the sender as the *name of a container*.  Quoted content may also
    contain names, so inspect direct container attributes in breadth-first order,
    never AXStaticText matches.  A deeper quote cannot outrank the row's sender.
    Ambiguous same-depth labels are left for the avatar/alignment fallback.
    """
    queue = deque([(row, 0)])
    candidates = []
    visited = 0
    while queue and visited < 2000:
        element, depth = queue.popleft()
        visited += 1
        if depth > 12 or _is_quote_container(element):
            continue
        role = _ax_attr(element, AX.kAXRoleAttribute)
        if depth > 0 and role == "AXGroup":
            for attribute in (AX.kAXDescriptionAttribute, AX.kAXTitleAttribute,
                              "AXName"):
                name = _ax_attr(element, attribute)
                if not isinstance(name, str):
                    continue
                name = name.strip()
                if (not name or len(name) > 40 or "\n" in name
                        or name in _NON_SENDER_LABELS
                        or re.fullmatch(r"\d{1,2}:\d{2}", name)):
                    continue
                candidates.append((depth, name, element))
                break
        for child in list(_ax_attr(element, AX.kAXChildrenAttribute) or []):
            queue.append((child, depth + 1))
    if not candidates:
        return None, None
    shallowest = min(depth for depth, _name, _element in candidates)
    top = [(name, element) for depth, name, element in candidates
           if depth == shallowest]
    return top[0] if len(top) == 1 else (None, None)


def _without_sender_label(row, texts, sender_name, sender_container):
    """Drop only a dedicated sender label, never a matching quote/body string."""
    if sender_container is None or _same_element(row, sender_container):
        return texts
    nested = _static_text_nodes(sender_container)
    if not nested or any(text != sender_name for _element, text, _rect in nested):
        return texts
    if not any(not any(_same_element(item[0], child[0]) for child in nested)
               for item in texts):
        return texts
    return [item for item in texts if not any(
        _same_element(item[0], child[0]) for child in nested)]


def _split_quote(row, texts):
    """Separate only an explicitly labelled AX quote subtree from the new message."""
    candidates = []
    for element, depth in _walk([row], max_nodes=2000, max_depth=16):
        if _ax_attr(element, AX.kAXRoleAttribute) not in {"AXGroup", "AXButton"}:
            continue
        if not _is_quote_container(element):
            continue
        descendants = _static_text_nodes(element)
        quoted = [item for item in texts if any(
            _same_element(item[0], nested[0]) for nested in descendants)]
        if 0 < len(quoted) < len(texts):
            candidates.append((depth, quoted))
    if not candidates:
        return texts, ""
    # The deepest labelled subtree is least likely to include the newly written text.
    quoted = max(candidates, key=lambda item: item[0])[1]
    body = [item for item in texts if not any(
        _same_element(item[0], quote[0]) for quote in quoted)]
    quote_text = "\n".join(item[1] for item in quoted
                           if item[1] not in _QUOTE_MARKERS).strip()
    return body or texts, quote_text


def _parse_message_list(message_list, window_rect,
                        max_messages: int | None = None) -> list[Message]:
    """Convert only visible message-list descendants into ordered messages."""
    viewport = _ax_rect(message_list)
    container = _row_container(message_list)
    if viewport is None or container is None or window_rect is None:
        return []
    vx, _vy, vw, _vh = viewport
    wx, wy, ww, wh = window_rect
    rows = []
    for row in list(_ax_attr(container, AX.kAXChildrenAttribute) or []):
        row_rect = _ax_rect(row)
        # One-point-tall rows are Chromium's off-screen virtualisation placeholders.
        if row_rect is None or row_rect[3] <= 2 or not _intersects(row_rect, viewport):
            continue
        texts = [item for item in _static_text_nodes(row)
                 if _intersects(item[2], viewport)]
        if not texts:
            continue                         # image/emoji-only row: no text to analyse
        sender_name, sender_container = _sender_container(row)
        avatar_side, avatar = _avatar_side(row, texts, viewport)
        side = (("me" if sender_name == "我" else "them") if sender_name
                else avatar_side)
        if avatar is not None:
            texts = [item for item in texts if not _same_element(item[0], avatar)]
        texts = _without_sender_label(row, texts, sender_name, sender_container)
        if not texts:
            continue
        # QQ can put a centred timestamp and the following bubble in the *same* row.
        # Remove only small centred chips when a side-aligned text node is also present;
        # otherwise a wide, genuinely ambiguous message remains available as "unknown".
        side_texts = [item for item in texts
                      if not (abs((item[2][0] + item[2][2] / 2)
                                  - (vx + vw / 2)) <= vw * 0.10
                              and item[2][2] <= vw * 0.30)]
        if side_texts:
            texts = side_texts
        texts, quoted_text = _split_quote(row, texts)
        text_rect = _union([item[2] for item in texts])
        if text_rect is None:
            continue
        center = text_rect[0] + text_rect[2] / 2
        list_center = vx + vw / 2
        # Time separators and system chips sit in the middle, unlike either bubble side.
        if (not sender_name and abs(center - list_center) <= vw * 0.10
                and text_rect[2] <= vw * 0.30):
            continue
        # The row's named sender is authoritative.  Some QQ builds omit it from
        # AX; only then fall back to a visible AX avatar or clear bubble alignment.
        if side == "unknown":
            side = _bubble_side(text_rect, viewport)
        values = [item[1] for item in texts]
        sender = sender_name if side == "them" else None
        body = "\n".join(value for value in values if value).strip()
        if not body:
            continue
        x, y, w, h = text_rect
        rows.append(Message(
            text=body, side=side, sender=sender, quoted_text=quoted_text,
            y=max(0.0, (y - wy) / wh), conf=1.0,
            h=max(0.0, h / wh), x=max(0.0, (x - wx) / ww),
            w=max(0.0, w / ww), lines=list(values),
        ))
    rows.sort(key=lambda message: message.y)
    return rows if max_messages is None else rows[-max_messages:]


def _chat_title(web_area, message_list, window_rect) -> str:
    """Best-effort current chat title from the header immediately above the list."""
    list_rect = _ax_rect(message_list)
    if web_area is None or list_rect is None or window_rect is None:
        return ""
    lx, ly, lw, _lh = list_rect
    _wx, wy, _ww, _wh = window_rect
    candidates = []
    for element, _depth in _walk([web_area]):
        role = _ax_attr(element, AX.kAXRoleAttribute)
        if role not in ("AXStaticText", "AXButton"):
            continue
        text, rect = _ax_text(element), _ax_rect(element)
        if not text or rect is None:
            continue
        x, y, w, h = rect
        # NTQQ exposes the conversation heading as a button at the left edge of the
        # 52-point header, not as AXStaticText.  Header controls live on the right half,
        # so prefer the first text-bearing node in the left half.
        if x >= lx and x < lx + lw * 0.5 and y >= wy and y + h <= ly + 3:
            distance = abs(x - (lx + 16)) + abs((y + h / 2) - (ly - 24))
            candidates.append((distance, text))
    return min(candidates, default=(0, ""))[1]


def _quartz_window(pid: int, ax_rect):
    # OptionAll still resolves geometry when Codex is in front during diagnostics.  The
    # HUD separately requires QQ to be frontmost before it reads or displays anything.
    options = (Quartz.kCGWindowListOptionAll
               | Quartz.kCGWindowListExcludeDesktopElements)
    windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    best = None
    best_delta = float("inf")
    for item in windows:
        if int(item.get("kCGWindowOwnerPID", -1)) != pid:
            continue
        bounds = item.get("kCGWindowBounds") or {}
        rect = (float(bounds.get("X", 0)), float(bounds.get("Y", 0)),
                float(bounds.get("Width", 0)), float(bounds.get("Height", 0)))
        if rect[2] < 400 or rect[3] < 300:
            continue
        delta = sum(abs(a - b) for a, b in zip(rect, ax_rect)) if ax_rect else 0
        if delta < best_delta:
            best, best_delta = item, delta
    return best


def _window_dict(pid: int, window, ax_rect):
    item = _quartz_window(pid, ax_rect)
    if item:
        bounds = item.get("kCGWindowBounds") or {}
        return {
            "wid": int(item.get("kCGWindowNumber", 0)), "pid": pid,
            "title": str(item.get("kCGWindowName") or "QQ"),
            "x": float(bounds.get("X", ax_rect[0])),
            "y": float(bounds.get("Y", ax_rect[1])),
            "w": float(bounds.get("Width", ax_rect[2])),
            "h": float(bounds.get("Height", ax_rect[3])),
        }
    x, y, w, h = ax_rect
    return {"wid": 0, "pid": pid, "title": "QQ", "x": x, "y": y, "w": w, "h": h}


def read_conversation(max_messages: int | None = None, previous_wid: int | None = None,
                      prev_fingerprint: bytes | None = None, prev_layout=None) -> dict:
    """Read the current, visible QQ message rows from its focused AX window."""
    del previous_wid  # focused/main AX ownership is safer than a sticky window id
    started = time.perf_counter()
    if not has_accessibility():
        return {"ok": False, "error": "需要辅助功能权限", "messages": []}
    app = _qq_app()
    if app is None:
        return {"ok": False, "error": "没找到 QQ 应用", "messages": []}
    pid = int(app.processIdentifier())
    app_element = AX.AXUIElementCreateApplication(pid)
    for window in _window_roots(app_element):
        window_rect = _ax_rect(window)
        if window_rect is None or window_rect[2] < 400 or window_rect[3] < 300:
            continue
        web_area = _find_web_area(window)
        message_list = _find_message_list(web_area)
        if message_list is None:
            continue
        messages = _parse_message_list(message_list, window_rect, max_messages)
        at_bottom = _at_live_bottom(message_list)
        title = _chat_title(web_area, message_list, window_rect)
        window_info = _window_dict(pid, window, window_rect)
        layout = (window_info["wid"], *window_rect, _ax_rect(message_list))
        digest = hashlib.sha256(repr((title, [(m.side, m.sender, m.text, m.quoted_text)
                                              for m in messages])).encode()).digest()
        elapsed = (time.perf_counter() - started) * 1000
        unchanged = digest == prev_fingerprint and layout == prev_layout
        return {
            "ok": True, "unchanged": unchanged,
            "layout": layout, "fingerprint": digest,
            "chat_title": title, "window": window_info,
            "at_bottom": at_bottom,
            "messages": [] if unchanged else messages,
            "n_blocks": sum(len(message.lines) for message in messages),
            "timing_ms": {"capture": 0.0, "ocr": 0.0, "total": elapsed,
                          "capture_path": "accessibility"},
        }
    return {"ok": False, "error": "当前 QQ 窗口里没找到消息列表", "messages": []}


if __name__ == "__main__":
    result = read_conversation()
    if not result.get("ok"):
        print("ERROR:", result.get("error", "unknown"))
        raise SystemExit(1)
    # Diagnostics intentionally contain no conversation title, sender, or message text.
    print({
        "window_id": result["window"]["wid"],
        "window_size": (result["window"]["w"], result["window"]["h"]),
        "message_count": len(result["messages"]),
        "sides": [message.side for message in result["messages"]],
        "timing_ms": round(result["timing_ms"]["total"], 1),
    })
