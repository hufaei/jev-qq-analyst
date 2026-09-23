"""Stage 4: focused dump of the 消息列表 (message list) subtree and chat header."""

from __future__ import annotations

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import uiautomation as uia


def describe(c: uia.Control) -> str:
    name = (c.Name or "").replace("\n", "\\n")
    if len(name) > 80:
        name = name[:80] + "…"
    r = c.BoundingRectangle
    rect = f"({r.left},{r.top},{r.right - r.left}x{r.bottom - r.top})" if r else "?"
    parts = [c.ControlTypeName, f"name={name!r}", rect]
    aid = c.AutomationId
    if aid:
        parts.append(f"aid={aid!r}")
    return " ".join(parts)


def find_message_window(root: uia.Control) -> uia.Control | None:
    queue = [root]
    while queue:
        c = queue.pop(0)
        if c.ControlTypeName == "WindowControl" and c.Name == "消息列表":
            return c
        queue.extend(c.GetChildren())
    return None


def dump(c: uia.Control, depth: int = 0, max_depth: int = 24) -> None:
    if depth > max_depth:
        return
    r = c.BoundingRectangle
    visible = r and (r.right - r.left) > 0 and (r.bottom - r.top) > 0
    print("  " * depth + describe(c) + ("" if visible else "  [hidden]"))
    try:
        children = c.GetChildren()
    except Exception as e:
        print("  " * depth + f"  <err {e}>")
        return
    for ch in children:
        dump(ch, depth + 1, max_depth)


def main() -> None:
    uia.SetGlobalSearchTimeout(5)
    pids = set()
    import psutil
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower().startswith("qq"):
                pids.add(p.pid)
        except Exception:
            pass
    wins = [w for w in uia.GetRootControl().GetChildren() if w.ProcessId in pids]
    if not wins:
        print("QQ window not found")
        return
    root = wins[0]

    msg = find_message_window(root)
    if msg is None:
        print("消息列表 not found — 打开一个聊天再试")
        return

    r = msg.BoundingRectangle
    print(f"消息列表 {describe(msg)}\n")
    dump(msg, max_depth=26)

    # chat header strip: same x-range, above the message list
    print("\n=== header strip nodes (y above message list, same x range) ===")
    queue = [(root, 0)]
    while queue:
        c, d = queue.pop(0)
        cr = c.BoundingRectangle
        if cr and cr.left >= r.left - 10 and cr.right <= r.right + 10 and cr.bottom <= r.top + 5 and (cr.right - cr.left) > 0:
            print("  " + describe(c))
        try:
            for ch in c.GetChildren():
                queue.append((ch, d + 1))
        except Exception:
            pass


if __name__ == "__main__":
    main()
