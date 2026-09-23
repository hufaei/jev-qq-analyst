"""Windows QQ (NTQQ) UI Automation probe.

Stage 1: list top-level windows owned by QQ processes.
Stage 2: shallow dump of the main window (first 3 levels).
Stage 3: bounded deep walk; collect node stats and keyword hits.

Usage:
    py -3.13 probe/win_uia_probe.py [stage]  # stage = 1|2|3 (default 3)

Read-only: walks the UIA tree QQ already exposes; never sends input to QQ.
"""

from __future__ import annotations

import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import psutil
import uiautomation as uia

MAX_NODES = 30000
MAX_DEPTH = 40

KEYWORDS = (
    "消息", "聊天", "会话", "标题", "message", "session", "chat",
    "title", "list", "对话", "联系人", "群",
)


def qq_pids() -> set[int]:
    pids = set()
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info["name"] or "").lower()
            if name.startswith("qq"):
                pids.add(p.pid)
        except Exception:
            continue
    return pids


def rect_str(c: uia.Control) -> str:
    r = c.BoundingRectangle
    if r is None:
        return "rect=?"
    return f"rect=({r.left},{r.top},{r.right - r.left}x{r.bottom - r.top})"


def describe(c: uia.Control) -> str:
    name = (c.Name or "").replace("\n", "\\n")
    if len(name) > 60:
        name = name[:60] + "…"
    parts = [
        f"{c.ControlTypeName}",
        f"cls={c.ClassName!r}",
        f"name={name!r}",
    ]
    aid = c.AutomationId
    if aid:
        parts.append(f"aid={aid!r}")
    parts.append(rect_str(c))
    return " ".join(parts)


def stage1() -> list[uia.Control]:
    pids = qq_pids()
    print(f"QQ pids: {sorted(pids)}")
    wins = []
    for w in uia.GetRootControl().GetChildren():
        if w.ProcessId in pids:
            wins.append(w)
            print(f"[win] {describe(w)}")
    if not wins:
        print("no top-level QQ windows found")
    return wins


def stage2(wins: list[uia.Control]) -> uia.Control | None:
    def area(w: uia.Control) -> int:
        r = w.BoundingRectangle
        if r is None:
            return 0
        return max(0, r.right - r.left) * max(0, r.bottom - r.top)

    main = max(wins, key=area)
    print(f"\n=== stage2: shallow dump of {describe(main)} ===")

    def walk(c, depth):
        if depth > 3:
            return
        print("  " * depth + describe(c))
        for ch in c.GetChildren():
            walk(ch, depth + 1)

    walk(main, 0)
    return main


def stage3(main: uia.Control) -> None:
    print(f"\n=== stage3: deep walk of {describe(main)} ===")
    stats: dict[str, int] = {}
    hits: list[tuple[int, str]] = []
    total = 0
    t0 = time.time()
    queue = [(main, 0)]
    while queue and total < MAX_NODES:
        c, d = queue.pop(0)
        total += 1
        stats[c.ControlTypeName] = stats.get(c.ControlTypeName, 0) + 1
        name = c.Name or ""
        low = name.lower()
        if name and any(k in low or k in name for k in KEYWORDS):
            if len(hits) < 80:
                hits.append((d, describe(c)))
        if d < MAX_DEPTH:
            try:
                for ch in c.GetChildren():
                    queue.append((ch, d + 1))
            except Exception as e:
                hits.append((d, f"<walk error: {e}>"))
    print(f"walked {total} nodes in {time.time() - t0:.1f}s")
    print("\n-- control type histogram --")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"{k:24s} {v}")
    print("\n-- keyword hits (depth, node) --")
    for d, s in hits:
        print(f"  d{d:>2} {s}")


if __name__ == "__main__":
    stg = sys.argv[1] if len(sys.argv) > 1 else "3"
    uia.SetGlobalSearchTimeout(5)
    wins = stage1()
    if stg == "1" or not wins:
        sys.exit(0)
    main = stage2(wins)
    if stg == "3":
        stage3(main)
