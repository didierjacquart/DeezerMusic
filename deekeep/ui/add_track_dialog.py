"""
Modal de recherche avancée.

Recherche Deezer par titre, artiste, album ou playlist :

* Une recherche "Artiste" ou "Album" affiche d'abord une liste d'entités
  cliquables (avec vignette). Un clic sur un artiste ouvre son top 50 ;
  un clic sur un album ouvre l'album (titre, jaquette et pistes).
* Les autres recherches affichent directement des pistes sélectionnables.

Les vignettes sont téléchargées par un thread dédié
(``image_thread.CoverDownloadThread``) : un loader "⏳" est affiché tant
que l'image n'est pas disponible, puis remplacé par la jaquette.

Chaque piste porte en outre une **pastille** disant si elle est déjà sur le
disque (``library_index``) : verte, le titre est là et sa case de sélection
est désactivée — il n'y a rien à extraire ; orange, un fichier lui ressemble
et la case reste libre mais décochée ; grise, le dossier de dépôt ne contient
rien de tel. « Tout sélectionner » ne coche que ces dernières.

Le bilan affiché à droite du filtre dit combien de lignes sont dans ce cas, et
deux boutons le prolongent : « Voir les doublons » ne laisse afficher que ces
lignes-là, et « Dédoublonner… » ouvre l'écran qui montre, sous chaque piste,
les fichiers du disque qu'elle a rencontrés et propose de les supprimer
(``dedup_dialog.LibraryDuplicateDialog``). Un fichier supprimé quitte l'index
sans relecture du dossier : la pastille repasse au gris et la piste redevient
extractible dans la seconde.
"""

import os

from PyQt6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QComboBox, QCheckBox, QProgressBar, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox,
    QApplication, QFrame
)
from PyQt6.QtCore import Qt, QEvent, QPoint
from PyQt6.QtGui import QPixmap, QPainter, QColor

from deekeep.audio import reencode_scan
from deekeep.library import library_index
from deekeep.ui import dedup_dialog
from deekeep.ui import player
from deekeep.ui.styles import STYLE_SHEET
from deekeep.workers.search_thread import DeezerSearchThread
from deekeep.workers.image_thread import CoverDownloadThread, CoverLookupThread

# Cache des vignettes de la fenêtre « Dernières recherches »
# (requête -> bytes de l'image), partagé entre les ouvertures du dialogue.
_SEARCH_COVER_CACHE = {}


class PopularityBars(QWidget):
    """Jauge de popularité façon Deezer : petites barres verticales.

    Le ``rank`` Deezer va de 0 à ~1 000 000 : la proportion de barres
    allumées reflète cette échelle absolue (comparable entre recherches).
    """

    BAR_COUNT = 7
    MAX_RANK = 1_000_000.0

    def __init__(self, rank, parent=None):
        super().__init__(parent)
        self.rank = max(0, int(rank or 0))
        self.setFixedSize(
            self.BAR_COUNT * 8 - 4, 20
        )
        self.setToolTip(f"Popularité Deezer : {self.rank:,}".replace(",", " "))

    def paintEvent(self, event):
        filled = round(min(self.rank / self.MAX_RANK, 1.0) * self.BAR_COUNT)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        bar_width, gap = 4, 4
        bar_height = self.height() - 4
        for i in range(self.BAR_COUNT):
            color = QColor("#00e5ff") if i < filled else QColor("#3a3d47")
            painter.setBrush(color)
            painter.drawRoundedRect(
                i * (bar_width + gap), 2, bar_width, bar_height, 1.5, 1.5
            )
        painter.end()


class AddTrackDialog(QDialog):
    def __init__(self, parent=None, search_history=None, index=None):
        """``index`` est l'index du dossier de dépôt (``library_index``).

        Il peut être ``None`` : sa construction tourne en tâche de fond et une
        recherche lancée aussitôt l'application ouverte ne l'attend pas. Les
        pastilles affichent alors « ⏳ », puis se mettent à jour dès que la
        fenêtre principale signale l'index prêt (``libraryIndexChanged``).
        """
        super().__init__(parent)
        self.setWindowTitle("Rechercher et ajouter des pistes")
        self.setMinimumWidth(950)
        self.setMinimumHeight(600)
        self.setStyleSheet(STYLE_SHEET)

        # Dimensionner la fenêtre à la quasi-totalité de l'écran afin
        # d'afficher le maximum de lignes de résultats possibles.
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.resize(
                min(1400, int(avail.width() * 0.9)),
                int(avail.height() * 0.9),
            )

        self.search_thread = None
        self.cover_thread = None
        self.header_cover_thread = None
        self.search_history = search_history if search_history is not None else []

        # Index du dossier de dépôt et états de la colonne des pastilles
        # (un ``library_index.Match`` par ligne affichée).
        self.library_index = index
        self._library_states = []
        # Filtre « doublons seuls » (bouton à bascule du bilan) : il se
        # combine au filtre textuel, il ne le remplace pas.
        self._dups_only = False
        if parent is not None and hasattr(parent, "libraryIndexChanged"):
            parent.libraryIndexChanged.connect(self.set_library_index)

        # Lecteur d'extraits (30 s) : un seul extrait joué à la fois.
        # Bouton dont l'extrait joue (module ``player``), s'il y en a un :
        # le dialogue ne possède plus de lecteur, il emprunte celui que
        # partagent tous les écrans.
        self._preview_btn = None

        self.search_cache = []        # pistes affichées
        self.entities_cache = []      # entités (artistes/albums) affichées
        self.entities_kind = None     # "artist" | "album"
        self.view_mode = "tracks"     # "tracks" | "entities"
        self.cover_col = 1            # colonne des vignettes selon le mode
        self.nav_stack = []           # pile de navigation (pour le bouton Retour)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(15)

        label = QLabel("Rechercher par morceau, artiste, album ou playlist :")
        label.setStyleSheet("font-weight: bold; font-size: 14px; color: #ffffff;")
        layout.addWidget(label)

        # Barre supérieure
        search_layout = QHBoxLayout()
        search_layout.setSpacing(10)

        # Champ de recherche avec fenêtre contextuelle « Dernières
        # recherches » (façon Deezer) affichée au clic / à la prise de focus.
        self.input_search = QLineEdit()
        self.input_search.setPlaceholderText("Saisissez un titre, un artiste, ou collez un lien de playlist...")
        self.input_search.returnPressed.connect(self.start_search)
        self.input_search.installEventFilter(self)
        self.input_search.textEdited.connect(lambda _: self._hide_search_popup())
        self._search_popup = None
        self._popup_cover_threads = []

        self.combo_type = QComboBox()
        self.combo_type.addItems(["Tout / Titre", "Artiste", "Album", "Playlist"])
        self.combo_type.setFixedWidth(130)

        self.btn_search = QPushButton("Rechercher")
        self.btn_search.setProperty("class", "accent-btn")
        self.btn_search.clicked.connect(self.start_search)

        search_layout.addWidget(self.input_search, 1)
        search_layout.addWidget(self.combo_type)
        search_layout.addWidget(self.btn_search)
        layout.addLayout(search_layout)

        # Barre dédiée : coller un lien Deezer
        link_layout = QHBoxLayout()
        link_layout.setSpacing(10)

        self.input_link = QLineEdit()
        self.input_link.setPlaceholderText("Collez un lien Deezer (https://www.deezer.com/track/… ou /playlist/…)")
        self.input_link.returnPressed.connect(self.search_from_link)

        self.btn_link = QPushButton("Coller et rechercher")
        self.btn_link.setProperty("class", "control-btn")
        self.btn_link.clicked.connect(self.search_from_link)

        link_layout.addWidget(self.input_link, 1)
        link_layout.addWidget(self.btn_link)
        layout.addLayout(link_layout)

        # Barre de progression indéterminée (loader de recherche)
        self.search_progress = QProgressBar()
        self.search_progress.setRange(0, 0)
        self.search_progress.setVisible(False)
        self.search_progress.setStyleSheet("height: 8px; border: none; background: #1c1e26;")
        layout.addWidget(self.search_progress)

        # Fil d'Ariane + bouton retour (visible dans les vues détaillées)
        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(10)
        self.btn_back = QPushButton("← Retour")
        self.btn_back.setProperty("class", "control-btn")
        self.btn_back.clicked.connect(self.go_back)
        self.btn_back.setVisible(False)
        self.lbl_breadcrumb = QLabel("")
        self.lbl_breadcrumb.setStyleSheet("color: #bac9cc; font-size: 13px;")
        nav_layout.addWidget(self.btn_back)
        nav_layout.addWidget(self.lbl_breadcrumb, 1)
        layout.addLayout(nav_layout)

        # En-tête d'album (jaquette + titre), masqué par défaut
        self.album_header = QWidget()
        # Hauteur figée : sans cela le widget s'étire verticalement et laisse
        # de grands espaces vides au-dessus et au-dessous de la jaquette.
        self.album_header.setFixedHeight(98)
        header_layout = QHBoxLayout(self.album_header)
        header_layout.setContentsMargins(0, 0, 0, 8)
        header_layout.setSpacing(16)
        self.header_cover = QLabel("⏳")
        self.header_cover.setFixedSize(90, 90)
        self.header_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.header_cover.setStyleSheet("background-color: #1c1e26; border-radius: 8px; color: #849396; font-size: 24px;")
        header_text = QWidget()
        header_text_layout = QVBoxLayout(header_text)
        header_text_layout.setContentsMargins(0, 0, 0, 0)
        header_text_layout.setSpacing(4)
        header_text_layout.addStretch()
        self.header_title = QLabel("")
        self.header_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #ffffff;")
        self.header_subtitle = QLabel("")
        self.header_subtitle.setStyleSheet("font-size: 13px; color: #bac9cc;")
        header_text_layout.addWidget(self.header_title)
        header_text_layout.addWidget(self.header_subtitle)
        header_text_layout.addStretch()
        header_layout.addWidget(self.header_cover)
        header_layout.addWidget(header_text, 1)
        self.album_header.setVisible(False)
        layout.addWidget(self.album_header)

        # Filtres & Outils de masse
        self.filter_bar = QWidget()
        filter_layout = QHBoxLayout(self.filter_bar)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(12)

        self.cb_select_all = QCheckBox("Tout sélectionner")
        self.cb_select_all.stateChanged.connect(self.toggle_select_all_search)
        self.cb_select_all.setStyleSheet("font-weight: 600;")

        self.input_filter = QLineEdit()
        self.input_filter.setPlaceholderText("Filtrer instantanément les résultats...")
        self.input_filter.textChanged.connect(self.filter_results)

        # Bilan de la confrontation au disque : combien de titres affichés
        # sont déjà rangés, combien ressemblent à un fichier existant.
        self.lbl_library = QLabel("")
        self.lbl_library.setStyleSheet("color: #bac9cc; font-size: 12px;")

        # Le bilan ne fait que constater ; ces deux boutons en font quelque
        # chose — n'afficher que les lignes en cause, ou ouvrir l'écran qui
        # supprime les fichiers du disque devenus inutiles.
        self.btn_show_dups = QPushButton("Voir les doublons")
        self.btn_show_dups.setProperty("class", "control-btn")
        self.btn_show_dups.setCheckable(True)
        self.btn_show_dups.setVisible(False)
        self.btn_show_dups.setToolTip(
            "N'afficher que les titres déjà sur le disque ou soupçonnés d'y être"
        )
        self.btn_show_dups.toggled.connect(self._toggle_dups_only)

        self.btn_dedup = QPushButton("Dédoublonner…")
        self.btn_dedup.setProperty("class", "control-btn")
        self.btn_dedup.setVisible(False)
        self.btn_dedup.setToolTip(
            "Examiner les fichiers du disque rapprochés de ces titres et "
            "supprimer ceux dont on ne veut plus"
        )
        self.btn_dedup.clicked.connect(self.review_duplicates)

        filter_layout.addWidget(self.cb_select_all)
        filter_layout.addWidget(self.input_filter, 1)
        filter_layout.addWidget(self.lbl_library)
        filter_layout.addWidget(self.btn_show_dups)
        filter_layout.addWidget(self.btn_dedup)
        layout.addWidget(self.filter_bar)

        # Tableau de résultats (pistes OU entités selon le mode)
        self.results_table = QTableWidget()
        self.results_table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._sort_state = (-1, Qt.SortOrder.AscendingOrder)
        self.results_table.horizontalHeader().setSectionsClickable(True)
        self.results_table.horizontalHeader().setSortIndicatorShown(True)
        self.results_table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.verticalHeader().setDefaultSectionSize(60)
        layout.addWidget(self.results_table, 1)

        # Validation
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Annuler")
        self.btn_cancel.setProperty("class", "control-btn")
        self.btn_cancel.clicked.connect(self.cancel_or_reject)

        self.btn_add = QPushButton("Ajouter la sélection")
        self.btn_add.setProperty("class", "primary-btn")
        self.btn_add.setEnabled(False)
        self.btn_add.clicked.connect(self.accept)

        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_add)
        layout.addLayout(btn_layout)

    # ==================================================================
    # Fenêtre contextuelle « Dernières recherches » (façon Deezer)
    # ==================================================================
    def eventFilter(self, obj, event):
        if obj is self.input_search:
            if event.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
                self._show_search_popup()
            elif event.type() == QEvent.Type.FocusOut:
                # Les clics dans la fenêtre contextuelle ne volent pas le
                # focus (NoFocus) : une perte de focus = clic ailleurs.
                if self._search_popup and not self._search_popup.underMouse():
                    self._hide_search_popup()
            elif (event.type() == QEvent.Type.KeyPress
                    and event.key() == Qt.Key.Key_Escape
                    and self._search_popup and self._search_popup.isVisible()):
                self._hide_search_popup()
                return True
        return super().eventFilter(obj, event)

    def _hide_search_popup(self):
        if self._search_popup:
            self._search_popup.hide()

    def _show_search_popup(self):
        if not self.search_history:
            self._hide_search_popup()
            return
        self._build_search_popup()
        pos = self.input_search.mapTo(self, QPoint(0, self.input_search.height() + 6))
        self._search_popup.setFixedWidth(max(460, self.input_search.width()))
        self._search_popup.adjustSize()
        self._search_popup.move(pos)
        self._search_popup.show()
        self._search_popup.raise_()

    def _build_search_popup(self):
        """(Re)construit le panneau des dernières recherches."""
        if self._search_popup:
            self._search_popup.deleteLater()

        popup = QFrame(self)
        popup.setObjectName("searchPopup")
        popup.setStyleSheet(
            "#searchPopup { background-color: #22242e; "
            "border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 14px; }"
        )
        lay = QVBoxLayout(popup)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(4)

        # En-tête : titre + corbeille (tout effacer)
        head = QHBoxLayout()
        title = QLabel("Dernières recherches")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #ffffff;")
        btn_trash = QPushButton("🗑")
        btn_trash.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_trash.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_trash.setToolTip("Effacer les dernières recherches")
        btn_trash.setFixedSize(30, 30)
        btn_trash.setStyleSheet(
            "QPushButton { background: transparent; border: none; font-size: 15px; }"
            "QPushButton:hover { background-color: rgba(255, 255, 255, 0.08); border-radius: 15px; }"
        )
        btn_trash.clicked.connect(self._clear_recent_searches)
        head.addWidget(title)
        head.addStretch()
        head.addWidget(btn_trash)
        lay.addLayout(head)

        for query in self.search_history[:8]:
            lay.addWidget(self._make_popup_row(query))

        self._search_popup = popup

    def _make_popup_row(self, query):
        """Ligne cliquable : vignette + libellé + suppression individuelle."""
        row = QWidget()
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setStyleSheet(
            "QWidget { background: transparent; border-radius: 8px; }"
            "QWidget:hover { background-color: rgba(255, 255, 255, 0.06); }"
        )
        lay = QHBoxLayout(row)
        lay.setContentsMargins(6, 5, 6, 5)
        lay.setSpacing(12)

        cover = QLabel("🎵")
        cover.setFixedSize(42, 42)
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover.setStyleSheet(
            "background-color: #1c1e26; border-radius: 6px; "
            "color: #849396; font-size: 15px;"
        )
        data = _SEARCH_COVER_CACHE.get(query)
        if data:
            self._apply_pixmap(cover, data, size=42)
        else:
            thread = CoverLookupThread("", query, None)
            thread.result_signal.connect(
                lambda d, q=query, lbl=cover: self._on_popup_cover(q, lbl, d)
            )
            self._popup_cover_threads.append(thread)
            thread.start()
        lay.addWidget(cover)

        text = QWidget()
        text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text_lay = QVBoxLayout(text)
        text_lay.setContentsMargins(0, 0, 0, 0)
        text_lay.setSpacing(1)
        lbl_query = QLabel(query)
        lbl_query.setStyleSheet("color: #ffffff; font-size: 14px; font-weight: 600; background: transparent;")
        lbl_kind = QLabel("Recherche")
        lbl_kind.setStyleSheet("color: #849396; font-size: 11px; background: transparent;")
        text_lay.addWidget(lbl_query)
        text_lay.addWidget(lbl_kind)
        lay.addWidget(text, 1)

        btn_del = QPushButton("✕")
        btn_del.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setToolTip("Retirer cette recherche")
        btn_del.setFixedSize(26, 26)
        btn_del.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #849396; font-size: 12px; }"
            "QPushButton:hover { background-color: rgba(255, 255, 255, 0.10); "
            "border-radius: 13px; color: #ffffff; }"
        )
        btn_del.clicked.connect(lambda _, q=query: self._remove_recent_search(q))
        lay.addWidget(btn_del)

        row.mousePressEvent = lambda event, q=query: self._use_recent_search(q)
        return row

    def _on_popup_cover(self, query, label, data):
        if not data:
            return
        _SEARCH_COVER_CACHE[query] = data
        try:
            self._apply_pixmap(label, data, size=42)
        except RuntimeError:
            pass  # la fenêtre contextuelle a été reconstruite entre-temps

    def _use_recent_search(self, query):
        self.input_search.setText(query)
        self._hide_search_popup()
        self.start_search()

    def _remove_recent_search(self, query):
        if query in self.search_history:
            self.search_history.remove(query)
        if self.search_history:
            self._show_search_popup()  # reconstruit la liste
        else:
            self._hide_search_popup()

    def _clear_recent_searches(self):
        self.search_history.clear()
        self._hide_search_popup()

    # ==================================================================
    # Lancement des recherches
    # ==================================================================
    def search_from_link(self):
        link = self.input_link.text().strip()
        if not link:
            return
        self.input_search.setText(link)
        self.start_search()

    def start_search(self):
        self._hide_search_popup()
        query = self.input_search.text().strip()
        search_type = self.combo_type.currentText()
        if not query:
            return

        # Mémoriser dans l'historique
        if query in self.search_history:
            self.search_history.remove(query)
        self.search_history.insert(0, query)

        # Réinitialiser la navigation (nouvelle recherche = niveau racine)
        self.nav_stack = []
        self.btn_back.setVisible(False)
        self.album_header.setVisible(False)
        self.lbl_breadcrumb.setText("")

        self._launch_thread(query, search_type)

    def _launch_thread(self, query, search_type, album_title=None, album_cover_url=None):
        """Démarre le thread de recherche et branche ses signaux."""
        self._stop_cover_threads()

        self.btn_search.setEnabled(False)
        self.btn_search.setText("Recherche...")
        self.btn_add.setEnabled(False)
        self.cb_select_all.setChecked(False)
        self.input_filter.clear()
        self.search_progress.setVisible(True)

        self.search_thread = DeezerSearchThread(
            query, search_type, album_title=album_title, album_cover_url=album_cover_url
        )
        self.search_thread.results_signal.connect(self.on_tracks_success)
        self.search_thread.entities_signal.connect(self.on_entities_success)
        self.search_thread.error_signal.connect(self.on_search_error)
        self.search_thread.start()

    def _reset_search_button(self):
        self.btn_search.setEnabled(True)
        self.btn_search.setText("Rechercher")
        self.search_progress.setVisible(False)

    # ==================================================================
    # Affichage des ENTITÉS (artistes / albums)
    # ==================================================================
    def on_entities_success(self, entities, kind):
        self._reset_search_button()
        self.view_mode = "entities"
        self.entities_cache = entities
        self.entities_kind = kind
        self._display_entities(entities, kind)

        if not entities:
            QMessageBox.information(self, "Aucun résultat", "Aucun résultat trouvé.")

    def _display_entities(self, entities, kind):
        self._stop_preview()
        self.view_mode = "entities"
        self.filter_bar.setVisible(False)
        self.album_header.setVisible(False)
        self.cover_col = 0

        noun = "artiste" if kind == "artist" else "album"
        action_label = "Voir le top 50" if kind == "artist" else "Ouvrir l'album"

        self.results_table.clear()
        self.results_table.setColumnCount(4)
        self.results_table.setHorizontalHeaderLabels(["", noun.upper(), "DÉTAILS", ""])
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.results_table.setColumnWidth(2, 220)
        self.results_table.setColumnWidth(3, 180)
        self.results_table.setRowCount(0)

        for row, ent in enumerate(entities):
            self.results_table.insertRow(row)
            self.results_table.setCellWidget(row, 0, self._make_cover_label(ent.get("cover_bytes")))
            self.results_table.setItem(row, 1, QTableWidgetItem(ent.get("name", "")))
            self.results_table.setItem(row, 2, QTableWidgetItem(ent.get("subtitle", "")))

            btn_open = QPushButton(action_label)
            btn_open.setStyleSheet(
                "QPushButton { background-color: #00e5ff; border: none; color: #00363d; "
                "padding: 6px 14px; border-radius: 14px; font-weight: bold; }"
                "QPushButton:hover { background-color: #5cefff; }"
            )
            btn_open.clicked.connect(lambda checked, e=ent: self.open_entity(e))
            # Conteneur avec marges pour éviter que le bouton soit rogné par
            # les bords de la cellule.
            cell = QWidget()
            cell_lay = QHBoxLayout(cell)
            cell_lay.setContentsMargins(8, 4, 8, 4)
            cell_lay.addWidget(btn_open)
            self.results_table.setCellWidget(row, 3, cell)

        self._start_cover_thread(entities)

    def open_entity(self, entity):
        """Explore une entité : top 50 (artiste) ou pistes de l'album.

        Peut être appelée depuis la liste d'entités OU depuis un lien
        artiste/album dans une liste de pistes.
        """
        kind = entity.get("kind")
        if not entity.get("id"):
            return

        # Mémoriser la vue courante pour pouvoir y revenir.
        self._push_snapshot()

        if kind == "artist":
            self.lbl_breadcrumb.setText(f"Artiste : {entity.get('name', '')} — top 50")
            self.album_header.setVisible(False)
            self._launch_thread(f"artist/{entity['id']}", "Artiste")
        elif kind == "album":
            self.lbl_breadcrumb.setText(f"Album : {entity.get('name', '')}")
            # Lancer la recherche AVANT d'afficher l'en-tête : _launch_thread
            # appelle _stop_cover_threads(), ce qui tuerait sinon le thread de
            # téléchargement de la jaquette d'album qu'on vient de démarrer.
            self._launch_thread(
                f"album/{entity['id']}", "Album",
                album_title=entity.get("name"),
                album_cover_url=entity.get("cover_url"),
            )
            self._show_album_header(entity)
        self.btn_back.setVisible(True)

    def _push_snapshot(self):
        """Sauvegarde l'état de la vue courante sur la pile de navigation."""
        header = None
        if self.album_header.isVisible():
            header = {
                "title": self.header_title.text(),
                "subtitle": self.header_subtitle.text(),
                "pixmap": self.header_cover.pixmap(),
            }
        self.nav_stack.append({
            "mode": self.view_mode,
            "tracks": list(self.search_cache),
            "entities": list(self.entities_cache),
            "kind": self.entities_kind,
            "breadcrumb": self.lbl_breadcrumb.text(),
            "header": header,
        })

    def go_back(self):
        """Revient à la vue précédente (sans nouvelle requête réseau)."""
        if not self.nav_stack:
            return
        self._stop_cover_threads()
        self._reset_search_button()
        snap = self.nav_stack.pop()

        self.lbl_breadcrumb.setText(snap["breadcrumb"])
        if snap["mode"] == "entities":
            self.entities_cache = snap["entities"]
            self.entities_kind = snap["kind"]
            self._display_entities(self.entities_cache, self.entities_kind)
        else:
            self.search_cache = snap["tracks"]
            # Restaurer l'en-tête d'album éventuel avant le rendu.
            if snap["header"]:
                self.album_header.setVisible(True)
                self.header_title.setText(snap["header"]["title"])
                self.header_subtitle.setText(snap["header"]["subtitle"])
                pix = snap["header"]["pixmap"]
                if pix and not pix.isNull():
                    self.header_cover.setPixmap(pix)
                    self.header_cover.setText("")
                    self.header_cover.setStyleSheet("border-radius: 8px;")
            else:
                self.album_header.setVisible(False)
            self._render_tracks(self.search_cache)

        self.btn_back.setVisible(bool(self.nav_stack))

    # ==================================================================
    # Affichage des PISTES
    # ==================================================================
    def on_tracks_success(self, results):
        self._reset_search_button()
        self.search_cache = results
        self._render_tracks(results)

        if len(results) == 0:
            QMessageBox.information(self, "Aucun résultat", "Aucun morceau trouvé pour cette recherche.")

        self._start_cover_thread(results)

    def _render_tracks(self, results):
        """Construit le tableau des pistes (artiste et album cliquables)."""
        self._stop_preview()
        self.view_mode = "tracks"
        self.filter_bar.setVisible(True)
        self.cover_col = 1

        self.results_table.clear()
        self.results_table.setColumnCount(8)
        self.results_table.setHorizontalHeaderLabels(
            ["", "JAQUETTE", "TITRE", "ARTISTE", "ALBUM", "DURÉE", "POP.", "ÉCOUTE"]
        )
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        header.setMinimumSectionSize(100)
        self.results_table.setColumnWidth(1, 60)
        self.results_table.setColumnWidth(3, 170)
        self.results_table.setColumnWidth(4, 170)
        self.results_table.setColumnWidth(6, 100)
        self.results_table.setColumnWidth(7, 100)
        self.results_table.setRowCount(0)

        for row, track in enumerate(results):
            self.results_table.insertRow(row)

            # Case de sélection, précédée de la pastille « déjà sur le
            # disque ? » : la coche et son verdict se lisent d'un seul coup
            # d'œil, sans colonne supplémentaire à faire tenir dans le tableau.
            container = QWidget()
            lay = QHBoxLayout(container)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(2)
            lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(library_index.make_badge())
            cb = QCheckBox()
            cb.stateChanged.connect(self.on_checkbox_changed)
            lay.addWidget(cb)
            self.results_table.setCellWidget(row, 0, container)

            self.results_table.setCellWidget(row, 1, self._make_cover_label(track.get("cover_bytes")))
            self.results_table.setItem(row, 2, QTableWidgetItem(track["title"]))

            # Artiste cliquable (ouvre le top 50) si un identifiant est connu.
            artist_id = track.get("artist_id")
            if artist_id:
                entity = {
                    "kind": "artist", "id": artist_id, "name": track["artist"],
                    "subtitle": "", "cover_url": None,
                }
                self.results_table.setCellWidget(
                    row, 3, self._make_link_label(track["artist"], entity)
                )
            else:
                self.results_table.setItem(row, 3, QTableWidgetItem(track["artist"]))

            # Album cliquable (ouvre l'album) si un identifiant est connu.
            album_id = track.get("album_id")
            if album_id:
                entity = {
                    "kind": "album", "id": album_id, "name": track["album"],
                    "subtitle": track["artist"], "cover_url": track.get("cover_url"),
                }
                self.results_table.setCellWidget(
                    row, 4, self._make_link_label(track["album"], entity)
                )
            else:
                self.results_table.setItem(row, 4, QTableWidgetItem(track["album"]))

            dur_item = QTableWidgetItem(track["duration"])
            dur_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.results_table.setItem(row, 5, dur_item)

            # Popularité (jauge façon Deezer, centrée dans la cellule)
            pop_container = QWidget()
            pop_lay = QHBoxLayout(pop_container)
            pop_lay.setContentsMargins(0, 0, 0, 0)
            pop_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pop_lay.addWidget(PopularityBars(track.get("rank", 0)))
            self.results_table.setCellWidget(row, 6, pop_container)

            # Bouton d'écoute de l'extrait (30 s)
            self.results_table.setCellWidget(row, 7, self._make_play_cell(track))

        self._apply_library_states()

    # ==================================================================
    # Confrontation au dossier de dépôt
    # ==================================================================
    def set_library_index(self, index):
        """Reçoit l'index du dossier de dépôt et rafraîchit les pastilles."""
        self.library_index = index
        if self.view_mode == "tracks":
            self._apply_library_states()

    def _apply_library_states(self):
        """Pose sur chaque ligne le verdict du disque.

        Un titre déjà présent voit sa case décochée *et* désactivée : la
        recherche n'a pas à proposer d'extraire un fichier que la
        bibliothèque contient. Un doublon possible est décoché sans être
        interdit — la ressemblance n'est pas une preuve, et l'utilisateur
        seul sait si l'autre version lui convient.
        """
        self._library_states = []
        for row in range(self.results_table.rowCount()):
            track = self.search_cache[row] if row < len(self.search_cache) else {}
            match = library_index.lookup(
                self.library_index, track.get("title", ""),
                track.get("artist", ""), track.get("album", ""),
            )
            self._library_states.append(match)
            container = self.results_table.cellWidget(row, 0)
            if container is None:
                continue
            badge = container.findChild(QLabel, "library_badge")
            if badge is not None:
                library_index.apply_badge(badge, match)
            cb = container.findChild(QCheckBox)
            if cb is None:
                continue
            present = match.state == library_index.STATE_PRESENT
            if present or match.state == library_index.STATE_SIMILAR:
                cb.setChecked(False)
            cb.setEnabled(not present)
            cb.setToolTip(
                library_index.tooltip(match) if present
                else "Ajouter ce titre à la file de téléchargement"
            )
        self._update_library_summary()

    def _update_library_summary(self):
        """Bilan affiché à droite du filtre (rien à dire s'il n'y a rien)."""
        if not hasattr(self, "lbl_library") or not hasattr(self, "btn_dedup"):
            return
        present = sum(1 for m in self._library_states
                      if m.state == library_index.STATE_PRESENT)
        similar = sum(1 for m in self._library_states
                      if m.state == library_index.STATE_SIMILAR)
        total = present + similar
        self.btn_show_dups.setVisible(bool(total))
        self.btn_dedup.setVisible(bool(self._duplicate_candidates()))
        if not total:
            self.lbl_library.setText("")
            self.lbl_library.setToolTip("")
            # Plus rien à montrer : le filtre « doublons seuls » se relâche,
            # sinon le tableau resterait vide sans raison visible.
            if self.btn_show_dups.isChecked():
                self.btn_show_dups.setChecked(False)
            return
        self.btn_show_dups.setText(f"Voir les doublons ({total})")
        parts = []
        if present:
            parts.append(
                f"<span style='color:{library_index.STATE_COLORS[library_index.STATE_PRESENT]};'>"
                f"✓ {present} déjà sur le disque</span>"
            )
        if similar:
            parts.append(
                f"<span style='color:{library_index.STATE_COLORS[library_index.STATE_SIMILAR]};'>"
                f"≈ {similar} doublon(s) possible(s)</span>"
            )
        self.lbl_library.setText(" · ".join(parts))
        self.lbl_library.setToolTip(
            "Les titres déjà présents ne peuvent pas être sélectionnés ; les "
            "doublons possibles restent libres mais décochés.\n"
            "« Tout sélectionner » ne coche ni les uns ni les autres.\n"
            "« Voir les doublons » n'affiche que ces lignes ; « Dédoublonner… » "
            "montre les fichiers du disque en cause."
        )

    def _make_link_label(self, text, entity):
        """Crée un libellé cliquable (lien) qui ouvre l'entité associée."""
        import html
        safe = html.escape(text or "")
        lbl = QLabel(f'<a href="#" style="color:#00e5ff; text-decoration:none;">{safe}</a>')
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet("padding-left: 6px;")
        lbl.setToolTip("Cliquer pour explorer")
        lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        lbl.linkActivated.connect(lambda _=None, e=entity: self.open_entity(e))
        return lbl

    # ==================================================================
    # Écoute d'un extrait (30 s) avant téléchargement
    # ==================================================================
    def _make_play_cell(self, track):
        """Cellule « ÉCOUTE » : bouton lecture/pause de l'extrait Deezer."""
        container = QWidget()
        lay = QHBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn = QPushButton("▶")
        btn.setFixedSize(34, 34)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setStyleSheet(
            "QPushButton { background-color: rgba(0, 229, 255, 0.15); color: #00e5ff; "
            "border: none; border-radius: 17px; font-size: 13px; font-weight: bold; }"
            "QPushButton:hover { background-color: #00e5ff; color: #00363d; }"
            "QPushButton:disabled { background-color: rgba(255, 255, 255, 0.05); color: #5f7d84; }"
        )
        if not player.available():
            btn.setEnabled(False)
            btn.setToolTip(player.unavailable_reason())
        elif not track.get("preview"):
            btn.setEnabled(False)
            btn.setToolTip("Aucun extrait disponible pour ce titre")
        else:
            btn.setToolTip("Écouter un extrait de 30 secondes")
            btn.clicked.connect(lambda checked, t=track, b=btn: self._toggle_preview(t, b))

        lay.addWidget(btn)
        return container

    def _toggle_preview(self, track, btn):
        """Lance l'extrait de la piste, ou l'arrête s'il est déjà en cours.

        L'extrait est joué par le lecteur partagé (``player.play_preview``),
        celui-là même qu'utilise la colonne « ÉCOUTE » de la page Musique :
        aucune fenêtre ne s'ouvre, et la fenêtre du lecteur complet — si elle
        joue un fichier — se met en pause le temps de l'extrait plutôt que de
        se superposer.
        """
        if btn is self._preview_btn:
            self._stop_preview()
            return

        # L'extrait précédent s'arrête d'abord : son rappel de fin remet le
        # bouton de sa ligne au repos, sans toucher à celui qui démarre.
        self._stop_preview()
        self._preview_btn = btn
        btn.setText("⏸")
        if not player.play_preview(track.get("preview") or "",
                                   self._on_preview_stopped):
            self._on_preview_stopped()

    def _stop_preview(self):
        """Arrête l'extrait en cours (le rappel de fin rend le bouton au repos)."""
        player.stop_preview()

    def _on_preview_stopped(self):
        """Extrait terminé (fin, erreur, arrêt) : bouton de la ligne au repos."""
        btn, self._preview_btn = self._preview_btn, None
        if btn is not None:
            try:
                btn.setText("▶")
            except RuntimeError:
                pass  # la ligne (et son bouton) a été supprimée entre-temps

    def done(self, result):
        """Fermeture du dialogue (Ajouter / Annuler / croix) : coupe l'extrait."""
        self._stop_preview()
        # Le dialogue a un parent : son objet survit à la fermeture. Sans cette
        # coupure, chaque reconstruction de l'index viendrait repeindre les
        # pastilles d'une fenêtre que plus personne ne regarde.
        parent = self.parent()
        if parent is not None and hasattr(parent, "libraryIndexChanged"):
            try:
                parent.libraryIndexChanged.disconnect(self.set_library_index)
            except TypeError:
                pass
        super().done(result)

    # ==================================================================
    # Chargement asynchrone des vignettes (loader)
    # ==================================================================
    def _make_cover_label(self, cover_bytes):
        """Crée la cellule vignette : image si disponible, sinon loader."""
        cover_label = QLabel()
        cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover_label.setFixedSize(50, 50)
        if cover_bytes:
            self._apply_pixmap(cover_label, cover_bytes)
        else:
            cover_label.setText("⏳")
            cover_label.setStyleSheet("color: #849396; font-size: 18px; border-radius: 6px; background-color: #1c1e26;")
        return cover_label

    def _apply_pixmap(self, label, data, size=50):
        pix = QPixmap()
        pix.loadFromData(data)
        label.setPixmap(pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        label.setText("")
        label.setStyleSheet("border-radius: 6px;")

    def _start_cover_thread(self, items):
        """Télécharge les vignettes manquantes dans un thread dédié."""
        pending = [
            (row, it["cover_url"])
            for row, it in enumerate(items)
            if it.get("cover_url") and not it.get("cover_bytes")
        ]
        if not pending:
            return
        self.cover_thread = CoverDownloadThread(pending, size="small")
        self.cover_thread.cover_ready.connect(self.on_cover_ready)
        self.cover_thread.start()

    def on_cover_ready(self, row, data):
        # Mémoriser dans le cache du mode courant
        cache = self.entities_cache if self.view_mode == "entities" else self.search_cache
        if 0 <= row < len(cache):
            cache[row]["cover_bytes"] = data
        label = self.results_table.cellWidget(row, self.cover_col)
        if isinstance(label, QLabel):
            self._apply_pixmap(label, data)

    def _show_album_header(self, entity):
        self.album_header.setVisible(True)
        self.header_title.setText(entity.get("name", ""))
        self.header_subtitle.setText(entity.get("subtitle", ""))
        self.header_cover.setText("⏳")
        self.header_cover.setStyleSheet("background-color: #1c1e26; border-radius: 8px; color: #849396; font-size: 24px;")
        url = entity.get("cover_url")
        if not url:
            return
        self.header_cover_thread = CoverDownloadThread([(0, url)], size="big")
        self.header_cover_thread.cover_ready.connect(self._on_header_cover_ready)
        self.header_cover_thread.start()

    def _on_header_cover_ready(self, row, data):
        pix = QPixmap()
        pix.loadFromData(data)
        self.header_cover.setPixmap(pix.scaled(90, 90, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.header_cover.setText("")
        self.header_cover.setStyleSheet("border-radius: 8px;")

    def _stop_cover_threads(self):
        for attr in ("cover_thread", "header_cover_thread"):
            thread = getattr(self, attr, None)
            if thread and thread.isRunning():
                thread.stop()
                thread.wait()

    # ==================================================================
    # Tri des résultats (clic sur les en-têtes de colonnes)
    # ==================================================================
    @staticmethod
    def _duration_seconds(text):
        """Convertit une durée « MM:SS » (ou « H:MM:SS ») en secondes."""
        try:
            return sum(
                int(p) * 60 ** i
                for i, p in enumerate(reversed(str(text).strip().split(":")))
            )
        except ValueError:
            return 0

    def _on_header_clicked(self, col):
        """Trie le cache de la vue courante puis re-rend le tableau."""
        if self.view_mode == "tracks":
            keys = {2: "title", 3: "artist", 4: "album", 5: "duration", 6: "rank"}
        else:
            keys = {1: "name", 2: "subtitle"}
        if col not in keys:
            return
        key = keys[col]

        prev_col, prev_order = self._sort_state
        # Premier clic sur la popularité : les plus populaires en tête.
        default_order = (
            Qt.SortOrder.DescendingOrder if key == "rank"
            else Qt.SortOrder.AscendingOrder
        )
        if col == prev_col:
            order = (
                Qt.SortOrder.DescendingOrder
                if prev_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            order = default_order
        self._sort_state = (col, order)
        reverse = order == Qt.SortOrder.DescendingOrder

        # Les threads de vignettes ciblent les lignes par index : on les
        # arrête avant de réordonner, puis on relance sur le nouvel ordre.
        self._stop_cover_threads()

        if self.view_mode == "tracks":
            checked_ids = {t["id"] for t in self.get_selected_tracks()}
            if key == "duration":
                sort_key = lambda t: self._duration_seconds(t.get("duration", ""))
            elif key == "rank":
                sort_key = lambda t: t.get("rank") or 0
            else:
                sort_key = lambda t: (t.get(key) or "").lower()
            self.search_cache.sort(key=sort_key, reverse=reverse)
            self._render_tracks(self.search_cache)
            self._start_cover_thread(self.search_cache)
            # Restaurer les cases cochées et le filtre courant.
            for row, track in enumerate(self.search_cache):
                if track["id"] in checked_ids:
                    widget = self.results_table.cellWidget(row, 0)
                    cb = widget.findChild(QCheckBox) if widget else None
                    if cb:
                        cb.setChecked(True)
            self.filter_results(self.input_filter.text())
        else:
            self.entities_cache.sort(
                key=lambda e: (e.get(key) or "").lower(), reverse=reverse
            )
            self._display_entities(self.entities_cache, self.entities_kind)

        self.results_table.horizontalHeader().setSortIndicator(col, order)

    # ==================================================================
    # Sélection / filtres / erreurs
    # ==================================================================
    def on_search_error(self, error_msg):
        self._reset_search_button()
        QMessageBox.warning(self, "Erreur de connexion", f"Erreur lors de la recherche : {error_msg}")

    def on_checkbox_changed(self, state):
        selected_count = len(self.get_selected_tracks())
        if selected_count > 0:
            self.btn_add.setEnabled(True)
            self.btn_add.setText(f"Ajouter la sélection ({selected_count})")
        else:
            self.btn_add.setEnabled(False)
            self.btn_add.setText("Ajouter la sélection")

    def cancel_or_reject(self):
        if self.search_thread and self.search_thread.isRunning():
            self.search_thread.terminate()
            self.search_thread.wait()
            self._reset_search_button()
        else:
            self._stop_cover_threads()
            self.reject()

    def filter_results(self, text):
        """N'affiche que les lignes retenues par les filtres en vigueur.

        Deux filtres se cumulent : le texte saisi et, quand le bouton du bilan
        est enfoncé, la seule appartenance aux doublons.
        """
        if self.view_mode != "tracks":
            return
        needle = (text or "").lower()
        for row in range(self.results_table.rowCount()):
            title_item = self.results_table.item(row, 2)
            if title_item is None:
                continue
            visible = needle in title_item.text().lower()
            if visible and self._dups_only:
                visible = self._is_duplicate(row)
            self.results_table.setRowHidden(row, not visible)
        self.on_checkbox_changed(0)

    def _toggle_dups_only(self, checked):
        """Bascule l'affichage « doublons seuls » (bouton du bilan)."""
        self._dups_only = bool(checked)
        self.filter_results(self.input_filter.text())

    def toggle_select_all_search(self, state):
        """Coche (ou décoche) les lignes visibles, les doublons exceptés.

        Tout cocher sur un album de cinquante titres ne doit pas remettre en
        file ce que la bibliothèque contient déjà : les titres présents et les
        doublons possibles restent à l'écart, et le bilan affiché à droite du
        filtre dit combien de lignes ont été laissées de côté. Chacune reste
        cochable à la main — sauf les vertes, dont la case est désactivée.
        """
        if self.view_mode != "tracks":
            return
        check_state = Qt.CheckState.Checked if state == 2 else Qt.CheckState.Unchecked
        checking = check_state == Qt.CheckState.Checked
        for row in range(self.results_table.rowCount()):
            if not self.results_table.isRowHidden(row):
                widget = self.results_table.cellWidget(row, 0)
                if widget:
                    cb = widget.findChild(QCheckBox)
                    if cb and not (checking and self._is_duplicate(row)):
                        cb.setCheckState(check_state)
        self.on_checkbox_changed(0)

    def _is_duplicate(self, row):
        """La ligne ``row`` désigne-t-elle un titre déjà sur le disque ?"""
        if row >= len(self._library_states):
            return False
        return self._library_states[row].state in (
            library_index.STATE_PRESENT, library_index.STATE_SIMILAR
        )

    # ==================================================================
    # Déduplication (fichiers du disque rapprochés des pistes affichées)
    # ==================================================================
    def _duplicate_candidates(self):
        """Pistes affichées auxquelles répond au moins un fichier du disque.

        Une pastille verte ou orange sans chemin connu — cela arrive quand le
        rapprochement vient d'une source dépourvue de fichier — n'a rien à
        proposer à l'examen : elle est écartée.
        """
        candidates = []
        for row, match in enumerate(self._library_states):
            if row >= len(self.search_cache):
                break
            if match.state not in (library_index.STATE_PRESENT,
                                   library_index.STATE_SIMILAR):
                continue
            if not match.paths:
                continue
            candidates.append({"track": self.search_cache[row], "match": match})
        return candidates

    def review_duplicates(self):
        """Ouvre l'écran des doublons et supprime les fichiers retenus."""
        candidates = self._duplicate_candidates()
        if not candidates:
            QMessageBox.information(
                self, "Doublons",
                "Aucun fichier du dossier de dépôt n'est rapproché des titres "
                "affichés.")
            return
        self._stop_preview()
        dialog = dedup_dialog.LibraryDuplicateDialog(candidates, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._delete_library_files(dialog.get_files_to_delete())

    def _delete_library_files(self, paths):
        """Supprime les fichiers choisis, puis rafraîchit les pastilles.

        L'index n'est pas reconstruit : les chemins supprimés en sont retirés
        un à un (``LibraryIndex.remove``), ce qui suffit à faire repasser au
        gris les pistes qui n'ont plus de correspondance — et donc à les rendre
        extractibles sans quitter la recherche. La fenêtre principale, quand
        elle est là, en fait autant pour ses propres pastilles.
        """
        if not paths:
            return
        deleted, errors = 0, []
        for path in paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
                deleted += 1
            except OSError as exc:
                errors.append(f"{os.path.basename(path)} : {exc}")

        gone = [p for p in paths if not os.path.exists(p)]
        reencode_scan.clear_tag_cache()
        if self.library_index is not None:
            for path in gone:
                self.library_index.remove(path)
        # La fenêtre principale tient le même index : la prévenir met à jour
        # ses propres pastilles (oublier un chemin deux fois ne coûte rien).
        parent = self.parent()
        if parent is not None and hasattr(parent, "on_library_files_deleted"):
            parent.on_library_files_deleted(gone)
        self._apply_library_states()

        message = f"{deleted} fichier(s) supprimé(s)."
        if errors:
            message += "\n\nErreurs :\n" + "\n".join(errors[:10])
        QMessageBox.information(self, "Déduplication", message)

    def get_selected_tracks(self):
        if self.view_mode != "tracks":
            return []
        selected = []
        for row in range(self.results_table.rowCount()):
            widget = self.results_table.cellWidget(row, 0)
            if widget:
                cb = widget.findChild(QCheckBox)
                if cb and cb.isChecked():
                    selected.append(self.search_cache[row])
        return selected
