"""Design tokens and QSS generation (AGENTS.md §4).

One coherent design system: 4/8-px spacing scale, ~8-px radii, restrained blue
accent, Segoe UI Variable → Segoe UI fallback, clearly differentiated
hover/pressed/selected/focused/disabled states. PDF pages stay white in both
themes; dark mode changes application chrome only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ThemeTokens:
    name: str
    # surfaces
    bg_main: str
    bg_panel: str
    bg_toolbar: str
    bg_input: str
    bg_hover: str
    bg_pressed: str
    bg_selected: str
    bg_tooltip: str
    # text
    text_main: str
    text_secondary: str
    text_disabled: str
    text_on_accent: str
    # lines
    border: str
    border_strong: str
    # accent
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str  # subtle accent-tinted background
    # semantic
    danger: str
    warning: str
    success: str
    # canvas backdrop (behind PDF pages)
    canvas_bg: str
    # misc
    scrollbar: str
    scrollbar_hover: str
    shadow: str
    shadow_rgba: tuple  # (r, g, b, a) 0-255, for QPainter use (QSS uses `shadow`)
    focus_ring: str


LIGHT = ThemeTokens(
    name="light",
    bg_main="#F3F5F8",
    bg_panel="#FFFFFF",
    bg_toolbar="#FBFCFD",
    bg_input="#FFFFFF",
    bg_hover="#E9EDF3",
    bg_pressed="#DDE3EC",
    bg_selected="#D7E3FB",
    bg_tooltip="#1D222B",
    text_main="#18202B",
    text_secondary="#596579",
    text_disabled="#98A2B3",
    text_on_accent="#FFFFFF",
    border="#DCE2EA",
    border_strong="#C4CCD9",
    accent="#2563EB",
    accent_hover="#1D55CC",
    accent_pressed="#1849B0",
    accent_soft="#EAF0FE",
    danger="#C62828",
    warning="#B26B00",
    success="#1B7F3B",
    canvas_bg="#E4E8EE",
    scrollbar="#C9D1DC",
    scrollbar_hover="#AAB5C4",
    shadow="rgba(16, 24, 40, 0.10)",
    shadow_rgba=(16, 24, 40, 26),
    focus_ring="#2563EB",
)

DARK = ThemeTokens(
    name="dark",
    bg_main="#15181E",
    bg_panel="#1D222B",
    bg_toolbar="#1A1E26",
    bg_input="#232936",
    bg_hover="#262D3A",
    bg_pressed="#2E3644",
    bg_selected="#2C3B57",
    bg_tooltip="#EDF1F7",
    text_main="#EDF1F7",
    text_secondary="#A7B1C0",
    text_disabled="#5D6675",
    text_on_accent="#101418",
    border="#333B49",
    border_strong="#454F60",
    accent="#82ABFF",
    accent_hover="#9CBCFF",
    accent_pressed="#6E99F2",
    accent_soft="#24304A",
    danger="#F07171",
    warning="#E0A33E",
    success="#6FCF8E",
    canvas_bg="#101318",
    scrollbar="#3A4353",
    scrollbar_hover="#4E596B",
    shadow="rgba(0, 0, 0, 0.45)",
    shadow_rgba=(0, 0, 0, 115),
    focus_ring="#82ABFF",
)

FAMILIES = '"Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", system-ui, sans-serif'
MONO = '"Cascadia Mono", "Consolas", monospace'
RADIUS = 8
RADIUS_SM = 6


def build_qss(t: ThemeTokens) -> str:
    """Generate the application stylesheet for a token set."""
    return f"""
* {{
    font-family: {FAMILIES};
    outline: none;
}}

QMainWindow, QDialog {{
    background: {t.bg_main};
    color: {t.text_main};
    font-size: 13px;
}}

QWidget:disabled {{
    color: {t.text_disabled};
}}

/* ---------- menu bar & menus ---------- */
QMenuBar {{
    background: {t.bg_toolbar};
    color: {t.text_main};
    border-bottom: 1px solid {t.border};
    padding: 2px 6px;
    font-size: 13px;
}}
QMenuBar::item {{
    background: transparent;
    padding: 5px 10px;
    border-radius: {RADIUS_SM}px;
}}
QMenuBar::item:selected {{
    background: {t.bg_hover};
}}
QMenuBar::item:pressed {{
    background: {t.bg_pressed};
}}
QMenu {{
    background: {t.bg_panel};
    color: {t.text_main};
    border: 1px solid {t.border};
    border-radius: {RADIUS}px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 28px 6px 12px;
    border-radius: {RADIUS_SM}px;
}}
QMenu::item:selected {{
    background: {t.bg_hover};
}}
QMenu::item:disabled {{
    color: {t.text_disabled};
    background: transparent;
}}
QMenu::separator {{
    height: 1px;
    background: {t.border};
    margin: 4px 8px;
}}
QMenu::indicator {{
    width: 14px; height: 14px;
    margin-left: 8px;
}}
QMenu::indicator:checked {{
    background: {t.accent_soft};
    border: 1px solid {t.accent};
    border-radius: 4px;
}}

/* ---------- toolbar & tool buttons ---------- */
QToolBar {{
    background: {t.bg_toolbar};
    border: none;
    border-bottom: 1px solid {t.border};
    padding: 4px 8px;
    spacing: 2px;
}}
QToolBar::separator {{
    width: 1px;
    background: {t.border};
    margin: 4px 6px;
}}
QToolButton {{
    background: transparent;
    color: {t.text_main};
    border: 1px solid transparent;
    border-radius: {RADIUS_SM}px;
    padding: 5px 7px;
    margin: 1px;
}}
QToolButton:hover {{
    background: {t.bg_hover};
}}
QToolButton:pressed {{
    background: {t.bg_pressed};
}}
QToolButton:checked {{
    background: {t.accent_soft};
    border-color: {t.accent};
    color: {t.accent};
}}
QToolButton:disabled {{
    color: {t.text_disabled};
    background: transparent;
}}
QToolButton:focus-visible {{
    border: 1px solid {t.focus_ring};
}}

/* ---------- push buttons ---------- */
QPushButton {{
    background: {t.bg_input};
    color: {t.text_main};
    border: 1px solid {t.border_strong};
    border-radius: {RADIUS}px;
    padding: 7px 16px;
    font-size: 13px;
}}
QPushButton:hover {{
    background: {t.bg_hover};
    border-color: {t.accent};
}}
QPushButton:pressed {{
    background: {t.bg_pressed};
}}
QPushButton:disabled {{
    color: {t.text_disabled};
    border-color: {t.border};
    background: {t.bg_main};
}}
QPushButton[accent="true"] {{
    background: {t.accent};
    color: {t.text_on_accent};
    border: 1px solid {t.accent};
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background: {t.accent_hover};
}}
QPushButton[accent="true"]:pressed {{
    background: {t.accent_pressed};
}}
QPushButton[accent="true"]:disabled {{
    background: {t.bg_pressed};
    color: {t.text_disabled};
    border-color: {t.border};
}}
QPushButton:focus-visible {{
    border: 1px solid {t.focus_ring};
}}
QPushButton[accent="true"]:focus-visible {{
    border: 2px solid {t.focus_ring};
    padding: 6px 15px;
}}
QPushButton[big="true"] {{
    padding: 12px 28px;
    font-size: 15px;
    border-radius: 10px;
}}

/* ---------- inputs ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {t.bg_input};
    color: {t.text_main};
    border: 1px solid {t.border_strong};
    border-radius: {RADIUS_SM}px;
    padding: 6px 10px;
    selection-background-color: {t.bg_selected};
    font-size: 13px;
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    border-color: {t.accent};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1.5px solid {t.focus_ring};
    padding: 5px 9px;
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background: {t.bg_main};
    color: {t.text_disabled};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background: {t.bg_panel};
    color: {t.text_main};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    selection-background-color: {t.bg_selected};
    selection-color: {t.text_main};
    padding: 4px;
}}

QPlainTextEdit, QTextEdit {{
    background: {t.bg_input};
    color: {t.text_main};
    border: 1px solid {t.border_strong};
    border-radius: {RADIUS_SM}px;
    padding: 6px 8px;
    font-size: 13px;
    selection-background-color: {t.bg_selected};
}}
QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1.5px solid {t.focus_ring};
    padding: 5px 7px;
}}

/* ---------- docks & panels ---------- */
QDockWidget {{
    color: {t.text_main};
    titlebar-close-icon: none;
    font-weight: 600;
}}
QDockWidget::title {{
    background: {t.bg_panel};
    padding: 8px 12px;
    border-bottom: 1px solid {t.border};
}}
QDockWidget QWidget {{
    background: {t.bg_panel};
}}

#SidebarPanel, #PropertiesPanel {{
    background: {t.bg_panel};
    border-right: 1px solid {t.border};
}}
#PropertiesPanel {{
    border-right: none;
    border-left: 1px solid {t.border};
}}

/* ---------- lists & trees ---------- */
QListWidget, QTreeWidget, QListView, QTreeView {{
    background: {t.bg_panel};
    color: {t.text_main};
    border: none;
    border-radius: {RADIUS_SM}px;
    font-size: 13px;
}}
QListWidget::item, QTreeWidget::item {{
    padding: 7px 10px;
    border-radius: {RADIUS_SM}px;
    margin: 1px 4px;
}}
QListWidget::item:hover, QTreeWidget::item:hover {{
    background: {t.bg_hover};
}}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {t.bg_selected};
    color: {t.text_main};
}}
QListWidget::item:selected:hover {{
    background: {t.bg_selected};
}}

/* ---------- tabs ---------- */
QTabWidget::pane {{
    border: none;
    background: {t.bg_panel};
}}
QTabBar::tab {{
    background: transparent;
    color: {t.text_secondary};
    padding: 8px 14px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 13px;
}}
QTabBar::tab:selected {{
    color: {t.accent};
    border-bottom: 2px solid {t.accent};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{
    color: {t.text_main};
    background: {t.bg_hover};
}}

/* ---------- status bar ---------- */
QStatusBar {{
    background: {t.bg_toolbar};
    color: {t.text_secondary};
    border-top: 1px solid {t.border};
    font-size: 12px;
}}
QStatusBar::item {{
    border: none;
}}
QStatusBar QLabel {{
    color: {t.text_secondary};
    padding: 0 6px;
    font-size: 12px;
}}

/* ---------- canvas ---------- */
QScrollArea {{
    border: none;
    background: {t.canvas_bg};
}}
/* The scroll area's viewport and page container must paint the canvas color
   through QSS: palette-based painting lags one theme switch behind when a
   stylesheet is active, which left an inverted frame around pages. */
#CanvasViewport, #CanvasContainer {{
    background: {t.canvas_bg};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {t.scrollbar};
    border-radius: 5px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t.scrollbar_hover};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {t.scrollbar};
    border-radius: 5px;
    min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {t.scrollbar_hover};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0; height: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}

/* ---------- splitters ---------- */
QSplitter::handle {{
    background: {t.border};
}}
QSplitter::handle:horizontal {{
    width: 1px;
}}
QSplitter::handle:vertical {{
    height: 1px;
}}

/* ---------- checkboxes / radio ---------- */
QCheckBox, QRadioButton {{
    color: {t.text_main};
    spacing: 8px;
    font-size: 13px;
}}
QCheckBox:disabled, QRadioButton:disabled {{
    color: {t.text_disabled};
}}
QCheckBox:focus-visible, QRadioButton:focus-visible {{
    color: {t.accent};
    font-weight: 600;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 17px; height: 17px;
    border: 1.5px solid {t.border_strong};
    background: {t.bg_input};
}}
QCheckBox::indicator {{
    border-radius: 5px;
}}
QRadioButton::indicator {{
    border-radius: 9px;
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {t.accent};
    border-color: {t.accent};
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {t.accent};
}}

/* ---------- sliders ---------- */
QSlider::groove:horizontal {{
    height: 4px;
    background: {t.border};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 16px; height: 16px;
    margin: -6px 0;
    border-radius: 8px;
    background: {t.accent};
}}
QSlider::sub-page:horizontal {{
    background: {t.accent};
    border-radius: 2px;
}}

/* ---------- tooltips ---------- */
QToolTip {{
    background: {t.bg_tooltip};
    color: {t.bg_panel if t.name == 'dark' else '#FFFFFF'};
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 6px 10px;
    font-size: 12px;
}}

/* ---------- progress ---------- */
QProgressBar {{
    background: {t.bg_input};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {t.accent};
    border-radius: {RADIUS_SM}px;
}}

/* ---------- message boxes ---------- */
QMessageBox {{
    background: {t.bg_panel};
}}
QMessageBox QLabel {{
    color: {t.text_main};
    font-size: 13px;
}}

/* ---------- labels with roles ---------- */
QLabel[role="heading"] {{
    font-size: 20px;
    font-weight: 700;
    color: {t.text_main};
}}
QLabel[role="subheading"] {{
    font-size: 14px;
    font-weight: 600;
    color: {t.text_main};
}}
QLabel[role="secondary"] {{
    color: {t.text_secondary};
    font-size: 12px;
}}
QLabel[role="caption"] {{
    color: {t.text_secondary};
    font-size: 11px;
}}
QLabel[role="danger"] {{
    color: {t.danger};
}}
QLabel[role="warning"] {{
    color: {t.warning};
}}
QLabel[role="success"] {{
    color: {t.success};
}}
QLabel[role="accent"] {{
    color: {t.accent};
    font-weight: 600;
}}

/* ---------- notice banners ---------- */
QFrame[notice="info"] {{
    background: {t.accent_soft};
    border: 1px solid {t.accent};
    border-radius: {RADIUS}px;
}}
QFrame[notice="warning"] {{
    background: {t.accent_soft};
    border: 1px solid {t.warning};
    border-radius: {RADIUS}px;
}}
QFrame[notice="danger"] {{
    background: {t.accent_soft};
    border: 1px solid {t.danger};
    border-radius: {RADIUS}px;
}}
"""


def tokens_dict(t: ThemeTokens) -> dict:
    return asdict(t)
