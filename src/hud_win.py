"""Floating Windows QQ panel for visible peer-message decisions.

Windows counterpart of hud.py: reads the current chat through qq_ax_win's
UIA adapter, judges visible counterpart messages via a user-configured
Jev-compatible endpoint (jev_client), and mirrors the macOS card layout —
full analysis cards for counterpart text, narrow context bars for own
messages, placeholder bars for media, and a "对方 N 条 · 已分析 M 条"
status line.  The panel only reads and displays; it never writes into QQ.
"""

from __future__ import annotations

import sys
import threading
import time
import queue
from pathlib import Path

import tkinter as tk
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).parent))

import jev_client
from jev_client import JevDirectJudge, load_settings, save_settings, settings_complete
import qq_ax_win
from qq_ax_win import frontmost_app_is_qq, read_conversation
from conversation_memory import ConversationMemory, visible_incoming

PANEL_W, PANEL_H = 380, 490
COLLAPSED_H = 82
# macOS ticks 0.25/1.0 with ~100ms AX reads; Windows UIA walks cost ~2s, so
# scale both beats accordingly: fast while unchanged to catch new messages,
# slow right after a change while the model works through the new rows.
FAST_TICK = 0.6
SLOW_TICK = 2.0
IDLE_INTERVAL = 0.5
READ_FAILURE_HIDE_S = 8.0   # sustained read failure while QQ is frontmost
JUDGE_TURNS = 8
MAX_ROWS = 14
FONT = "Microsoft YaHei UI"
IDLE_STATUS = "等待 QQ 消息…"
PALETTE = {
    "bg": "#F5F7F6", "text": "#202C2A", "muted": "#687773",
    "green": "#16775D", "amber": "#A66D20", "red": "#B4474E",
    "surface": "#FFFFFF", "row": "#E9EFEC", "own_row": "#ECF1EF",
    "own_edge": "#E2E6E8", "own_text": "#58636A",
    "metadata_pill": "#F3F5F5", "metadata_text": "#58636A",
    "prob_high_bg": "#E2EDF2", "prob_high_text": "#315B70",
    "prob_mid_bg": "#EAF0F3", "prob_mid_text": "#526D7B",
    "prob_low_bg": "#F1F3F4", "prob_low_text": "#72797E",
    "reply_yes_bg": "#E3F0E9", "reply_wait_bg": "#F5EBDC",
    "action_row": "#F4F7F5", "edge": "#DDE6E1", "accent": "#D7EEE3",
}
LOG_PATH = jev_client.SETTINGS_DIR / "hud.log"


def _log(msg: str) -> None:
    """Stage lines only; never chat text."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class SettingsDialog(tk.Toplevel):
    """Connection mode / URL / model route / API key, Android-parity fields."""

    def __init__(self, root, on_saved):
        super().__init__(root)
        self.on_saved = on_saved
        self.title("Jev · QQ 设置")
        self.configure(bg=PALETTE["bg"])
        self.resizable(False, False)
        self.grab_set()
        current = load_settings()
        frame = tk.Frame(self, bg=PALETTE["bg"], padx=20, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="连接 Jev", bg=PALETTE["bg"], fg=PALETTE["text"],
                 font=(FONT, 16, "bold"), anchor="w").pack(fill="x")
        tk.Label(frame, text="选择数据发送到哪里，保存前可以先测试连接。",
                 bg=PALETTE["bg"], fg=PALETTE["muted"], font=(FONT, 9),
                 anchor="w").pack(fill="x", pady=(2, 14))

        mode_row = tk.Frame(frame, bg=PALETTE["bg"])
        mode_row.pack(anchor="w", pady=(0, 8))
        self.mode = tk.StringVar(value=current["mode"])
        for value, label in jev_client.MODES.items():
            tk.Radiobutton(mode_row, text=label, value=value, variable=self.mode,
                           command=self._mode_changed, bg=PALETTE["bg"],
                           activebackground=PALETTE["bg"], fg=PALETTE["text"],
                           selectcolor=PALETTE["surface"], font=(FONT, 10)).pack(
                               side="left", padx=(0, 16))

        self.vars = {}
        for key, label in (("url", "URL（含 /v1/systemone）"),
                           ("model", "模型路由"), ("api_key", "API Key（仅官方模式需要）")):
            tk.Label(frame, text=label, bg=PALETTE["bg"], fg=PALETTE["text"],
                     font=(FONT, 10, "bold"), anchor="w").pack(fill="x", pady=(8, 3))
            var = tk.StringVar(value=current[key])
            entry = tk.Entry(frame, textvariable=var, width=54, bd=0,
                             bg=PALETTE["surface"], fg=PALETTE["text"],
                             insertbackground=PALETTE["text"], font=(FONT, 10),
                             highlightthickness=1, highlightbackground=PALETTE["edge"],
                             highlightcolor=PALETTE["green"],
                             show="•" if key == "api_key" else "")
            entry.pack(fill="x", ipady=7)
            if key == "api_key":
                self.key_entry = entry
            self.vars[key] = var
        self.show_key = tk.BooleanVar(value=False)
        tk.Checkbutton(frame, text="显示密钥", variable=self.show_key,
                       command=lambda: self.key_entry.config(
                           show="" if self.show_key.get() else "•"),
                       bg=PALETTE["bg"], activebackground=PALETTE["bg"],
                       fg=PALETTE["muted"], font=(FONT, 9)).pack(anchor="w", pady=(4, 0))
        tk.Label(frame, text="外部地址需 HTTPS；本机网关可使用 HTTP。密钥由 Windows DPAPI 保存。",
                 bg=PALETTE["bg"], fg=PALETTE["muted"], font=(FONT, 9),
                 wraplength=480, justify="left").pack(anchor="w", pady=(5, 0))
        self.status = tk.Label(frame, text="", bg=PALETTE["bg"], fg=PALETTE["muted"],
                               font=(FONT, 9), wraplength=480, justify="left")
        self.status.pack(anchor="w", pady=(10, 5))
        buttons = tk.Frame(frame, bg=PALETTE["bg"])
        buttons.pack(fill="x")
        self.test_button = tk.Button(buttons, text="测试连接", command=self._test,
                                     bd=0, bg=PALETTE["row"], fg=PALETTE["text"],
                                     activebackground=PALETTE["accent"], padx=15, pady=7,
                                     cursor="hand2", font=(FONT, 10))
        self.test_button.pack(side="left")
        tk.Button(buttons, text="保存设置", command=self._save, bd=0,
                  bg=PALETTE["green"], fg="white", activebackground="#105B47",
                  activeforeground="white", padx=15, pady=7, cursor="hand2",
                  font=(FONT, 10, "bold")).pack(side="right")

    def _mode_changed(self) -> None:
        # swap the preset URL when the field still holds the other mode's default
        presets = set(jev_client.DEFAULT_URLS.values())
        if self.vars["url"].get().strip() in presets:
            self.vars["url"].set(jev_client.DEFAULT_URLS[self.mode.get()])

    def _test(self) -> None:
        values = (self.mode.get(), self.vars["url"].get(),
                  self.vars["model"].get(), self.vars["api_key"].get())
        self.status.config(text="正在连接…", fg=PALETTE["muted"])
        self.test_button.config(state="disabled")

        def run():
            try:
                backend = jev_client.test_connection(*values)
                outcome = f"连接成功 · 实际路由 {backend}"
                color = PALETTE["green"]
            except Exception as exc:
                outcome = f"失败 · {type(exc).__name__}: {str(exc)[:80]}"
                color = PALETTE["red"]
            def finish():
                if self.winfo_exists():
                    self.status.config(text=outcome, fg=color)
                    self.test_button.config(state="normal")
            try:
                self.after(0, finish)
            except tk.TclError:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _save(self) -> None:
        try:
            save_settings(self.mode.get(), self.vars["url"].get(), self.vars["model"].get(),
                          self.vars["api_key"].get())
        except (OSError, ValueError) as exc:
            self.status.config(text=str(exc), fg=PALETTE["red"])
            return
        self.on_saved()
        self.destroy()


class HudApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Jev · QQ")
        self.root.geometry(f"{PANEL_W}x{PANEL_H}")
        self.root.minsize(340, 280)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.97)
        self.root.configure(bg=PALETTE["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        # 常驻映射 + 屏外停靠：永不用 withdraw/deiconify —— tk 的 deiconify 会显式
        # 激活窗口，把 QQ 挤下前台（曾导致面板每轮读取闪一次的自我振荡）。
        self.root.geometry(f"{PANEL_W}x{PANEL_H}+-32000+-32000")
        self.root.bind("<Map>", lambda _e: self._make_nonactivating())
        self.root.bind("<Configure>", self._on_configure)
        self._make_nonactivating()

        self.memory = ConversationMemory()
        self.judge: JevDirectJudge | None = None
        self._paused = False
        self._collapsed = False
        self._configured = settings_complete()
        self._chat = ""
        self._messages = []
        self._targets: list = []
        self._display_rows: list = []
        self._errors: dict = {}
        self._last_ok_ts = 0.0
        self._visible_signature = None
        self._reset_scroll = True
        self._expanded_keys: set[tuple] = set()
        self._last_width = PANEL_W
        self._expanded_size = (PANEL_W, PANEL_H)
        self._judge_epoch = 0
        self._judge_event = threading.Event()
        self._judge_lock = threading.Lock()
        self._pos_offset = None
        self._placing = False
        self._events: queue.Queue = queue.Queue()
        self._fingerprint = None
        self._layout = None
        self._shown = None
        self._empty_streak = 0
        # macOS-parity state: foreground epoch + read-failure state machine
        self._foreground_epoch = 0
        self._qq_frontmost = None
        self._read_fail_since = None
        self._read_fail_hidden = False
        self._last_full = None

        self._build_ui()
        self._build_menu()
        threading.Thread(target=self._reader_supervisor, daemon=True).start()
        threading.Thread(target=self._judge_loop, daemon=True).start()
        self.root.after(150, self._drain)
        if not self._configured:
            self.root.after(200, self.open_settings)

    # ---------- UI ----------

    def _label(self, parent, text, size, color, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                        font=(FONT, size, "bold" if bold else "normal"), **kwargs)

    def _chip(self, parent, text, bg, fg, size=10):
        chip = tk.Frame(parent, bg=bg)
        self._label(chip, text, size, fg, bold=True).pack(padx=8, pady=2)
        return chip

    def _make_nonactivating(self) -> None:
        """WS_EX_NOACTIVATE: the panel never steals foreground (macOS
        NonactivatingPanel parity).  tk's withdraw() DESTROYS the physical
        window and deiconify() recreates it, dropping custom styles — so this
        must run on every <Map>, not once at startup."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x00000080
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not style & WS_EX_NOACTIVATE:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                      style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg=PALETTE["surface"],
                          highlightbackground=PALETTE["edge"], highlightthickness=1)
        header.pack(fill="x")
        title_group = tk.Frame(header, bg=PALETTE["surface"])
        title_group.pack(side="left", fill="x", expand=True, padx=12, pady=8)
        self._label(title_group, "JEV  /  QQ", 9, PALETTE["green"],
                    bold=True, anchor="w").pack(fill="x")
        self.title_label = self._label(title_group, IDLE_STATUS, 12, PALETTE["text"],
                                       bold=True, anchor="w")
        self.title_label.pack(fill="x")
        self._buttons = {}
        for text, key, command in (("⚙", "settings", self.open_settings),
                                   ("▾", "collapse", self.toggle_collapse),
                                   ("⏸", "pause", self.toggle_pause)):
            button = tk.Button(header, text=text, command=command, bd=0, bg=PALETTE["surface"],
                               fg=PALETTE["muted"], activebackground=PALETTE["row"],
                               font=("Segoe UI", 10), width=3, cursor="hand2")
            button.pack(side="right", padx=(0, 3), pady=8)
            self._buttons[key] = button
        self.status_label = self._label(self.root, "", 9, PALETTE["muted"], anchor="w")
        self.status_label.pack(fill="x", padx=12, pady=(8, 6))

        for widget in (header, title_group, self.title_label):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        self.canvas = tk.Canvas(self.root, bg=PALETTE["bg"], highlightthickness=0)
        self.scroll = ttk.Scrollbar(self.root, orient="vertical", command=self.canvas.yview)
        self.cards = tk.Frame(self.canvas, bg=PALETTE["bg"])
        self.cards.bind("<Configure>",
                        lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.cards, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._scroll_cards)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")

    def _scroll_cards(self, event) -> None:
        if not self._collapsed:
            self.canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def _drag_start(self, event) -> None:
        self._drag_origin = (event.x_root - self.root.winfo_x(),
                             event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        if not hasattr(self, "_drag_origin"):
            return
        x, y = event.x_root - self._drag_origin[0], event.y_root - self._drag_origin[1]
        x = min(max(0, x), self.root.winfo_screenwidth() - 48)
        y = min(max(0, y), self.root.winfo_screenheight() - 48)
        self._place(x, y)
        if self._layout:
            _wid, wx, wy, _ww, _wh = self._layout[:5]
            self._pos_offset = (x - wx, y - wy)

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="重新分析", command=self.reanalyze)
        menu.add_command(label="设置…", command=self.open_settings)
        menu.add_separator()
        menu.add_command(label="退出", command=self.quit)
        self._menu = menu
        for widget in (self.root, self.title_label, self.status_label):
            widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    # ---------- cards (macOS hud.py layout parity) ----------

    def _bar_row(self, message, own: bool, media: bool) -> None:
        bar = tk.Frame(self.cards, bg=PALETTE["own_row"] if own else PALETTE["row"],
                       highlightbackground=PALETTE["own_edge"] if own else PALETTE["row"],
                       highlightthickness=1 if own else 0)
        bar.pack(fill="x", padx=(28 if own else 8, 8), pady=3)
        sender = "我" if own else message.sender or "对方"
        self._label(bar, f"{sender} · {'仅显示' if media else '上下文'}",
                    9, PALETTE["muted"], anchor="w").pack(fill="x", padx=10, pady=(6, 0))
        self._label(bar, message.text, 10, PALETTE["own_text"], anchor="w",
                    wraplength=max(220, self.root.winfo_width() - 90),
                    justify="left").pack(fill="x", padx=10, pady=(0, 6))

    def _peer_card(self, message, key, peer_index: int) -> None:
        card = tk.Frame(self.cards, bg=PALETTE["surface"],
                        highlightbackground=PALETTE["edge"], highlightthickness=1)
        card.pack(fill="x", padx=8, pady=4)
        quote = getattr(message, "quoted_text", "")
        self._label(card, f"{message.sender or '对方'}  ·  {peer_index:02d}",
                    9, PALETTE["muted"], anchor="w").pack(fill="x", padx=12, pady=(10, 0))
        if quote:
            self._label(card, f"引用 · {quote[:70]}", 9, PALETTE["muted"], anchor="w",
                        wraplength=max(220, self.root.winfo_width() - 55),
                        justify="left").pack(fill="x", padx=12, pady=(3, 0))
        self._label(card, message.text, 12, PALETTE["text"], anchor="w",
                    wraplength=max(220, self.root.winfo_width() - 55),
                    justify="left").pack(fill="x", padx=12, pady=(4, 7))

        verdict = self.memory.get_verdict(key) if key else None
        error = self._errors.get(key) if key else None
        if verdict is None:
            self._label(card, error or "正在分析…", 10,
                        PALETTE["red"] if error else PALETTE["muted"],
                        anchor="w").pack(fill="x", padx=12, pady=(0, 10))
            return

        risk = int(round(float(verdict.get("risk", 0))))
        risk_color = (PALETTE["green"] if risk <= 3 else
                      PALETTE["amber"] if risk <= 6 else PALETTE["red"])
        emotion = verdict.get("emotion") if verdict.get("emotion_confidence", 0) >= .45 else None
        verdict_row = tk.Frame(card, bg=PALETTE["surface"])
        verdict_row.pack(fill="x", padx=12, pady=(0, 4))
        self._label(verdict_row, verdict.get("intent", "—"), 12,
                    PALETTE["text"], bold=True, anchor="w").pack(side="left", fill="x", expand=True)
        self._label(verdict_row, f"风险 {risk}/9", 10, risk_color, bold=True,
                    anchor="e").pack(side="right")

        reply_probability = verdict.get("reply_probability")
        if reply_probability is None:
            reply_bg, reply_ink = PALETTE["prob_low_bg"], PALETTE["prob_low_text"]
            reply_label = "是否值得回复 · 待判断"
        elif reply_probability < .40:
            reply_bg, reply_ink = PALETTE["prob_high_bg"], PALETTE["prob_high_text"]
            reply_label = f"是否值得回复 · {reply_probability:.0%}"
        elif reply_probability <= .60:
            reply_bg, reply_ink = PALETTE["reply_wait_bg"], PALETTE["amber"]
            reply_label = f"是否值得回复 · {reply_probability:.0%}"
        else:
            reply_bg, reply_ink = PALETTE["reply_yes_bg"], PALETTE["green"]
            reply_label = f"是否值得回复 · {reply_probability:.0%}"
        summary = tk.Frame(card, bg=PALETTE["surface"])
        summary.pack(fill="x", padx=12, pady=(0, 7))
        self._chip(summary, reply_label, reply_bg, reply_ink, 9).pack(side="left")
        if emotion:
            self._label(summary, emotion, 9, PALETTE["muted"]).pack(side="right")

        expanded = key in self._expanded_keys
        tk.Button(card, text="收起详情  ▴" if expanded else "查看依据与建议  ▾",
                  command=lambda: self._toggle_details(key), bd=0,
                  bg=PALETTE["surface"], fg=PALETTE["green"],
                  activebackground=PALETTE["surface"],
                  activeforeground=PALETTE["green"], cursor="hand2",
                  font=(FONT, 9, "bold"), anchor="w").pack(
                      fill="x", padx=10, pady=(0, 7))
        if expanded:
            self._card_details(card, verdict)

    def _toggle_details(self, key) -> None:
        if key in self._expanded_keys:
            self._expanded_keys.remove(key)
        else:
            self._expanded_keys.add(key)
        self._render_cards()

    def _card_details(self, card, verdict) -> None:
        ranking = " · ".join(f"{item['label']} {item['probability']:.0%}"
                             for item in (verdict.get("intent_ranking") or [])[:3])
        if ranking:
            self._label(card, f"意图可能  {ranking}", 9, PALETTE["muted"],
                        anchor="w", justify="left",
                        wraplength=max(220, self.root.winfo_width() - 55)).pack(
                            fill="x", padx=12, pady=(0, 5))
        behavior = verdict.get("behavior") if verdict.get("behavior_confidence", 0) >= .45 else "—"
        need = verdict.get("need") if verdict.get("need_confidence", 0) >= .45 else "—"
        self._label(card, f"行为 · {behavior}    需要 · {need}", 9,
                    PALETTE["metadata_text"], anchor="w", justify="left",
                    wraplength=max(220, self.root.winfo_width() - 55)).pack(
                        fill="x", padx=12, pady=(0, 4))

        signals = " · ".join(verdict.get("signal_labels", [])[:2])
        if signals:
            self._label(card, signals, 10, PALETTE["muted"], anchor="w").pack(
                fill="x", padx=12, pady=(2, 0))
        self._label(card, "下一步", 10, PALETTE["muted"], bold=True, anchor="w").pack(
            fill="x", padx=12, pady=(3, 1))
        ranked_actions = verdict.get("action_rankings", [])[:3]
        for index, item in enumerate(ranked_actions):
            row = tk.Frame(card, bg=PALETTE["action_row"])
            row.pack(fill="x", padx=10, pady=1)
            self._label(row, f"{index + 1:02d}", 9, PALETTE["muted"]).pack(side="left", padx=(6, 4))
            self._label(row, item["label"], 9, PALETTE["text"], anchor="w",
                        justify="left", wraplength=max(170, self.root.winfo_width() - 115)).pack(
                side="left", fill="x", expand=True)
            score_color = (PALETTE["green"] if item["score"] >= 3 else
                           PALETTE["amber"] if item["score"] >= 2 else PALETTE["muted"])
            self._label(row, f"{item['score']:.1f}/4", 10, score_color, bold=True).pack(
                side="right", padx=6)
        if not ranked_actions:
            self._label(card, "暂无可靠建议", 11, PALETTE["muted"], anchor="w").pack(
                fill="x", padx=12, pady=(0, 4))

    def _render_cards(self) -> None:
        for child in self.cards.winfo_children():
            child.destroy()
        peer_index = 0
        for message, key in self._display_rows:
            kind = getattr(message, "kind", "text")
            if kind != "text":
                self._bar_row(message, own=message.side == "me", media=True)
            elif key is None:
                self._bar_row(message, own=message.side == "me", media=False)
            else:
                peer_index += 1
                self._peer_card(message, key, peer_index)
        if not self._display_rows:
            self._label(self.cards, "当前页面没有对方的可见消息", 9,
                        PALETTE["muted"]).pack(pady=12)
        self.cards.update_idletasks()  # flush layout so the scrollregion reflects
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))  # the NEW content
        if self._reset_scroll:
            self._reset_scroll = False
            self.canvas.yview_moveto(1.0)  # 新视图钉在底部（最新消息），mac 同款
        if not self._paused:  # 暂停时状态钉住“已暂停”，不被计数覆盖
            ready = sum(self.memory.get_verdict(key) is not None
                        for _message, key in self._targets)
            status = (f"对方 {len(self._targets)} 条 · 已分析 {ready} 条" if self._targets
                      else "当前窗口没有可分析的对方文字")
            self.status_label.config(text=status)

    def _set_status(self, text: str, color: str | None = None) -> None:
        self.status_label.config(text=text, fg=color or PALETTE["muted"])

    # ---------- event plumbing ----------

    def _drain(self) -> None:
        try:
            try:
                while True:
                    kind, payload = self._events.get_nowait()
                    if kind == "foreground":
                        self._apply_foreground(payload)
                    elif kind == "read":
                        epoch, result = payload
                        if epoch == self._foreground_epoch:  # drop stale reads
                            self._apply_read(result)
                    elif kind == "verdict":
                        self._render_cards()
            except queue.Empty:
                pass
        except Exception as exc:
            # a UI-callback bug must never kill the after-loop chain: that
            # froze every card at "分析中" while workers kept running
            _log(f"界面刷新异常 {type(exc).__name__}: {str(exc)[:80]}")
        self.root.after(150, self._drain)

    def _apply_foreground(self, frontmost: bool) -> None:
        """macOS parity: leaving QQ parks the panel off-screen and clears
        instantly; returning waits for the next good read before showing."""
        if not frontmost:
            self._targets, self._display_rows, self._messages = [], [], []
            self._visible_signature = None
            self._errors = {}
            self._reset_scroll = True
            self.title_label.config(text=IDLE_STATUS)
            self._set_status("QQ 不在前台")
            for child in self.cards.winfo_children():
                child.destroy()
            if self._shown is not False:
                self._place(-32000, -32000)
                self._shown = False
        # frontmost=True: nothing to show yet; the next good read shows the panel

    def _read_failure_gate(self, result: dict) -> bool:
        """macOS parity: hide only after sustained read failure, reset on ok."""
        if result.get("ok"):
            self._read_fail_since = None
            self._read_fail_hidden = False
            return True
        now = time.monotonic()
        if self._read_fail_since is None:
            self._read_fail_since = now
        if not self._read_fail_hidden and now - self._read_fail_since >= READ_FAILURE_HIDE_S:
            self._read_fail_hidden = True
            self._apply_foreground(False)
            self._set_status(result.get("error", "读取失败"))
        return not self._read_fail_hidden

    def _apply_read(self, result: dict) -> None:
        if not self._read_failure_gate(result):
            return
        if not result.get("ok"):
            self._set_status(result.get("error", "读取失败，正在重试"))
            return
        self._last_ok_ts = time.monotonic()
        if self._shown is not True:
            self._shown = True
            self._follow_qq(force=True)  # 从屏外移回，纯位置移动、不激活
        else:
            self._follow_qq()  # per-read with dead-band, macOS applyPosition parity
        messages = result["messages"]
        chat = (result.get("chat_title") or "").strip()
        if not chat:
            self._judge_epoch += 1
            self._visible_signature = None
            with self._judge_lock:
                self._chat, self._targets, self._messages = "", [], []
            self._display_rows = []
            self.title_label.config(text="会话未识别")
            for child in self.cards.winfo_children():
                child.destroy()
            self._set_status("未识别到会话标题，暂停分析")
            return
        self.title_label.config(text=chat)
        live = self.memory.observe(chat, messages, result.get("at_bottom"))

        # display + targets exactly like the macOS _visible_update
        targets, display_rows = [], []
        prior_incoming = ""
        for message in messages:
            kind = getattr(message, "kind", "text")
            if kind != "text":
                if message.side in ("me", "them"):
                    display_rows.append((message, None))
                continue
            if not message.text.strip():
                continue
            if message.side == "me":
                display_rows.append((message, None))
                continue
            if message.side != "them":
                continue
            key = self.memory.verdict_key(chat, message, prior_incoming)
            targets.append((message, key))
            display_rows.append((message, key))
            prior_incoming = message.text
        display_rows = display_rows[-MAX_ROWS:]

        signature = (chat, tuple((m.side, m.sender or "", getattr(m, "kind", "text"),
                                  m.text, m.quoted_text)
                                 for m, _k in display_rows))
        if signature == self._visible_signature:
            return  # macOS _visible_update: same view, nothing new to show or judge
        self._visible_signature = signature
        self._judge_epoch += 1
        self._errors = {}
        self._reset_scroll = True
        with self._judge_lock:
            self._chat = chat
            self._targets = targets
            self._messages = list(messages)
        self._display_rows = display_rows
        self._render_cards()
        if targets:
            self._judge_event.set()
        _log(f"读取成功 · {len(messages)} 行 · live={live}")

    # ---------- worker threads ----------

    # 会话自愈参数：UIA 会话钝化（快速切会话后读到滞留快照）后重建
    SESSION_MAX_AGE = 300.0     # 最长 5 分钟重建一次会话
    SESSION_MAX_CHANGES = 20    # 或累计 20 次视图变化（数轮会话切换）后重建

    def _reader_supervisor(self) -> None:
        """Restart the reader (and its UIA session) whenever it retires."""
        while True:
            try:
                self._reader_loop()
            except Exception as exc:
                _log(f"读取线程异常退出 {type(exc).__name__}: {str(exc)[:60]}")
            try:
                import comtypes
                comtypes.CoUninitialize()
            except Exception:
                pass
            qq_ax_win.reset_session()
            _log("UIA 会话已重建")
            time.sleep(0.5)

    def _reader_loop(self) -> None:
        """One COM-initialized reader session (macOS _work_inner parity).

        Tracks the foreground epoch, adapts the poll beat, substitutes the
        last full result on unchanged reads, and retires itself on session
        age/churn so the supervisor can rebuild a fresh UIA session.
        """
        try:
            import comtypes
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except Exception:
            pass
        qq_ax_win.ensure_listener()
        heartbeat, next_read_ts, changes = 0, 0.0, 0
        pending_front = [None, 0]  # value, consecutive count — debounce IME/transient flips
        session_started = time.monotonic()
        while True:
            if self._paused or not self._configured:
                time.sleep(IDLE_INTERVAL)
                continue
            try:
                if (time.monotonic() - session_started > self.SESSION_MAX_AGE
                        or changes > self.SESSION_MAX_CHANGES):
                    return  # retire; supervisor rebuilds the UIA session
                observed = frontmost_app_is_qq()
                if observed is None:
                    time.sleep(IDLE_INTERVAL)
                    continue
                if observed != self._qq_frontmost:
                    pending_front[0], pending_front[1] = observed, (
                        pending_front[1] + 1 if pending_front[0] == observed else 1)
                    if pending_front[1] >= 2:  # stable for two checks before switching
                        frontmost = observed
                    else:
                        time.sleep(0.15)
                        continue
                else:
                    frontmost = observed
                    pending_front[1] = 0
                if frontmost is not self._qq_frontmost:  # 前台切换
                    self._qq_frontmost = frontmost
                    self._foreground_epoch += 1
                    self._judge_epoch += 1
                    self._fingerprint = None
                    self._layout = None
                    self._last_full = None
                    self._read_fail_since = None
                    self._read_fail_hidden = False
                    next_read_ts = 0.0
                    self._events.put(("foreground", frontmost))
                    detail = ""
                    if not frontmost:  # 取证：到底谁抢了前台
                        try:
                            import ctypes
                            user32 = ctypes.windll.user32
                            fg = user32.GetForegroundWindow()
                            pid = ctypes.c_ulong()
                            user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
                            name = ctypes.create_unicode_buffer(64)
                            user32.GetClassNameW(fg, name, 64)
                            import psutil
                            proc = psutil.Process(pid.value).info["name"]
                            detail = f" · 前台={name.value} pid={pid.value}({proc})"
                        except Exception:
                            pass
                    _log("前台切换 · " + ("QQ 回到前台，重新读取" if frontmost else "QQ 离开前台") + detail)
                if not frontmost:
                    time.sleep(FAST_TICK)
                    continue
                if time.monotonic() < next_read_ts:
                    time.sleep(0.1)
                    continue
                epoch = self._foreground_epoch
                result = read_conversation(prev_fingerprint=self._fingerprint,
                                           prev_layout=self._layout)
                if self._foreground_epoch != epoch:
                    continue  # foreground flipped mid-read; drop the stale result
                unchanged = bool(result.get("unchanged"))
                if result.get("ok"):
                    self._fingerprint = result.get("fingerprint")
                    self._layout = result.get("layout")
                    if unchanged and self._last_full is not None:
                        result = self._last_full  # full rows for observe()
                    else:
                        self._last_full = result
                        changes += 1  # 内容变化计数：会话切换越多，越早重建 UIA 会话
                self._events.put(("read", (epoch, result)))
                heartbeat += 1
                if heartbeat % 10 == 0:  # ~30s 心跳，用于诊断读取链路
                    _log(f"读取心跳 · unchanged={unchanged} · "
                         f"rows={len(result.get('messages', []))} · "
                         f"frontmost={frontmost_app_is_qq()}")
                next_read_ts = time.monotonic() + (
                    FAST_TICK if unchanged else SLOW_TICK)
            except Exception as exc:  # keep the reader alive no matter what
                _log(f"读取异常 {type(exc).__name__}: {str(exc)[:80]}")
                time.sleep(2.0)

    def _judge_loop(self) -> None:
        while True:
            self._judge_event.wait()
            self._judge_event.clear()
            epoch = self._judge_epoch
            time.sleep(0.55)  # let a scrolling viewport settle before spending calls
            with self._judge_lock:
                chat, targets, messages = self._chat, list(self._targets), list(self._messages)
            pending = sum(1 for _m, key in targets if self.memory.get_verdict(key) is None)
            if pending:
                _log(f"分析循环 · 待分析 {pending}/{len(targets)} 条")
            for message, key in targets:
                if epoch != self._judge_epoch or self._paused:
                    if pending:
                        _log("分析循环被新视图打断")
                    break
                if self.memory.get_verdict(key) is not None:
                    continue
                try:
                    if self.judge is None:
                        self.judge = JevDirectJudge()
                    context = self.memory.context(chat, message, messages, turns=JUDGE_TURNS)
                    verdict = self.judge.judge(message.text, context=context,
                                               quoted_text=message.quoted_text)
                    if epoch != self._judge_epoch or self._paused:
                        break
                    self.memory.put_verdict(key, verdict)
                    _log(f"对方消息已分析 · {verdict.get('intent', '—')}")
                except Exception as exc:
                    self._errors[key] = f"分析失败 · {type(exc).__name__}"
                    _log(f"分析失败 {type(exc).__name__}: {str(exc)[:60]}")
                self._events.put(("verdict", key))

    # ---------- panel behaviour ----------

    def _place(self, x: int, y: int) -> None:
        """Move the panel ourselves; flagged so <Configure> can tell our
        moves from the user's drags."""
        self._placing = True
        self.root.geometry(f"+{x}+{y}")
        self.root.after_idle(lambda: setattr(self, "_placing", False))

    def _on_configure(self, event) -> None:
        """A user drag re-bases the follow offset; our own moves don't."""
        if event.widget is self.root and not self._collapsed and event.width != self._last_width:
            self._last_width = event.width
            if self._display_rows:
                self.root.after_idle(self._render_cards)
        if (event.widget is not self.root or self._placing or self._shown is not True
                or not self._layout or event.x < -10000 or event.y < -10000):
            return
        _wid, wx, wy, _ww, _wh = self._layout[:5]
        if (event.x - wx, event.y - wy) != self._pos_offset:
            self._pos_offset = (event.x - wx, event.y - wy)

    def _follow_qq(self, force: bool = False) -> None:
        """Dock beside QQ; keep the user's drag offset; dead-band small moves.
        Showing/hiding is pure geometry (off-screen parking) — never activate."""
        result_layout = self._layout
        if not result_layout:
            if force:
                self._place(-32000, -32000)
            return
        # layout = (wid, window_x, window_y, window_w, window_h, *viewport_rect)
        _wid, wx, wy, ww, _wh = result_layout[:5]
        if self._pos_offset is None:
            screen_w = self.root.winfo_screenwidth()
            x = wx + ww + 8
            panel_w = self.root.winfo_width() or PANEL_W
            if x + panel_w > screen_w:
                x = max(0, wx - panel_w - 8)
            self._pos_offset = (x - wx, max(0, wy) - wy)
        dx, dy = self._pos_offset
        target = (max(0, wx + dx), max(0, wy + dy))
        current = (self.root.winfo_x(), self.root.winfo_y())
        if force or abs(target[0] - current[0]) > 24 or abs(target[1] - current[1]) > 24:
            self._place(*target)

    def toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._expanded_size = (self.root.winfo_width(), self.root.winfo_height())
            self.canvas.pack_forget()
            self.scroll.pack_forget()
            self.root.resizable(False, False)
            self.root.geometry(f"{self._expanded_size[0]}x{COLLAPSED_H}")
            self._buttons["collapse"].config(text="▴")
        else:
            width, height = self._expanded_size
            self.root.resizable(True, True)
            self.root.geometry(f"{width}x{height}")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.scroll.pack(side="right", fill="y")
            self._buttons["collapse"].config(text="▾")

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        self._buttons["pause"].config(text="▶" if self._paused else "⏸",
                                      fg=PALETTE["amber"] if self._paused else PALETTE["muted"])
        if self._paused:
            self._judge_epoch += 1
            self._judge_event.clear()
            self._set_status("已暂停（点 ▶ 继续）")
            _log("用户暂停")
        else:
            self._fingerprint = None
            self._set_status("读取中…")
            _log("用户继续")

    def reanalyze(self) -> None:
        self.memory.reset_view(self._chat)
        self.memory.invalidate_verdicts(self._chat)
        self._judge_epoch += 1
        self._visible_signature = None
        self._errors = {}
        self._fingerprint = None
        self._set_status("重新分析中…")

    def open_settings(self) -> None:
        SettingsDialog(self.root, self._settings_saved)

    def _settings_saved(self) -> None:
        self._judge_epoch += 1
        self._judge_event.clear()
        self.memory = ConversationMemory()
        with self._judge_lock:
            self._chat, self._targets, self._messages = "", [], []
        self._display_rows = []
        self._expanded_keys.clear()
        self._visible_signature = None
        self._errors = {}
        for child in self.cards.winfo_children():
            child.destroy()
        self._configured = settings_complete()
        self.judge = None  # rebuild with new credentials on next call
        self._fingerprint = None
        self._last_full = None
        self._set_status("设置已保存 · 读取中…" if self._configured else "请完成设置")

    def quit(self) -> None:
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-check":
        # CI verifies that the frozen EXE imports all runtime modules and data.
        # This path never starts the UI, reads QQ, or contacts a model endpoint.
        Path(sys.argv[2]).write_text("ok", encoding="ascii")
    else:
        _log("HUD 启动 · Windows")
        HudApp().run()
