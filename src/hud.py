"""Floating macOS QQ panel for visible peer-message decisions."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import objc
import AppKit
from AppKit import (NSAppearance, NSBackingStoreBuffered, NSColor, NSPanel,
                    NSScreen, NSView, NSWindowCloseButton,
                    NSWindowMiniaturizeButton, NSWindowZoomButton,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskNonactivatingPanel, NSWindowStyleMaskTitled)
from Foundation import NSMakeRect, NSMakeSize, NSObject, NSTimer

sys.path.insert(0, str(Path(__file__).parent))
import userconfig  # noqa: E402
userconfig.load()

from qq_ax import (frontmost_app_is_qq, has_accessibility,
                   read_conversation, request_accessibility)  # noqa: E402
from decision_infra import DecisionInfraJudge  # noqa: E402
from conversation_memory import ConversationMemory  # noqa: E402
import ui_style  # noqa: E402

PANEL_W, PANEL_H = 400, 430
COLLAPSED_H = 96
FAST_TICK = 0.25
SLOW_TICK = 1.0
READ_FAILURE_HIDE_S = 2.0
JUDGE_TURNS = 8
IDLE_STATUS = "等待 QQ 消息…"
PALETTE = ui_style.PALETTE
LOG_PATH = Path.home() / "Library" / "Logs" / "jev-jarvis.log"


def _log(msg: str) -> None:
    """Record stage timing without logging chat text."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        if os.fstat(sys.stdout.fileno()).st_ino == LOG_PATH.stat().st_ino:
            return
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class HudController(NSObject):
    def init(self):
        self = objc.super(HudController, self).init()
        if self is None:
            return None
        self.judge = DecisionInfraJudge()
        self.memory = ConversationMemory()
        self._fixed = []
        self._collapsed = False
        self._paused = False
        self._busy = False
        self._next_read_ts = 0.0
        self._fingerprint = None
        self._layout_key = None
        self._last_full = None
        self._win_wid = None
        self._qq_frontmost = None
        self._foreground_epoch = 0
        self._read_fail_since = None
        self._read_fail_hidden = False
        self._asked_permission = False
        self._chat_title = ""
        self._last_origin = None
        self._pending_origin = None
        self._model_lock = threading.Lock()
        self._build_panel()
        self._install_visible_list()
        return self

    @objc.python_method
    def _build_panel(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskNonactivatingPanel)
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), style, NSBackingStoreBuffered, False)
        self.panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setHasShadow_(True)
        self.panel.setAppearance_(NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
        self.panel.setBackgroundColor_(PALETTE["bg"])
        self.panel.setTitle_("Jev · QQ")
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, PANEL_H))
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        self._solid_backdrop = ui_style.make_surface(0, NSColor.clearColor())
        self._solid_backdrop.setFrame_(view.bounds())
        self._solid_backdrop.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        view.addSubview_(self._solid_backdrop)
        self.rows = {}
        for key, top, size, color in (
            ("chat", 14, 15, PALETTE["text"]),
            ("status", 40, 10, PALETTE["muted"]),
        ):
            label = ui_style.make_label("", 22, 0, PANEL_W - 84, 24 if key == "chat" else 15,
                                        size, color, key == "chat", selectable=True)
            view.addSubview_(label)
            self.rows[key] = label
            self._fixed.append((label, 22, top, PANEL_W - 84,
                                24 if key == "chat" else 15))
        self.settings_button = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 30, 30))
        icon = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "gearshape", "模型设置")
        icon = icon.imageWithSymbolConfiguration_(
            AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                12, AppKit.NSFontWeightRegular))
        self.settings_button.setImage_(icon)
        self.settings_button.setImagePosition_(AppKit.NSImageOnly)
        self.settings_button.setBordered_(False)
        self.settings_button.setContentTintColor_(PALETTE["muted"])
        self.settings_button.setTarget_(self)
        self.settings_button.setAction_("openSettings:")
        self.settings_button.setToolTip_("模型设置")
        view.addSubview_(self.settings_button)
        self._fixed.append((self.settings_button, PANEL_W - 48, 12, 30, 30))
        self.panel.setContentView_(view)
        self._title_h = self.panel.frame().size.height - PANEL_H
        self.rows["status"].setStringValue_(IDLE_STATUS)
        self._wire_window_controls()
        self._install_status_item()
        AppKit.NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
            self, "accessibilityDisplayChanged:",
            AppKit.NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification, None)
        self.applyAccessibilityAppearance_(None)

    @objc.python_method
    def _install_visible_list(self):
        """Show peer verdicts and compact own-message context in visible order."""
        self._visible_signature = None
        self._visible_targets = ()
        self._visible_display_rows = ()
        self._visible_messages = ()
        self._visible_reset_scroll = True
        self._visible_epoch = 0
        self._visible_errors = {}
        self._visible_event = threading.Event()
        self._visible_scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            NSMakeRect(14, 14, PANEL_W - 28, 194))
        self._visible_scroll.setDrawsBackground_(False)
        self._visible_scroll.setHasVerticalScroller_(True)
        self._visible_scroll.setAutohidesScrollers_(True)
        self._visible_scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
        self.panel.contentView().addSubview_(self._visible_scroll)
        # Keep the header above the scrolling cards without detaching AppKit controls.
        for control in (self.rows["chat"], self.rows["status"], self.settings_button):
            control.setWantsLayer_(True)
            control.layer().setZPosition_(10)
        self._resize_visible_panel(194)
        self._draw_visible_cards()
        threading.Thread(target=self._visible_worker_loop, daemon=True).start()

    @objc.python_method
    def _resize_visible_panel(self, document_h: int):
        viewport_h = min(540, document_h)
        content_h = 64 + viewport_h + 14
        self.panel.contentView().setFrameSize_(NSMakeSize(PANEL_W, content_h))
        for control, x, top, w, h in self._fixed:
            if control in (self.rows["chat"], self.rows["status"], self.settings_button):
                control.setFrame_(NSMakeRect(x, content_h - top - h, w, h))
        self._visible_scroll.setFrame_(NSMakeRect(14, 14, PANEL_W - 28, viewport_h))
        frame = self.panel.frame()
        top = frame.origin.y + frame.size.height
        frame_h = content_h + self._title_h
        if not self._collapsed:
            self.panel.setFrame_display_(NSMakeRect(frame.origin.x, top - frame_h,
                                                   PANEL_W, frame_h), True)
        self._expanded_h = frame_h

    @objc.python_method
    def _card_label(self, parent, value, x, top, width, height, size=11,
                    color=None, bold=False, lines=1, card_height=188):
        label = ui_style.make_label(value, x, card_height - top - height, width, height,
                                    size, color or PALETTE["text"], bold)
        label.cell().setWraps_(True)
        label.cell().setScrollable_(False)
        label.setMaximumNumberOfLines_(lines)
        parent.addSubview_(label)
        return label

    @objc.python_method
    def _card_chip(self, parent, value, x, top, width, background, foreground,
                   card_height=320):
        chip = ui_style.make_surface(8, background)
        chip.setFrame_(NSMakeRect(x, card_height - top - 24, width, 24))
        parent.addSubview_(chip)
        label = self._card_label(chip, value, 9, 4, width - 18, 16, 10,
                                 foreground, True, card_height=24)
        label.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        return chip

    @objc.python_method
    def _draw_visible_cards(self):
        targets = self._visible_targets
        display_rows = self._visible_display_rows
        document_h = max(194, sum((320 if key is not None else 54) + 8
                                  for _message, key in display_rows))
        old_y = self._visible_scroll.contentView().bounds().origin.y
        document = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W - 28, document_h))
        cursor = document_h
        peer_index = 0
        for message, key in display_rows:
            card_h = 320 if key is not None else 54
            cursor -= card_h + 8
            y = cursor + 8
            if key is None:
                card = ui_style.make_surface(10, PALETTE["own_row"], PALETTE["own_edge"])
                card.setFrame_(NSMakeRect(28, y, PANEL_W - 56, card_h))
                document.addSubview_(card)
                self._card_label(card, "我 · 上下文", 12, 6, 302, 14, 9,
                                 PALETTE["own_text"], card_height=card_h)
                label = self._card_label(card, message.text, 12, 24, 302, 20, 11,
                                         PALETTE["own_text"], card_height=card_h)
                label.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
                label.setToolTip_(message.text)
                continue
            peer_index += 1
            card = ui_style.make_surface(12, PALETTE["surface"], PALETTE["edge"])
            card.setFrame_(NSMakeRect(0, y, PANEL_W - 28, card_h))
            document.addSubview_(card)
            label = message.sender or "对方"
            quote = getattr(message, "quoted_text", "")
            header = f"{label} · {peer_index}" + (f" · 引用：{quote[:14]}" if quote else "")
            header_label = self._card_label(card, header, 14, 10, 326, 16,
                                            10, PALETTE["muted"], card_height=card_h)
            if quote:
                header_label.setToolTip_(f"引用：{quote}")
            self._card_label(card, message.text, 14, 30, 340, 47,
                             13, PALETTE["text"], False, 3, card_h)
            verdict = self.memory.get_verdict(key)
            error = self._visible_errors.get(key)
            if verdict is None:
                self._card_label(card, error or "分析中…", 14, 86, 340, 20,
                                 12, PALETTE["red"] if error else PALETTE["muted"],
                                 card_height=card_h)
                continue
            risk = int(round(float(verdict.get("risk", 0))))
            risk_color = (PALETTE["green"] if risk <= 3 else
                          PALETTE["amber"] if risk <= 6 else PALETTE["red"])
            emotion = verdict.get("emotion") if verdict.get("emotion_confidence", 0) >= .45 else "难判断"
            self._card_label(card, f"{verdict.get('intent', '—')}   ·   {emotion or '难判断'}",
                             14, 84, 252, 22, 14, PALETTE["text"], True,
                             card_height=card_h)
            self._card_label(card, f"{risk}/9", 298, 86, 58, 19, 12, risk_color, True,
                             card_height=card_h)
            reply_probability = verdict.get("reply_probability")
            if reply_probability is None:
                reply_bg, reply_ink = PALETTE["prob_low_bg"], PALETTE["prob_low_text"]
                reply_label = "是否回复 · 待判断"
            elif reply_probability < .40:
                reply_bg, reply_ink = PALETTE["prob_high_bg"], PALETTE["prob_high_text"]
                reply_label = f"现在回复 {reply_probability:.0%}"
            elif reply_probability <= .60:
                reply_bg, reply_ink = PALETTE["reply_wait_bg"], PALETTE["amber"]
                reply_label = f"现在回复 {reply_probability:.0%}"
            else:
                reply_bg, reply_ink = PALETTE["reply_yes_bg"], PALETTE["green"]
                reply_label = f"现在回复 {reply_probability:.0%}"
            self._card_chip(card, reply_label, 14, 112, 150, reply_bg, reply_ink, card_h)
            ranked_intents = verdict.get("intent_ranking") or []
            self._card_label(card, "意图可能", 14, 149, 50, 14, 9, PALETTE["muted"],
                             card_height=card_h)
            for index, item in enumerate(ranked_intents[:3]):
                probability = item["probability"]
                tone = ("high" if probability >= .60 else
                        "mid" if probability >= .25 else "low")
                self._card_chip(
                    card, f"{item['label']} {probability:.0%}", 66 + index * 98,
                    144, 90, PALETTE[f"prob_{tone}_bg"],
                    PALETTE[f"prob_{tone}_text"], card_h)
            behavior = verdict.get("behavior") if verdict.get("behavior_confidence", 0) >= .45 else "—"
            need = verdict.get("need") if verdict.get("need_confidence", 0) >= .45 else "—"
            self._card_label(card, f"行为 {behavior}   ·   需要 {need}",
                             14, 175, 340, 18, 11, PALETTE["muted"],
                             card_height=card_h)
            signals = " · ".join(verdict.get("signal_labels", [])[:2])
            self._card_label(card, signals, 14, 198, 340, 15, 10,
                             PALETTE["muted"], card_height=card_h)
            self._card_label(card, "下一步", 14, 222, 340, 15, 10,
                             PALETTE["muted"], True, card_height=card_h)
            ranked_actions = verdict.get("action_rankings", [])[:3]
            for index, item in enumerate(ranked_actions):
                row_top = 242 + index * 24
                row = ui_style.make_surface(6, PALETTE["action_row"])
                row.setFrame_(NSMakeRect(12, card_h - row_top - 22, 348, 22))
                card.addSubview_(row)
                self._card_label(row, f"{index + 1:02d}", 8, 3, 24, 16, 9,
                                 PALETTE["muted"], card_height=22)
                action_label = self._card_label(row, item["label"], 34, 2, 238, 18, 10,
                                                PALETTE["text"], card_height=22)
                action_label.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
                score_color = (PALETTE["green"] if item["score"] >= 3 else
                               PALETTE["amber"] if item["score"] >= 2 else
                               PALETTE["muted"])
                self._card_label(row, f"{item['score']:.1f}/4", 282, 2, 58, 18, 10,
                                 score_color, True, card_height=22)
            if not ranked_actions:
                self._card_label(card, "暂无可靠建议", 14, 243, 340, 18, 11,
                                 PALETTE["muted"], card_height=card_h)
        self._visible_scroll.setDocumentView_(document)
        self._visible_document = document
        self._resize_visible_panel(document_h)
        viewport_h = self._visible_scroll.contentView().bounds().size.height
        max_y = max(0, document_h - viewport_h)
        self._visible_scroll.contentView().scrollToPoint_(
            (0, max_y if self._visible_reset_scroll else min(old_y, max_y)))
        self._visible_scroll.reflectScrolledClipView_(self._visible_scroll.contentView())
        self._visible_reset_scroll = False
        ready = sum(self.memory.get_verdict(key) is not None for _message, key in targets)
        status = (f"对方 {len(targets)} 条 · 已分析 {ready} 条" if targets else
                  "当前窗口没有可分析的对方消息")
        self._render("status", status, PALETTE["muted"])

    def applyVisibleRows_(self, epoch):
        if epoch != self._visible_epoch or self._qq_frontmost is not True or self._paused:
            return
        self._show()
        self._draw_visible_cards()

    @objc.python_method
    def _visible_update(self, chat: str, messages: list):
        if self._paused or self._qq_frontmost is not True:
            return
        targets = []
        display_rows = []
        prior_incoming = ""
        for message in messages:
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
        signature = (chat, tuple((message.side, message.sender or "", message.text,
                                  getattr(message, "quoted_text", ""), key)
                                 for message, key in display_rows))
        if signature == self._visible_signature:
            return
        self._visible_signature = signature
        self._visible_targets = tuple(targets)
        self._visible_display_rows = tuple(display_rows)
        self._visible_messages = tuple(messages)
        self._visible_reset_scroll = True
        self._visible_errors = {}
        self._visible_epoch += 1
        self._visible_changed_at = time.monotonic()
        epoch = self._visible_epoch
        self._push("applyVisibleRows:", epoch)
        if any(self.memory.get_verdict(key) is None for _message, key in targets):
            self._visible_event.set()

    @objc.python_method
    def _visible_worker_loop(self):
        while True:
            self._visible_event.wait()
            self._visible_event.clear()
            epoch = self._visible_epoch
            targets = self._visible_targets
            messages = self._visible_messages
            chat = self._visible_signature[0] if self._visible_signature else ""
            # Let a scrolling viewport settle before spending provider calls.
            while epoch == self._visible_epoch and time.monotonic() - self._visible_changed_at < .55:
                time.sleep(.05)
            for message, key in targets:
                if epoch != self._visible_epoch or self._paused or self._qq_frontmost is not True:
                    break
                if self.memory.get_verdict(key) is not None:
                    continue
                context = self.memory.context(chat, message, list(messages),
                                              turns=JUDGE_TURNS)
                try:
                    with self._model_lock:
                        verdict = self.judge.judge(
                            message.text, context=context,
                            quoted_text=getattr(message, "quoted_text", ""))
                    verdict = dict(verdict, context_turns=len(context.splitlines()) if context else 0)
                    self.memory.put_verdict(key, verdict)
                    _log(f"可视对方消息已分析 · {verdict.get('intent', '—')}")
                except Exception as exc:
                    self._visible_errors[key] = f"分析失败 · {type(exc).__name__}"
                    _log(f"可视消息分析失败 {type(exc).__name__}: {str(exc)[:60]}")
                if epoch == self._visible_epoch:
                    self._push("applyVisibleRows:", epoch)

    @objc.python_method
    def _wire_window_controls(self):
        close = self.panel.standardWindowButton_(NSWindowCloseButton)
        mini = self.panel.standardWindowButton_(NSWindowMiniaturizeButton)
        zoom = self.panel.standardWindowButton_(NSWindowZoomButton)
        if close:
            close.setTarget_(self)
            close.setAction_("quitApp:")
            close.setToolTip_("退出 jev-qq-analyst")
        if mini:
            mini.setTarget_(self)
            mini.setAction_("collapsePanel:")
            mini.setToolTip_("收起 / 展开面板")
        if zoom:
            zoom.setHidden_(True)

    @objc.python_method
    def _install_status_item(self):
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.status_item.button().setTitle_("J")
        self.status_item.button().setToolTip_("jev-qq-analyst · QQ 意图助手")
        menu = AppKit.NSMenu.alloc().init()
        for title, action, key in (
            ("显示 / 收起面板", "collapsePanel:", ""),
            ("暂停读取", "togglePause:", ""),
            ("立即重新分析", "reanalyze:", ""),
            ("模型设置…", "openSettings:", ","),
        ):
            menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("退出 jev-qq-analyst", "quitApp:", "q")
        for item in menu.itemArray():
            item.setTarget_(self)
        self.pause_item = menu.itemArray()[1]
        self.status_item.setMenu_(menu)

    def openSettings_(self, sender):
        from settings import SettingsController
        if (getattr(self, "settings_controller", None)
                and self.settings_controller.window.isVisible()):
            self.settings_controller.show()
            return
        try:
            self.settings_controller = SettingsController.alloc().init().build()
            self.settings_controller.show()
        except OSError:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("无法读取配置文件，请检查文件权限。")
            alert.runModal()

    def accessibilityDisplayChanged_(self, notification):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "applyAccessibilityAppearance:", None, False)

    def applyAccessibilityAppearance_(self, notification):
        reduced = AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceTransparency()
        background = ui_style.SOLID_PALETTE["bg"] if reduced else NSColor.clearColor()
        self._solid_backdrop.layer().setBackgroundColor_(background.CGColor())
        self.panel.contentView().setNeedsDisplay_(True)

    @objc.python_method
    def _show(self):
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()

    @objc.python_method
    def _render(self, key: str, text: str, color: NSColor | None = None):
        field = self.rows[key]
        field.setStringValue_(text)
        if color is not None:
            field.setTextColor_(color)

    @objc.python_method
    def _display_height(self) -> float:
        """Height of the display whose origin is (0,0) — the Quartz<->Cocoa flip constant.

        Taking this from the *target* screen is wrong on multi-display setups: a screen
        placed above the main one has origin.y > 0 and the flip must still use the
        primary display's height.
        """
        for scr in NSScreen.screens():
            f = scr.frame()
            if f.origin.x == 0 and f.origin.y == 0:
                return f.size.height
        return NSScreen.mainScreen().frame().size.height

    @objc.python_method
    def _position_near(self, win: dict | None):
        """Dock the panel beside QQ, on the screen QQ is actually on.

        Uses global Cocoa coordinates throughout. NSScreen.mainScreen() must NOT be used:
        it follows whichever display holds the key window, so relying on it made the panel
        hop ~1369 px between displays a few times a minute.
        """
        flip = self._display_height()
        panel_h = self.panel.frame().size.height or PANEL_H
        panel_w = self.panel.frame().size.width or PANEL_W
        screens = list(NSScreen.screens())
        primary = next((s for s in screens
                        if s.frame().origin.x == 0 and s.frame().origin.y == 0), screens[0])

        if win:
            # CGWindow bounds are top-left origin global pixels -> Cocoa bottom-left
            wx, wy = win["x"], win["y"]
            ww, wh = win["w"], win["h"]
            cx_win = wx + ww / 2.0
            cyan = flip - (wy + wh / 2.0)
            host = next((s for s in screens
                         if s.frame().origin.x <= cx_win <= s.frame().origin.x + s.frame().size.width
                         and s.frame().origin.y <= cyan <= s.frame().origin.y + s.frame().size.height),
                        primary)
            sf = host.frame()
            # dock right of QQ if it fits on that screen, else left, else its right edge
            x = wx + ww + 8
            if x + panel_w > sf.origin.x + sf.size.width:
                x = wx - panel_w - 8
            if x < sf.origin.x:
                x = sf.origin.x + sf.size.width - panel_w - 12
            y = flip - wy - panel_h
            y = max(sf.origin.y + 40, min(y, sf.origin.y + sf.size.height - panel_h - 40))
        else:
            sf = primary.frame()
            x = sf.size.width - panel_w - 12
            y = sf.size.height - panel_h - 60

        # dead-band: ignore sub-2pt corrections and one-off blips, so QQ's own window
        # animations (and our own numeric noise) stop nudging the panel around
        target = (round(x), round(y))
        last = self._last_origin
        if last is None:                    # first placement: apply without debounce
            self._last_origin = target
            self._pending_origin = target
            self.panel.setFrameOrigin_(target)
            return
        if abs(target[0] - last[0]) <= 2 and abs(target[1] - last[1]) <= 2:
            return
        if target != self._pending_origin:
            self._pending_origin = target
            return  # require the same target on two consecutive ticks before moving
        self._last_origin = target
        self.panel.setFrameOrigin_(target)

    def collapsePanel_(self, sender):
        self._set_collapsed(not self._collapsed)

    def togglePause_(self, sender):
        self._paused = not self._paused
        self.pause_item.setTitle_("继续读取" if self._paused else "暂停读取")
        self._visible_epoch += 1
        self._visible_signature = None
        self._visible_errors = {}
        if self._paused:
            self._visible_targets = ()
            self._visible_display_rows = ()
            self._visible_messages = ()
            self._draw_visible_cards()
            self._render("status", "已暂停", PALETTE["muted"])
        else:
            self._reset_read()
            self._render("status", "读取中…", PALETTE["muted"])

    def reanalyze_(self, sender):
        self.memory.reset_view(self._chat_title)
        self.memory.invalidate_verdicts(self._chat_title)
        self._visible_epoch += 1
        self._visible_signature = None
        self._visible_errors = {}
        self._reset_read()
        self._render("status", "重新分析中…", PALETTE["muted"])

    def quitApp_(self, sender):
        AppKit.NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        self._visible_scroll.setHidden_(collapsed)
        frame = self.panel.frame()
        new_h = COLLAPSED_H if collapsed else self._expanded_h
        self.panel.setFrame_display_(NSMakeRect(
            frame.origin.x, frame.origin.y + frame.size.height - new_h,
            PANEL_W, new_h), True)
        self._last_origin = None

    @objc.python_method
    def _reset_read(self):
        self._fingerprint = None
        self._layout_key = None
        self._last_full = None
        self._win_wid = None
        self._next_read_ts = 0

    @objc.python_method
    def _set_foreground_state(self, frontmost):
        if frontmost is None or frontmost is self._qq_frontmost:
            return
        self._qq_frontmost = frontmost
        self._foreground_epoch += 1
        self._visible_epoch += 1
        self._visible_signature = None
        self._visible_errors = {}
        self._read_fail_since = None
        self._read_fail_hidden = False
        self._reset_read()
        if frontmost:
            _log("前台切换 · QQ 回到前台，重新读取辅助功能树")
        else:
            _log("前台切换 · QQ 离开前台，隐藏面板")
            self._push("applyForegroundHidden:", "QQ 不在前台")

    def tick_(self, timer):
        frontmost = frontmost_app_is_qq()
        if frontmost is None:
            return
        self._set_foreground_state(frontmost)
        if not frontmost or self._paused or self._busy or time.time() < self._next_read_ts:
            return
        self._busy = True
        threading.Thread(target=self._work, daemon=True).start()

    @objc.python_method
    def _work(self):
        try:
            self._work_inner()
        finally:
            self._busy = False

    @objc.python_method
    def _work_inner(self):
        frontmost = frontmost_app_is_qq()
        if frontmost is None:
            self._next_read_ts = time.time() + FAST_TICK
            return
        self._set_foreground_state(frontmost)
        if not frontmost:
            self._next_read_ts = time.time() + FAST_TICK
            return
        if not has_accessibility():
            if not self._asked_permission:
                self._asked_permission = True
                request_accessibility()
            self._push("applyError:", "需要辅助功能权限 · 系统设置 › 隐私与安全性")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        capture_epoch = self._foreground_epoch
        try:
            result = read_conversation(previous_wid=self._win_wid,
                                       prev_fingerprint=self._fingerprint,
                                       prev_layout=self._layout_key)
        except Exception as exc:
            self._push("applyError:", f"读取失败: {type(exc).__name__}")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        frontmost = frontmost_app_is_qq()
        if frontmost is None:
            self._next_read_ts = time.time() + FAST_TICK
            return
        self._set_foreground_state(frontmost)
        if not frontmost or self._paused or capture_epoch != self._foreground_epoch:
            self._next_read_ts = time.time() + FAST_TICK
            return
        if not result["ok"]:
            now = time.monotonic()
            if self._read_fail_since is None:
                self._read_fail_since = now
            if (not self._read_fail_hidden
                    and now - self._read_fail_since >= READ_FAILURE_HIDE_S):
                self._read_fail_hidden = True
                self._visible_epoch += 1
                self._visible_signature = None
                self._reset_read()
                self._push("applyForegroundHidden:", result["error"])
            self._next_read_ts = time.time() + FAST_TICK
            return
        self._read_fail_since = None
        self._read_fail_hidden = False
        self._fingerprint = result.get("fingerprint")
        self._layout_key = result.get("layout")
        self._next_read_ts = time.time() + (FAST_TICK if result["unchanged"] else SLOW_TICK)
        live_window = result["window"]
        self._win_wid = live_window["wid"]
        self._push("applyPosition:", live_window)
        if result["unchanged"] and self._last_full is not None:
            result = self._last_full
        else:
            self._last_full = result
            self._push("applyChat:", result.get("chat_title") or "")
        chat = result.get("chat_title") or ""
        messages = result["messages"]
        self.memory.observe(chat, messages, result.get("at_bottom"))
        self._visible_update(chat, messages)

    @objc.python_method
    def _push(self, selector: str, payload=None):
        if selector in {"applyChat:", "applyError:", "applyForegroundHidden:",
                        "applyPosition:"}:
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "applyEpochUpdate:", (self._foreground_epoch, selector, payload), False)
            return
        self.performSelectorOnMainThread_withObject_waitUntilDone_(selector, payload, False)

    def applyEpochUpdate_(self, update):
        epoch, selector, payload = update
        if epoch == self._foreground_epoch:
            getattr(self, selector.replace(":", "_"))(payload)

    def applyChat_(self, title):
        if self._qq_frontmost is not True:
            return
        self._chat_title = title
        self._render("chat", title, PALETTE["accent"])

    def applyError_(self, message):
        if self._qq_frontmost is True:
            self._show()
            self._render("status", message, PALETTE["red"])

    def applyForegroundHidden_(self, reason):
        self._visible_targets = ()
        self._visible_display_rows = ()
        self._visible_messages = ()
        self._visible_signature = None
        self._draw_visible_cards()
        self._chat_title = ""
        self._render("chat", "", PALETTE["muted"])
        self._render("status", reason, PALETTE["muted"])
        if self.panel.isVisible():
            self.panel.orderOut_(None)

    def applyPosition_(self, win):
        if self._qq_frontmost is True:
            self._position_near(win)

def main() -> None:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    controller = HudController.alloc().init()
    if os.environ.get("JEV_VISUAL_DEMO") == "1":
        # Synthetic, offline visual acceptance. Never reads QQ or calls the gateway.
        controller.applyChat_("对话预览")
        examples = (
            ("这件事今天能有个结果吗？🙂", "催进度", "着急", "时间承诺", 4.2),
            ("我还想确认一下时间。", "求确认", "平静", "确认", 2.1),
        )
        targets = []
        display_rows = []
        previous = ""
        for message, intent, emotion, need, risk in examples:
            item = SimpleNamespace(side="them", sender="对方", text=message)
            key = controller.memory.verdict_key("对话预览", item, previous)
            controller.memory.put_verdict(key, {
                "message": message, "intent": intent, "confidence": .86,
                "risk": risk, "behavior": "提问", "behavior_confidence": .8,
                "emotion": emotion, "emotion_confidence": .72,
                "need": need, "need_confidence": .8,
                "intent_ranking": [
                    {"label": intent, "probability": .68},
                    {"label": "求确认", "probability": .21},
                    {"label": "闲聊", "probability": .08},
                ],
                "reply_probability": .76,
                "action_rankings": [
                    {"id": "answer", "label": "直接回答对方的问题", "score": 3.8},
                    {"id": "schedule", "label": "给出可信的时间安排", "score": 3.4},
                    {"id": "clarify", "label": "先澄清对方具体指什么", "score": 2.9},
                ],
                "signal_labels": ["可能有隐含请求"],
                "actions": ["先给明确答复", "确认时间"],
            })
            targets.append((item, key))
            display_rows.append((item, key))
            previous = message
            if len(display_rows) == 1:
                display_rows.append((SimpleNamespace(
                    side="me", sender=None, text="我先确认一下，稍后回复你。"), None))
        controller._visible_targets = tuple(targets)
        controller._visible_display_rows = tuple(display_rows)
        controller._visible_signature = ("对话预览", tuple(key for _item, key in targets))
        controller._qq_frontmost = True
        controller.applyVisibleRows_(controller._visible_epoch)
        controller._render("status", "界面预览 · 合成消息", PALETTE["muted"])
        controller.panel.center()
        controller._show()
        app.run()
        return
    _log(f"启动 · 判断层 Decision Infra"
         f" · {controller.judge.endpoint} · 路由 {controller.judge.model}"
         f" · 仅分析可见对方消息")
    controller._show()
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        FAST_TICK, controller, "tick:", None, True)
    AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, AppKit.NSDefaultRunLoopMode)
    app.run()


if __name__ == "__main__":
    main()
