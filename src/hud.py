"""Jev-only floating HUD beside macOS QQ showing intent and risk.

Design notes
  * NSWindowStyleMaskNonactivatingPanel + floating level: the panel never steals focus
    from QQ.
  * Poll loop: read the chat, hash the newest message, judge only when it changes.
  * The moment a new message is seen, Jev pre-analysis starts. The settle gate only shows
    a verdict that still belongs to the newest visible incoming message.
  * The panel positions itself against QQ's window each tick, so it follows moves,
    resizes and monitor changes without any window-server hooks.
  * The HUD uses native macOS vibrancy with semantic green/amber/red accents. The
    Appearance stays pinned to Aqua so labels and controls keep the same tested contrast.
  * There is no reply generator, candidate ranking, copy, fill, or send action.
"""

from __future__ import annotations

import objc
import os
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import AppKit
import Quartz
import sys

from AppKit import (
    NSAppearance,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBackgroundColorAttributeName,
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSPanel,
    NSPopUpButton,
    NSScreen,
    NSTextField,
    NSView,
    NSWindowMiniaturizeButton,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled,
    NSWindowZoomButton,
    NSWindowCloseButton,
)
from Foundation import (NSMutableAttributedString, NSMakeRange, NSMakeRect,
                        NSMakeSize, NSObject, NSTimer)

sys.path.insert(0, str(Path(__file__).parent))
import userconfig  # noqa: E402

userconfig.load()   # ~/.config/jev-jarvis/env -> os.environ (Finder apps inherit none)

from qq_ax import (  # noqa: E402
    frontmost_app_is_wechat, read_conversation, screen_capture_ok,
    request_screen_capture, warm_ocr)
from decision_infra import DecisionInfraJudge  # noqa: E402
from conversation_memory import ConversationMemory  # noqa: E402
import styles  # noqa: E402
import ui_style  # noqa: E402

PANEL_W, PANEL_H = 400, 430   # dark, flat decision HUD
COLLAPSED_H = 96              # height when the panel is rolled up
# The tick timer fires at FAST_TICK; a read only runs when due. A quiet screen (fingerprint
# match ⇒ no OCR) re-checks every FAST_TICK — a new message surfaces within 0.25 s instead
# of within 1 s. A read that found a change (full capture+OCR paid) first keeps a SHORT
# cadence for a few reads (a burst's next message is noticed in ~0.45 s, not after a full
# SLOW_TICK) and only settles back to SLOW_TICK if the pane keeps moving — that is the
# cadence the old fixed poll had, kept as the CPU guard for a continuously moving screen.
FAST_TICK = 0.25         # re-check cadence while the chat pane is quiet
BURST_TICK = 0.45        # short cadence right after a change: catch the burst's next message
BURST_READS = 3          # how many reads stay on BURST_TICK before falling back to SLOW_TICK
SLOW_TICK = 1.0          # re-check cadence while the chat pane keeps moving
READ_FAILURE_HIDE_S = 2.0  # do not flicker on a transient capture/window miss
SETTLE_S = 1.2           # upper bound on the settle wait (anti-flood; unchanged by design)
EARLY_SETTLE_S = 0.70    # the gate may open this early …
STABLE_READS = 3         # … but only after this many consecutive unchanged reads
MIN_GAP_S = 2.0          # never restart analysis faster than this
CONTEXT_TURNS = 4        # recent turns the generation half sees
JUDGE_TURNS = 8          # session-observed turns; capped again by character count
IDLE_STATUS = "等待 QQ 消息…"       # the resting status line (also set at build time)
WARM_STATUS = "正在连接 Decision Infra…"


# Shared with the settings window so both surfaces keep one visual vocabulary.
PALETTE = ui_style.PALETTE
_rgb = ui_style.rgb

# Compact reply rows: probability rail, fully wrapped reply, then the two existing actions.
# Only the minimum is fixed. _relayout() measures each candidate and grows the row as needed.
CAND_ROW_X, CAND_ROW_W, CAND_ROW_MIN_H = 20, PANEL_W - 40, 50
CAND_PROB_X, CAND_PROB_W = 30, 44
CAND_TEXT_X, CAND_TEXT_W = 82, 144
CAND_BTN_W, CAND_BTN_H, CAND_BTN_GAP = 48, 22, 4
CAND_BTN_X = PANEL_W - 26 - (2 * CAND_BTN_W + CAND_BTN_GAP)
CAND_ROW_GAP = 4

# 话术 groups. Each group is headed by its dropdown; its candidates sit under it. The panel
# is only as tall as the groups in use, so nothing is reserved for a tone that is switched
# off (that reservation is what used to leave a dead gap in the middle).
TONE_DD_X, TONE_DD_W, TONE_DD_H, TONE_DD_GAP = 20, PANEL_W - 40, 24, 5
TONE_DD_INSET = 8         # the popup sits this far inside its field, like text in an input box
TONE_DD_FONT = 12         # compact but still the clearest interactive label in each group
                          # thing on the panel the user is meant to click
GROUP_PAD_Y = 5           # breathing room above the selector and below the final reply
GROUP_GAP = 10            # between one group's rows and the next group's dropdown
BOTTOM_PAD = 14           # below the last group


LOG_PATH = Path.home() / "Library" / "Logs" / "jev-jarvis.log"


def _log(msg: str) -> None:
    """One line per stage: to stdout, and into ~/Library/Logs/jev-jarvis.log.

    "It feels slow" is not actionable on its own, so every analysis prints what each stage
    cost; that is the whole point of this function. Deliberately **no message text and no
    candidate text**: this file is meant to be pasted into an issue, and the app's premise
    is that chat content stays on the machine.

    Both destinations on purpose: the .app launcher already redirects stdout into this same
    file, while `./start.command` only shows a terminal — so which place held the evidence
    depended on how the user happened to launch it. The inode check stops the .app case
    from writing every line twice.
    """
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        if os.fstat(sys.stdout.fileno()).st_ino == LOG_PATH.stat().st_ino:
            return                       # stdout already IS that file (the .app case)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass                             # a log we cannot write is not worth breaking over


class _BoxesView(NSView):
    """The YOLO overlay's canvas: paints whatever `boxes` last held.

    boxes: [(NSRect, NSColor, line_width, NSAttributedString chip), ...] in view
    coordinates, set from the main thread and followed by setNeedsDisplay_. The view
    owns no data — it only renders the controller's most recent read, which is what
    keeps the overlay honest: what you see boxed is exactly what the pipeline read.
    """

    def drawRect_(self, rect):
        for box in getattr(self, "boxes", None) or []:
            r, color, lw, chip = box[:4]
            color.set()
            NSBezierPath.setDefaultLineWidth_(lw)
            if len(box) > 4 and box[4]:
                path = NSBezierPath.bezierPathWithRect_(r)
                path.setLineWidth_(lw)
                path.setLineDash_count_phase_([6.0, 4.0], 2, 0)
                path.stroke()
            else:
                NSBezierPath.strokeRect_(r)
            chip.drawAtPoint_((r.origin.x, r.origin.y + r.size.height + 2))


class HudController(NSObject):
    def init(self):
        self = objc.super(HudController, self).init()
        if self is None:
            return None
        self.last_seen = None          # newest message text observed
        self._reply_key = None         # (conversation, incoming text), never an outgoing message
        self._reply_epoch = 0          # invalidate even if the same text reappears later
        self._reply_worker = threading.local()
        self.last_change_ts = 0.0      # when it last changed (burst detection)
        self.last_analyze_ts = 0.0     # rate limit for analysis starts
        self.analyzed_text = None      # what the panel currently shows
        self._judged_once = False      # first judge call includes the local model load
        self._read_once = False        # first AX read is called out separately in timing
        self._last_skip_reason = None
        self.judge = DecisionInfraJudge()
        self.memory = ConversationMemory()
        self._normal_status = ("等待 QQ 消息…", PALETTE["muted"])
        self._model_status = None
        # Jev-only mode: no generator, tone picker, candidate rows, copy, or fill action.
        self.slot_tones = []
        self._dds: list = []
        self._dd_boxes: list = []       # the flat fields the dropdowns are drawn into
        self._group_boxes: list = []    # translucent surfaces behind active tone groups
        self._rows: list = []
        self._appearance_surfaces = []
        self._appearance_buttons = []
        self._message_expanded = False
        self._message_text = ""
        self._layout_key = None
        self._fixed: list = []          # (control, x, dy_from_top, w, h) — the rows above
        self._detail_views: list = []   # non-data chrome hidden with the expanded details
        self._risk_dots: list = []      # low / medium / high indicators, presentation only
        self._group_top = 0             # where the first group starts, from the top
        self._title_h = 28              # measured right after the panel is built
        self.cand_texts: list[str | None] = [None] * (styles.MAX_SLOTS * styles.PER_TONE)
        self._last_intent = ""          # kept so a tone change can re-rank without re-judging
        # streaming candidates: each generation run bumps this epoch at its start and its
        # streamed lines carry the value, so a late line from a run a tone change or a new
        # message superseded is dropped instead of written into the new run's rows
        self._gen_epoch = 0
        self._stream_rows: dict[int, int] = {}   # slot -> lines already shown, per run

        self._busy = False
        self._next_read_ts = 0.0    # reads before this timestamp are skipped (quiet screen)
        self._fingerprint = None    # last visible-message fingerprint; equal ⇒ reuse result
        self._last_full = None      # last full AX result, reused while the pane is unchanged
        self._analyzing = False     # judge+generate runs off the tick path
        # Pre-judgment: the local judge starts the moment a new message is seen, and the
        # settle gate consumes the verdict if the text is unchanged — intent/risk land on
        # screen ~1 s earlier and only the (paid) generation half still waits. Single-slot
        # request = latest-wins: a newer text overwrites the slot and retires the verdict.
        self._model_lock = threading.Lock()   # never two local forwards (judge/rank) at once
        self._prejudge_req = None             # (text, context, sender, prev, reply epoch)
        self._prejudge_result = None          # (text, verdict, sender, prev, reply epoch)
        self._prejudging = False              # a pre-judge forward is running right now
        self._prejudge_event = threading.Event()
        threading.Thread(target=self._prejudge_loop, daemon=True).start()
        # Early generation: the paid half starts the moment a message is seen too, with the
        # same latest-wins slot discipline. The settle window (~1 s) then hides the whole
        # generation latency, and only the local ranking is left after the gate opens.
        # Cost: a burst's intermediate messages each fire one discarded API call — cheap at
        # glm-4-flash-class pricing, and superseded results are never consumed.
        self._pregen_req = None              # (text, context, tones tuple, reply epoch)
        self._pregen_result = None           # (text, tones, gen dict, reply epoch)
        self._pregen_running = False         # a pre-generation request is in flight
        self._pregen_event = threading.Event()  # legacy state; no worker is started
        self._burst_left = BURST_READS       # short-cadence reads left after a change
        self._stable_n = 0                   # consecutive unchanged reads since last change
        self._collapsed = False
        self._expanded_h = None       # full height, captured the first time we collapse
        self._paused = False
        # YOLO overlay default: JEV_BOXES=1 (or true/yes/on) in the env file starts it on;
        # either way the menu-bar item flips it at runtime
        self._show_boxes = userconfig.get("JEV_BOXES").strip().lower() in (
            "1", "true", "yes", "on")
        self._last_risk = 0.0         # newest verdict's risk, for the overlay's highlight
        self._chat_title = ""
        self._asked_permission = False
        self._win_wid = None          # current QQ window id (AX focus still wins)
        self._wechat_frontmost = None # foreground boundary; False means capture state is stale
        self._foreground_epoch = 0    # catches leave+return while one capture is in flight
        self._read_fail_since = None  # debounce transient foreground capture failures
        self._read_fail_hidden = False
        self._last_origin = None      # last applied panel origin
        self._pending_origin = None   # candidate origin awaiting confirmation
        self._build_compact_panel()
        self._install_visible_list()
        self._build_overlay()
        self._expanded_h = self.panel.frame().size.height
        return self

    # ------------------------------------------------------------------ ui
    @objc.python_method
    def _build_compact_panel(self):
        """Quiet native decision card: message, judgment, next action, optional clues."""
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
        self._solid_backdrop.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        view.addSubview_(self._solid_backdrop)
        self.rows = {}

        def add_label(key, x, top, w, h, size, color, bold=False, detail=True):
            label = self._make_label(x, 0, w, h, size=size, color=color, bold=bold)
            view.addSubview_(label)
            self.rows[key] = label
            self._fixed.append((label, x, top, w, h))
            if detail:
                self._detail_views.append(label)
            return label

        def add_surface(x, top, w, h, color=PALETTE["surface"]):
            surface = self._make_surface(14, color)
            view.addSubview_(surface)
            self._fixed.append((surface, x, top, w, h))
            self._detail_views.append(surface)
            return surface

        add_label("chat", 22, 14, PANEL_W - 84, 24, 15, PALETTE["text"], True, False)
        add_label("status", 22, 40, PANEL_W - 44, 15, 10, PALETTE["muted"], False, False)
        self.settings_button = self._make_button(PANEL_W - 46, 0, 30, 30,
                                                 "", "openSettings:", 0)
        icon = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "gearshape", "模型设置")
        icon = icon.imageWithSymbolConfiguration_(
            AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                12, AppKit.NSFontWeightRegular))
        self.settings_button.setImage_(icon)
        self.settings_button.setImagePosition_(AppKit.NSImageOnly)
        self.settings_button.setBordered_(False)
        self.settings_button.setContentTintColor_(PALETTE["muted"])
        self.settings_button.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
        self.settings_button.layer().setBorderWidth_(0)
        self.settings_button.setToolTip_("模型设置")
        self.settings_button.setHidden_(False)
        view.addSubview_(self.settings_button)
        self._fixed.append((self.settings_button, PANEL_W - 48, 12, 30, 30))

        self._message_surface = add_surface(18, 68, PANEL_W - 36, 77, PALETTE["surface"])
        add_label("sender", 30, 77, PANEL_W - 112, 14, 10, PALETTE["muted"])
        message = self._make_label(0, 0, PANEL_W - 60, 36, size=13,
                                   color=PALETTE["text"])
        message.cell().setWraps_(True)
        message.cell().setScrollable_(False)
        message.cell().setUsesSingleLineMode_(False)
        message.cell().setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        message.setMaximumNumberOfLines_(2)
        self.rows["message"] = message
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(30, 0, PANEL_W - 60, 36))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
        scroll.setDocumentView_(message)
        self._message_scroll = scroll
        view.addSubview_(scroll)
        self._fixed.append((scroll, 30, 98, PANEL_W - 60, 36))
        self._detail_views.extend((scroll, message))
        self._message_toggle = self._make_button(PANEL_W - 84, 0, 60, 18,
                                                  "展开", "toggleMessage:", 0)
        self._message_toggle.setAccessibilityLabel_("展开或收起消息")
        view.addSubview_(self._message_toggle)
        self._fixed.append((self._message_toggle, PANEL_W - 90, 74, 60, 18))
        self._detail_views.append(self._message_toggle)

        add_surface(22, 161, PANEL_W - 44, 1, PALETTE["edge"])
        for title, x, width in (("意图", 22, 120), ("情绪", 162, 104), ("风险", 312, 62)):
            label = self._make_label(0, 0, width, 14, size=10, color=PALETTE["muted"])
            label.setStringValue_(title)
            view.addSubview_(label)
            self._fixed.append((label, x, 177, width, 14))
            self._detail_views.append(label)
        add_label("intent", 22, 194, 132, 30, 23, PALETTE["text"], True)
        add_label("emotion", 162, 198, 124, 26, 17, PALETTE["text"], True)
        add_label("risk", 312, 198, 80, 25, 14, PALETTE["green"], True)
        add_label("confidence", 22, 225, 120, 14, 10, PALETTE["muted"])
        add_label("behavior", 22, 252, PANEL_W - 44, 18, 12, PALETTE["text"])
        add_label("need", 22, 277, PANEL_W - 44, 18, 12, PALETTE["text"])
        add_label("signals", 22, 302, PANEL_W - 44, 18, 11, PALETTE["muted"])
        add_label("context", 22, 326, PANEL_W - 44, 18, 10, PALETTE["muted"])

        add_surface(22, 350, PANEL_W - 44, 1, PALETTE["edge"])
        action_title = self._make_label(0, 0, 48, 16, size=10, color=PALETTE["muted"])
        action_title.setStringValue_("下一步")
        view.addSubview_(action_title)
        self._fixed.append((action_title, 22, 368, 48, 16))
        self._detail_views.append(action_title)
        actions = add_label("actions", 22, 390, PANEL_W - 44, 38, 13, PALETTE["text"])
        actions.cell().setWraps_(True)

        # Compatibility status sink for old no-op candidate callbacks. It has no
        # frame in the visible card and is always hidden.
        hidden = self._make_label(0, 0, 1, 1, size=1)
        hidden.setHidden_(True)
        self.rows["cand_header"] = hidden
        view.addSubview_(hidden)
        self._group_top = 430
        self.panel.setContentView_(view)
        self._title_h = self.panel.frame().size.height - PANEL_H
        self._relayout()
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
        for control in self._detail_views:
            control.setHidden_(True)
        self._visible_mode = True
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
        if epoch != self._visible_epoch or self._wechat_frontmost is not True:
            return
        self._show()
        self._draw_visible_cards()

    @objc.python_method
    def _visible_update(self, chat: str, messages: list):
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
                if epoch != self._visible_epoch or self._paused or self._wechat_frontmost is not True:
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
    def _build_panel(self):
        # Closable/Miniaturizable are what actually CREATE the standard window buttons;
        # NonactivatingPanel alone gives a title bar with no controls at all.
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskNonactivatingPanel)
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), style, NSBackingStoreBuffered, False)
        self.panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setAlphaValue_(1.0)
        self.panel.setHasShadow_(True)
        # The title bar and button bezels are drawn from the appearance, not from the
        # background colour, so pin Aqua: a dark-mode system would otherwise give a dark
        # title bar above a white panel.
        self.panel.setAppearance_(NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setTitle_("jev-jarvis")
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)

        # NSVisualEffectView is the native implementation of the reference's light frosted
        # material. The tint keeps text readable when the wallpaper behind it is busy.
        view = AppKit.NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H))
        view.setMaterial_(getattr(
            AppKit, "NSVisualEffectMaterialSidebar",
            getattr(AppKit, "NSVisualEffectMaterialLight", 1)))
        view.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        view.setState_(AppKit.NSVisualEffectStateActive)
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        # A tint subview sits above the system material. Setting the effect view's
        # backing-layer color alone can be covered by macOS's accessibility fallback.
        self._solid_backdrop = ui_style.make_surface(0, NSColor.clearColor())
        self._solid_backdrop.setFrame_(view.bounds())
        self._solid_backdrop.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        view.addSubview_(self._solid_backdrop)
        self.rows: dict[str, NSTextField] = {}

        # The latest master adds model settings to this same header. Keep it as a quiet,
        # standalone icon so the new control does not collide with the chat title.
        self.settings_button = self._make_button(PANEL_W - 44, 0, 32, 32,
                                                 "", "openSettings:", 0)
        settings_icon = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "gearshape", "模型设置")
        symbol_config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
            12, AppKit.NSFontWeightRegular)
        settings_icon = settings_icon.imageWithSymbolConfiguration_(symbol_config)
        self.settings_button.setImage_(settings_icon)
        self.settings_button.setImagePosition_(AppKit.NSImageOnly)
        self.settings_button.setImageScaling_(AppKit.NSImageScaleNone)
        self.settings_button.setBordered_(False)
        self.settings_button.setContentTintColor_(PALETTE["muted"])
        self.settings_button.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
        self.settings_button.layer().setBorderWidth_(0.0)
        self.settings_button.setToolTip_("模型设置")
        self.settings_button.setAccessibilityLabel_("模型设置")
        self.settings_button.setHidden_(False)
        view.addSubview_(self.settings_button)
        self._fixed.append((self.settings_button, PANEL_W - 44, 4, 32, 32))

        # Decorative surfaces are fixed; every string still comes from the existing rows.
        for surface, x, top, w, h in (
            (self._make_surface(12, PALETTE["surface"]), 14, 54, PANEL_W - 28, 62),
            (self._make_surface(12, PALETTE["surface"]), 14, 124, PANEL_W - 28, 72),
            (self._make_surface(10, PALETTE["surface"]), 14, 204, PANEL_W - 28, 34),
        ):
            if top == 54:
                self._message_surface = surface
            view.addSubview_(surface)
            self._fixed.append((surface, x, top, w, h))
            self._detail_views.append(surface)

        # Summary separators and static labels carry no model data; they only make the
        # existing intent/risk/action fields scan like the approved design.
        for x in (150, 260):
            divider = self._make_surface(0, PALETTE["edge"])
            view.addSubview_(divider)
            self._fixed.append((divider, x, 136, 1, 46))
            self._detail_views.append(divider)

        action_label = self._make_label(0, 0, 58, 16, size=11,
                                        color=PALETTE["text"], bold=True)
        action_label.setStringValue_("具体行动")
        view.addSubview_(action_label)
        self._fixed.append((action_label, 26, 213, 58, 16))
        self._detail_views.append(action_label)

        risk_title = self._make_label(0, 0, 64, 14, size=9, color=PALETTE["muted"])
        risk_title.setStringValue_("风险等级")
        view.addSubview_(risk_title)
        self._fixed.append((risk_title, 272, 132, 64, 14))
        self._detail_views.append(risk_title)
        for i, (title, color) in enumerate((
            ("低", PALETTE["green"]), ("中", PALETTE["amber"]), ("高", PALETTE["red"]))):
            center_x = 278 + i * 26
            dot = self._make_surface(4, color.colorWithAlphaComponent_(0.68))
            view.addSubview_(dot)
            self._fixed.append((dot, center_x - 4, 152, 8, 8))
            self._detail_views.append(dot)
            self._risk_dots.append(dot)
            label = self._make_label(0, 0, 20, 14, size=9, color=PALETTE["muted"])
            label.setAlignment_(AppKit.NSTextAlignmentCenter)
            label.setStringValue_(title)
            view.addSubview_(label)
            self._fixed.append((label, center_x - 10, 164, 20, 14))
            self._detail_views.append(label)

        for key, x, top, w, h, size, color, bold in (
            ("chat", 20, 14, PANEL_W - 76, 20, 15, PALETTE["accent"], True),
            ("status", 20, 36, PANEL_W - 40, 14, 10, PALETTE["muted"], False),
            ("message", 22, 82, PANEL_W - 44, 38, 14, PALETTE["text"], False),
            ("sender", 22, 62, PANEL_W - 112, 14, 10, PALETTE["muted"], False),
            ("intent", 26, 136, 116, 26, 20, PALETTE["text"], True),
            ("confidence", 26, 166, 116, 16, 11, PALETTE["muted"], False),
            ("risk", 164, 137, 92, 24, 14, PALETTE["green"], True),
            ("actions", 94, 213, 236, 16, 11, PALETTE["text"], False),
        ):
            tf = self._make_label(x, 0, w, h, size=size, color=color, bold=bold)
            if key in {"message", "actions"}:
                tf.cell().setWraps_(True)
            self.rows[key] = tf
            if key == "message":
                tf.cell().setScrollable_(False)
                tf.cell().setUsesSingleLineMode_(False)
                tf.cell().setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
                tf.setMaximumNumberOfLines_(2)
                scroll = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(x, 0, w, h))
                scroll.setDrawsBackground_(False)
                scroll.setHasVerticalScroller_(False)
                scroll.setAutohidesScrollers_(True)
                scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
                scroll.setDocumentView_(tf)
                self._message_scroll = scroll
                view.addSubview_(scroll)
                self._fixed.append((scroll, x, top, w, h))
                self._detail_views.append(scroll)
            else:
                view.addSubview_(tf)
                self._fixed.append((tf, x, top, w, h))
            if key not in {"chat", "status"}:
                self._detail_views.append(tf)

        self._message_toggle = self._make_button(PANEL_W - 82, 0, 60, 18,
                                                  "展开 ▾", "toggleMessage:", 0)
        self._message_toggle.setAccessibilityLabel_("展开或收起完整消息")
        view.addSubview_(self._message_toggle)
        self._fixed.append((self._message_toggle, PANEL_W - 82, 60, 60, 18))
        self._detail_views.append(self._message_toggle)

        header = self._make_label(18, 0, PANEL_W - 36, 18,
                                  size=12, color=PALETTE["text"], bold=True)
        view.addSubview_(header)
        self.rows["cand_header"] = header
        self._set_candidate_header("仅做 Jev 分析 · 不生成回复")
        self._fixed.append((header, 18, 250, PANEL_W - 36, 18))
        self._detail_views.append(header)
        self._group_top = 274

        # ---- 话术 groups: each dropdown heads a group and its candidates sit underneath,
        # so the tone is labelled by the thing that selects it. Every group's controls exist
        # from the start; _relayout() decides which are on screen. The button tags are slot
        # arithmetic (slot * PER_TONE + row) so they never shift when a group's results are
        # still in flight.
        tone_items = styles.labels() + [styles.NONE_LABEL]
        for slot in range(len(self.slot_tones)):
            group_box = self._make_surface(12, PALETTE["row"], PALETTE["edge"])
            view.addSubview_(group_box)
            self._group_boxes.append(group_box)

            box = self._make_surface(8, PALETTE["field"], PALETTE["edge"])
            view.addSubview_(box)
            self._dd_boxes.append(box)

            pop = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(0, 0, TONE_DD_W - 2 * TONE_DD_INSET, TONE_DD_H), False)
            pop.setBordered_(False)          # <- no bezel, no accent-coloured chevron
            # the one discoverability aid the flat field gets: grey-on-grey reads as text,
            # a tooltip costs nothing visually and answers "can I click this?"
            pop.setToolTip_("点这里换话术（每种一组，各出 2 条）")
            pop.setFont_(NSFont.boldSystemFontOfSize_(TONE_DD_FONT))
            pop.setContentTintColor_(PALETTE["text"])
            pop.addItemsWithTitles_(tone_items)
            pop.selectItemWithTitle_(self.slot_tones[slot])
            pop.setTarget_(self)
            pop.setAction_("toneChanged:")
            view.addSubview_(pop)
            self._dds.append(pop)

            slot_rows = []
            for row in range(styles.PER_TONE):
                tag = slot * styles.PER_TONE + row
                row_box = self._make_surface(8, PALETTE["row"], PALETTE["edge"])
                row_box.setHidden_(True)
                view.addSubview_(row_box)
                prob = self._make_label(CAND_PROB_X, 0, CAND_PROB_W, 32,
                                        size=10, color=PALETTE["green"], bold=True)
                prob.cell().setWraps_(True)
                text = self._make_label(CAND_TEXT_X, 0, CAND_TEXT_W, 18,
                                        size=11, color=PALETTE["text"])
                text.cell().setWraps_(True)
                text.cell().setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
                if hasattr(text.cell(), "setMaximumNumberOfLines_"):
                    text.cell().setMaximumNumberOfLines_(0)
                copy_btn = self._make_button(CAND_BTN_X, 0, CAND_BTN_W, CAND_BTN_H,
                                             "复制", "copyCandidate:", tag)
                fill_btn = self._make_button(CAND_BTN_X + CAND_BTN_W + CAND_BTN_GAP, 0,
                                             CAND_BTN_W, CAND_BTN_H, "填入", "fillCandidate:", tag)
                track = self._make_surface(2, PALETTE["track"])
                fill_bar = self._make_surface(2, PALETTE["green"])
                track.setHidden_(True)
                fill_bar.setHidden_(True)
                for c in (prob, text, copy_btn, fill_btn, track, fill_bar):
                    view.addSubview_(c)
                slot_rows.append({"box": row_box, "prob": prob, "text": text,
                                  "btn": copy_btn, "fill_btn": fill_btn,
                                  "track": track, "fill": fill_bar})
            self._rows.append(slot_rows)

        self.panel.setContentView_(view)
        self._title_h = self.panel.frame().size.height - PANEL_H   # measured, not assumed
        self._relayout()
        self.rows["status"].setStringValue_(IDLE_STATUS)
        self._wire_window_controls()
        self._install_status_item()
        AppKit.NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
            self, "accessibilityDisplayChanged:",
            AppKit.NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification, None)
        self.applyAccessibilityAppearance_(None)

    @objc.python_method
    def _build_overlay(self):
        """A transparent, click-through window aligned to QQ: the YOLO-style view.

        Pure visualization of what perception already returns — every message's bounding
        box and AX confidence, the judged one carrying intent+risk on its chip.
        Three properties keep it safe: it is OFF by default (menu-bar toggle); clicks pass
        through (`ignoresMouseEvents`), so QQ never gets blocked. AX reading is independent
        of this overlay, so the panel cannot pollute the message data.
        Coordinate mapping assumes the 1x nominal capture's pixel size equals the window's
        point size — that is exactly what kCGWindowImageNominalResolution promises.
        """
        self._ov_panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 200, 200), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False)
        self._ov_panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self._ov_panel.setOpaque_(False)
        self._ov_panel.setHasShadow_(False)
        self._ov_panel.setIgnoresMouseEvents_(True)   # never steal a click meant for QQ
        self._ov_panel.setHidesOnDeactivate_(False)
        self._ov_panel.setBackgroundColor_(NSColor.clearColor())
        view = _BoxesView.alloc().init()
        view.boxes = []
        self._ov_panel.setContentView_(view)

    @objc.python_method
    def _slot_active(self, slot: int) -> bool:
        return self.slot_tones[slot] in styles.PRESETS

    @objc.python_method
    def _relayout(self):
        """Place every control for the current tone selection and size the panel to fit.

        Two things are computed here rather than at build time. Positions are measured from
        the TOP, so when the panel grows or shrinks nothing above the change moves — only the
        bottom edge does. And the height follows the groups in use: a slot on 不用 reserves
        neither a dropdown's worth of rows nor its candidates, which is what removes the dead
        space a fixed-height panel left in the middle.
        """
        message_h, document_h, overflow = self._message_metrics()
        message_delta = message_h - 26  # sender row now precedes the body
        self._message_toggle.setHidden_(self._collapsed or not overflow)
        self._message_toggle.setTitle_("收起 ▴" if self._message_expanded else "展开 ▾")
        self._message_scroll.setHasVerticalScroller_(document_h > message_h)
        field = self.rows["message"]
        field.setMaximumNumberOfLines_(0 if self._message_expanded else 2)
        field.setFrame_(NSMakeRect(0, 0, PANEL_W - 44, document_h))
        dy = self._group_top + message_delta
        placements = []          # (control, x, dy_from_top, w, h)
        for slot in range(len(self.slot_tones)):
            active = self._slot_active(slot)
            self._group_boxes[slot].setHidden_(not active)
            group_top = dy
            selector_top = dy + (GROUP_PAD_Y if active else 0)
            placements.append((self._dd_boxes[slot], TONE_DD_X, selector_top,
                               TONE_DD_W, TONE_DD_H))
            placements.append((self._dds[slot], TONE_DD_X + TONE_DD_INSET, selector_top,
                               TONE_DD_W - 2 * TONE_DD_INSET, TONE_DD_H))
            dy = selector_top + TONE_DD_H
            if active:
                dy += TONE_DD_GAP
            for row in range(styles.PER_TONE):
                r = self._rows[slot][row]
                controls = self._row_controls(slot, row)
                if active:
                    # The candidate decides its own height. Short replies keep the compact
                    # minimum; longer localized text grows without truncation.
                    text_h = self._candidate_text_height(r["text"])
                    row_h = max(CAND_ROW_MIN_H, text_h + 12)
                    text_top = dy + (row_h - text_h) / 2
                    button_top = dy + (row_h - CAND_BTN_H) / 2
                    metric_top = dy + (row_h - 40) / 2
                    progress_w = max(0.0, min(36.0, r["fill"].frame().size.width))
                    placements += [
                        (r["box"], CAND_ROW_X, dy, CAND_ROW_W, row_h),
                        (r["text"], CAND_TEXT_X, text_top, CAND_TEXT_W, text_h),
                        (r["prob"], CAND_PROB_X, metric_top, CAND_PROB_W, 32),
                        (r["btn"], CAND_BTN_X, button_top, CAND_BTN_W, CAND_BTN_H),
                        (r["fill_btn"], CAND_BTN_X + CAND_BTN_W + CAND_BTN_GAP, button_top,
                         CAND_BTN_W, CAND_BTN_H),
                        (r["track"], CAND_PROB_X + 4, metric_top + 36, 36, 4),
                        (r["fill"], CAND_PROB_X + 4, metric_top + 36, progress_w, 4),
                    ]
                    dy += row_h
                    if row < styles.PER_TONE - 1:
                        dy += CAND_ROW_GAP
                else:
                    for c in controls:
                        c.setHidden_(True)
            if active:
                dy += GROUP_PAD_Y
                placements.append((self._group_boxes[slot], 14, group_top,
                                   PANEL_W - 28, dy - group_top))
            if slot < styles.MAX_SLOTS - 1:
                dy += GROUP_GAP

        content_h = dy + BOTTOM_PAD
        view = self.panel.contentView()
        view.setFrameSize_(NSMakeSize(PANEL_W, content_h))
        fixed = []
        for ctrl, x, top, w, h in self._fixed:
            if ctrl is self._message_surface:
                h += message_delta
            elif ctrl is self._message_scroll:
                h = message_h
            elif top >= 124:
                top += message_delta
            fixed.append((ctrl, x, top, w, h))
        for ctrl, x, top, w, h in placements + fixed:
            ctrl.setFrame_(NSMakeRect(x, content_h - top - h, w, h))

        # resize the window with its TOP edge pinned: growing downwards is what the eye
        # expects here, and _position_near() anchors the panel to WeChat's top anyway
        f = self.panel.frame()
        top = f.origin.y + f.size.height
        frame_h = content_h + self._title_h
        self.panel.setFrame_display_(
            NSMakeRect(f.origin.x, top - frame_h, PANEL_W, frame_h), True)
        self._expanded_h = frame_h

    @objc.python_method
    def _wire_window_controls(self):
        """Native traffic lights, mapped to this app's actions.

        red    -> quit. A hidden panel would otherwise be unreachable: LSUIElement apps
                  have no Dock icon, so a plain order-out looks like a crash.
        yellow -> roll the panel up instead of miniaturizing, for the same reason.
        green  -> hidden: the HUD has a fixed size and nothing to zoom.
        """
        close = self.panel.standardWindowButton_(NSWindowCloseButton)
        mini = self.panel.standardWindowButton_(NSWindowMiniaturizeButton)
        zoom = self.panel.standardWindowButton_(NSWindowZoomButton)
        if close:
            close.setTarget_(self)
            close.setAction_("quitApp:")
            close.setToolTip_("退出 jev-jarvis")
        if mini:
            mini.setTarget_(self)
            mini.setAction_("collapsePanel:")
            mini.setToolTip_("收起 / 展开面板")
        if zoom:
            zoom.setHidden_(True)

    @objc.python_method
    def _install_status_item(self):
        """Menu-bar item — the standard place for a background helper's controls."""
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.status_item.button().setTitle_("J")
        self.status_item.button().setToolTip_("jev-jarvis · QQ 意图助手")

        menu = AppKit.NSMenu.alloc().init()
        for title, action, key in (
            ("显示 / 收起面板", "collapsePanel:", ""),
            ("暂停读屏", "togglePause:", ""),
            ("YOLO 检测框", "toggleBoxes:", ""),
            ("立即重新分析", "reanalyze:", ""),
            ("模型设置…", "openSettings:", ","),
        ):
            menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("退出 jev-jarvis", "quitApp:", "q")
        for item in menu.itemArray():
            item.setTarget_(self)
        self.pause_item = menu.itemArray()[1]
        self.boxes_item = menu.itemArray()[2]
        self.boxes_item.setState_(
            AppKit.NSOnState if self._show_boxes else AppKit.NSOffState)
        self.status_item.setMenu_(menu)

    def openSettings_(self, sender):
        from settings import SettingsController
        if getattr(self, "settings_controller", None) and self.settings_controller.window.isVisible():
            self.settings_controller.show()
            return
        try:
            self.settings_controller = SettingsController.alloc().init().build()
            self.settings_controller.show()
        except OSError:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("无法读取配置文件，请检查文件权限。")
            alert.runModal()

    @objc.python_method
    def _make_surface(self, radius: float, color: NSColor,
                      border: NSColor | None = None) -> NSView:
        surface = ui_style.make_surface(radius, color, border)
        self._appearance_surfaces.append((surface, radius, color, border))
        return surface

    def accessibilityDisplayChanged_(self, notification):
        # Workspace notifications are independent of the polling/model workers.
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "applyAccessibilityAppearance:", None, False)

    def applyAccessibilityAppearance_(self, notification):
        reduced = AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceTransparency()
        self._apply_accessibility_palette(bool(reduced))

    @objc.python_method
    def _apply_accessibility_palette(self, reduced):
        def adapted(color):
            if color is None or not reduced:
                return color
            for key, replacement in ui_style.SOLID_PALETTE.items():
                if color.isEqual_(PALETTE[key]):
                    return replacement
            return color  # risk/status colors retain their semantic meaning

        background = ui_style.SOLID_PALETTE["bg"] if reduced else NSColor.clearColor()
        self._solid_backdrop.layer().setBackgroundColor_(background.CGColor())
        for surface, radius, color, original_border in self._appearance_surfaces:
            layer = surface.layer()
            layer.setBackgroundColor_(adapted(color).CGColor())
            border = adapted(original_border)
            if reduced and color.isEqual_(PALETTE["surface"]):
                border = ui_style.SOLID_PALETTE["edge"]
            layer.setBorderWidth_(0.75 if border is not None else 0)
            if border is not None:
                layer.setBorderColor_(border.CGColor())
        for button in self._appearance_buttons:
            if button is self.settings_button:
                continue  # the gear stays an unboxed icon
            button.layer().setBackgroundColor_(adapted(PALETTE["row"]).CGColor())
            button.layer().setBorderColor_(adapted(PALETTE["edge"]).CGColor())
        self.panel.contentView().setNeedsDisplay_(True)

    @objc.python_method
    def _make_label(self, x, y, w, h, size=13, color=None, bold=False):
        return ui_style.make_label("", x, y, w, h, size, color, bold, selectable=True)

    @objc.python_method
    def _make_button(self, x, y, w, h, title, action, tag):
        """Compact native action with a light outline over the vibrancy material."""
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        btn.setTitle_(title)
        ui_style.style_button(btn, font_size=10, radius=CAND_BTN_H / 2)
        btn.setTarget_(self)
        btn.setAction_(action)
        btn.setTag_(tag)
        btn.setHidden_(True)
        self._appearance_buttons.append(btn)
        return btn

    @objc.python_method
    def _show(self):
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()

    @objc.python_method
    def _context_line(self, sender, prev: str) -> str:
        parts = []
        if sender:
            parts.append(f"来自 {sender}")
        if prev:
            parts.append(f"上文：{prev[:26]}")
        return " · ".join(parts)

    @objc.python_method
    def _render(self, key: str, text: str, color: NSColor | None = None):
        if key == "status":
            self._normal_status = (text, color)
            status = self.judge.load_status
            # A red error line stays visible: a concurrent progress status must not
            # repaint over it, and _normal_status keeps it after the load ends.
            if status and color is not PALETTE["red"]:
                text, color = status, PALETTE["amber"]
        tf = self.rows[key]
        if key == "message" and text != self._message_text:
            self._message_expanded = False
            self._message_text = text
        tf.setStringValue_(text)
        if key == "message" and not self._collapsed and not getattr(self, "_visible_mode", False):
            self._relayout()
            tf.scrollRectToVisible_(NSMakeRect(0, max(0, tf.frame().size.height - 1), 1, 1))
        if color is not None:
            tf.setTextColor_(color)

    @objc.python_method
    def _message_metrics(self):
        field = self.rows["message"]
        attributed = NSAttributedString.alloc().initWithString_attributes_(
            field.stringValue() or " ", {NSFontAttributeName: field.font()})
        bounds = attributed.boundingRectWithSize_options_(
            NSMakeSize(PANEL_W - 50, 100000),
            AppKit.NSStringDrawingUsesLineFragmentOrigin | AppKit.NSStringDrawingUsesFontLeading)
        two_lines = NSAttributedString.alloc().initWithString_attributes_(
            "国\n国", {NSFontAttributeName: field.font()})
        two_bounds = two_lines.boundingRectWithSize_options_(
            NSMakeSize(PANEL_W - 50, 100000),
            AppKit.NSStringDrawingUsesLineFragmentOrigin | AppKit.NSStringDrawingUsesFontLeading)
        collapsed = float(int(two_bounds.size.height + 5.999))
        full = max(collapsed, float(int(bounds.size.height + 5.999)))
        overflow = full > collapsed
        document = full if self._message_expanded else collapsed
        return min(180, document), document, overflow

    def toggleMessage_(self, sender):
        self._message_expanded = not self._message_expanded
        self._relayout()
        field = self.rows["message"]
        field.scrollRectToVisible_(NSMakeRect(0, max(0, field.frame().size.height - 1), 1, 1))

    @objc.python_method
    def _set_candidate_header(self, text: str):
        """Keep the section title strong while treating its live status as metadata."""
        value = NSMutableAttributedString.alloc().initWithString_(text)
        value.addAttributes_range_({
            NSFontAttributeName: NSFont.systemFontOfSize_(12),
            NSForegroundColorAttributeName: PALETTE["muted"],
        }, NSMakeRange(0, len(text)))
        title_len = len(text)
        value.addAttributes_range_({
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(12),
            NSForegroundColorAttributeName: PALETTE["text"],
        }, NSMakeRange(0, title_len))
        self.rows["cand_header"].setAttributedStringValue_(value)

    @objc.python_method
    def _set_probability_label(self, field: NSTextField, row: int, probability: str):
        """Match the reference hierarchy: quiet rank, vivid bold probability."""
        rank = f"#{row + 1}"
        text = f"{rank}\n{probability}"
        value = NSMutableAttributedString.alloc().initWithString_(text)
        value.addAttributes_range_({
            NSFontAttributeName: NSFont.systemFontOfSize_(9),
            NSForegroundColorAttributeName: PALETTE["muted"],
        }, NSMakeRange(0, len(text)))
        value.addAttributes_range_({
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(11),
            NSForegroundColorAttributeName: (
                PALETTE["muted"] if probability == "排序中" else PALETTE["green"]),
        }, NSMakeRange(len(rank) + 1, len(probability)))
        field.setAttributedStringValue_(value)

    @objc.python_method
    def _candidate_text_height(self, field: NSTextField) -> float:
        """Measure the full rendered reply so layout never relies on a character cutoff."""
        text = field.stringValue()
        if not text:
            return 18
        attributed = NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: field.font()})
        options = (AppKit.NSStringDrawingUsesLineFragmentOrigin
                   | AppKit.NSStringDrawingUsesFontLeading)
        bounds = attributed.boundingRectWithSize_options_(
            NSMakeSize(CAND_TEXT_W, 10_000), options)
        return max(18, float(int(bounds.size.height + 4.999)))

    @objc.python_method
    def _set_progress(self, slot: int, row: int, value: float | None):
        """Paint the existing rank probability; the model payload is never changed."""
        r = self._rows[slot][row]
        progress = max(0.0, min(1.0, float(value or 0.0)))
        frame = r["fill"].frame()
        r["fill"].setFrameSize_(NSMakeSize(36 * progress, frame.size.height or 4))

    @objc.python_method
    def _set_risk_scale(self, risk: int | None):
        selected = None if risk is None else (0 if risk <= 3 else (1 if risk <= 6 else 2))
        colors = (PALETTE["green"], PALETTE["amber"], PALETTE["red"])
        for i, dot in enumerate(self._risk_dots):
            layer = dot.layer()
            layer.removeAnimationForKey_("risk-breathe")
            alpha = 1.0 if i == selected else 0.68
            layer.setBackgroundColor_(colors[i].colorWithAlphaComponent_(alpha).CGColor())
            layer.setShadowOpacity_(0.0)
            if i == selected:
                layer.setShadowColor_(colors[i].CGColor())
                layer.setShadowOffset_(NSMakeSize(0, 0))
                layer.setShadowRadius_(4.0)
                layer.setShadowOpacity_(0.55)
                # Keep the dot itself fully saturated; only its halo breathes.
                pulse = Quartz.CABasicAnimation.animationWithKeyPath_("shadowOpacity")
                pulse.setFromValue_(0.24)
                pulse.setToValue_(0.72)
                pulse.setDuration_(1.2)
                pulse.setAutoreverses_(True)
                pulse.setRepeatCount_(float("inf"))
                pulse.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                    Quartz.kCAMediaTimingFunctionEaseInEaseOut))
                layer.addAnimation_forKey_(pulse, "risk-breathe")

    @objc.python_method
    def _row_controls(self, slot: int, row: int):
        r = self._rows[slot][row]
        return (r["box"], r["prob"], r["text"], r["btn"], r["fill_btn"],
                r["track"], r["fill"])

    @objc.python_method
    def _render_groups(self, payload: list):
        """payload: [(slot, tone, [{"text","prob"}, ...]), ...] — one entry per active tone.

        Rows the model did not fill are emptied and their buttons hidden. Every active tone
        still reserves its two minimum rows, while a returned long reply expands only its own
        row so the complete text remains visible.
        """
        wanted = set()
        for slot, _tone, items in payload:
            for row in range(styles.PER_TONE):
                if row < len(items):
                    it = items[row]
                    wanted.add((slot, row))
                    r = self._rows[slot][row]
                    prob = "排序中" if it["prob"] is None else f"{it['prob'] * 100:.0f}%"
                    self._set_probability_label(r["prob"], row, prob)
                    r["text"].setStringValue_(it["text"])
                    self._set_progress(slot, row, it["prob"])
                    for c in self._row_controls(slot, row):
                        c.setHidden_(not self._slot_active(slot))
                    self.cand_texts[slot * styles.PER_TONE + row] = it["text"]
        for slot in range(len(self.slot_tones)):
            for row in range(styles.PER_TONE):
                if (slot, row) not in wanted and self._slot_active(slot):
                    r = self._rows[slot][row]
                    r["prob"].setStringValue_("")
                    r["text"].setStringValue_("")
                    self._set_progress(slot, row, None)
                    for c in self._row_controls(slot, row):
                        c.setHidden_(True)
                    self.cand_texts[slot * styles.PER_TONE + row] = None
        self._relayout()

    @objc.python_method
    def _clear_candidates(self):
        for slot in range(len(self.slot_tones)):
            for row in range(styles.PER_TONE):
                r = self._rows[slot][row]
                r["prob"].setStringValue_("")
                r["text"].setStringValue_("")
                self._set_progress(slot, row, None)
                for c in self._row_controls(slot, row):
                    c.setHidden_(True)
                self.cand_texts[slot * styles.PER_TONE + row] = None

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
            # dock right of WeChat if it fits on that screen, else left, else its right edge
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

        # dead-band: ignore sub-2pt corrections and one-off blips, so WeChat's own window
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

    def toneChanged_(self, sender):
        """A 话术 dropdown moved: the verdict is still valid, only the writing changes."""
        picked = [p.titleOfSelectedItem() or styles.NONE_LABEL for p in self._dds]
        if picked == self.slot_tones:
            return
        self.slot_tones = picked
        # the panel is sized by how many slots are in use, so re-lay-out *before* the new
        # candidates arrive: the empty rows appear at once and nothing jumps later
        self._clear_candidates()
        self._stream_rows = {}     # the run _regenerate starts streams into fresh rows
        self._regenerate()

    @objc.python_method
    def _regenerate(self):
        """Re-run just the generation half for the message on screen.

        No re-judging and no re-reading of the screen: the intent and risk do not depend on
        the tone, and re-running them would make a dropdown click feel like a new analysis.
        """
        self._relayout()
        text = self.analyzed_text
        if not text:
            self._render("status", "话术已选 · 下条消息生效", PALETTE["muted"])
            return
        active = [t for t in self.slot_tones if t in styles.PRESETS]
        if not active:
            self._render("status", "没选话术 · 至少选一个", PALETTE["amber"])
            return
        self._render("status", f"换话术中…（{'、'.join(active)}）", PALETTE["muted"])
        self._set_candidate_header("候选回复 · 生成中…")
        threading.Thread(target=self._reply_task,
                         args=(self._reply_epoch, self._regen_work,
                               text, self._last_intent, list(self.slot_tones)),
                         daemon=True).start()

    @objc.python_method
    def _payload_from_gen(self, gen: dict):
        """Generation result -> unranked [(slot, tone, items)] (prob=None ⇒ 待排序).

        None when nothing usable came back — the caller shows gen's error then.
        """
        groups = [g for g in (gen.get("groups") or []) if g.get("texts")]
        if not groups:
            return None
        return [(g["slot"], g["tone"], [{"text": t, "prob": None} for t in g["texts"]])
                for g in groups]

    @objc.python_method
    def _rank_payload(self, payload: list, message: str, intent: str) -> list:
        """Legacy compatibility hook; Jev-only mode never ranks reply text."""
        return payload

    @objc.python_method
    def _stream_hook(self, t0: float, label: str = ""):
        """The on_candidate callback for the generation run starting now.

        Shared by all three run starters (_analyze, _run_generation, _regen_work) so the
        streaming lines follow one epoch/rows discipline no matter which path produced
        them. The callback runs on the run's worker thread; it hops to the main thread for
        every UI touch, and the first line it sees logs the latency that streaming is here
        for. The epoch check inside applyStreamLine_ is what makes a superseded run's late
        lines harmless.
        """
        reply_epoch = getattr(self._reply_worker, "epoch", self._reply_epoch)
        self._gen_epoch += 1
        epoch = self._gen_epoch
        prefix = f"{label} " if label else ""
        first_line = {"shown": False}

        def on_candidate(slot: int, _tone: str, text: str) -> None:
            if not first_line["shown"]:
                first_line["shown"] = True
                _log(f"{prefix}首条候选上屏 {(time.perf_counter() - t0) * 1000:.0f}ms（未排序）")
            self._push_reply("applyStreamLine:", (epoch, slot, text), reply_epoch)
        return on_candidate

    @objc.python_method
    def _regen_work(self, text: str, intent: str, slot_tones: list[str]):
        """Disabled: Jev-only mode has no reply-generation worker."""
        return None

    @objc.python_method
    def _payload_current(self, payload) -> bool:
        """False when the tone selection moved on — a late result must not repaint it.

        Generation+ranking now pushes twice (unranked, then ranked); a dropdown click
        between the two would otherwise bring back the tone the user just switched away
        from. Same guard for a 换话术 result racing a second click.
        """
        return all(self.slot_tones[slot] == tone for slot, tone, _items in payload)

    @objc.python_method
    def _cand_header(self, payload) -> str:
        pending = any(it["prob"] is None for _s, _t, items in payload for it in items)
        return "候选回复 · 排序中…" if pending else "候选回复（按合适度排序）"

    def applyTones_(self, payload):
        if not self._payload_current(payload):
            return
        self._set_candidate_header(self._cand_header(payload))
        total = sum(len(items) for _s, _t, items in payload)
        self._render("status", f"已换话术 · {total} 条", PALETTE["muted"])
        self._render_groups(payload)

    # ------------------------------------------------------------ controls
    def collapsePanel_(self, sender):
        self._set_collapsed(not self._collapsed)

    def togglePause_(self, sender):
        self._paused = not self._paused
        self.pause_item.setTitle_("继续读屏" if self._paused else "暂停读屏")
        if getattr(self, "_visible_mode", False):
            self._visible_epoch += 1
            if self._paused:
                self._visible_targets = ()
                self._visible_display_rows = ()
                self._visible_signature = None
                self._draw_visible_cards()
                self._render("status", "已暂停", PALETTE["muted"])
            else:
                self._fingerprint = None
                self._last_full = None
                self._visible_signature = None
                self._next_read_ts = 0
                self._render("status", "读取中…", PALETTE["muted"])
            return
        if self._paused:
            self._prejudge_req = None        # a paused app judges nothing further
            self._prejudge_result = None
            self._pregen_req = None          # …and generates nothing further
            self._pregen_result = None
            if self._ov_panel.isVisible():   # frozen boxes would lie about "realtime"
                self._ov_panel.orderOut_(None)
            self._render("status", "已暂停 · 不再读屏", PALETTE["amber"])
            self._render("message", "", PALETTE["text"])
            self._render("sender", "", PALETTE["muted"])
            self._render("intent", "—", PALETTE["muted"])
            self._render("confidence", "", PALETTE["muted"])
            self._render("risk", "", PALETTE["muted"])
            if hasattr(self, "_risk_dots"):
                self._set_risk_scale(None)
            for key in ("emotion", "behavior", "need", "signals", "context"):
                self._render(key, "", PALETTE["muted"])
            self._render("actions", "", PALETTE["text"])
            self.rows["cand_header"].setStringValue_("")
            self._clear_candidates()
        else:
            self._prejudge_result = None
            self.last_seen = None      # force a fresh read of whatever is on screen
            self.analyzed_text = None
            self._render("status", "已恢复 · 读屏中", PALETTE["muted"])

    def reanalyze_(self, sender):
        self.memory.reset_view(self._chat_title)
        if getattr(self, "_visible_mode", False):
            self.memory.invalidate_verdicts(self._chat_title)
            self._visible_epoch += 1
            self._visible_signature = None
            self._fingerprint = None
            self._last_full = None
            self._next_read_ts = 0
            self._render("status", "重新分析中…", PALETTE["muted"])
            return
        self._prejudge_req = None      # "re-analyze" means re-run, not reuse the pre-judge
        self._prejudge_result = None
        self._pregen_req = None        # …and not reuse the early generation either
        self._pregen_result = None
        self.last_seen = None
        self.analyzed_text = None
        self._render("status", "重新分析中…", PALETTE["muted"])

    def quitApp_(self, sender):
        AppKit.NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _set_collapsed(self, collapsed: bool):
        """Roll the panel up to a title+status strip, or back to full height."""
        if getattr(self, "_visible_mode", False):
            self._collapsed = collapsed
            self._visible_scroll.setHidden_(collapsed)
            frame = self.panel.frame()
            new_h = COLLAPSED_H if collapsed else self._expanded_h
            self.panel.setFrame_display_(NSMakeRect(
                frame.origin.x, frame.origin.y + frame.size.height - new_h,
                PANEL_W, new_h), True)
            self._last_origin = None
            return
        self._collapsed = collapsed
        controlled = ["message", "sender", "intent", "confidence", "risk", "actions",
                      "emotion", "behavior", "need", "signals", "context",
                      "cand_header"]   # "chat" and "status" survive collapsing
        for key in controlled:
            self.rows[key].setHidden_(collapsed)
        for view in self._detail_views:
            view.setHidden_(collapsed)
        for slot in range(len(self.slot_tones)):
            self._dds[slot].setHidden_(collapsed)
            self._dd_boxes[slot].setHidden_(collapsed)
            self._group_boxes[slot].setHidden_(collapsed or not self._slot_active(slot))
            for row in range(styles.PER_TONE):
                has = self.cand_texts[slot * styles.PER_TONE + row] is not None
                for c in self._row_controls(slot, row):
                    c.setHidden_(collapsed or not has)
        if not collapsed:
            # re-expanding puts every control back where _relayout() wants it, and re-hides
            # the slots that are switched off — the collapse above cannot know that
            self._relayout()
            self._last_origin = None      # let the next tick re-dock cleanly
            return

        rect = self.panel.frame()
        # _expanded_h is maintained by _relayout() (it changes with the tone selection), so
        # expanding reads the current full height rather than a value captured at startup
        new_h = COLLAPSED_H if collapsed else (self._expanded_h or PANEL_H)
        self.panel.setFrame_display_(
            NSMakeRect(rect.origin.x, rect.origin.y + (rect.size.height - new_h),
                       rect.size.width, new_h), True)
        self._last_origin = None      # let the next tick re-dock cleanly

    # --------------------------------------------------------------- loop
    @objc.python_method
    def _refresh_model_status(self):
        """Repaint the status line while a load/progress status is live (this PR).

        Runs before the paused/busy/read gates so progress stays visible while OCR is
        in flight; `_normal_status` decides what the line falls back to.
        """
        status = self.judge.load_status
        if status != self._model_status:
            was_live = self._model_status is not None
            self._model_status = status
            if status:
                self._show()
            elif was_live and self._wechat_frontmost is False:
                # The load just finished; applyHidden_ kept the panel up while it ran,
                # so a WeChat that left in the meantime is hidden only now (review #41).
                if self.panel.isVisible():
                    self.panel.orderOut_(None)
            self._render("status", *self._normal_status)

    def _set_foreground_state(self, frontmost):
        """Apply one hard lifecycle boundary when QQ gains/loses focus."""
        if frontmost is None or frontmost is self._wechat_frontmost:
            return False

        self._wechat_frontmost = frontmost
        self._foreground_epoch += 1
        self._reply_epoch += 1
        self._reply_key = None
        self.last_seen = None
        self.analyzed_text = None
        self._prejudge_req = self._prejudge_result = None
        self._pregen_req = self._pregen_result = None
        self._gen_epoch += 1
        self._fingerprint = None
        self._last_full = None
        self._win_wid = None
        self._input_target = None
        self._input_window = None
        self._input_next = 0
        self._stable_n = 0
        self._burst_left = BURST_READS
        self._last_skip_reason = None
        self._read_fail_since = None
        self._read_fail_hidden = False
        if getattr(self, "_visible_mode", False):
            self._visible_epoch += 1
            self._visible_signature = None

        if frontmost:
            self._next_read_ts = 0
            _log("前台切换 · QQ 回到前台，强制重新读取辅助功能树")
        else:
            _log("前台切换 · QQ 离开前台，隐藏面板并清空旧结果")
            self._push("applyForegroundHidden:", "QQ 不在前台")
        return True

    def tick_(self, timer):
        # Progress/status refresh first: a live download must stay visible even while
        # WeChat is gone or the read loop is gated (applyHidden_ keeps the panel up).
        self._refresh_model_status()
        # Check activation before pause/busy/read-cadence gates.  The timer keeps
        # firing while OCR is in flight, so a quick WeChat -> Chrome -> WeChat
        # round trip still advances _foreground_epoch and retires that capture.
        frontmost_is_wechat = frontmost_app_is_wechat()
        if frontmost_is_wechat is None:
            return
        self._set_foreground_state(frontmost_is_wechat)
        if not frontmost_is_wechat:
            return
        if self._paused or self._busy or time.time() < self._next_read_ts:
            return  # paused, a previous read is still running, or not due yet
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
        # The panel is a global floating window. Showing it over Chrome while continuing
        # to reuse the last WeChat frame makes stale text look like browser OCR. Treat app
        # activation as a hard display/capture boundary before even checking permissions:
        # a missing screen grant must not keep an error panel floating over other apps.
        frontmost_is_wechat = frontmost_app_is_wechat()
        if frontmost_is_wechat is None:
            # A transient NSWorkspace failure is not proof that the user left WeChat.
            # Freeze both reads and UI updates for one short tick without cancelling a
            # valid in-flight reply or manufacturing a leave/return transition.
            self._next_read_ts = time.time() + FAST_TICK
            return
        self._set_foreground_state(frontmost_is_wechat)
        if not frontmost_is_wechat:
            self._next_read_ts = time.time() + FAST_TICK
            return
        if not screen_capture_ok():
            if not self._asked_permission:
                self._asked_permission = True
                request_screen_capture()      # compatibility name: opens Accessibility prompt
            self._push("applyError:", "需要辅助功能权限 · 系统设置 › 隐私与安全性")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        capture_foreground_epoch = self._foreground_epoch
        try:
            res = read_conversation(previous_wid=self._win_wid,
                                    prev_fingerprint=self._fingerprint,
                                    prev_layout=getattr(self, "_layout_key", None))
        except Exception as e:
            self._push("applyError:", f"读取失败: {type(e).__name__}: {str(e)[:40]}")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        # Re-check after the blocking AX walk.  tick_ may have observed a
        # complete leave+return while this worker was busy; in that case even a
        # currently-frontmost WeChat does not make this old snapshot current.
        frontmost_is_wechat = frontmost_app_is_wechat()
        if frontmost_is_wechat is None:
            self._next_read_ts = time.time() + FAST_TICK
            return
        self._set_foreground_state(frontmost_is_wechat)
        if (not frontmost_is_wechat
                or capture_foreground_epoch != self._foreground_epoch):
            self._next_read_ts = time.time() + FAST_TICK
            return
        if not res["ok"]:
            # Window enumeration/capture can miss one frame while WeChat redraws.
            # Keep the already-current HUD stable for a short grace period, then
            # hide and force rediscovery if the failure really persists.
            now_mono = time.monotonic()
            if self._read_fail_since is None:
                self._read_fail_since = now_mono
            if (not self._read_fail_hidden
                    and now_mono - self._read_fail_since >= READ_FAILURE_HIDE_S):
                self._read_fail_hidden = True
                # A persistent miss is no longer a harmless one-frame redraw.
                # Retire every reply callback before hiding; otherwise an in-flight
                # generation from the last visible frame could call _show() again.
                self._reply_epoch += 1
                self._reply_key = None
                self.last_seen = None
                self.analyzed_text = None
                self._prejudge_req = self._prejudge_result = None
                self._pregen_req = self._pregen_result = None
                self._gen_epoch += 1
                self._fingerprint = None
                self._last_full = None
                self._win_wid = None
                self._push("applyForegroundHidden:", res["error"])
            self._next_read_ts = time.time() + FAST_TICK
            return

        self._read_fail_since = None
        self._read_fail_hidden = False

        # Same fingerprint ⇒ same pixels ⇒ the messages are exactly what we last read.
        # Cadence follows the screen: quiet checks back in FAST_TICK (capture+hash only,
        # ~30 ms); a change first keeps BURST_TICK for a few reads so the burst's NEXT
        # message is noticed quickly (this also feeds _stable_n, the early-settle signal),
        # and only a pane that keeps moving settles back to SLOW_TICK like the old poll.
        self._fingerprint = res.get("fingerprint")
        self._layout_key = res.get("layout")
        if res["unchanged"]:
            self._stable_n += 1
            self._burst_left = BURST_READS
            self._next_read_ts = time.time() + FAST_TICK
        elif self._burst_left > 0:
            self._burst_left -= 1
            self._stable_n = 0
            self._next_read_ts = time.time() + BURST_TICK
        else:
            self._stable_n = 0
            self._next_read_ts = time.time() + SLOW_TICK

        # position immediately: analysis takes seconds, and a delayed correction
        # showed up as a visible jump after the verdict landed. Pushed on unchanged
        # frames too — the window can move while its pixels stay identical.
        live_window = res["window"]
        live_at_bottom = res.get("at_bottom")
        live_input_rect = res.get("input_rect")
        self._win_wid = res["window"]["wid"]
        self._push("applyPosition:", res["window"])
        if res["unchanged"] and self._last_full is not None:
            # the settle/analyze gate below still runs every read; an unchanged frame
            # just skips re-deriving the messages it would act on
            res = self._last_full
        else:
            self._last_full = res
            self._push("applyChat:", res.get("chat_title") or "")

        res = dict(res, window=live_window, input_rect=live_input_rect,
                   at_bottom=live_at_bottom)
        msgs = res["messages"]
        chat = res.get("chat_title") or ""
        if getattr(self, "_visible_mode", False):
            self.memory.observe(chat, msgs, res.get("at_bottom"))
            self._visible_update(chat, msgs)
            return
        at_live_tail = self.memory.observe(chat, msgs, res.get("at_bottom"))
        thems = [m for m in msgs if m.side == "them"]
        # The most recent incoming bubble remains the useful target even after I reply.
        # My later bubbles must not trigger a new decision for that same target; the
        # reply key and bounded verdict cache handle that. An own-only viewport has no
        # target, and a history viewport still cannot trigger a call.
        newest = thems[-1] if at_live_tail and thems else None
        prev_text = thems[-2].text if newest is not None and len(thems) > 1 else ""

        key = self.memory.verdict_key(chat, newest, prev_text) if newest else None
        if key != self._reply_key:
            self._reply_epoch += 1
            self._reply_key = key
            self.last_seen = None
            self.analyzed_text = None
            self._prejudge_req = self._prejudge_result = None
            self._pregen_req = self._pregen_result = None
            self._gen_epoch += 1

        # YOLO overlay: repaint whenever a read produced geometry — unchanged reads reuse
        # the cached messages, so the boxes stay up even while the pane is quiet
        if self._show_boxes:
            self._push("applyBoxes:", (res["window"], msgs,
                                       newest.text if newest else None))
        if newest is None:
            self._push("applyWaiting:", "浏览历史消息" if not at_live_tail and msgs
                       else "等待对方消息")
            return
        now = time.time()

        # --- anti-flood: track arrivals, never analyze mid-burst
        if newest.text != self.last_seen:
            self.last_seen = newest.text
            self.last_change_ts = now
            # only on arrival: this function runs every second, and a per-tick line would
            # bury the timing that matters
            t = res.get("timing_ms") or {}
            first_read = not self._read_once
            self._read_once = True
            # Vision loads on the first call and costs ~2x steady state; saying so keeps a
            # one-off from being read as a regression (same reason the judge line does it)
            note = "（首次）" if first_read else ""
            _log(f"辅助功能树读取 {t.get('total', 0):.0f}ms · 读到 {len(msgs)} 条"
                 f"（对方 {len(thems)} 条）{note}")
            _log(f"新消息 · Jev 预判先跑，停稳 {SETTLE_S}s（连续 {STABLE_READS} 跳不变最早 "
                 f"{EARLY_SETTLE_S}s）后上屏（两次完整分析最小间隔 {MIN_GAP_S}s）")
            # latest-wins: overwrite the slot, retire the old verdict — only the newest
            # text's judgment can ever be consumed, and only by the settle gate below
            cached = self.memory.get_verdict(key)
            if cached is not None:
                self._prejudge_req = None
                self._prejudge_result = (newest.text, cached, newest.sender,
                                         prev_text, self._reply_epoch)
                _log("判断缓存命中 · 同一会话的消息无需重复请求")
            else:
                self._prejudge_req = (
                    newest.text, self._context_text(msgs, newest, JUDGE_TURNS, chat),
                    newest.sender, prev_text, self._reply_epoch, key)
                self._prejudge_result = None
                self._prejudge_event.set()
            # keep the previous verdict readable; just badge that something new landed
            self._push("applyIncoming:", (newest.text, newest.sender, prev_text))

        # Anti-flood, two signals: the blind wait (SETTLE_S, unchanged upper bound) or a
        # content-stability early open — the pane went quiet for STABLE_READS consecutive
        # reads spanning at least EARLY_SETTLE_S, which is itself evidence the burst is
        # over. A burst keeps resetting _stable_n, so mid-burst opens cannot happen.
        elapsed = now - self.last_change_ts
        settled = elapsed >= SETTLE_S or (elapsed >= EARLY_SETTLE_S
                                          and self._stable_n >= STABLE_READS)
        cooled = (now - self.last_analyze_ts) >= MIN_GAP_S
        pr = self._prejudge_result
        pre_hit = pr is not None and pr[0] == newest.text and pr[4] == self._reply_epoch
        # A pre-judged verdict needs no cooling: its cost was already paid per arrival.
        # Only the full path (no usable pre-judgment) still waits MIN_GAP_S out.
        if (newest.text != self.analyzed_text and settled and not self._analyzing
                and not self._prejudging and (pre_hit or cooled)):
            self.last_analyze_ts = now
            self.analyzed_text = newest.text
            self._prejudge_result = None      # spent: a verdict is shown exactly once
            self._analyzing = True
            if pre_hit:
                # Judgment already ran inside the settle window; show it and finish.
                _log(f"停稳 · 用预判结论上屏 · 这条消息出现到现在 {now - self.last_change_ts:.1f}s")
                self._push("applyJudgment:", (pr[1], pr[2], pr[3]))
                self._analyzing = False
            else:
                _log(f"开始分析 · 这条消息出现到现在 {now - self.last_change_ts:.1f}s")
                self._push("applyPending:", (newest.text, newest.sender, prev_text))
                # off the tick path on purpose: Jev can take over a second, and
                # while it runs the loop must keep reading — a message landing mid-analysis
                # used to wait the whole analysis out before anyone even saw it
                threading.Thread(target=self._reply_task,
                                 args=(self._reply_epoch, self._run_analysis,
                                       newest, msgs, prev_text, chat), daemon=True).start()
        elif newest.text != self.analyzed_text:
            # the wait is deliberate; say so once per arrival change so "it feels slow" can
            # be told apart from "it is still waiting out the burst window"
            why = ("消息还在变" if not settled else
                   "上一条还在分析" if self._analyzing else
                   "预判还在跑" if self._prejudging else
                   f"距上次分析不足 {MIN_GAP_S}s")
            if self._last_skip_reason != why:
                self._last_skip_reason = why
                _log(f"暂不分析（{why}）")
        else:
            self._last_skip_reason = None

    @objc.python_method
    def _run_analysis(self, newest, msgs, prev_text: str, chat: str = ""):
        try:
            if not self._reply_current():
                return
            self._analyze(newest, msgs, prev_text, chat)
        except Exception as e:
            _log(f"分析失败 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"分析失败: {type(e).__name__}: {str(e)[:40]}")
        finally:
            self._analyzing = False

    @objc.python_method
    def _prejudge_loop(self):
        """Judge a message the moment it is seen, so the settle gate can skip the wait.

        One resident worker serializes the passes (a forward takes ~1 s). The request slot
        holds only the newest text, so a burst queues one judgment, not one per tick, and a
        verdict survives only if its text is still the newest when the pass ends
        (latest-wins — checked before and after). Nothing is drawn here: the settle gate in
        _work_inner is the only place a verdict reaches the panel, so a stale conclusion
        cannot be shown no matter how the timing lands.
        """
        while True:
            try:
                self._prejudge_event.wait()
                self._prejudge_event.clear()
                req = self._prejudge_req
                self._prejudge_req = None
                if req is None:
                    continue
                text, context, sender, prev, epoch, cache_key = req
                if self._paused or text != self.last_seen or epoch != self._reply_epoch:
                    continue          # superseded while queued: only the newest text counts
                self._prejudging = True
                try:
                    t0 = time.perf_counter()
                    with self._model_lock:
                        verdict = self.judge.judge(text, context=context)
                    verdict = dict(verdict, context_turns=len(context.splitlines()) if context else 0)
                    ms = (time.perf_counter() - t0) * 1000
                    first = not self._judged_once
                    self._judged_once = True
                    note = "（首次，含本地模型加载）" if first else ""
                    _log(f"预判 {ms:.0f}ms → {verdict.get('intent', '?')}"
                         f" 把握 {verdict.get('confidence', 0):.0%}"
                         f" 风险 {verdict.get('risk', '?')}{note}（待停稳上屏）")
                except Exception as e:
                    _log(f"预判失败 {type(e).__name__}: {str(e)[:60]}")
                    verdict = None
                finally:
                    self._prejudging = False
                if (verdict is not None and not self._paused and text == self.last_seen
                        and epoch == self._reply_epoch):
                    self.memory.put_verdict(cache_key, verdict)
                    self._prejudge_result = (text, verdict, sender, prev, epoch)
            except Exception:
                pass                  # a resident worker must not die on one bad request

    @objc.python_method
    def _pregen_loop(self):
        """Disabled: Jev-only mode never starts this legacy worker."""
        return None

    @objc.python_method
    def _take_pregen(self, text: str, tones: tuple) -> tuple[dict | None, float]:
        """Collect the early generation: (gen, waited_ms). gen=None ⇒ caller generates.

        A stored result counts only when BOTH the text and the tone selection match — the
        text because a newer message retired it, the tones because a dropdown click during
        the window changed what should be generated. While a matching request is in flight
        we wait for it (it started ~1 s ago at detection, so what is left is usually a few
        hundred ms — still cheaper than a fresh call, and free of a second TLS handshake).
        """
        t0 = time.perf_counter()
        deadline = time.time() + 30   # generation's own timeout; never wait longer
        while time.time() < deadline:
            if not self._reply_current():
                return None, (time.perf_counter() - t0) * 1000
            r = self._pregen_result
            if (r is not None and r[0] == text and r[1] == tones
                    and r[3] == self._reply_epoch):
                self._pregen_result = None      # spent: each result is consumed exactly once
                return r[2], (time.perf_counter() - t0) * 1000
            if (not self._pregen_running
                    and (self._pregen_req is None or self._pregen_req[0] != text)):
                return None, (time.perf_counter() - t0) * 1000
            time.sleep(0.03)
        return None, (time.perf_counter() - t0) * 1000

    @objc.python_method
    def _gen_with_pregen(self, text: str, context: str | None,
                         on_candidate=None) -> dict:
        """Disabled compatibility hook; returns no generated groups."""
        return {"groups": []}

    @objc.python_method
    def _run_generation(self, newest, msgs, verdict: dict):
        """Disabled: a completed Jev verdict is the end of the pipeline."""
        self._analyzing = False

    @objc.python_method
    def _context_text(self, msgs, newest, turns: int = CONTEXT_TURNS,
                      chat: str | None = None) -> str | None:
        """Recent visible/session-observed context before the target, up to 2,000 chars."""
        return self.memory.context(chat if chat is not None else self._chat_title,
                                   newest, msgs, turns=turns)

    @objc.python_method
    def _analyze(self, newest, msgs, prev_text: str = "", chat: str = ""):
        """Run Jev analysis only; no reply generation or candidate ranking.

        Runs on its own thread (started by _work_inner): it takes over a second and must
        not hold the read loop hostage.
        """
        t_judge = time.perf_counter()
        try:
            context = self._context_text(msgs, newest, JUDGE_TURNS, chat)
            with self._model_lock:
                verdict = self.judge.judge(newest.text, context=context)
            verdict = dict(verdict, context_turns=len(context.splitlines()) if context else 0)
            key = self.memory.verdict_key(chat, newest, prev_text)
            self.memory.put_verdict(key, verdict)
            ms = (time.perf_counter() - t_judge) * 1000
            first = not self._judged_once
            self._judged_once = True
            note = "（首次，含本地模型加载）" if first else ""
            _log(f"Jev 分析 {ms:.0f}ms → {verdict.get('intent', '?')}"
                 f" 把握 {verdict.get('confidence', 0):.0%}"
                 f" 风险 {verdict.get('risk', '?')}{note}")
            self._push("applyJudgment:", (verdict, newest.sender, prev_text))
        except Exception as e:
            _log(f"判断失败 {type(e).__name__}: {str(e)[:60]}")
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                message = "Decision Infra 未注册 jev-latest · 检查 infra 的 TYPESAFE_API_KEY"
            elif isinstance(e, urllib.error.HTTPError) and e.code == 503:
                message = "Decision Infra Provider 不可用 · 未切换其他模型"
            elif isinstance(e, (ConnectionRefusedError, TimeoutError, OSError)):
                message = "连接不到 Decision Infra · 请先启动本机网关"
            else:
                message = f"Decision Infra 判断失败: {type(e).__name__}"
            self._push("applyError:", message)

    @objc.python_method
    def _finish_generate(self, gen: dict, newest, t0: float, verdict: dict | None,
                         note: str = ""):
        """Disabled compatibility hook; Jev-only mode has no second stage."""
        return None

    @objc.python_method
    def _push(self, selector: str, payload=None):
        if selector in {"applyIncoming:", "applyPending:", "applyJudgment:",
                        "applyCandidates:", "applyStreamLine:", "applyWaiting:", "applyError:"}:
            epoch = getattr(self._reply_worker, "epoch", self._reply_epoch)
            self._push_reply(selector, payload, epoch)
            return
        self.performSelectorOnMainThread_withObject_waitUntilDone_(selector, payload, False)

    @objc.python_method
    def _reply_task(self, epoch, callback, *args):
        self._reply_worker.epoch = epoch
        try:
            return callback(*args)
        finally:
            del self._reply_worker.epoch

    @objc.python_method
    def _reply_current(self):
        return (self._wechat_frontmost is True
                and self._reply_key is not None and not self._paused
                and getattr(self._reply_worker, "epoch", self._reply_epoch) == self._reply_epoch)

    @objc.python_method
    def _push_reply(self, selector, payload, epoch):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "applyReplyUpdate:", (epoch, selector, payload), False)

    def applyReplyUpdate_(self, update):
        epoch, selector, payload = update
        if self._wechat_frontmost is not True or epoch != self._reply_epoch:
            return
        if selector not in {"applyWaiting:", "applyError:"} and not self._reply_current():
            return
        getattr(self, selector.replace(":", "_"))(payload)

    def applyWaiting_(self, _payload):
        self._show()
        self._last_intent = ""
        self._last_risk = 0.0
        self._clear_candidates()
        self._stream_rows = {}
        for key in ("message", "sender", "intent", "confidence", "risk", "actions",
                    "emotion", "behavior", "need", "signals", "context"):
            self._render(key, "", PALETTE["muted"])
        if hasattr(self, "_risk_dots"):
            self._set_risk_scale(None)
        if hasattr(self, "_set_candidate_header"):
            self._set_candidate_header("仅做 Jev 分析 · 不生成回复")
        else:
            # Lightweight test harnesses load this callback without constructing AppKit.
            self.rows["cand_header"].setStringValue_("仅做 Jev 分析 · 不生成回复")
        self._render("status", _payload or "等待可确认的对方消息…", PALETTE["muted"])

    # --- main-thread callbacks (AppKit is not thread safe)
    def applyChat_(self, title):
        self._chat_title = title
        self._render("chat", title, PALETTE["accent"])

    def applyIncoming_(self, payload):
        # a new message landed but we are not analysing yet (burst in progress):
        # keep the previous verdict visible, just badge it
        text, sender, prev = payload
        self._show()
        self._render("status", "有新消息 · 等消息停稳…", PALETTE["muted"])
        self._render("message", text, PALETTE["muted"])   # grey: not analysed yet
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])

    def applyPending_(self, payload):
        text, sender, prev = payload
        self._show()
        self._render("status", "分析中…", PALETTE["muted"])
        self._render("message", text, PALETTE["text"])    # inked: this is the one
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])
        self._clear_candidates()
        self._set_candidate_header("Jev 正在分析…")

    def applyJudgment_(self, payload):
        v, sender, prev = payload
        self._show()
        self._last_intent = v.get("intent", "")
        self._last_risk = v.get("risk", 0)   # and so the overlay can badge the message
        self._render("message", v["message"], PALETTE["text"])
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])
        backend = v.get("backend", "")
        if backend.startswith("local (Jev"):
            # the backend label is "local (Jev-shaped decider-2b)": take what is inside the
            # parens without the paren, or the status line reads "... decider-2b)"
            detail = backend.split("(", 1)[1].rstrip(")")
            self._render("status", f"本地兜底 · {detail[:26]}", PALETTE["amber"])
        elif backend:
            self._render("status", "Jev · 已分析", PALETTE["muted"])
        else:
            self._render("status", "分析完成", PALETTE["muted"])
        self._render("intent", v["intent"], PALETTE["text"])
        emotion = v.get("emotion", "")
        if v.get("emotion_confidence", 0) < 0.45:
            emotion = "难判断"
        self._render("emotion", emotion or "难判断", PALETTE["text"])
        behavior = v.get("behavior", "") if v.get("behavior_confidence", 0) >= 0.45 else ""
        need = v.get("need", "") if v.get("need_confidence", 0) >= 0.45 else ""
        self._render("behavior", f"行为 · {behavior}" if behavior else "", PALETTE["muted"])
        self._render("need", f"需要 · {need}" if need else "", PALETTE["muted"])
        self._render("signals", " · ".join(v.get("signal_labels", [])) or "无明显言外之意 / 边界信号",
                     PALETTE["muted"])
        count = int(v.get("context_turns", 0))
        self._render("context", f"上文 {count} 轮 · 本次已见" if count else "仅当前消息",
                     PALETTE["muted"])
        # the intent recognition rate, read off the judged intent — same muted slot
        self._render("confidence", f"把握 {v['confidence']:.0%}", PALETTE["muted"])
        # Rounded, so the panel does not claim a precision it has: the judge reports a
        # mean like 4.7 out of a 10-level distribution, and "4.7/9" reads as a measurement
        # while "5/9" reads as the estimate it is. Deliberately the mean and not the most
        # likely level — measured on 8 real messages, this model's top level never exceeds
        # 0.4 and the argmax jumps 1/3/6 across near-identical criticism messages, while the
        # mean holds (派活 2.0–2.4, 批评 3.0–4.0, 闲聊 1.7).
        risk = int(round(float(v.get("risk", 0))))
        label = "安全" if risk <= 3 else ("留神" if risk <= 6 else "危险")
        color = PALETTE["green"] if risk <= 3 else (
            PALETTE["amber"] if risk <= 6 else PALETTE["red"])
        self._render("risk", f"● {label}  {risk}/9", color)
        if hasattr(self, "_risk_dots"):
            self._set_risk_scale(risk)
        self._render("actions", " · ".join(v.get("actions", [])), PALETTE["text"])
        self._set_candidate_header("Jev 分析完成 · 不生成回复")

    def applyCandidates_(self, payload):
        if not self._payload_current(payload):
            return
        self._set_candidate_header(self._cand_header(payload))
        self._render_groups(payload)

    def applyStreamLine_(self, payload):
        """One streamed candidate line, shown the moment it completes (not ranked yet).

        applyCandidates_ re-fills every row with scores when the full result lands, so the
        "#n" here is only "nth line of this tone" and the score slot reads as pending. A
        superseded run's lines are dropped by the epoch check — a tone change or a new
        message starting mid-stream must not write into the new run's rows.
        """
        epoch, slot, text = payload
        if epoch != self._gen_epoch or not self._slot_active(slot):
            return
        row = self._stream_rows.get(slot, 0)
        if row >= styles.PER_TONE:
            return                       # the prompt asks for PER_TONE lines; extras stray
        self._stream_rows[slot] = row + 1
        self.cand_texts[slot * styles.PER_TONE + row] = text
        if self._collapsed:
            return      # collapse keeps the data; _set_collapsed(False) puts it back up
        r = self._rows[slot][row]
        self._set_probability_label(r["prob"], row, "排序中")
        r["text"].setStringValue_(text)
        self._set_progress(slot, row, None)
        for c in self._row_controls(slot, row):
            c.setHidden_(False)
        self._relayout()

    def applyError_(self, text):
        self._show()                       # never vanish without telling the user why
        self._render("status", text, PALETTE["red"])

    def applyStatus_(self, text):
        """A neutral status line pushed from a worker thread (e.g. the warm-up)."""
        self._render("status", text, PALETTE["muted"])

    def applyWarmFailed_(self, text):
        """Warm-up failure: same as applyError_ but outside the reply-epoch guard.

        The warm-up is not a reply run — a message that starts analysing while it
        fails must not be able to swallow this line like it swallows late results.
        It is still a global panel, though: with WeChat in the background the red
        line must not surface over other apps (the foreground boundary stays hard).
        The hint is not lost — every later analysis that hits LowMemoryError reports
        it again through the epoch-guarded applyError_ path, which WeChat foregrounds.
        """
        if self._wechat_frontmost is not True:
            return
        self._show()                       # never vanish without telling the user why
        self._render("status", text, PALETTE["red"])

    def applyWarmDone_(self, _payload):
        """Clear the warm-up line, but only if nothing more urgent replaced it.

        The first load can run for minutes; a message that arrived and got analysed in
        that window owns the status line now, and this must not steal it back.
        """
        if self.rows["status"].stringValue() == WARM_STATUS:
            self._render("status", IDLE_STATUS, PALETTE["muted"])

    def applyHidden_(self, reason):
        # WeChat gone or unreadable -> take the panel away (the app "opens with WeChat")
        self._render("status", reason, PALETTE["muted"])
        if self.panel.isVisible() and not self.judge.load_status:
            self.panel.orderOut_(None)
        if self._ov_panel.isVisible():
            self._ov_panel.orderOut_(None)

    def applyForegroundHidden_(self, reason):
        """Hide a global panel without leaving stale conversation state behind."""
        if getattr(self, "_visible_mode", False):
            self._visible_targets = ()
            self._visible_display_rows = ()
            self._visible_messages = ()
            self._visible_signature = None
            self._draw_visible_cards()
        self._last_intent = ""
        self._last_risk = 0.0
        self._stream_rows = {}
        self._chat_title = ""
        self._clear_candidates()
        for key in ("chat", "message", "sender", "intent", "confidence", "risk", "actions",
                    "emotion", "behavior", "need", "signals", "context"):
            self._render(key, "", PALETTE["muted"])
        self.rows["cand_header"].setStringValue_("仅做 Jev 分析 · 不生成回复")
        self.applyHidden_(reason)

    def applyPosition_(self, win):
        if self._wechat_frontmost is not True:
            return
        self._position_near(win)

    # --- YOLO overlay callbacks (visual only; see _build_overlay)
    def applyBoxes_(self, payload):
        """Repaint the overlay from the last read's window geometry + messages."""
        if self._wechat_frontmost is not True or not self._show_boxes:
            return
        win, msgs, newest_text = payload
        W, H = win["w"], win["h"]
        flip = self._display_height()
        # top-left (Quartz) -> bottom-left (Cocoa), covering WeChat exactly
        self._ov_panel.setFrame_display_(
            NSMakeRect(win["x"], flip - win["y"] - H, W, H), False)
        font = (NSFont.fontWithName_size_("Menlo-Bold", 10)
                or NSFont.boldSystemFontOfSize_(10))
        judged = (newest_text is not None and newest_text == self.analyzed_text
                  and bool(self._last_intent))
        risk = int(round(float(self._last_risk)))
        boxes = []
        for m in msgs:
            if m.w <= 0:
                continue               # pre-overlay geometry: nothing to draw
            who = m.sender or {"them": "对方", "me": "我"}.get(m.side, "方向未确认")
            label = f"{who} {m.conf:.2f}"
            if judged and m.side == "them" and m.text == newest_text:
                color = (PALETTE["green"] if risk <= 3 else
                         PALETTE["amber"] if risk <= 6 else PALETTE["red"])
                lw = 2.5
                label += f" · {self._last_intent} 风险{risk}/9"
            else:
                color = _rgb(0x576B95) if m.side == "me" else PALETTE["green"]
                lw = 1.5
            chip = NSAttributedString.alloc().initWithString_attributes_(
                label,
                {NSFontAttributeName: font,
                 NSForegroundColorAttributeName: NSColor.whiteColor(),
                 NSBackgroundColorAttributeName: color.colorWithAlphaComponent_(0.85)})
            y = H - (m.y + m.h) * H     # normalized top-origin -> view bottom-origin
            boxes.append((NSMakeRect(m.x * W, y, m.w * W, m.h * H), color, lw, chip))
        view = self._ov_panel.contentView()
        view.boxes = boxes
        view.setNeedsDisplay_(True)
        if not self._ov_panel.isVisible():
            self._ov_panel.orderFrontRegardless()

    def toggleBoxes_(self, sender):
        """Menu-bar switch; JEV_BOXES=1 in the env file makes it start on instead."""
        self._show_boxes = not self._show_boxes
        self.boxes_item.setState_(
            AppKit.NSOnState if self._show_boxes else AppKit.NSOffState)
        if not self._show_boxes and self._ov_panel.isVisible():
            self._ov_panel.orderOut_(None)

    # --------------------------------------------------------------- warm-up
    @objc.python_method
    def _warm(self):
        """Prepare the AX reader; model lifecycle belongs to Decision Infra."""
        t0 = time.perf_counter()
        ax_ms = warm_ocr()  # compatibility hook; QQ AX has no OCR model to warm
        if ax_ms >= 0:
            _log("辅助功能读取已就绪 · 不使用 OCR")
        try:
            self.judge.warm()
        except Exception as e:
            _log(f"Decision Infra 准备失败 {type(e).__name__}: {str(e)[:60]}")
        else:
            _log(f"Decision Infra 客户端就绪 · 总耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")

    # ------------------------------------------------- first-run choice (#38)
    @objc.python_method
    def _onboarding_needed(self) -> bool:
        """Decision Infra owns provider setup; the desktop app needs no key dialog."""
        return False

    def maybeOnboard_(self, sender):
        """Ask once how to judge: cloud key, offline model, or later.

        Runs on the main thread before the warm-up thread starts (see main()), so the
        choice is already in os.environ when _warm reads it — userconfig.get prefers the
        real environment, which is how the pick takes effect without a restart.
        """
        if not self._onboarding_needed():
            return
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("选择判断方式")
        alert.setInformativeText_(
            "未配置判断层 key。判断每条消息的意图与风险，可以用云端 key"
            "（轻量、无下载），也可以下载离线模型（约 7 GB，之后完全离线）。")
        accessory = AppKit.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 84))
        choices = (("cloud", "配置 key 在线判断（推荐）", "轻量、无下载，需要 TypeSafe key"),
                   ("local", "下载离线模型", "约 7 GB 磁盘，下载后完全离线可用"))
        radios = []
        y = 58
        for _value, title, detail in choices:
            radio = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(4, y, 348, 20))
            radio.setButtonType_(AppKit.NSRadioButton)
            radio.setTitle_(title)
            radio.setFont_(AppKit.NSFont.systemFontOfSize_(13))
            accessory.addSubview_(radio)
            radios.append(radio)
            note = ui_style.make_label(detail, 24, y - 15, 320, 14, 11,
                                       AppKit.NSColor.secondaryLabelColor())
            accessory.addSubview_(note)
            y -= 38
        radios[0].setState_(AppKit.NSControlStateValueOn)
        alert.setAccessoryView_(accessory)
        alert.addButtonWithTitle_("确定")
        alert.addButtonWithTitle_("稍后再说")
        alert.addButtonWithTitle_("打开模型设置…")
        choice = alert.runModal()
        if choice == AppKit.NSAlertFirstButtonReturn:
            picked = next((v for (v, _t, _d), r in zip(choices, radios)
                           if r.state() == AppKit.NSControlStateValueOn), "skip")
        elif choice == AppKit.NSAlertSecondButtonReturn:
            picked = "skip"           # Esc lands here too: postpone, no side effects
        else:
            picked = next((v for (v, _t, _d), r in zip(choices, radios)
                           if r.state() == AppKit.NSControlStateValueOn), "skip")
        self._record_onboarding(picked)
        if picked == "local":
            # Start the download now — rerunning _warm is cheap: the OCR half was already
            # paid at launch, and the judge half reads the choice from os.environ.
            threading.Thread(target=self._warm, daemon=True).start()
        elif choice == AppKit.NSAlertThirdButtonReturn:
            self.openSettings_(None)

    @objc.python_method
    def _record_onboarding(self, value: str) -> None:
        """Persist the choice: env file for future launches, session override for now.

        A plain os.environ write does not work here: userconfig froze its snapshot of
        the environment at import time. session_override sits in front of every source
        until the process exits, so the warm-up below sees the pick on this launch.
        """
        userconfig.session_override("JUDGE_BACKEND", value)
        try:
            import settings_config
            path = userconfig.env_files()[0]
            original = settings_config.read_document(path)
            settings_config.write_settings(path, original, {"JUDGE_BACKEND": value})
        except (ValueError, OSError) as e:
            # The session still honours the pick; a failed write just means the dialog
            # asks again next launch.
            _log(f"首次引导写入 env 失败 {type(e).__name__}: {str(e)[:60]}")


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
        controller._wechat_frontmost = True
        controller.applyVisibleRows_(controller._visible_epoch)
        controller._render("status", "界面预览 · 合成消息", PALETTE["muted"])
        controller.panel.center()
        controller._show()
        app.run()
        return
    # First line of every run: which backends are actually in play. Support requests
    # always need it, and it proves the log is live before the first message arrives.
    _log(f"启动 · 判断层 Decision Infra"
         f" · {controller.judge.endpoint} · 路由 {controller.judge.model}"
         f" · 仅分析，不调用生成模型"
         + (" · YOLO 框开" if controller._show_boxes else ""))
    controller._show()
    # No provider onboarding here: Decision Infra owns credentials and model services.
    threading.Thread(target=controller._warm, daemon=True).start()
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        FAST_TICK, controller, "tick:", None, True)
    AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, AppKit.NSDefaultRunLoopMode)
    app.run()


if __name__ == "__main__":
    main()
