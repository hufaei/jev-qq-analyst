"""Shared AppKit presentation primitives for the HUD and its settings window."""
from __future__ import annotations

import AppKit as A
from Foundation import NSMakeRect


def rgb(hex_code: int, alpha: float = 1.0) -> A.NSColor:
    return A.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        ((hex_code >> 16) & 0xFF) / 255.0,
        ((hex_code >> 8) & 0xFF) / 255.0,
        (hex_code & 0xFF) / 255.0,
        alpha,
    )


# Neutral surfaces; restrained colour distinguishes probabilities and decisions.
PALETTE = {
    "bg": rgb(0xF4F5F5),
    "text": rgb(0x23272B),
    "muted": rgb(0x72797E),
    "accent": rgb(0x23272B),
    "green": rgb(0x398269),
    "amber": rgb(0x9B6F2D),
    "red": rgb(0xB24952),
    "surface": rgb(0xFFFFFF),
    "row": rgb(0xF0F1F2),
    "own_row": rgb(0xE8EEF1),
    "own_edge": rgb(0xD4E0E5),
    "own_text": rgb(0x40545F),
    "prob_high_bg": rgb(0xE2EDF2),
    "prob_high_text": rgb(0x315B70),
    "prob_mid_bg": rgb(0xEAF0F3),
    "prob_mid_text": rgb(0x526D7B),
    "prob_low_bg": rgb(0xF1F3F4),
    "prob_low_text": rgb(0x72797E),
    "reply_yes_bg": rgb(0xE3F0E9),
    "reply_wait_bg": rgb(0xF5EBDC),
    "action_row": rgb(0xF5F6F7),
    "field": rgb(0xFFFFFF),
    "edge": rgb(0xDCE0E2),
    "track": rgb(0xC8CDCF),
}

# Opaque layers need their own contrast; white-on-white vibrancy colors disappear
# when macOS substitutes a solid backdrop for Reduce Transparency.
SOLID_PALETTE = {
    "bg": rgb(0xF4F5F5),
    "surface": rgb(0xFFFFFF),
    "row": rgb(0xF0F1F2),
    "field": rgb(0xFFFFFF),
    "edge": rgb(0xDCE0E2),
    "track": rgb(0xC8CDCF),
}

RADIUS_FIELD = 8
RADIUS_CARD = 12


def make_surface(radius: float, color: A.NSColor,
                 border: A.NSColor | None = None) -> A.NSView:
    """Create a layer-backed visual surface without application state or behavior."""
    surface = A.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
    surface.setWantsLayer_(True)
    surface.layer().setBackgroundColor_(color.CGColor())
    surface.layer().setCornerRadius_(radius)
    if border is not None:
        surface.layer().setBorderColor_(border.CGColor())
        surface.layer().setBorderWidth_(0.75)
    return surface


def make_label(text: str, x: float, y: float, width: float, height: float,
               size: float = 13, color: A.NSColor | None = None,
               bold: bool = False, selectable: bool = False) -> A.NSTextField:
    field = A.NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, width, height))
    field.setStringValue_(text)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(selectable)
    field.setTextColor_(PALETTE["text"] if color is None else color)
    font = A.NSFont.boldSystemFontOfSize_(size) if bold else A.NSFont.systemFontOfSize_(size)
    field.setFont_(font)
    return field


def style_button(button: A.NSButton, *, font_size: float = 11,
                 radius: float = 16, primary: bool = False) -> A.NSButton:
    """Apply the same quiet pill treatment used by actions on the HUD."""
    button.setBordered_(False)
    button.setFont_(A.NSFont.boldSystemFontOfSize_(font_size)
                    if primary else A.NSFont.systemFontOfSize_(font_size))
    button.setContentTintColor_(A.NSColor.whiteColor() if primary else PALETTE["text"])
    button.setWantsLayer_(True)
    button.layer().setBackgroundColor_((PALETTE["text"] if primary else PALETTE["row"]).CGColor())
    button.layer().setBorderColor_((PALETTE["text"] if primary else PALETTE["edge"]).CGColor())
    button.layer().setBorderWidth_(0.75)
    button.layer().setCornerRadius_(radius)
    return button
