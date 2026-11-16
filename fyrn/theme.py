"""Design tokens: every colour in the app, in one place.

One change here changes the colour everywhere: the stylesheet
and every inline setStyleSheet call read from these dicts, so a
hex value appears in exactly one place in the fyrn package (this
file). Two themes: the dark look Fyrn has always had, and a
high-contrast theme for users who need stronger contrast (every
text/background pair in it meets 7:1, checked by the suite).

No Qt imports here on purpose: the tokens and the contrast
maths are plain data, testable without a QApplication.
"""

from string import Template

# The dark theme: the look Fyrn has always had. Deep blue-black
# surfaces drawn from the icon tile, ember amber for selection
# and highlights, ember orange for progress.
DARK = {
    "bg": "#14202e",
    "bg_raised": "#1c2836",
    "bg_deep": "#0f1822",
    "bg_button": "#243447",
    "bg_button_pressed": "#1a2836",
    "bg_sunken": "#1a2836",
    "border": "#3a4d63",
    "border_dim": "#555555",
    "border_disabled": "#2a3644",
    "text": "#e8e8e8",
    "text_muted": "#999999",
    "text_dim": "#bbbbbb",
    "text_disabled": "#666666",
    "accent": "#f2c14e",
    "accent_warm": "#e8890c",
    # Keyboard focus on a card that is not selected: a cool blue,
    # dashed, so it can never be mistaken for the solid amber of
    # a selected card (the one Load will act on).
    "focus_ring": "#8fb8dd",
    "badge_text": "#ffffff",
    "selection_bg": "#e8890c",
    "selection_text": "#ffffff",
    "fit_ok": "#2e7d32",
    "fit_tight": "#b7791f",
    "fit_big": "#b3261e",
    "fit_unknown": "#666666",
    "skeleton_bg": "#1a2531",
    "skeleton_border": "#444444",
    "skeleton_bar": "#2a3644",
}

# High contrast: black surfaces, white text, yellow highlights.
# Every text/background pair below is 7:1 or better (WCAG AAA);
# the suite checks each pair with contrast_ratio, so a token that
# drifts below the bar fails the build instead of shipping.
HIGH_CONTRAST = {
    "bg": "#000000",
    "bg_raised": "#0d0d0d",
    "bg_deep": "#000000",
    "bg_button": "#1a1a1a",
    "bg_button_pressed": "#333333",
    "bg_sunken": "#1a1a1a",
    "border": "#ffffff",
    "border_dim": "#ffffff",
    "border_disabled": "#808080",
    "text": "#ffffff",
    "text_muted": "#e6e6e6",
    "text_dim": "#d9d9d9",
    "text_disabled": "#a6a6a6",
    "accent": "#ffff00",
    "accent_warm": "#ffb340",
    # Focus here is a white dashed ring: distinct from the solid
    # yellow of a selected card, and visible on black.
    "focus_ring": "#ffffff",
    "badge_text": "#ffffff",
    "selection_bg": "#ffff00",
    "selection_text": "#000000",
    # Fit badges, darkened until white text on them clears 7:1.
    "fit_ok": "#145a1e",
    "fit_tight": "#7a4d00",
    "fit_big": "#8f1d1d",
    "fit_unknown": "#595959",
    "skeleton_bg": "#0d0d0d",
    "skeleton_border": "#ffffff",
    "skeleton_bar": "#404040",
}

THEMES = {
    "dark": DARK,
    "high-contrast": HIGH_CONTRAST,
}

# The stylesheet, as a template over the tokens. No fixed point
# sizes anywhere: the application font (and the user's system
# text size) shows through, and hierarchy comes from weight and
# colour, not size.
_STYLESHEET_TEMPLATE = Template("""
QWidget {
    background-color: $bg;
    color: $text;
}
QMainWindow, QDialog {
    background-color: $bg;
}
QLabel {
    color: $text;
}
QPushButton {
    background-color: $bg_button;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 6px 16px;
    color: $text;
}
QPushButton:hover {
    border-color: $accent;
}
QPushButton:pressed {
    background-color: $bg_button_pressed;
}
QPushButton:focus {
    border-color: $accent;
}
QPushButton:disabled {
    color: $text_disabled;
    border-color: $border_disabled;
}
QLineEdit, QTextEdit {
    background-color: $bg_deep;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 5px 8px;
    color: $text;
    selection-background-color: $selection_bg;
    selection-color: $selection_text;
}
QLineEdit:focus, QTextEdit:focus {
    border-color: $accent;
}
QTabWidget::pane {
    border: 1px solid $border;
    background-color: $bg;
}
QTabBar::tab {
    background-color: $bg_sunken;
    border: 1px solid $border;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 8px 20px;
    color: $text_dim;
}
QTabBar::tab:selected {
    background-color: $bg_button;
    color: $accent;
    border-bottom: 2px solid $accent;
}
QMenuBar {
    background-color: $bg;
    color: $text;
}
QMenuBar::item {
    padding: 4px 10px;
}
QMenuBar::item:selected {
    background-color: $bg_button;
}
QMenu {
    background-color: $bg_raised;
    border: 1px solid $border;
    color: $text;
}
QMenu::item:selected {
    background-color: $bg_button;
    color: $accent;
}
QProgressBar {
    background-color: $bg_deep;
    border: 1px solid $border;
    border-radius: 6px;
    text-align: center;
    color: $text;
}
QProgressBar::chunk {
    background-color: $accent_warm;
    border-radius: 6px;
}
QScrollBar:vertical {
    background-color: $bg;
    width: 12px;
}
QScrollBar::handle:vertical {
    background-color: $border;
    border-radius: 6px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: $accent;
}
QScrollBar:horizontal {
    background-color: $bg;
    height: 12px;
}
QScrollBar::handle:horizontal {
    background-color: $border;
    border-radius: 6px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover {
    background-color: $accent;
}
QListWidget {
    background-color: $bg_deep;
    border: 1px solid $border;
    border-radius: 6px;
}
QListWidget::item:selected {
    background-color: $bg_button;
    color: $accent;
}
QScrollArea {
    background-color: $bg;
    border: none;
}
QStatusBar {
    background-color: $bg_deep;
    color: $text_dim;
}
""")


def build_stylesheet(tokens):
    """The app stylesheet for a token dict."""
    return _STYLESHEET_TEMPLATE.substitute(tokens)


def fit_badges(tokens):
    """Fit badge (text, background) per fit status, for a theme."""
    return {
        "fits": ("Fits", tokens["fit_ok"]),
        "tight": ("Tight", tokens["fit_tight"]),
        "too big": ("Too big", tokens["fit_big"]),
        "unknown": ("Unknown", tokens["fit_unknown"]),
    }


def _linear(channel):
    """An sRGB channel (0-255) as linear light, per WCAG."""
    value = channel / 255.0
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color):
    """WCAG relative luminance of a #rrggbb colour (0-1)."""
    red = int(hex_color[1:3], 16)
    green = int(hex_color[3:5], 16)
    blue = int(hex_color[5:7], 16)
    return (0.2126 * _linear(red) + 0.7152 * _linear(green)
            + 0.0722 * _linear(blue))


def contrast_ratio(foreground, background):
    """WCAG contrast ratio between two #rrggbb colours (1-21)."""
    light = relative_luminance(foreground)
    dark = relative_luminance(background)
    if light < dark:
        light, dark = dark, light
    return (light + 0.05) / (dark + 0.05)


def rgba_string(hex_color, alpha):
    """A #rrggbb colour as an rgba(r, g, b, a) stylesheet value
    (0-255 alpha), for the overscroll glow's gradient."""
    red = int(hex_color[1:3], 16)
    green = int(hex_color[3:5], 16)
    blue = int(hex_color[5:7], 16)
    return "rgba(%d, %d, %d, %d)" % (red, green, blue, alpha)
