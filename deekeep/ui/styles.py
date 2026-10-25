"""
Feuille de style QSS globale de l'application DeeKeep.

Design « Neon Nocturne » (voir DESIGN.md) : fond charbon profond,
accent cyan néon pour les éléments interactifs, boutons « pilules »,
navigation à barre indicatrice néon et dégradé navy → cyan pour les
barres de progression.
"""

import os

from deekeep.core.settings import data_dir

# --- Palette Neon Nocturne ---
BACKGROUND_DEEP = "#0a0b10"      # niveau 0 : fond de l'application
SURFACE = "#12141c"              # niveau 1 : barre latérale, dialogues
SURFACE_CHARCOAL = "#1c1e26"     # niveau 1 : cartes, champs, vignettes
SURFACE_HIGH = "#252833"         # niveau 2 : survols, popups
PRIMARY = "#00e5ff"              # cyan néon (interactif, actif, progression)
PRIMARY_HOVER = "#5cefff"
ON_PRIMARY = "#00363d"           # texte sombre posé sur le cyan
SECONDARY = "#343d96"            # navy profond (départ des dégradés)
TERTIARY = "#5eeb7f"             # vert succès
ERROR = "#ffb4ab"                # rouge doux (texte d'erreur)
ERROR_CONTAINER = "#93000a"      # rouge container (boutons destructifs)
TEXT = "#e5e2e1"
TEXT_MUTED = "#bac9cc"
TEXT_FAINT = "#849396"
GLASS_BORDER = "rgba(255, 255, 255, 0.08)"

HEADLINE_FONT = '"Montserrat", "Segoe UI", "Helvetica Neue", Arial, sans-serif'
BODY_FONT = '"Inter", "Segoe UI", "Helvetica Neue", Arial, sans-serif'

# --- Glyphes des cases à cocher ---
# Dès qu'une feuille de style peint le fond d'un « ::indicator », Qt renonce à
# dessiner la coche native : le témoin coché devient un carré cyan plein, donc
# indiscernable au premier coup d'oeil. On redessine donc le glyphe nous-mêmes
# via « image: », ce qui impose un vrai fichier sur disque (QSS n'accepte pas
# les données en ligne). Les fichiers sont écrits dans le dossier de données au
# premier import et réécrits si leur contenu a changé.

def _check_svg(size, color):
    """Coche vectorielle, dessinée avec 20 % de marge dans son cadre."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
        f'height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        'stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M5 12.5 L10 17 L19 7"/></svg>'
    )


def _dash_svg(size, color):
    """Tiret de l'état partiellement coché (indeterminate)."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
        f'height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        'stroke-width="3.4" stroke-linecap="round">'
        '<path d="M6 12 L18 12"/></svg>'
    )


def _glyph_rule(name, svg):
    """
    Écrit *svg* dans le dossier de données et renvoie la déclaration QSS
    « image: url(...) » correspondante. Renvoie une chaîne vide si le fichier
    ne peut pas être écrit : la feuille de style reste alors valide, on perd
    seulement la coche.
    """
    try:
        folder = os.path.join(data_dir(), "icons")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        current = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                current = handle.read()
        if current != svg:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(svg)
    except OSError:
        return ""
    # QSS attend des séparateurs « / », y compris sous Windows.
    return 'image: url("%s");' % path.replace("\\", "/")


CHECK_18 = _glyph_rule("check_18.svg", _check_svg(12, ON_PRIMARY))
CHECK_18_DISABLED = _glyph_rule("check_18_disabled.svg", _check_svg(12, TEXT_FAINT))
CHECK_16 = _glyph_rule("check_16.svg", _check_svg(10, ON_PRIMARY))
DASH_16 = _glyph_rule("dash_16.svg", _dash_svg(10, ON_PRIMARY))

STYLE_SHEET = f"""
    QMainWindow {{
        background-color: {BACKGROUND_DEEP};
    }}
    QWidget {{
        color: {TEXT};
        font-family: {BODY_FONT};
    }}

    /* Barre latérale gauche (verre sombre + trait lumineux) */
    QFrame#sidebar {{
        background-color: #101219;
        border-right: 1px solid {GLASS_BORDER};
        min-width: 250px;
        max-width: 250px;
    }}

    /* Logo de l'application */
    QLabel#logo_title {{
        font-family: {HEADLINE_FONT};
        font-size: 20px;
        font-weight: 800;
        color: {PRIMARY};
    }}
    QLabel#logo_subtitle {{
        font-size: 9px;
        font-weight: 600;
        color: {TEXT_MUTED};
    }}

    /* Boutons de navigation : barre indicatrice néon à gauche */
    QPushButton.nav-btn {{
        background-color: transparent;
        border: none;
        border-left: 3px solid transparent;
        color: {TEXT_MUTED};
        text-align: left;
        padding: 12px 20px;
        font-size: 14px;
        font-weight: 500;
        margin: 2px 0px;
    }}
    QPushButton.nav-btn:hover {{
        background-color: rgba(255, 255, 255, 0.05);
        color: {TEXT};
    }}
    QPushButton.nav-btn[active="true"] {{
        border-left: 3px solid {PRIMARY};
        background-color: rgba(0, 229, 255, 0.10);
        color: {PRIMARY};
        font-weight: bold;
    }}

    /* En-tête de groupe repliable (« Outils ») : plus discret qu'un onglet */
    QPushButton#nav_group {{
        color: {TEXT_FAINT};
        font-size: 13px;
        font-weight: 600;
    }}
    QPushButton#nav_group:hover {{
        color: {TEXT};
    }}

    /* Sous-éléments d'un groupe : indentés sous l'en-tête */
    QPushButton.nav-sub-btn {{
        background-color: transparent;
        border: none;
        border-left: 3px solid transparent;
        color: {TEXT_MUTED};
        text-align: left;
        padding: 10px 20px 10px 44px;
        font-size: 13px;
        font-weight: 500;
        margin: 1px 0px;
    }}
    QPushButton.nav-sub-btn:hover {{
        background-color: rgba(255, 255, 255, 0.05);
        color: {TEXT};
    }}
    QPushButton.nav-sub-btn[active="true"] {{
        border-left: 3px solid {PRIMARY};
        background-color: rgba(0, 229, 255, 0.10);
        color: {PRIMARY};
        font-weight: bold;
    }}

    /* Badge de notification (Historique) */
    QLabel#badge {{
        background-color: {PRIMARY};
        color: {ON_PRIMARY};
        border-radius: 9px;
        font-size: 10px;
        font-weight: bold;
        padding: 2px 7px;
        min-width: 14px;
        min-height: 14px;
        max-height: 14px;
    }}

    /* Boutons de contrôle (pilules « verre ») */
    QPushButton.control-btn {{
        background-color: transparent;
        border: 1px solid {GLASS_BORDER};
        color: {TEXT_MUTED};
        padding: 10px 20px;
        border-radius: 18px;
        font-size: 12px;
        font-weight: 600;
    }}
    QPushButton.control-btn:hover {{
        background-color: rgba(255, 255, 255, 0.06);
        border-color: rgba(255, 255, 255, 0.20);
        color: {TEXT};
    }}
    /* Un bouton de contrôle inactif doit se lire comme tel : sans cette
       règle, la feuille de style lui laisse les couleurs de l'état actif. */
    QPushButton.control-btn:disabled {{
        color: #4a5c60;
        border-color: rgba(255, 255, 255, 0.04);
    }}
    /* Bouton de contrôle à bascule enfoncé (filtre d'affichage actif) */
    QPushButton.control-btn:checked {{
        background-color: rgba(0, 229, 255, 0.12);
        border-color: rgba(0, 229, 255, 0.45);
        color: {PRIMARY};
    }}

    /* Bouton d'action secondaire (pilule cyan ajourée).
       Occupe, dans chaque barre d'actions, la place qui précède le bouton
       principal : l'étape qui alimente ou analyse la liste (« Rechercher »,
       « Ajouter des fichiers », « Analyser »), avant celle qui écrit sur le
       disque. */
    QPushButton.accent-btn {{
        background-color: rgba(0, 229, 255, 0.10);
        border: 1px solid {PRIMARY};
        color: {PRIMARY};
        padding: 10px 22px;
        border-radius: 18px;
        font-size: 13px;
        font-weight: bold;
    }}
    QPushButton.accent-btn:hover {{
        background-color: rgba(0, 229, 255, 0.20);
        border-color: {PRIMARY_HOVER};
        color: {PRIMARY_HOVER};
    }}
    QPushButton.accent-btn:disabled {{
        background-color: transparent;
        border-color: rgba(0, 229, 255, 0.15);
        color: #5f7d84;
    }}

    /* Bouton destructif (pilule rouge) : réservé aux suppressions de
       fichiers sur le disque. */
    QPushButton.danger-btn {{
        background-color: {ERROR_CONTAINER};
        border: none;
        color: #ffdad6;
        padding: 10px 22px;
        border-radius: 18px;
        font-size: 13px;
        font-weight: bold;
    }}
    QPushButton.danger-btn:hover {{
        background-color: #b1121e;
    }}
    QPushButton.danger-btn:disabled {{
        background-color: rgba(147, 0, 10, 0.35);
        color: #8a6f6d;
    }}

    /* Bouton principal (pilule cyan néon, texte sombre) */
    QPushButton.primary-btn {{
        background-color: {PRIMARY};
        border: none;
        color: {ON_PRIMARY};
        padding: 10px 24px;
        border-radius: 18px;
        font-size: 13px;
        font-weight: bold;
    }}
    QPushButton.primary-btn:hover {{
        background-color: {PRIMARY_HOVER};
    }}
    QPushButton.primary-btn:disabled {{
        background-color: rgba(0, 229, 255, 0.15);
        color: #5f7d84;
    }}

    /* Tableaux (cartes translucides) */
    QTableWidget {{
        background-color: rgba(28, 30, 38, 0.55);
        border: 1px solid {GLASS_BORDER};
        border-radius: 12px;
        gridline-color: transparent;
        selection-background-color: rgba(0, 229, 255, 0.08);
    }}
    QTableWidget::item {{
        border-bottom: 1px solid rgba(255, 255, 255, 0.06);
        padding: 12px;
    }}
    QTableWidget::item:selected {{
        background-color: rgba(0, 229, 255, 0.08);
        color: {TEXT};
    }}
    QHeaderView::section {{
        background-color: transparent;
        color: {TEXT_FAINT};
        padding: 10px 8px;
        border: none;
        border-bottom: 1px solid {GLASS_BORDER};
        font-weight: bold;
        font-size: 10px;
    }}
    QTableCornerButton::section {{
        background-color: transparent;
        border: none;
    }}

    /* Arborescences (écran « Organiser ») : même carte translucide que les
       tableaux, lignes compactes pour afficher des milliers de fichiers */
    QTreeWidget {{
        background-color: rgba(28, 30, 38, 0.55);
        border: 1px solid {GLASS_BORDER};
        border-radius: 12px;
        alternate-background-color: transparent;
        selection-background-color: rgba(0, 229, 255, 0.08);
        outline: none;
        font-size: 12px;
    }}
    QTreeWidget::item {{
        padding: 4px 2px;
        border: none;
    }}
    QTreeWidget::item:selected {{
        background-color: rgba(0, 229, 255, 0.10);
        color: {TEXT};
    }}
    QTreeWidget::item:hover {{
        background-color: rgba(255, 255, 255, 0.04);
    }}
    QTreeWidget::indicator {{
        width: 16px;
        height: 16px;
        border-radius: 4px;
        border: 1px solid rgba(255, 255, 255, 0.20);
        background-color: {SURFACE_CHARCOAL};
    }}
    QTreeWidget::indicator:checked {{
        background-color: {PRIMARY};
        border-color: {PRIMARY};
        {CHECK_16}
    }}
    QTreeWidget::indicator:indeterminate {{
        background-color: rgba(0, 229, 255, 0.35);
        border-color: {PRIMARY};
        {DASH_16}
    }}

    /* Bouton à menu déroulant (« Insérer une balise ») */
    QToolButton {{
        background-color: transparent;
        border: 1px solid {GLASS_BORDER};
        color: {TEXT_MUTED};
        padding: 8px 14px;
        border-radius: 14px;
        font-size: 12px;
        font-weight: 600;
    }}
    QToolButton:hover {{
        background-color: rgba(255, 255, 255, 0.06);
        border-color: rgba(255, 255, 255, 0.20);
        color: {TEXT};
    }}
    QToolButton::menu-indicator {{
        image: none;
        width: 0px;
    }}

    /* Menus (liste des balises, menu du tray) */
    QMenu {{
        background-color: {SURFACE_HIGH};
        border: 1px solid {GLASS_BORDER};
        border-radius: 8px;
        padding: 6px;
    }}
    QMenu::item {{
        padding: 6px 24px 6px 14px;
        border-radius: 6px;
        color: {TEXT_MUTED};
    }}
    QMenu::item:selected {{
        background-color: rgba(0, 229, 255, 0.15);
        color: {PRIMARY};
    }}
    QMenu::separator {{
        height: 1px;
        background: {GLASS_BORDER};
        margin: 4px 8px;
    }}

    /* Onglets de l'historique (soulignement néon) */
    QTabWidget::pane {{
        border: 1px solid {GLASS_BORDER};
        background: rgba(28, 30, 38, 0.35);
        border-radius: 12px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {TEXT_MUTED};
        padding: 10px 20px;
        border: none;
        border-bottom: 2px solid transparent;
        margin-right: 4px;
        font-weight: 600;
    }}
    QTabBar::tab:hover {{
        color: {TEXT};
    }}
    QTabBar::tab:selected {{
        color: {PRIMARY};
        border-bottom: 2px solid {PRIMARY};
    }}

    /* Barres de progression (dégradé navy -> cyan néon) */
    QProgressBar {{
        border: none;
        border-radius: 6px;
        background-color: {SURFACE_CHARCOAL};
        text-align: center;
        font-size: 10px;
        font-weight: bold;
        color: {TEXT};
        height: 14px;
    }}
    QProgressBar::chunk {{
        background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0,
            stop:0 {SECONDARY}, stop:1 {PRIMARY});
        border-radius: 6px;
    }}

    /* Barre globale spécifique */
    QProgressBar#global_progress_bar {{
        height: 20px;
        border-radius: 9px;
        font-size: 11px;
    }}
    QProgressBar#global_progress_bar::chunk {{
        background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0,
            stop:0 {SECONDARY}, stop:1 {PRIMARY});
        border-radius: 9px;
    }}

    /* Cases à cocher (accent cyan) */
    QCheckBox {{
        color: {TEXT};
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border-radius: 5px;
        border: 1px solid rgba(255, 255, 255, 0.20);
        background-color: {SURFACE_CHARCOAL};
    }}
    QCheckBox::indicator:hover {{
        border-color: {PRIMARY};
    }}
    QCheckBox::indicator:checked {{
        background-color: {PRIMARY};
        border-color: {PRIMARY};
        {CHECK_18}
    }}

    /* Dialogues et Modaux */
    QDialog {{
        background-color: {SURFACE};
        border: 1px solid {GLASS_BORDER};
        border-radius: 12px;
    }}
    QLineEdit {{
        background-color: rgba(28, 30, 38, 0.80);
        border: 1px solid {GLASS_BORDER};
        color: {TEXT};
        padding: 10px 14px;
        border-radius: 10px;
        font-size: 13px;
    }}
    QLineEdit:focus {{
        border: 1px solid {PRIMARY};
    }}
    QComboBox {{
        background-color: rgba(28, 30, 38, 0.80);
        border: 1px solid {GLASS_BORDER};
        color: {TEXT};
        padding: 8px 12px;
        border-radius: 10px;
        font-size: 13px;
    }}
    QComboBox:focus {{
        border: 1px solid {PRIMARY};
    }}
    QComboBox::drop-down {{
        border: none;
        padding-right: 10px;
    }}
    QSpinBox {{
        background-color: rgba(28, 30, 38, 0.80);
        border: 1px solid {GLASS_BORDER};
        color: {TEXT};
        padding: 6px 8px;
        border-radius: 10px;
        font-size: 13px;
    }}
    QSpinBox:focus {{
        border: 1px solid {PRIMARY};
    }}
    /* État désactivé : la feuille de style impose les couleurs, elle doit
       donc aussi décrire le gris des champs inactifs — sans ces règles, un
       champ désactivé reste indiscernable d'un champ actif. */
    QLabel:disabled {{
        color: #5f7d84;
    }}
    QComboBox:disabled, QSpinBox:disabled, QLineEdit:disabled {{
        color: #5f7d84;
        background-color: rgba(28, 30, 38, 0.35);
        border-color: rgba(255, 255, 255, 0.04);
    }}
    QToolButton:disabled {{
        color: #4a5c60;
        border-color: rgba(255, 255, 255, 0.04);
    }}
    QCheckBox:disabled {{
        color: #5f7d84;
    }}
    QCheckBox::indicator:disabled {{
        border-color: rgba(255, 255, 255, 0.08);
        background-color: rgba(28, 30, 38, 0.35);
    }}
    QCheckBox::indicator:checked:disabled {{
        background-color: rgba(0, 229, 255, 0.25);
        border-color: rgba(0, 229, 255, 0.25);
        {CHECK_18_DISABLED}
    }}

    QComboBox QAbstractItemView {{
        background-color: {SURFACE_CHARCOAL};
        color: {TEXT};
        selection-background-color: rgba(0, 229, 255, 0.20);
        selection-color: {PRIMARY};
        border: 1px solid {GLASS_BORDER};
    }}

    /* Barres de défilement (fines, lueur cyan) */
    QScrollBar:vertical {{
        background: transparent;
        width: 8px;
        margin: 0px;
    }}
    QScrollBar::handle:vertical {{
        background: rgba(0, 229, 255, 0.20);
        border-radius: 4px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: rgba(0, 229, 255, 0.40);
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 8px;
        margin: 0px;
    }}
    QScrollBar::handle:horizontal {{
        background: rgba(0, 229, 255, 0.20);
        border-radius: 4px;
        min-width: 30px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: rgba(0, 229, 255, 0.40);
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
    }}

    QToolTip {{
        background-color: {SURFACE_HIGH};
        color: {TEXT};
        border: 1px solid {GLASS_BORDER};
        padding: 4px 8px;
    }}
"""
