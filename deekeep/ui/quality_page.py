"""
Écran « Qualité » : titres à la qualité sonore médiocre.

Un titre mal extrait ne se reconnaît ni à son format ni à son débit : c'est
le signal qu'il faut écouter. Cet écran mesure celui de chaque fichier de la
bibliothèque (voir ``quality_scan``), ne retient que ceux qui présentent un
défaut — souffle, craquements, saturation, plage muette au début ou à la fin
— et propose de les **retélécharger** pour remplacer sur place la version
abîmée.

Le retéléchargement emprunte exactement le chemin du réencodage : une
correspondance Deezer est résolue à partir des tags locaux, l'extraction se
fait via ``TrackDownloaderThread`` avec ``original_path``, et le fichier
produit prend la place de l'ancien — rangé selon les règles de l'écran
« Organiser » (``library_rules``), et non forcément dans le dossier où
traînait la version abîmée. Chaque titre remplacé est ensuite **réanalysé** :
la ligne dit alors si l'opération a réellement corrigé le défaut, ce
qu'aucun message de téléchargement ne pourrait affirmer à sa place.

L'analyse tourne dans un thread dédié, comme celle de « Organiser », et ses
résultats s'exploitent **sans attendre sa fin** : les lignes arrivent par
lots, se cochent et se retéléchargent pendant qu'elle continue. Une
bibliothèque entière demande plusieurs dizaines de minutes en analyse
approfondie ; faire patienter jusque-là pour agir sur un titre signalé dès
la dixième seconde n'avait pas de sens. « Arrêter » l'interrompt sur-le-champ
sans rien perdre de ce qui est déjà mesuré (voir ``_refresh_controls``).
"""

import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QProgressBar, QFileDialog, QMessageBox, QMenu, QDoubleSpinBox,
    QToolButton, QGridLayout,
)
from PyQt6.QtCore import Qt, QElapsedTimer, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices

from deekeep.core import config
from deekeep.core.utils import reveal_in_explorer
from deekeep.library import library_rules
from deekeep.ui import player
from deekeep.audio import quality_scan
from deekeep.workers.download_thread import TrackDownloaderThread
from deekeep.audio.quality_scan import (
    DEFECT_LABELS, DEFECT_ORDER, DEEP_DEFECTS, MeasureCache,
    QualityScanThread, ReferenceLookupThread, SingleAnalysisThread,
)
from deekeep.audio.reference_duration import ReferenceCache

# Colonnes du tableau (une constante par colonne : l'ajout d'une colonne ne
# décale plus les indices écrits en dur).
(QC_CHECK, QC_PLAY, QC_SCORE, QC_TITLE, QC_ARTIST, QC_DEFECTS, QC_HISS,
 QC_CRACKLE, QC_CLIPPING, QC_GAPS, QC_DURATION, QC_SILENCE, QC_FORMAT,
 QC_PATH, QC_STATUS) = range(15)

COLUMN_LABELS = [
    "", "ÉCOUTE", "NOTE", "TITRE", "ARTISTE", "DÉFAUTS", "SOUFFLE",
    "CRAQUEMENTS", "SATURATION", "COUPURES", "DURÉE", "BLANCS",
    "FORMAT / DÉBIT", "FICHIER", "STATUT",
]

# Couleurs d'état (palette Neon Nocturne, comme les autres écrans).
COLOR_BAD = QColor("#ff8a80")
COLOR_WARN = QColor("#ffcf7a")
COLOR_OK = QColor("#5eeb7f")
COLOR_MUTED = QColor("#849396")

# Au-delà de cette note, le titre est tenu pour sain.
SCORE_GOOD = 100
# En deçà, la ligne est signalée en rouge plutôt qu'en orange.
SCORE_SEVERE = 75


class QualityPage(QWidget):
    """Analyse la qualité sonore d'une bibliothèque et propose de la corriger."""

    # Nombre de retéléchargements menés de front (même ordre de grandeur que
    # la page Musique : au-delà, YouTube ralentit toutes les extractions).
    MAX_CONCURRENT = 5

    def __init__(self, root, app_settings=None, save_callback=None,
                 export_callback=None, parent=None):
        super().__init__(parent)
        self.root = root
        self._settings = app_settings if app_settings is not None else {}
        self._save_callback = save_callback
        # Rend les réglages d'exportation courants (format, qualité, cookies) :
        # ils vivent dans la fenêtre principale et peuvent changer entre deux
        # retéléchargements.
        self._export_callback = export_callback

        self._cache = MeasureCache()
        self._scan_thread = None
        self._single_threads = []

        # Durées de référence : cache disque, thread de recherche en cours et
        # file des titres qui l'attendent. Seuls les fichiers portant un blanc
        # y entrent — les autres n'ont aucun verdict qui en dépende.
        self._ref_cache = ReferenceCache()
        self._ref_thread = None
        self._ref_queue = []
        self._ref_asked = set()     # identifiants déjà confiés à une recherche
        self._ref_done = 0

        self._entries = []          # toutes les lignes analysées
        self._rows = []             # lignes réellement affichées (après filtre)
        self._known = set()         # chemins déjà adoptés (anti-doublon)
        self._uid = 0
        self._by_uid = {}
        self._scan_total = 0
        self._scanning = False
        self._scan_complete = True
        # Une bibliothèque entière demande plusieurs dizaines de minutes en
        # analyse approfondie : la durée restante est annoncée plutôt que
        # laissée à deviner.
        self._scan_clock = QElapsedTimer()
        self._rendering = False     # garde anti-réentrance du rendu

        # Retéléchargements : workers actifs et file d'attente, indexés par uid
        # (et non par ligne : le tri déplace les lignes en cours de route).
        self._workers = {}
        self._queue = []
        self._batch = []
        # Décompte des titres par artiste du lot de retéléchargement
        # (``library_rules.Tally``), partagé par les extractions parallèles.
        # Aucune réservation ici : un titre réextrait remplace un fichier
        # déjà présent, que le disque compte déjà pour son artiste.
        self._tally = None

        self._sort = (QC_SCORE, Qt.SortOrder.AscendingOrder)
        self._show_healthy = False
        self._defect_filters = {}

        # Rendu différé : les durées de référence arrivent par dizaines
        # pendant plusieurs minutes, et reconstruire le tableau à chacune
        # coûterait bien plus que la recherche elle-même.
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(500)
        self._render_timer.timeout.connect(self._render)

        self._build_ui()
        self.sync_from_settings()
        self._refresh_controls()

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 24)
        layout.setSpacing(14)

        title = QLabel("Qualité des titres")
        title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 26px; "
            "font-weight: bold; color: #00e5ff;"
        )
        layout.addWidget(title)

        sub = QLabel(
            "Écoute le signal de chaque fichier — souffle continu, "
            "craquements, saturation, coupures au milieu du morceau — et "
            "compare sa durée à celle du morceau chez Deezer ou MusicBrainz : "
            "un fichier plus court ou plus long n'est pas la version attendue, "
            "et c'est le seul indice qu'un rip radio propre laisse derrière "
            "lui. Les résultats s'exploitent sans attendre la fin de "
            "l'analyse : cochez et retéléchargez au fil de l'eau. Les titres "
            "réextraits remplacent la version défectueuse — rangés selon les "
            "règles de l'écran « Organiser » — puis sont réanalysés pour "
            "vérifier que le défaut a bien disparu."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #849396; font-size: 12px;")
        layout.addWidget(sub)

        # --- Dossier analysé ---
        dir_layout = QHBoxLayout()
        self.lbl_dir = QLabel()
        self.lbl_dir.setStyleSheet("color: #bac9cc; font-size: 12px;")
        self.btn_options = QToolButton()
        self.btn_options.setCheckable(True)
        self.btn_options.setToolTip("Afficher ou masquer les seuils de détection")
        self.btn_options.toggled.connect(self._on_options_toggled)
        btn_dir = QPushButton("Modifier le dossier")
        btn_dir.setProperty("class", "control-btn")
        btn_dir.clicked.connect(self._choose_dir)
        dir_layout.addWidget(self.lbl_dir, 1)
        dir_layout.addWidget(self.btn_options)
        dir_layout.addWidget(btn_dir)
        layout.addLayout(dir_layout)
        self._refresh_dir_label()

        layout.addWidget(self._build_options_panel())

        # --- Barre d'actions ---
        # Même disposition sur tous les écrans (voir ``styles.py``) : la barre
        # se lit au-dessus de la liste, la manipulation de l'affichage à
        # gauche, les actions à droite — l'analyse en pilule ajourée, puis
        # l'action qui écrit sur le disque en pilule pleine.
        action_layout = QHBoxLayout()
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(12)

        self.btn_all = QPushButton("Tout cocher")
        self.btn_all.setProperty("class", "control-btn")
        self.btn_all.clicked.connect(lambda: self._set_all_checked(True))

        self.btn_none = QPushButton("Tout décocher")
        self.btn_none.setProperty("class", "control-btn")
        self.btn_none.clicked.connect(lambda: self._set_all_checked(False))

        self.btn_clear = QPushButton("Vider la liste")
        self.btn_clear.setProperty("class", "control-btn")
        self.btn_clear.setToolTip(
            "Retirer tous les résultats affichés (les fichiers du disque "
            "sont conservés)"
        )
        self.btn_clear.clicked.connect(self._clear_results)

        self.btn_stop = QPushButton("Arrêter")
        self.btn_stop.setProperty("class", "control-btn")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_scan)

        self.btn_scan = QPushButton("Analyser")
        self.btn_scan.setProperty("class", "accent-btn")
        self.btn_scan.setToolTip(
            "Mesurer la qualité sonore de tous les fichiers du dossier"
        )
        self.btn_scan.clicked.connect(self._start_scan)

        self.btn_download = QPushButton("Retélécharger la sélection")
        self.btn_download.setProperty("class", "primary-btn")
        self.btn_download.setToolTip(
            "Réextraire les titres cochés et remplacer le fichier abîmé "
            "dans son dossier d'origine"
        )
        self.btn_download.clicked.connect(self._redownload)

        action_layout.addWidget(self.btn_all)
        action_layout.addWidget(self.btn_none)
        action_layout.addWidget(self.btn_clear)
        action_layout.addStretch()
        action_layout.addWidget(self.btn_stop)
        action_layout.addWidget(self.btn_scan)
        action_layout.addWidget(self.btn_download)
        layout.addLayout(action_layout)

        # --- Progression ---
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setStyleSheet("height: 8px; border: none; background: #1c1e26;")
        layout.addWidget(self.progress)

        self.download_bar = QProgressBar()
        self.download_bar.setObjectName("global_progress_bar")
        self.download_bar.setVisible(False)
        self.download_bar.setValue(0)
        layout.addWidget(self.download_bar)

        # --- Filtres d'affichage ---
        filter_layout = QHBoxLayout()
        filter_layout.setSpacing(14)
        filter_layout.addWidget(QLabel("Afficher :"))
        for key in DEFECT_ORDER:
            box = QCheckBox(DEFECT_LABELS[key])
            box.setChecked(True)
            box.stateChanged.connect(self._render)
            self._defect_filters[key] = box
            filter_layout.addWidget(box)
        self.cb_healthy = QCheckBox("Titres sans défaut")
        self.cb_healthy.setToolTip(
            "Afficher aussi les titres qu'aucune mesure ne met en cause"
        )
        self.cb_healthy.stateChanged.connect(self._render)
        filter_layout.addWidget(self.cb_healthy)
        filter_layout.addStretch()
        layout.addLayout(filter_layout)

        # --- Tableau ---
        self.table = QTableWidget()
        self.table.setColumnCount(len(COLUMN_LABELS))
        self.table.setHorizontalHeaderLabels(COLUMN_LABELS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemChanged.connect(self._on_item_changed)
        # Le double-clic ouvre le lecteur : sur cet écran, écouter est
        # l'action attendue par défaut — c'est elle qui tranche. Un simple
        # clic suffit dans la colonne « ÉCOUTE ».
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.cellClicked.connect(self._on_cell_clicked)
        # Les lignes portent un bouton de lecture : hauteur fixée pour que
        # toutes fassent la même, avec ou sans bouton.
        self.table.verticalHeader().setDefaultSectionSize(36)

        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_clicked)
        for column in range(len(COLUMN_LABELS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(QC_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(QC_PLAY, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(QC_PATH, QHeaderView.ResizeMode.Stretch)
        widths = {QC_PLAY: 66,
                  QC_SCORE: 60, QC_TITLE: 190, QC_ARTIST: 150, QC_DEFECTS: 190,
                  QC_HISS: 80, QC_CRACKLE: 100, QC_CLIPPING: 90, QC_GAPS: 90,
                  QC_DURATION: 120, QC_SILENCE: 130, QC_FORMAT: 120,
                  QC_STATUS: 190}
        for column, width in widths.items():
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)

        self.lbl_status = QLabel(
            "Lancez l'analyse pour repérer les titres à la qualité médiocre."
        )
        self.lbl_status.setStyleSheet("color: #5f7d84; font-size: 12px;")
        layout.addWidget(self.lbl_status)

    def _build_options_panel(self):
        """Volet repliable des seuils de détection."""
        self.options_panel = QWidget()
        self.options_panel.setObjectName("qualityPanel")
        self.options_panel.setStyleSheet(
            "QWidget#qualityPanel { background-color: #1c1e26; "
            "border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 12px; }"
        )
        panel = QVBoxLayout(self.options_panel)
        panel.setContentsMargins(20, 16, 20, 16)
        panel.setSpacing(12)

        self.cb_deep = QCheckBox("Détecter les craquements (analyse ~4× plus lente)")
        self.cb_deep.setToolTip(
            "Les craquements se repèrent en prédisant chaque échantillon à\n"
            "partir des précédents : c'est la mesure la plus sûre, et de loin\n"
            "la plus coûteuse. Décochez-la pour une analyse rapide limitée au\n"
            "souffle, à la saturation et aux blancs."
        )
        self.cb_deep.stateChanged.connect(self._on_deep_changed)
        panel.addWidget(self.cb_deep)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(10)

        # (attribut, libellé, min, max, pas, décimales, suffixe, infobulle)
        specs = (
            ("hiss_db", "Souffle au-dessus de", -90.0, -30.0, 1.0, 0, " dB",
             "Plancher de bruit mesuré au-dessus de 8 kHz. Un titre sain\n"
             "reste sous −80 dB ; un souffle audible remonte ce plancher\n"
             "à son propre niveau.\n"
             "Un morceau que rien n'interrompt n'a pas de plancher à\n"
             "mesurer : il n'est signalé que si le bruit relevé est bien\n"
             "large bande, ce que la colonne « Souffle » explique au survol."),
            ("crackle_pct", "Craquements au-delà de", 0.0005, 1.0, 0.0005, 4, " %",
             "Part d'échantillons imprévisibles. Titres sains : moins de\n"
             "0,001 % ; deux craquements par seconde : 0,004 %."),
            ("clipping_pct", "Saturation au-delà de", 0.005, 5.0, 0.005, 3, " %",
             "Part d'échantillons collés à la butée du format. Une variété\n"
             "récente en montre couramment 0,03 à 0,08 % : c'est le master\n"
             "qui est ainsi, retélécharger n'y changerait rien. Au-delà de\n"
             "0,2 %, la distorsion s'entend."),
            ("gap_density_pct", "Coupures occupant plus de", 5.0, 90.0, 5.0, 0, " %",
             "Part de silence numérique — des échantillons nuls, non du signal\n"
             "faible — du passage de dix secondes le plus troué.\n"
             "Les compter ne suffirait pas : « We Will Rock You » montre\n"
             "soixante-neuf silences, ceux qui séparent ses frappes de pied,\n"
             "et se porte très bien. Ce qui trahit une extraction qui hoquette,\n"
             "c'est qu'elle blanchit une large part d'un passage entier :\n"
             "51 % là où un fichier sain ne dépasse pas 19 %, et reste le plus\n"
             "souvent sous 5 %.\n"
             "Les blancs de début et de fin sont exclus : ils ont leur propre\n"
             "seuil."),
            ("tail_seconds", "Blanc de fin au-delà de", 1.0, 60.0, 1.0, 0, " s",
             "Durée de la plage muette terminant le fichier. Une fin en\n"
             "fondu en occupe deux ou trois.\n"
             "Un blanc n'est signalé que si le fichier dépasse en outre la\n"
             "durée du morceau (voir le seuil ci-dessous)."),
            ("head_seconds", "Blanc de début au-delà de", 0.5, 30.0, 0.5, 1, " s",
             "Durée de la plage muette ouvrant le fichier.\n"
             "Un blanc n'est signalé que si le fichier dépasse en outre la\n"
             "durée du morceau (voir le seuil ci-dessous)."),
            ("duration_gap", "Durée s'écartant de la référence de plus de",
             2.0, 120.0, 1.0, 0, " s",
             "Écart, dans un sens ou dans l'autre, entre la durée du fichier\n"
             "et celle du morceau chez Deezer ou MusicBrainz.\n"
             "Un fichier plus court ou plus long n'est pas la version\n"
             "attendue : montage écourté, édition radio, extraction d'une\n"
             "mauvaise source. C'est souvent le seul indice exploitable — un\n"
             "rip radio propre se mesure comme un master propre.\n"
             "Sur soixante-dix titres tirés au hasard, l'écart médian est de\n"
             "1,6 s et les trois quarts tiennent sous 3,4 s ; au-delà de dix\n"
             "secondes commence une autre famille."),
            ("duration_excess", "Blancs signalés si la durée dépasse de",
             1.0, 60.0, 1.0, 0, " s",
             "Un silence n'est un défaut que s'il a été ajouté : beaucoup de\n"
             "morceaux se terminent sur un fondu ou des applaudissements que\n"
             "leur master contient bel et bien.\n"
             "La durée du morceau est cherchée chez Deezer, puis chez\n"
             "MusicBrainz, pour les seuls fichiers portant un blanc — et la\n"
             "plus proche de celle du fichier est retenue, pour ne pas\n"
             "accuser un titre de ne pas durer autant qu'un remix.\n"
             "Sur des fichiers sains, l'écart mesuré ne dépasse pas 3 s.\n"
             "Sans durée de référence, aucun blanc n'est signalé."),
        )
        self.spins = {}
        for index, (key, label, low, high, step, decimals, suffix, tip) in enumerate(specs):
            spin = QDoubleSpinBox()
            spin.setRange(low, high)
            spin.setSingleStep(step)
            spin.setDecimals(decimals)
            spin.setSuffix(suffix)
            spin.setToolTip(tip)
            spin.valueChanged.connect(self._on_threshold_changed)
            caption = QLabel(label)
            caption.setToolTip(tip)
            row, column = divmod(index, 2)
            grid.addWidget(caption, row, column * 2)
            grid.addWidget(spin, row, column * 2 + 1)
            self.spins[key] = spin
        panel.addLayout(grid)

        hint = QLabel(
            "Modifier un seuil réévalue aussitôt l'analyse déjà faite : "
            "aucun fichier n'est relu."
        )
        hint.setStyleSheet("color: #5f7d84; font-size: 11px;")
        panel.addWidget(hint)

        return self.options_panel

    def _on_options_toggled(self, opened):
        self.options_panel.setVisible(opened)
        self.btn_options.setText("▾ Seuils" if opened else "▸ Seuils")

    # ------------------------------------------------------------------
    # Paramètres persistants
    # ------------------------------------------------------------------
    def sync_from_settings(self):
        """Relit les seuils enregistrés (appelé à chaque affichage de la page)."""
        self._rendering = True
        try:
            self.cb_deep.setChecked(bool(self._settings.get("qual_deep", True)))
            for key, spin in self.spins.items():
                spin.setValue(float(self._settings.get(
                    "qual_" + key, quality_scan.DEFAULT_THRESHOLDS[key])))
        finally:
            self._rendering = False
        self._on_options_toggled(self.btn_options.isChecked())
        # Un seuil modifié ailleurs (rechargement de la configuration) doit
        # se voir sur une analyse déjà affichée.
        if self._entries:
            self._reevaluate()

    def _store_settings(self):
        self._settings["qual_deep"] = self.cb_deep.isChecked()
        for key, spin in self.spins.items():
            self._settings["qual_" + key] = spin.value()
        if self._save_callback:
            self._save_callback()

    def thresholds(self):
        """Seuils courants, sous la forme attendue par ``quality_scan``."""
        return {key: spin.value() for key, spin in self.spins.items()}

    def _on_threshold_changed(self, *_args):
        if self._rendering:
            return
        self._store_settings()
        self._reevaluate()

    def _on_deep_changed(self, *_args):
        if self._rendering:
            return
        self._store_settings()
        # Les craquements ne sont mesurés que par l'analyse approfondie :
        # les masquer plutôt que laisser croire qu'aucun n'a été trouvé.
        self._render()

    # ------------------------------------------------------------------
    # Dossier
    # ------------------------------------------------------------------
    def _refresh_dir_label(self):
        self.lbl_dir.setText(f"Dossier analysé : {self.root}")
        self.lbl_dir.setToolTip(self.root)

    def set_root(self, root):
        """Change le dossier ciblé (suit le dossier d'export par défaut)."""
        if os.path.normcase(os.path.abspath(root)) == os.path.normcase(
                os.path.abspath(self.root)):
            return
        self.root = root
        self._refresh_dir_label()
        self._clear("Dossier modifié : relancez l'analyse.")

    def _choose_dir(self):
        if self._busy():
            return
        chosen = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier à analyser", self.root)
        if chosen:
            self.set_root(chosen)

    # ------------------------------------------------------------------
    # Analyse
    # ------------------------------------------------------------------
    def _busy(self):
        return self._scanning or bool(self._workers) or bool(self._queue)

    def _clear(self, message):
        self._render_timer.stop()
        self._stop_reference_lookup()
        self._entries = []
        self._rows = []
        self._known = set()
        self._by_uid = {}
        self._ref_queue = []
        self._ref_asked = set()
        self._ref_done = 0
        self.table.setRowCount(0)
        self.lbl_status.setText(message)

    def _clear_results(self):
        """Vide l'écran de tous ses résultats, sur demande explicite.

        Refusé pendant une analyse ou un retéléchargement : les workers en
        cours rendraient aussitôt des lignes dans un tableau censé être vide.
        Une analyse de bibliothèque coûte des dizaines de minutes, d'où la
        confirmation avant de la jeter.
        """
        if self._busy():
            QMessageBox.information(
                self, "Vidage indisponible",
                "Attendez la fin de l'analyse et des retéléchargements en "
                "cours avant de vider la liste.")
            return
        if not self._entries:
            return

        reply = QMessageBox.question(
            self, "Vider la liste",
            f"Retirer les {len(self._entries)} résultat(s) d'analyse ?\n"
            "Les fichiers du disque sont conservés ; il faudra relancer "
            "l'analyse pour les revoir.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._clear("Écran vidé : relancez l'analyse.")
        self._scan_total = 0
        self.progress.setVisible(False)
        self.download_bar.setVisible(False)
        self.download_bar.setValue(0)
        self._refresh_controls()

    def _start_scan(self):
        if self._busy():
            return
        if not os.path.isdir(self.root):
            QMessageBox.warning(
                self, "Dossier introuvable",
                f"Le dossier suivant n'existe pas :\n{self.root}")
            return
        from deekeep.audio.audio_probe import ffmpeg_exe
        if ffmpeg_exe() is None:
            QMessageBox.warning(
                self, "FFmpeg indisponible",
                "L'analyse du signal repose sur FFmpeg, qui est introuvable.\n"
                "Consultez la page Diagnostic pour connaître son état.")
            return

        deep = self.cb_deep.isChecked()
        self._clear("Analyse en cours…")
        self._scan_clock.start()
        self._scanning = True
        self._refresh_controls()
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)   # indéterminé tant que le total est inconnu

        self._scan_thread = QualityScanThread([self.root], deep=deep, cache=self._cache)
        self._scan_thread.total.connect(self._on_scan_total)
        self._scan_thread.progress.connect(self._on_scan_progress)
        self._scan_thread.batch.connect(self._on_scan_batch)
        self._scan_thread.finished_scan.connect(self._on_scan_finished)
        self._scan_thread.start()

    def _stop_scan(self):
        """Interrompt l'analyse en gardant tout ce qu'elle a déjà mesuré.

        Les lectures ffmpeg en cours sont tuées plutôt qu'attendues : l'arrêt
        est immédiat, et les fichiers déjà lus restent dans le tableau, prêts
        à être retéléchargés.
        """
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self.lbl_status.setText("Arrêt de l'analyse…")
            self.btn_stop.setEnabled(False)
            self._scan_thread.stop()

    def _on_scan_total(self, total):
        self._scan_total = total
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(0)
        self.lbl_status.setText(f"Analyse en cours… {total} fichier(s) à lire")

    def _on_scan_progress(self, count):
        self.progress.setValue(count)
        flagged = sum(1 for e in self._entries if e["defects"])
        text = (f"Analyse en cours… {count} / {self._scan_total} fichiers "
                f"({flagged} signalés)")
        remaining = self._remaining_text(count)
        if remaining:
            text += f" — {remaining}"
        self.lbl_status.setText(text)

    def _remaining_text(self, done):
        """« environ 12 min restantes », d'après la cadence observée."""
        if done < 5 or not self._scan_total or not self._scan_clock.isValid():
            return ""
        elapsed = self._scan_clock.elapsed() / 1000.0
        if elapsed <= 0:
            return ""
        seconds = (self._scan_total - done) * elapsed / done
        if seconds < 45:
            return "moins d'une minute restante"
        minutes = int(round(seconds / 60.0))
        return "environ %d min restante%s" % (minutes, "s" if minutes > 1 else "")

    def _ingest(self, entries):
        """Adopte des entrées analysées ; rend celles qui méritent une ligne."""
        limits = self.thresholds()
        fresh = []
        for entry in entries:
            key = os.path.normcase(os.path.abspath(entry["path"]))
            if key in self._known:
                continue
            self._known.add(key)
            self._uid += 1
            entry["_uid"] = self._uid
            entry["selected"] = False
            entry.setdefault("reference", "?")   # « ? » : pas encore cherchée
            entry["defects"], entry["severities"], entry["score"] = \
                quality_scan.evaluate(entry["measures"], limits,
                                      self._reference_of(entry))
            self._by_uid[self._uid] = entry
            self._entries.append(entry)
            self._queue_reference(entry)
            if self._keep(entry):
                fresh.append(entry)
        self._pump_reference()
        return fresh

    # ------------------------------------------------------------------
    # Durée de référence (Deezer, puis MusicBrainz)
    # ------------------------------------------------------------------
    @staticmethod
    def _reference_of(entry):
        """Durée de référence exploitable de l'entrée, ou None.

        « ? » marque une recherche pas encore faite : elle vaut None pour
        l'évaluation — tant que la référence est inconnue, aucun blanc n'est
        mis en cause — mais se distingue d'une recherche restée sans réponse,
        que la colonne « Blancs » signale.
        """
        reference = entry.get("reference")
        return reference if isinstance(reference, (int, float)) else None

    def _needs_reference(self, entry):
        """Vrai si le verdict de cette entrée dépend d'une référence à chercher.

        Tout fichier dont les tags nomment un artiste et un titre est
        concerné : sa durée est comparée à celle du morceau, et un fichier
        sans le moindre blanc peut parfaitement être la mauvaise version.
        Un fichier illisible ou sans tags, lui, n'a rien à comparer.
        """
        if entry.get("reference") != "?" or entry["measures"].get("error"):
            return False
        if not entry["measures"].get("duration"):
            return False
        return bool((entry.get("artist") or "").strip()
                    and (entry.get("title") or "").strip())

    def _queue_reference(self, entry):
        if entry["_uid"] in self._ref_asked or not self._needs_reference(entry):
            return
        self._ref_asked.add(entry["_uid"])
        self._ref_queue.append((entry["_uid"], entry["artist"], entry["title"],
                                entry["measures"].get("duration") or 0.0))

    def _pump_reference(self):
        """Confie les titres en attente au thread de recherche, démarré au besoin."""
        if not self._ref_queue:
            return
        if self._ref_thread is None or not self._ref_thread.isRunning():
            self._ref_thread = ReferenceLookupThread(cache=self._ref_cache, parent=self)
            self._ref_thread.found.connect(self._on_reference_found)
            self._ref_thread.start()
        self._ref_thread.submit(self._ref_queue)
        self._ref_queue = []

    def _on_reference_found(self, results):
        limits = self.thresholds()
        touched, reshuffle = [], False
        for uid, reference in results:
            entry = self._by_uid.get(uid)
            if entry is None:
                continue
            was_kept = self._keep(entry)
            entry["reference"] = reference      # None = aucune trouvée
            entry["defects"], entry["severities"], entry["score"] = \
                quality_scan.evaluate(entry["measures"], limits, reference)
            # Une référence peut faire apparaître ou disparaître une ligne :
            # seul ce cas oblige à reconstruire le tableau. Rebâtir plusieurs
            # milliers de lignes à chaque lot de dix réponses coûterait bien
            # plus que la recherche elle-même.
            reshuffle = reshuffle or (was_kept != self._keep(entry))
            touched.append(entry)
            self._ref_done += 1
        if reshuffle:
            # Les réponses arrivent par dizaines pendant plusieurs minutes, et
            # chacune peut faire entrer ou sortir une ligne. Reconstruire le
            # tableau à chaque lot le rebâtirait des centaines de fois sur une
            # bibliothèque entière : le rendu est différé et regroupé.
            self._render_timer.start()
        elif touched:
            self._rendering = True
            try:
                for entry in touched:
                    row = self._row_of(entry)
                    if row >= 0:
                        self._fill_row(row, entry)
            finally:
                self._rendering = False
        self._refresh_summary()

    def _stop_reference_lookup(self):
        """Arrête la recherche de références et enregistre ce qu'elle a trouvé."""
        if self._ref_thread is not None:
            self._ref_thread.stop()
            if self._ref_thread.isRunning():
                # La boucle se réveille toutes les 300 ms ; une requête déjà
                # partie a jusqu'à son délai d'attente pour revenir.
                self._ref_thread.wait(12000)
            self._ref_thread.deleteLater()
        self._ref_thread = None
        self._ref_cache.save()

    def _on_scan_batch(self, entries):
        fresh = self._ingest(entries)
        if fresh:
            self._append_rows(fresh)

    def _on_scan_finished(self, entries, complete):
        # Une analyse arrêtée n'émet pas son dernier lot : ces fichiers ont
        # pourtant bien été mesurés, la liste finale les rattrape.
        self._ingest(entries)
        self._scanning = False
        self._scan_thread = None
        self._scan_complete = complete
        self.progress.setVisible(False)
        self._refresh_controls()
        self._reevaluate()
        self._refresh_summary()

    def _refresh_summary(self):
        """Réécrit le bilan de l'analyse (appelé aussi quand une référence arrive).

        Le décompte des titres douteux change encore après la fin de
        l'analyse : les blancs attendent leur durée de référence, et un bilan
        figé sur l'instant où ffmpeg s'est tu annoncerait deux titres là où il
        y en aura neuf.
        """
        if self._scanning or not self._entries:
            return
        flagged = sum(1 for e in self._entries if e["defects"])
        errors = sum(1 for e in self._entries if e["measures"].get("error"))
        prefix = ("Analyse terminée" if getattr(self, "_scan_complete", True)
                  else "Analyse interrompue")
        parts = [f"{prefix} : {len(self._entries)} fichier(s) lus, "
                 f"{flagged} titre(s) à la qualité douteuse"]
        if errors:
            parts.append(f"{errors} illisible(s)")
        waiting = len(self._ref_asked) - self._ref_done
        if waiting > 0:
            parts.append(f"durée du morceau en cours de recherche pour "
                         f"{waiting} titre(s)")
        if not self.cb_deep.isChecked():
            parts.append("craquements non mesurés (analyse rapide)")
        self.lbl_status.setText(" — ".join(parts) + ".")

    # ------------------------------------------------------------------
    # Verdict et rendu
    # ------------------------------------------------------------------
    def _reevaluate(self):
        """Recalcule les défauts de toutes les lignes selon les seuils courants."""
        limits = self.thresholds()
        for entry in self._entries:
            entry["defects"], entry["severities"], entry["score"] = \
                quality_scan.evaluate(entry["measures"], limits,
                                      self._reference_of(entry))
            # Relever le seuil des blancs peut faire entrer de nouveaux
            # fichiers dans le champ de la recherche de référence.
            self._queue_reference(entry)
        self._pump_reference()
        self._render()

    def _visible_defects(self):
        """Défauts retenus par les cases de filtrage (analyse rapide comprise)."""
        deep = self.cb_deep.isChecked()
        return {key for key, box in self._defect_filters.items()
                if box.isChecked() and (deep or key not in DEEP_DEFECTS)}

    def _keep(self, entry):
        wanted = self._visible_defects()
        defects = [d for d in entry["defects"] if d in wanted]
        if defects:
            return True
        # Un fichier illisible mérite d'être vu : il ne sera jamais « sain ».
        if entry["measures"].get("error"):
            return True
        return self.cb_healthy.isChecked() and not entry["defects"]

    def _sorted_rows(self):
        column, order = self._sort
        rows = [e for e in self._entries if self._keep(e)]

        def key(entry):
            if column == QC_SCORE:
                return (entry["score"], entry["title"].lower())
            if column == QC_TITLE:
                return (entry["title"].lower(),)
            if column == QC_ARTIST:
                return (entry["artist"].lower(), entry["title"].lower())
            if column == QC_DEFECTS:
                return (len(entry["defects"]), entry["score"])
            if column == QC_HISS:
                return (_finite(entry["measures"].get("hiss_db")),)
            if column == QC_CRACKLE:
                return (entry["measures"].get("crackle_pct") or -1.0,)
            if column == QC_CLIPPING:
                return (entry["measures"].get("clipping_pct") or 0.0,)
            if column == QC_GAPS:
                return (entry["measures"].get("gap_density") or 0.0,)
            if column == QC_DURATION:
                # Trier sur l'écart, non sur la durée : c'est lui qui compte.
                # Les titres sans référence se rangent au plus bas, il n'y a
                # rien à en dire.
                gap = quality_scan.duration_excess(
                    entry["measures"], self._reference_of(entry))
                return (abs(gap) if gap is not None else -1.0,)
            if column == QC_SILENCE:
                return (max(entry["measures"].get("tail_seconds", 0.0),
                            entry["measures"].get("head_seconds", 0.0)),)
            if column == QC_FORMAT:
                return (entry.get("bitrate", 0), entry.get("format", ""))
            if column == QC_PATH:
                return (entry["path"].lower(),)
            if column == QC_STATUS:
                return (entry.get("status", ""),)
            return (entry["score"],)

        rows.sort(key=key, reverse=(order == Qt.SortOrder.DescendingOrder))
        return rows

    def _render(self):
        if self._rendering:
            return
        self._rendering = True
        self.table.setUpdatesEnabled(False)
        try:
            self._rows = self._sorted_rows()
            self.table.setRowCount(len(self._rows))
            for row, entry in enumerate(self._rows):
                entry["_row"] = row
                self._fill_row(row, entry)
        finally:
            self.table.setUpdatesEnabled(True)
            self._rendering = False
        column, order = self._sort
        self.table.horizontalHeader().setSortIndicator(column, order)

    def _append_rows(self, entries):
        """Ajoute des lignes à la fin du tableau sans le reconstruire.

        Pendant l'analyse, les résultats arrivent par lots toutes les
        quelques secondes : reconstruire tout le tableau à chaque lot
        coûterait un temps proportionnel au carré du nombre de fichiers, ce
        qui se voit dès le millier. Les lignes sont donc simplement empilées
        dans leur ordre d'arrivée, et le tri définitif n'a lieu qu'à la fin
        de l'analyse (ou dès que l'utilisateur touche à un filtre).
        """
        self._rendering = True
        self.table.setUpdatesEnabled(False)
        try:
            start = self.table.rowCount()
            self.table.setRowCount(start + len(entries))
            for offset, entry in enumerate(entries):
                row = start + offset
                entry["_row"] = row
                self._rows.append(entry)
                self._fill_row(row, entry)
        finally:
            self.table.setUpdatesEnabled(True)
            self._rendering = False

    def _fill_row(self, row, entry):
        measures = entry["measures"]
        error = measures.get("error")

        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        check.setCheckState(Qt.CheckState.Checked if entry.get("selected")
                            else Qt.CheckState.Unchecked)
        check.setData(Qt.ItemDataRole.UserRole, entry["_uid"])
        self.table.setItem(row, QC_CHECK, check)

        # Bouton de lecture. Il est lié à l'identifiant de la ligne et non à
        # son rang : le tri déplace les lignes, l'identifiant ne bouge pas.
        uid = entry["_uid"]
        # L'existence du fichier n'est pas vérifiée ici : le rendu d'un
        # tableau de plusieurs milliers de lignes ne va pas interroger le
        # disque autant de fois. Un fichier disparu est signalé par le
        # lecteur lui-même, à l'ouverture.
        listen = player.play_item(
            "Écouter ce titre dans le lecteur (égaliseur, déplacement)")
        listen.setData(Qt.ItemDataRole.UserRole, uid)
        self.table.setItem(row, QC_PLAY, listen)

        score = QTableWidgetItem("—" if error else str(entry["score"]))
        if not error:
            score.setForeground(self._score_color(entry["score"]))
        self.table.setItem(row, QC_SCORE, score)

        self.table.setItem(row, QC_TITLE, QTableWidgetItem(entry["title"]))
        self.table.setItem(row, QC_ARTIST, QTableWidgetItem(entry["artist"]))

        if error:
            defects_item = QTableWidgetItem("Illisible")
            defects_item.setForeground(COLOR_BAD)
            defects_item.setToolTip(error)
        else:
            names = ", ".join(DEFECT_LABELS[d] for d in entry["defects"])
            defects_item = QTableWidgetItem(names or "Aucun")
            defects_item.setForeground(COLOR_OK if not entry["defects"]
                                       else self._score_color(entry["score"]))
        self.table.setItem(row, QC_DEFECTS, defects_item)

        self._measure_cell(row, QC_HISS, entry, quality_scan.HISS)
        self._measure_cell(row, QC_CRACKLE, entry, quality_scan.CRACKLE)
        self._measure_cell(row, QC_CLIPPING, entry, quality_scan.CLIPPING)
        self._measure_cell(row, QC_GAPS, entry, quality_scan.GAPS)
        self._duration_cell(row, entry)
        self._silence_cell(row, entry)

        fmt = entry.get("format") or "?"
        bitrate = entry.get("bitrate", 0)
        self.table.setItem(row, QC_FORMAT, QTableWidgetItem(
            f"{fmt} · {bitrate} kbps" if bitrate else fmt))

        path_item = QTableWidgetItem(entry["path"])
        path_item.setToolTip(entry["path"])
        self.table.setItem(row, QC_PATH, path_item)

        self._set_status(row, entry.get("status", ""))

    def _measure_cell(self, row, column, entry, defect):
        """Cellule chiffrant une mesure, colorée si elle dépasse le seuil."""
        measures = entry["measures"]
        if measures.get("error"):
            item = QTableWidgetItem("—")
        elif defect in DEEP_DEFECTS and measures.get("crackle_pct") is None:
            item = QTableWidgetItem("non mesuré")
            item.setForeground(COLOR_MUTED)
            item.setToolTip("Cochez « Détecter les craquements » puis relancez "
                            "l'analyse.")
        else:
            item = QTableWidgetItem(quality_scan.describe(measures, defect))
            if defect in entry["defects"]:
                item.setForeground(COLOR_BAD)
            else:
                item.setForeground(COLOR_MUTED)
            if defect == quality_scan.HISS:
                item.setToolTip(self._hiss_tooltip(measures))
            elif defect == quality_scan.GAPS:
                item.setToolTip(self._gaps_tooltip(measures))
        self.table.setItem(row, column, item)

    @staticmethod
    def _gaps_tooltip(measures):
        """Détaille les coupures : combien, quelle durée, quelle densité."""
        count = measures.get("gap_count", 0) or 0
        if not count:
            return ("Aucun silence numérique au milieu du titre.\n"
                    "Les blancs de début et de fin ont leur propre colonne.")
        return ("%d coupure(s) au milieu du titre, %.2f s en tout.\n"
                "Passage de dix secondes le plus troué : %.0f %% de silence.\n"
                "C'est cette densité qui fait le verdict — un morceau bâti sur\n"
                "des silences en compte beaucoup sans en être abîmé."
                % (count, measures.get("gap_seconds", 0.0) or 0.0,
                   100.0 * (measures.get("gap_density", 0.0) or 0.0)))

    @staticmethod
    def _hiss_tooltip(measures):
        """Explique pourquoi un plancher de bruit élevé est — ou non — un souffle."""
        if not quality_scan.hiss_measurable(measures):
            return ("Fichier échantillonné à %d Hz : il ne contient rien "
                    "au-dessus de 8 kHz,\noù se mesure le souffle."
                    % (measures.get("sample_rate") or 0))
        gap = quality_scan.trough_gap(measures)
        if gap is None:
            return ("Plancher de bruit au-dessus de 8 kHz.\n"
                    "Le fichier comporte du silence numérique franc.")
        verdict = ("bruit large bande : c'est un souffle"
                   if gap <= quality_scan.HISS_TROUGH_GAP_DB
                   else "aigus absents des passages calmes : c'est de la "
                        "musique, pas un souffle")
        return ("Plancher de bruit au-dessus de 8 kHz.\n"
                "Au moment le plus calme, la bande haute est %.0f dB sous le "
                "signal entier\n(%s)." % (gap, verdict))

    @staticmethod
    def _mmss(seconds):
        """« 3:36 » — une durée se lit en minutes, pas en secondes."""
        total = int(round(seconds or 0))
        return "%d:%02d" % (total // 60, total % 60)

    def _duration_cell(self, row, entry):
        """Cellule de la durée, comparée à celle du morceau.

        La durée seule n'apprendrait rien : c'est l'écart à la référence qui
        dit si le fichier est bien la version attendue.
        """
        measures = entry["measures"]
        duration = measures.get("duration") or 0.0
        reference = entry.get("reference")

        if measures.get("error") or not duration:
            item = QTableWidgetItem("—")
            item.setForeground(COLOR_MUTED)
        elif reference == "?":
            item = QTableWidgetItem("%s — référence…" % self._mmss(duration))
            item.setForeground(COLOR_MUTED)
        elif reference is None:
            item = QTableWidgetItem("%s — sans référence" % self._mmss(duration))
            item.setForeground(COLOR_MUTED)
        else:
            gap = duration - reference
            item = QTableWidgetItem("%s  (%+.0f s)" % (self._mmss(duration), gap))
            item.setForeground(COLOR_BAD if quality_scan.DURATION in entry["defects"]
                               else COLOR_MUTED)
        item.setToolTip(self._duration_tooltip(entry))
        self.table.setItem(row, QC_DURATION, item)

    def _duration_tooltip(self, entry):
        """Dit d'où vient la référence et ce que l'écart signifie."""
        measures = entry["measures"]
        duration = measures.get("duration") or 0.0
        reference = entry.get("reference")
        if reference == "?":
            return ("Durée du fichier : %s.\n"
                    "Durée du morceau en cours de recherche…" % self._mmss(duration))
        if reference is None:
            return ("Durée du fichier : %s.\n"
                    "Ni Deezer ni MusicBrainz ne connaissent ce titre : sa\n"
                    "durée n'est comparée à rien." % self._mmss(duration))
        gap = duration - reference
        if abs(gap) > self.thresholds()["duration_gap"]:
            verdict = ("Le fichier est plus %s que le morceau : ce n'est pas la\n"
                       "version attendue — montage écourté, édition radio, ou\n"
                       "extraction d'une mauvaise source."
                       % ("court" if gap < 0 else "long"))
        else:
            verdict = "Le fichier dure ce que dure le morceau."
        return ("Durée du fichier : %s — morceau : %s (%+.0f s).\n%s\n"
                "La référence retenue est, parmi les versions connues, celle\n"
                "dont la durée est la plus proche."
                % (self._mmss(duration), self._mmss(reference), gap, verdict))

    def _silence_cell(self, row, entry):
        """Cellule des blancs : « 3 s au début », « 12 s à la fin »…

        Un blanc n'est un défaut que si le fichier dépasse la durée du
        morceau : la cellule dit donc aussi ce que la référence a permis de
        conclure, faute de quoi un silence mesuré mais non retenu passerait
        pour une mesure ratée.
        """
        measures = entry["measures"]
        parts = []
        if quality_scan.HEAD in entry["defects"]:
            parts.append("%.0f s début" % measures.get("head_seconds", 0.0))
        if quality_scan.TAIL in entry["defects"]:
            parts.append("%.0f s fin" % measures.get("tail_seconds", 0.0))

        if parts:
            item = QTableWidgetItem(" / ".join(parts))
            item.setForeground(COLOR_BAD)
        elif measures.get("error"):
            item = QTableWidgetItem("—")
            item.setForeground(COLOR_MUTED)
        else:
            reference = entry.get("reference")
            silence = max(measures.get("head_seconds", 0.0),
                          measures.get("tail_seconds", 0.0))
            limits = self.thresholds()
            measured = (measures.get("tail_seconds", 0.0) > limits["tail_seconds"]
                        or measures.get("head_seconds", 0.0) > limits["head_seconds"])
            if not measured:
                item = QTableWidgetItem("ok")
            elif reference == "?":
                item = QTableWidgetItem("%.0f s — référence…" % silence)
            elif reference is None:
                item = QTableWidgetItem("%.0f s — sans référence" % silence)
            else:
                item = QTableWidgetItem("%.0f s — dans le master" % silence)
            item.setForeground(COLOR_MUTED)

        item.setToolTip(self._silence_tooltip(entry))
        self.table.setItem(row, QC_SILENCE, item)

    def _silence_tooltip(self, entry):
        """Explique pourquoi un blanc mesuré est — ou non — tenu pour un défaut."""
        measures = entry["measures"]
        lines = ["Début : %.1f s — Fin : %.1f s"
                 % (measures.get("head_seconds", 0.0),
                    measures.get("tail_seconds", 0.0))]
        duration = measures.get("duration") or 0.0
        reference = entry.get("reference")
        if reference == "?":
            lines.append("Durée de référence en cours de recherche…")
        elif reference is None:
            lines.append(
                "Durée du fichier : %.0f s.\n"
                "Ni Deezer ni MusicBrainz ne connaissent ce titre : sans\n"
                "durée de référence, rien ne dit si ce silence appartient au\n"
                "morceau. Aucun blanc n'est donc signalé." % duration)
        else:
            excess = duration - reference
            verdict = ("le fichier dépasse le morceau : le silence a été "
                       "ajouté" if excess > self.thresholds()["duration_excess"]
                       else "le silence appartient au master, il n'y a rien "
                            "à corriger")
            lines.append(
                "Durée du fichier : %.0f s — référence : %d s (%+.0f s).\n%s"
                % (duration, reference, excess, verdict))
        return "\n".join(lines)

    @staticmethod
    def _score_color(score):
        if score >= SCORE_GOOD:
            return COLOR_OK
        return COLOR_BAD if score < SCORE_SEVERE else COLOR_WARN

    def _set_status(self, row, text):
        item = self.table.item(row, QC_STATUS)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, QC_STATUS, item)
        item.setText(text)

    def _on_header_clicked(self, column):
        if column == QC_CHECK:
            return
        current, order = self._sort
        if current == column:
            order = (Qt.SortOrder.DescendingOrder
                     if order == Qt.SortOrder.AscendingOrder
                     else Qt.SortOrder.AscendingOrder)
        else:
            order = Qt.SortOrder.AscendingOrder
        self._sort = (column, order)
        self._render()

    # ------------------------------------------------------------------
    # Sélection
    # ------------------------------------------------------------------
    def _on_item_changed(self, item):
        if self._rendering or item.column() != QC_CHECK:
            return
        entry = self._by_uid.get(item.data(Qt.ItemDataRole.UserRole))
        if entry is not None:
            entry["selected"] = item.checkState() == Qt.CheckState.Checked

    def _set_all_checked(self, checked):
        """Coche ou décoche toutes les lignes visibles."""
        self._rendering = True
        try:
            for row, entry in enumerate(self._rows):
                # Un fichier illisible n'a rien à retélécharger : ses tags,
                # d'où viendrait la recherche, n'ont pas pu être lus.
                allowed = checked and not entry["measures"].get("error")
                entry["selected"] = allowed
                item = self.table.item(row, QC_CHECK)
                if item is not None:
                    item.setCheckState(Qt.CheckState.Checked if allowed
                                       else Qt.CheckState.Unchecked)
        finally:
            self._rendering = False

    def _row_of(self, entry):
        row = entry.get("_row", -1)
        if 0 <= row < len(self._rows) and self._rows[row] is entry:
            return row
        try:
            return self._rows.index(entry)
        except ValueError:
            return -1

    # ------------------------------------------------------------------
    # Menu contextuel
    # ------------------------------------------------------------------
    def _show_context_menu(self, point):
        row = self.table.rowAt(point.y())
        if not 0 <= row < len(self._rows):
            return
        entry = self._rows[row]
        menu = QMenu(self)
        menu.addAction("Écouter dans DeeKeep",
                       lambda: self._play(entry["_uid"]))
        menu.addAction("Ouvrir avec le lecteur du système",
                       lambda: self._open(entry["path"]))
        menu.addAction("Ouvrir le dossier",
                       lambda: self._reveal(entry["path"]))
        menu.exec(self.table.viewport().mapToGlobal(point))

    def _on_cell_clicked(self, row, column):
        if column == QC_PLAY and 0 <= row < len(self._rows):
            self._play(self._rows[row]["_uid"])

    def _on_cell_double_clicked(self, row, _column):
        if 0 <= row < len(self._rows):
            self._play(self._rows[row]["_uid"])

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------
    def _play(self, uid):
        """Ouvre le lecteur sur le titre ``uid``, la liste affichée en file.

        La file suit l'ordre du tableau : après le titre écouté vient celui
        de la ligne suivante, ce qui permet de passer en revue une série de
        titres signalés sans revenir au tableau entre chaque.
        """
        rows = self._rows
        if not any(entry["_uid"] == uid for entry in rows):
            # Ligne disparue entre-temps (filtre, nouvelle analyse) : on
            # écoute le titre seul plutôt que de démarrer ailleurs.
            entry = self._by_uid.get(uid)
            rows = [entry] if entry is not None else []
        tracks, index = [], 0
        for entry in rows:
            if entry["_uid"] == uid:
                index = len(tracks)
            tracks.append(player.make_track(
                path=entry["path"],
                title=entry["title"],
                artist=entry["artist"],
                subtitle=self._player_subtitle(entry),
            ))
        player.play_tracks(self, tracks, index)

    @staticmethod
    def _player_subtitle(entry):
        """Ligne de contexte affichée par le lecteur : ce qu'il faut écouter."""
        parts = []
        fmt = entry.get("format") or "?"
        bitrate = entry.get("bitrate", 0)
        parts.append("%s · %s kbps" % (fmt, bitrate) if bitrate else fmt)
        if entry["measures"].get("error"):
            parts.append("fichier illisible")
        else:
            parts.append("note %s" % entry["score"])
            if entry["defects"]:
                parts.append(", ".join(DEFECT_LABELS[d]
                                       for d in entry["defects"]).lower())
        return "  ·  ".join(parts)

    @staticmethod
    def _open(path):
        if os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @staticmethod
    def _reveal(path):
        """Ouvre l'explorateur sur le fichier, sélectionné dans son dossier."""
        if path:
            reveal_in_explorer(path)

    # ------------------------------------------------------------------
    # Retéléchargement
    # ------------------------------------------------------------------
    def _redownload(self):
        # L'analyse n'est plus un obstacle : elle en a fini avec les fichiers
        # dont elle a rendu la ligne, et rien n'oblige à attendre les autres.
        if self._workers or self._queue:
            QMessageBox.information(
                self, "Téléchargement en cours",
                "Des retéléchargements sont déjà en cours.")
            return
        if not config.YTDLP_AVAILABLE:
            QMessageBox.warning(
                self, "Extraction indisponible",
                "yt-dlp n'est pas installé. Installez-le via : pip install yt-dlp")
            return

        selected = [e for e in self._rows
                    if e.get("selected") and not e["measures"].get("error")]
        if not selected:
            QMessageBox.information(
                self, "Aucune sélection",
                "Cochez au moins un titre à retélécharger.")
            return

        # La réextraction cherche « Artiste - Titre » : sans l'un des deux,
        # elle rapporterait n'importe quoi et l'écraserait par-dessus le
        # fichier existant. Ces titres sont écartés, pas devinés.
        targets = [e for e in selected if (e.get("artist") or "").strip()
                   and (e.get("title") or "").strip()]
        skipped = len(selected) - len(targets)
        if not targets:
            QMessageBox.warning(
                self, "Tags insuffisants",
                "Les titres cochés n'ont ni artiste ni titre dans leurs tags : "
                "la recherche n'aurait rien à chercher, et le fichier trouvé "
                "remplacerait l'existant à l'aveugle.\n\n"
                "Renseignez d'abord les tags — l'écran « Organiser » sait les "
                "retrouver à partir du signal.")
            return

        message = (f"{len(targets)} titre(s) vont être réextraits et "
                   f"remplaceront la version abîmée.\n\n"
                   f"Le fichier produit est rangé selon les règles de l'écran "
                   f"« Organiser » (sous-dossiers, modèle de nom, « Divers ») : "
                   f"un titre mal placé peut donc changer de dossier au "
                   f"passage.\n\n"
                   f"Chaque titre est ensuite réanalysé pour vérifier que le "
                   f"défaut a disparu.")
        if skipped:
            message += (f"\n\n{skipped} titre(s) coché(s) sont écartés : leurs "
                        f"tags ne donnent pas l'artiste ou le titre nécessaires "
                        f"à la recherche.")
        answer = QMessageBox.question(
            self, "Retélécharger", message + "\n\nContinuer ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._batch = [e["_uid"] for e in targets]
        self._queue = list(self._batch)
        # Lecture du disque une seule fois pour tout le lot : les cinq
        # extractions simultanées rangent alors d'après le même décompte.
        self._tally = library_rules.Tally(self.root)
        for entry in targets:
            entry["status"] = "En attente…"
            entry["_pct"] = 0
            row = self._row_of(entry)
            if row >= 0:
                self._set_status(row, entry["status"])
        self.download_bar.setValue(0)
        self.download_bar.setVisible(True)
        self._refresh_controls()
        self._pump()

    def _pump(self):
        """Démarre des extractions tant qu'un emplacement est libre."""
        while self._queue and len(self._workers) < self.MAX_CONCURRENT:
            self._launch(self._queue.pop(0))

    def _export_options(self):
        """Réglages d'exportation courants, avec des valeurs de repli sûres."""
        options = {"file_format": "MP3", "quality": "320 kbps (HQ)",
                   "cookies_browser": None, "cookies_file": ""}
        if self._export_callback:
            try:
                options.update(self._export_callback() or {})
            except Exception as exc:
                print(f"[quality] Réglages d'exportation illisibles : {exc}")
        return options

    def _launch(self, uid):
        entry = self._by_uid.get(uid)
        if entry is None:
            return
        options = self._export_options()

        entry["status"] = "Initialisation…"
        entry["_pct"] = 5
        row = self._row_of(entry)
        if row >= 0:
            self._set_status(row, entry["status"])

        worker = TrackDownloaderThread(
            uid, "", entry.get("title", ""), entry.get("artist", ""),
            entry.get("album", ""),
            # La destination est la bibliothèque, pas le dossier du fichier
            # abîmé : ce sont les règles d'« Organiser » qui décident où le
            # titre remplacé se range, sans quoi une version saine viendrait
            # se poser exactement là où l'analyse de rangement la reprendrait.
            self.root,
            options["file_format"], options["quality"], None,
            layout=library_rules.rules_from_settings(self._settings),
            tally=self._tally,
            # Tags du fichier abîmé : ils font foi sur ceux que le catalogue
            # rapporte (l'édition possédée n'est pas forcément la sienne), et
            # ils comblent ce qu'il ne rapporterait pas. Le thread demande le
            # reste à Deezer (voir ``_resolve_extra_tags``).
            extra_tags=entry.get("tags") or {},
            cookies_browser=options["cookies_browser"],
            cookies_file=options["cookies_file"],
            original_path=entry["path"],
            # Aucune correspondance Deezer n'a été cherchée pendant l'analyse :
            # elle est résolue ici, à partir des tags du fichier abîmé.
            resolve_metadata=True,
        )
        worker.progress_signal.connect(self._on_download_progress)
        worker.finished_signal.connect(self._on_download_finished)
        self._workers[uid] = worker
        worker.start()

    def _on_download_progress(self, uid, percent, text):
        entry = self._by_uid.get(uid)
        if entry is None:
            return
        entry["_pct"] = percent
        entry["status"] = text
        row = self._row_of(entry)
        if row >= 0:
            self._set_status(row, f"{text} — {percent}%")
        self._update_download_bar()

    def _update_download_bar(self):
        if not self._batch:
            return
        total = sum(self._by_uid[uid].get("_pct", 0) for uid in self._batch
                    if uid in self._by_uid)
        self.download_bar.setValue(int(total / len(self._batch)))

    def _on_download_finished(self, uid, success, message, track_info):
        entry = self._by_uid.get(uid)
        self._workers.pop(uid, None)
        if entry is not None:
            entry["_pct"] = 100 if success else 0
            if success:
                new_path = (track_info or {}).get("path") or entry["path"]
                entry["path"] = new_path
                for field in ("title", "artist", "album"):
                    if (track_info or {}).get(field):
                        entry[field] = track_info[field]
                # Le fichier a changé : sa mesure d'hier ne vaut plus rien.
                self._cache.forget(new_path)
                entry["status"] = "Réanalyse…"
                self._reanalyse(uid, new_path)
            else:
                entry["status"] = "Échec"
                print(f"[Qualité] {entry['path']} : {message}")
            row = self._row_of(entry)
            if row >= 0:
                self._fill_row(row, entry)

        self._update_download_bar()
        self._pump()
        if not self._workers and not self._queue:
            self._refresh_controls()
            # Ne pas écraser le compte rendu de l'analyse encore en cours :
            # elle reprend la main sur la ligne d'état au lot suivant.
            if not self._scanning:
                self.lbl_status.setText(
                    "Retéléchargement terminé : les titres remplacés sont "
                    "réanalysés, la colonne « Défauts » dit ce qu'il en reste.")

    def _reanalyse(self, uid, path):
        """Remesure un fichier fraîchement remplacé."""
        thread = SingleAnalysisThread(uid, path, deep=self.cb_deep.isChecked(),
                                      cache=self._cache, parent=self)
        thread.done.connect(self._on_reanalysed)
        thread.finished.connect(self._prune_single_threads)
        self._single_threads.append(thread)
        thread.start()

    def _prune_single_threads(self):
        self._single_threads = [t for t in self._single_threads if t.isRunning()]

    def _on_reanalysed(self, uid, fresh):
        entry = self._by_uid.get(uid)
        if entry is None:
            return
        if fresh is None:
            entry["status"] = "Remplacé ✓ (réanalyse impossible)"
        else:
            entry["measures"] = fresh["measures"]
            entry["bitrate"] = fresh["bitrate"]
            entry["format"] = fresh["format"]
            entry["tags"] = fresh.get("tags") or entry.get("tags") or {}
            entry["defects"], entry["severities"], entry["score"] = \
                quality_scan.evaluate(fresh["measures"], self.thresholds(),
                                      self._reference_of(entry))
            # Le fichier remplacé peut porter un blanc que l'ancien n'avait
            # pas : sa référence est alors à chercher.
            self._queue_reference(entry)
            self._pump_reference()
            entry["status"] = ("Remplacé ✓ défaut corrigé" if not entry["defects"]
                               else "Remplacé ✓ défaut persistant")
        row = self._row_of(entry)
        if row >= 0:
            self._fill_row(row, entry)

    # ------------------------------------------------------------------
    def _refresh_controls(self):
        """Rend disponible tout ce qui l'est réellement dans l'état courant.

        Un seul verrou par le passé : l'analyse coupait l'accès aux cases
        comme au retéléchargement, ce qui obligeait à attendre la fin d'une
        bibliothèque entière — plusieurs dizaines de minutes — avant de
        pouvoir agir sur les titres déjà signalés. Chaque commande est
        désormais liée à ce qui l'empêche vraiment :

        - cocher et retélécharger ne dérangent pas l'analyse, qui a fini avec
          les fichiers dont elle a rendu la ligne ;
        - relancer une analyse en pleine analyse, ou pendant une extraction
          qui va remplacer des fichiers, n'aurait pas de sens ;
        - la détection des craquements décide de ce que l'analyse mesure :
          elle ne se change pas en cours de route.
        - les seuils, eux, ne relisent aucun fichier : ils restent réglables.
        """
        downloading = bool(self._workers) or bool(self._queue)
        self.btn_scan.setEnabled(not self._scanning and not downloading)
        self.btn_stop.setEnabled(self._scanning)
        self.btn_all.setEnabled(True)
        self.btn_none.setEnabled(True)
        self.btn_download.setEnabled(not downloading)
        self.btn_clear.setEnabled(not self._scanning and not downloading)
        self.cb_deep.setEnabled(not self._scanning)
        for spin in self.spins.values():
            spin.setEnabled(True)

    def can_leave(self):
        """Vrai si l'application peut se fermer sans interrompre une extraction."""
        return not self._workers and not self._queue

    def stop_threads(self):
        """Interrompt analyses et extractions (fermeture de l'application)."""
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._scan_thread.stop()
            self._scan_thread.wait(5000)
        self._ref_queue = []
        self._stop_reference_lookup()
        # Plus rien ne sera lancé, et l'on laisse un instant aux extractions
        # en vol pour finir d'écrire leur fichier : les couper en plein
        # remplacement laisserait un titre à demi écrit sur le disque.
        self._queue = []
        for worker in list(self._workers.values()):
            if worker.isRunning():
                worker.wait(3000)
        for thread in self._single_threads:
            if thread.isRunning():
                thread.wait(3000)
        self._cache.save()


def _finite(value):
    """Valeur triable pour un plancher de bruit éventuellement infini."""
    if value is None or value != value:
        return float("-inf")
    return value
