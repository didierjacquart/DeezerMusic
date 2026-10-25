"""
Écran « Bibliothèque » : tout ce qui est sur le disque, à portée d'écoute.

Les six écrans existants regardent la bibliothèque par le petit bout de leur
lorgnette : ce qui est en train d'être téléchargé, ce qui est mal rangé, ce
qui sonne mal. Aucun ne répondait à la question la plus simple — « qu'est-ce
que j'ai, et où est ce morceau ? ». Celui-ci ne fait que cela : lister,
chercher, écouter.

Presque rien n'est relu du disque. Les trois mille titres viennent de la
table ``tracks`` du cache local, déjà remplie par l'analyse de la
bibliothèque : l'écran s'ouvre en quelques millisecondes, et fonctionne sans
réseau. Seules la date et la taille de chaque fichier sont vérifiées à
l'ouverture, ce qui suffit à distinguer un titre encore présent d'un titre
disparu, et une copie en ligne à jour d'une copie périmée.

Une exception, et elle se paie en dixièmes de seconde : les fichiers dont la
base **ignore le débit** voient leur en-tête relu (``library_mirror.probe_bitrate``).
Ils sont deux cents, la lecture coûte 0,4 ms chacun, et sans elle le miroir
se trompe deux fois — la jauge les compte pour incompressibles, et la colonne
FORMAT les affiche sans débit.

**La recherche porte sur tous les tags à la fois** — titre, artiste, album,
artiste d'album, genre, année, format — et non sur une colonne choisie dans
une liste déroulante. On tape ce dont on se souvient, sans avoir à dire de
quoi il s'agit : « discovery » trouve l'album, « daft » l'artiste, « 2001 »
l'année. Les mots sont exigés tous ensemble mais dans n'importe quel ordre
(« daft one more » trouve le titre), accents et ponctuation ignorés
(``library_index.canonical``).

Dès la **troisième lettre**, cinq propositions cliquables s'affichent sous le
champ. Elles ne sortent pas d'un dictionnaire : ce sont les valeurs de tags
réellement présentes dans la bibliothèque, chacune annoncée par sa nature
(« Artiste · Daft Punk ») et par le nombre de titres qu'elle rassemble. Un
clic remplit le champ et filtre — c'est le chemin le plus court entre un
souvenir vague et l'album entier.

L'**archive** (voir ``library_mirror``) tient sur le même écran, et non dans
un coin des Paramètres : une copie n'est pas un réglage, c'est un état de la
bibliothèque. La colonne NUAGE le dit titre par titre. Un titre dont le
fichier a disparu du disque se restaure depuis l'archive, octet pour octet —
elle ne diffuse pas de flux, il faut le rendre au disque pour l'écouter.
"""

import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QLineEdit, QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QProgressBar, QComboBox, QToolButton,
    QCheckBox, QMenu, QMessageBox, QApplication,
)
from PyQt6.QtCore import Qt, QEvent, QTimer
from PyQt6.QtGui import QColor

from deekeep.audio import library_mirror
from deekeep.core.utils import reveal_in_explorer

from deekeep.db import local_db
from deekeep.db import sqlite_store
from deekeep.library.library_index import canonical
from deekeep.ui import player
from deekeep.ui.styles import (ERROR, PRIMARY, TERTIARY, TEXT_FAINT,
                               TEXT_MUTED)


# Colonnes du tableau (une constante par colonne : ajouter une colonne ne
# décale plus les indices écrits en dur).
# NUAGE est posée à gauche, juste après ÉCOUTE : onze colonnes ne tiennent
# pas dans la largeur d'une fenêtre, et l'état du miroir ne doit pas être la
# première victime du défilement horizontal. Le chemin, lui, s'accommode
# d'être en bout de ligne — il reste lisible au survol et dans le menu
# contextuel.
(LC_CHECK, LC_PLAY, LC_CLOUD, LC_TITLE, LC_ARTIST, LC_ALBUM, LC_GENRE,
 LC_YEAR, LC_DURATION, LC_FORMAT, LC_PATH) = range(11)

COLUMN_LABELS = ["", "ÉCOUTE", "NUAGE", "TITRE", "ARTISTE", "ALBUM", "GENRE",
                 "ANNÉE", "DURÉE", "FORMAT", "CHEMIN"]

# Colonnes sur lesquelles un clic d'en-tête trie, et la clé de tri associée.
SORT_KEYS = {
    LC_TITLE: "title", LC_ARTIST: "artist", LC_ALBUM: "album",
    LC_GENRE: "genre", LC_YEAR: "year", LC_DURATION: "duration",
    LC_FORMAT: "format", LC_CLOUD: "cloud_order", LC_PATH: "path",
}

# Tags interrogés par la recherche, et le mot qui les annonce dans les
# propositions. L'ordre compte : à nombre de titres égal, un artiste est
# proposé avant un genre.
SEARCH_FIELDS = (
    ("artist", "Artiste"),
    ("album", "Album"),
    ("title", "Titre"),
    ("album_artist", "Album de"),
    ("genre", "Genre"),
    ("year", "Année"),
    ("format", "Format"),
)

# Longueur à partir de laquelle les propositions s'affichent, et leur nombre.
MIN_SUGGEST_CHARS = 3
MAX_SUGGESTIONS = 5

# Délai avant filtrage, le temps que la frappe se termine : refiltrer trois
# mille lignes à chaque lettre ferait saccader la saisie.
FILTER_DELAY_MS = 220


def human_bytes(count):
    """Taille lisible : 1,2 Go, 340 Mo, 12 Ko."""
    value = float(count or 0)
    for unit, threshold in (("Go", 1e9), ("Mo", 1e6), ("Ko", 1e3)):
        if value >= threshold:
            return ("%.1f %s" % (value / threshold, unit)).replace(".", ",")
    return "%d o" % int(value)


def mmss(seconds):
    """Durée en mm:ss, ou "" si elle est inconnue."""
    if not seconds:
        return ""
    total = int(round(float(seconds)))
    return "%d:%02d" % divmod(total, 60)


class LibraryPage(QWidget):
    """Liste, recherche, écoute et miroir en ligne de la bibliothèque."""

    def __init__(self, app_settings=None, save_callback=None, parent=None):
        super().__init__(parent)
        self._settings = app_settings if app_settings is not None else {}
        self._save_callback = save_callback

        self._entries = []          # tous les titres connus du cache
        self._rows = []             # titres réellement affichés (après filtre)
        self._checked = set()       # chemins cochés (survit au filtrage)
        self._by_path = {}
        self._suggestions = []      # (libellé, valeur, nombre de titres)
        self._values = []           # index des valeurs de tags, pour ci-dessus
        self._query = ""
        self._sort = (LC_ARTIST, Qt.SortOrder.AscendingOrder)
        self._rendering = False

        self._mirror_thread = None
        self._restore_thread = None
        self._usage_thread = None
        self._check_thread = None
        # Client du seau, créé à la première nécessité : il porte une session
        # HTTP, et en fabriquer un par titre à l'ouverture du lecteur se
        # verrait. Remis à zéro dès que les identifiants changent.
        self._store_cache = None
        # Rang d'affichage de chaque chemin : mettre à jour la cellule d'un
        # titre ne doit pas coûter un parcours de la liste entière.
        self._row_of = {}

        # Filtrage différé : voir FILTER_DELAY_MS.
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(FILTER_DELAY_MS)
        self._filter_timer.timeout.connect(self._apply_query)

        self._build_ui()
        self.sync_from_settings()
        self.reload()

    # ==================================================================
    # Construction de l'interface
    # ==================================================================
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 24)
        layout.setSpacing(14)

        title = QLabel("Bibliothèque")
        title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 26px; "
            "font-weight: bold; color: #00e5ff;"
        )
        layout.addWidget(title)

        sub = QLabel(
            "Tout ce que la base connaît du disque, sans le relire : tags, "
            "durée, format et emplacement de chaque fichier. La recherche "
            "porte sur l'ensemble des tags à la fois — dès la troisième "
            "lettre, cinq propositions tirées de vos propres tags s'affichent "
            "sous le champ. Le clic sur ▶ écoute le titre, et la liste "
            "affichée devient la file d'attente du lecteur."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #849396; font-size: 12px;")
        layout.addWidget(sub)

        # --- Recherche ---
        search_layout = QHBoxLayout()
        search_layout.setSpacing(12)

        self.input_search = QLineEdit()
        self.input_search.setPlaceholderText(
            "Rechercher dans tous les tags : titre, artiste, album, genre, "
            "année, format…")
        self.input_search.setClearButtonEnabled(True)
        self.input_search.textChanged.connect(self._on_search_changed)
        self.input_search.returnPressed.connect(self._on_search_return)
        self.input_search.installEventFilter(self)

        self.btn_cloud_panel = QToolButton()
        self.btn_cloud_panel.setCheckable(True)
        self.btn_cloud_panel.setToolTip(
            "Afficher ou masquer les réglages du miroir en ligne")
        self.btn_cloud_panel.toggled.connect(self._on_cloud_panel_toggled)

        search_layout.addWidget(self.input_search, 1)
        search_layout.addWidget(self.btn_cloud_panel)
        layout.addLayout(search_layout)

        # Propositions : un enfant de la page, et non une fenêtre — le champ
        # garde le clavier, et la liste se pose par-dessus le tableau sans
        # décaler quoi que ce soit.
        self.list_suggest = QListWidget(self)
        self.list_suggest.setObjectName("suggestList")
        self.list_suggest.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list_suggest.setCursor(Qt.CursorShape.PointingHandCursor)
        self.list_suggest.setStyleSheet(
            "QListWidget#suggestList { background-color: #252833; "
            "border: 1px solid rgba(0, 229, 255, 0.35); border-radius: 10px; "
            "padding: 4px; color: #e5e2e1; font-size: 13px; }"
            "QListWidget#suggestList::item { padding: 7px 10px; "
            "border-radius: 6px; }"
            "QListWidget#suggestList::item:selected { background-color: "
            "rgba(0, 229, 255, 0.18); color: #e5e2e1; }"
        )
        self.list_suggest.itemClicked.connect(self._on_suggestion_clicked)
        self.list_suggest.hide()

        layout.addWidget(self._build_cloud_panel())

        # --- Barre d'actions ---
        # Même disposition que les autres écrans (voir ``styles.py``) : la
        # manipulation de l'affichage à gauche, les actions à droite —
        # l'analyse en pilule ajourée, l'action qui écrit en pilule pleine.
        action_layout = QHBoxLayout()
        action_layout.setSpacing(12)

        self.btn_all = QPushButton("Tout cocher")
        self.btn_all.setProperty("class", "control-btn")
        self.btn_all.clicked.connect(lambda: self._set_all_checked(True))

        self.btn_none = QPushButton("Tout décocher")
        self.btn_none.setProperty("class", "control-btn")
        self.btn_none.clicked.connect(lambda: self._set_all_checked(False))

        self.cb_missing = QCheckBox("Titres absents du disque")
        self.cb_missing.setToolTip(
            "Afficher aussi les titres que seul l'historique connaît, dont le "
            "fichier n'est plus sur le disque. Ceux qui sont en miroir restent "
            "écoutables."
        )
        self.cb_missing.stateChanged.connect(self._on_missing_changed)

        # Pilule ajourée : c'est l'étape qui alimente la liste, à la place
        # que la convention des barres d'actions lui réserve (voir README).
        self.btn_refresh = QPushButton("Actualiser")
        self.btn_refresh.setProperty("class", "accent-btn")
        self.btn_refresh.setToolTip("Relire la liste depuis la base")
        self.btn_refresh.clicked.connect(self.reload)

        self.btn_restore = QPushButton("Restaurer")
        self.btn_restore.setProperty("class", "control-btn")
        self.btn_restore.setToolTip(
            "Redescendre du seau les titres qui ne sont plus sur le disque. "
            "Rien n'est écrasé : seuls les fichiers absents sont écrits."
        )
        self.btn_restore.clicked.connect(self._start_restore)

        self.btn_stop = QPushButton("Arrêter")
        self.btn_stop.setProperty("class", "control-btn")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_work)

        self.btn_mirror = QPushButton("Archiver")
        self.btn_mirror.setProperty("class", "primary-btn")
        self.btn_mirror.setToolTip(
            "Copier les titres cochés dans l'archive, tels quels — aucun "
            "réencodage, les fichiers d'origine ne sont pas touchés"
        )
        self.btn_mirror.clicked.connect(self._start_mirror)

        action_layout.addWidget(self.btn_all)
        action_layout.addWidget(self.btn_none)
        action_layout.addWidget(self.cb_missing)
        action_layout.addStretch()
        action_layout.addWidget(self.btn_restore)
        action_layout.addWidget(self.btn_refresh)
        action_layout.addWidget(self.btn_stop)
        action_layout.addWidget(self.btn_mirror)
        layout.addLayout(action_layout)

        self.progress = QProgressBar()
        self.progress.setObjectName("global_progress_bar")
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

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
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.verticalHeader().setDefaultSectionSize(36)

        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                   | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_clicked)
        for column in range(len(COLUMN_LABELS)):
            header.setSectionResizeMode(column,
                                        QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(LC_CHECK,
                                    QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(LC_PLAY, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(LC_PATH, QHeaderView.ResizeMode.Stretch)
        # Somme réglée sur la largeur utile d'une fenêtre de 1 280 px, barre
        # latérale et marges déduites : tout ce qui compte se lit sans
        # défilement horizontal, et la colonne CHEMIN, étirée, absorbe ce
        # qui reste.
        widths = {LC_PLAY: 66, LC_CLOUD: 96, LC_TITLE: 200, LC_ARTIST: 140,
                  LC_ALBUM: 150, LC_GENRE: 90, LC_YEAR: 58, LC_DURATION: 60,
                  LC_FORMAT: 92}
        for column, width in widths.items():
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #5f7d84; font-size: 12px;")
        layout.addWidget(self.lbl_status)

        self._on_cloud_panel_toggled(False)
        # Connexion tardive : alimenter la liste des débits émet
        # ``currentIndexChanged``, et le tableau n'existait pas encore.
        self.input_archive.editingFinished.connect(self._on_archive_changed)

    def _build_cloud_panel(self):
        """Volet repliable du miroir en ligne : seau, clés, débit."""
        self.cloud_panel = QWidget()
        self.cloud_panel.setObjectName("cloudPanel")
        self.cloud_panel.setStyleSheet(
            "QWidget#cloudPanel { background-color: #1c1e26; "
            "border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 12px; }"
        )
        panel = QVBoxLayout(self.cloud_panel)
        panel.setContentsMargins(20, 16, 20, 16)
        panel.setSpacing(12)

        intro = QLabel(
            "La copie de la bibliothèque vit dans une base SQLite, sur un "
            "disque à soi. Les titres y entrent **tels quels**, octet pour "
            "octet : rien n'est réencodé, rien n'est perdu, et une "
            "restauration rend l'original — son empreinte est revérifiée à "
            "la sortie. La seule limite est la place du disque, que la ligne "
            "ci-dessous rappelle. Un seul fichier : le déplacer vers un "
            "disque plus grand se résume à le copier, puis à corriger le "
            "chemin ci-dessus. Les fichiers d'origine ne sont pas touchés."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #849396; font-size: 11px;")
        panel.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)

        self.input_archive = QLineEdit()
        self.input_archive.setPlaceholderText(r"C:\temp\deekeep-archive.db")
        self.input_archive.setToolTip(
            "Fichier de l'archive. Un seul fichier : le déplacer vers un "
            "autre disque se résume à le copier, puis à corriger ce chemin."
        )

        fields = (
            ("Fichier d'archive", self.input_archive),
        )
        for index, (label, widget) in enumerate(fields):
            row, column = divmod(index, 2)
            caption = QLabel(label)
            caption.setStyleSheet("color: #bac9cc; font-size: 12px;")
            grid.addWidget(caption, row, column * 2)
            grid.addWidget(widget, row, column * 2 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        panel.addLayout(grid)

        self.lbl_gauge = QLabel("")
        self.lbl_gauge.setWordWrap(True)
        self.lbl_gauge.setStyleSheet("color: #849396; font-size: 11px;")
        panel.addWidget(self.lbl_gauge)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        self.lbl_cloud_state = QLabel("")
        self.lbl_cloud_state.setWordWrap(True)
        self.lbl_cloud_state.setStyleSheet("color: #849396; font-size: 11px;")

        self.btn_cloud_test = QPushButton("Tester")
        self.btn_cloud_test.setProperty("class", "control-btn")
        self.btn_cloud_test.setToolTip(
            "Vérifier les identifiants et l'existence du seau, sans rien y "
            "écrire")
        self.btn_cloud_test.clicked.connect(self._test_cloud)

        self.btn_cloud_usage = QPushButton("Mesurer l'occupation")
        self.btn_cloud_usage.setProperty("class", "control-btn")
        self.btn_cloud_usage.setToolTip(
            "Compter les objets réellement présents dans le seau")
        self.btn_cloud_usage.clicked.connect(self._measure_usage)

        self.btn_cloud_save = QPushButton("Enregistrer")
        self.btn_cloud_save.setProperty("class", "accent-btn")
        self.btn_cloud_save.clicked.connect(self._save_cloud)

        buttons.addWidget(self.lbl_cloud_state, 1)
        buttons.addWidget(self.btn_cloud_test)
        buttons.addWidget(self.btn_cloud_usage)
        buttons.addWidget(self.btn_cloud_save)
        panel.addLayout(buttons)

        hint = QLabel(
            "Les clés sont rangées dans %APPDATA%\\DeeKeep\\bucket.json, hors "
            "de la base — celle-ci n'a pas à porter de quoi vider un seau. "
            "Les variables d'environnement DEEKEEP_S3_ENDPOINT, _BUCKET, "
            "_REGION, _KEY et _SECRET sont prioritaires."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #5f7d84; font-size: 11px;")
        panel.addWidget(hint)

        self._load_cloud_fields()
        return self.cloud_panel

    def _on_cloud_panel_toggled(self, opened):
        self.cloud_panel.setVisible(opened)
        self.btn_cloud_panel.setText("▾ Archive" if opened
                                     else "▸ Archive")

    # ==================================================================
    # Paramètres persistants
    # ==================================================================
    def sync_from_settings(self):
        """Relit l'emplacement de l'archive (à chaque affichage de la page).

        Il n'y a plus que cela à relire. Le débit, le choix de réencoder et
        la destination ont disparu avec le miroir Opus : un titre entre dans
        l'archive tel quel, et il n'y a rien à régler.
        """
        self._rendering = True
        try:
            self.input_archive.setText(sqlite_store.load_config()["root"])
        finally:
            self._rendering = False

    def _on_archive_changed(self, *_args):
        """Nouvel emplacement saisi : le magasin ouvert ne vaut plus."""
        if self._rendering:
            return
        self._store_cache = None
        self._refresh_gauge()
        self._refresh_controls()

    def _refresh_gauge(self):
        """Annonce ce que l'archive pèse, et ce qu'il reste de place.

        Il n'y a plus de débit à arbitrer ni d'offre gratuite à ne pas
        dépasser : la seule question est celle du disque.
        """
        if not self._entries:
            self.lbl_gauge.setText("")
            return
        self._refresh_archive_gauge(
            [entry for entry in self._entries if entry["exists"]])

    def _refresh_archive_gauge(self, present):
        """Ce que l'archive pèserait, face à la place réellement disponible.

        Pas d'offre gratuite à opposer ici : la seule limite est le disque.
        En revanche une mise en garde s'impose quand l'archive partage le
        volume de la bibliothèque — elle protège alors d'un effacement par
        mégarde, pas d'une panne.
        """
        poids = sum(int(entry.get("file_size") or 0) for entry in present)
        chemin = (self.input_archive.text().strip()
                  or sqlite_store.load_config()["root"])
        if not chemin:
            self.lbl_gauge.setText(
                "Indiquez le fichier d'archive : les %d titres présents sur "
                "le disque y entreraient tels quels, soit %s."
                % (len(present), human_bytes(poids)))
            self.lbl_gauge.setStyleSheet("font-size: 11px; color: %s;" % PRIMARY)
            return

        libre = sqlite_store.free_bytes(chemin)
        tient = libre > poids * 1.1
        memes = sqlite_store.same_device_as(
            chemin, present[0]["path"] if present else chemin)
        parts = [
            "Les %d titres présents sur le disque entreraient tels quels "
            "dans l'archive, soit %s — pour %s de libre sur son volume."
            % (len(present), human_bytes(poids), human_bytes(libre))
        ]
        if not tient:
            parts.append("La place ne suffit pas.")
        else:
            parts.append("Tous reviendraient octet pour octet.")
        if memes:
            parts.append("Attention : l'archive est sur le même volume que "
                         "la bibliothèque — elle ne survivrait pas à une "
                         "panne de ce disque.")
        self.lbl_gauge.setText(" ".join(parts))
        self.lbl_gauge.setStyleSheet(
            "font-size: 11px; color: %s;"
            % (PRIMARY if (not tient or memes) else TERTIARY))

    def _load_cloud_fields(self):
        self.input_archive.setText(sqlite_store.load_config()["root"])

    def _cloud_fields(self):
        """Ce que le volet décrit : l'emplacement de l'archive."""
        return {"root": self.input_archive.text().strip(),
                "prefix": sqlite_store.DEFAULT_PREFIX}

    def _store(self):
        """Magasin courant, partagé par l'écran. Lève ``BlobError`` sans lui."""
        if self._store_cache is None:
            self._store_cache = library_mirror.open_store(
                library_mirror.store_credentials())
        return self._store_cache

    def _save_cloud(self):
        self._store_cache = None
        sqlite_store.save_config(self.input_archive.text().strip())
        self._load_cloud_fields()
        self._set_cloud_state(
            "Archive enregistrée." if library_mirror.store_is_configured()
            else "Indiquez le fichier d'archive.")
        self._refresh_gauge()
        self._refresh_controls()

    def _test_cloud(self):
        if self._check_thread is not None and self._check_thread.isRunning():
            return
        self._set_cloud_state("Vérification de l'archive…")
        self._check_thread = library_mirror.CheckThread(
            self._cloud_fields(), self)
        self._check_thread.checked.connect(self._on_cloud_checked)
        self._check_thread.start()

    def _on_cloud_checked(self, ok, message):
        self._set_cloud_state(
            "Seau joignable : identifiants acceptés." if ok else message,
            error=not ok)

    def _measure_usage(self):
        if self._usage_thread is not None and self._usage_thread.isRunning():
            return
        self._set_cloud_state("Mesure de l'archive…")
        self._usage_thread = library_mirror.UsageThread(
            self._cloud_fields(), self)
        self._usage_thread.measured.connect(self._on_usage_measured)
        self._usage_thread.start()

    def _on_usage_measured(self, count, total, message):
        if message:
            self._set_cloud_state(message, error=True)
            return
        chemin = (self.input_archive.text().strip()
                  or sqlite_store.load_config()["root"])
        libre = sqlite_store.free_bytes(chemin) if chemin else 0
        self._set_cloud_state(
            "%d titres archivés, %s — %s de libre sur le volume."
            % (count, human_bytes(total), human_bytes(libre)))

    def _set_cloud_state(self, message, error=False):
        self.lbl_cloud_state.setText(message)
        self.lbl_cloud_state.setStyleSheet(
            "font-size: 11px; color: %s;" % (ERROR if error else TEXT_FAINT))

    # ==================================================================
    # Chargement des titres
    # ==================================================================
    def reload(self):
        """Relit la table des titres et celle du miroir, puis réaffiche."""
        db = local_db.shared()
        try:
            rows = db.query(
                "SELECT path, title, artist, album, album_artist, genre, "
                "year, duration, bitrate, codec, ext, file_size, source "
                "FROM tracks WHERE deleted = 0")
            mirror = {row["path"]: dict(row) for row in db.query(
                "SELECT path, sha1, stamp, key, bytes, bitrate, source_bytes "
                "FROM archive WHERE deleted = 0")}
        except Exception as exc:                # base illisible : on le dit
            self.lbl_status.setText("Base illisible : %s" % exc)
            return

        entries = []
        for row in rows:
            entry = dict(row)
            path = entry["path"] or ""
            # Un seul appel système par titre : il rend à la fois la
            # présence du fichier et son empreinte date+taille.
            try:
                info = os.stat(path)
                entry["exists"] = True
                entry["stamp"] = "%d:%d" % (int(info.st_mtime), info.st_size)
                entry["file_size"] = info.st_size
            except OSError:
                entry["exists"] = False
                entry["stamp"] = ""
            # Débit ignoré de la base — 198 lignes de la bibliothèque de
            # référence, durée comprise, entrées sans que leurs en-têtes
            # aient été lus. Le relire coûte 0,4 ms par fichier (en-tête
            # mutagen, sans sous-processus : 0,08 s pour les 198) et lève
            # deux travers : une jauge qui les compte comme incompressibles
            # alors que l'envoi, lui, les sonde et les réduirait, et une
            # colonne FORMAT amputée de son débit.
            if entry["exists"] and not (entry.get("bitrate") or 0):
                entry["bitrate"] = library_mirror.probe_bitrate(path) or 0
            entry["format"] = self._format_of(entry)
            entry["mirror"] = mirror.get(path)
            entries.append(entry)

        self._entries = entries
        self._by_path = {entry["path"]: entry for entry in entries}
        self._checked &= set(self._by_path)
        self._refresh_mirror_states()
        self._build_value_index()
        self._apply_query()

    @staticmethod
    def _format_of(entry):
        """Format lisible d'un fichier : « MP3 320 », « FLAC », « OPUS 128 »."""
        codec = (entry.get("codec") or entry.get("ext") or "").strip()
        codec = codec.lstrip(".").upper()
        rate = entry.get("bitrate") or 0
        if codec and rate:
            return "%s %d" % (codec, int(rate))
        return codec or ""

    def _refresh_mirror_states(self):
        """Recalcule l'état d'archive de chaque titre."""
        for entry in self._entries:
            self._refresh_entry_state(entry)

    def _refresh_entry_state(self, entry, rate=None, source_only=None):
        """État d'archive d'un seul titre.

        ``rate`` et ``source_only`` ne servent plus à rien ; ils restent
        acceptés pour que les appels existants n'aient pas à changer.
        """
        row = entry.get("mirror")
        entry["planned_bitrate"] = (entry.get("bitrate")
                                    or (row or {}).get("bitrate") or 0)
        # Tout entre tel quel : il n'y a plus de cas « réduit ».
        entry["as_is"] = True
        state = library_mirror.state_of(row, entry.get("stamp"))
        entry["state"] = state
        entry["cloud_order"] = {library_mirror.FRESH: 0,
                                library_mirror.STALE: 1}.get(state, 2)

    def _build_value_index(self):
        """Index des valeurs de tags présentes, pour les propositions.

        Construit une fois par chargement : proposer une valeur qui n'existe
        pas dans la bibliothèque n'aurait aucun intérêt, et compter les
        titres qu'elle rassemble permet de mettre en tête celle qui répond
        le plus probablement à la frappe.

        Une valeur n'apparaît **qu'une fois**, quel que soit le nombre de
        tags qui la portent : « Daft Punk » est presque toujours à la fois
        l'artiste et l'artiste d'album, et l'annoncer deux fois gâcherait
        deux des cinq places pour un filtre identique. La nature retenue est
        la première de ``SEARCH_FIELDS`` — artiste plutôt qu'artiste
        d'album.
        """
        counts = {}
        for entry in self._entries:
            if not self._selectable(entry):
                continue
            # Une même valeur portée par deux tags du même titre ne compte
            # qu'un titre : c'est bien un seul résultat qu'un clic donnera.
            best = {}
            for priority, (field, label) in enumerate(SEARCH_FIELDS):
                raw = str(entry.get(field) or "").strip()
                key = canonical(raw) if raw else ""
                if not key:
                    continue
                if key not in best or priority < best[key][0]:
                    best[key] = (priority, raw, label)
            for key, (priority, raw, label) in best.items():
                slot = counts.get(key)
                if slot is None:
                    counts[key] = {"label": label, "value": raw, "key": key,
                                   "count": 1, "priority": priority}
                elif priority < slot["priority"]:
                    slot.update(label=label, value=raw, priority=priority,
                                count=slot["count"] + 1)
                else:
                    slot["count"] += 1
        self._values = list(counts.values())
        # Tri de base : les valeurs les plus représentées d'abord, la nature
        # la plus parlante ensuite. Le classement d'une frappe donnée n'a
        # plus qu'à privilégier celles qui commencent par ce qui est tapé.
        self._values.sort(key=lambda item: (-item["count"], item["priority"],
                                            item["key"]))

    def _refilter(self):
        """Rejoue filtre et propositions après un changement d'état du miroir.

        Un simple rendu ne suffirait pas : retirer un titre du nuage peut le
        faire sortir de la liste (fichier absent du disque, plus de copie en
        ligne), et il n'a alors plus rien à proposer non plus.
        """
        self._refresh_mirror_states()
        self._build_value_index()
        self._apply_query()

    def _on_missing_changed(self, *_args):
        """Les titres absents entrent ou sortent — des lignes et des propositions."""
        self._build_value_index()
        self._apply_query()

    def _on_search_changed(self, text):
        self._filter_timer.start()
        stripped = text.strip()
        if len(stripped) < MIN_SUGGEST_CHARS:
            self._hide_suggestions()
            return
        self._show_suggestions(self._suggestions_for(stripped))

    def _on_search_return(self):
        """Entrée : la proposition retenue, sinon la frappe telle quelle."""
        item = self.list_suggest.currentItem()
        if self.list_suggest.isVisible() and item is not None:
            self._on_suggestion_clicked(item)
            return
        self._hide_suggestions()
        self._filter_timer.stop()
        self._apply_query()

    def _suggestions_for(self, text):
        """Les cinq valeurs de tags les plus probables pour cette frappe."""
        needle = canonical(text)
        if not needle:
            return []
        starts, contains = [], []
        for item in self._values:
            if item["key"].startswith(needle):
                starts.append(item)
            elif needle in item["key"]:
                contains.append(item)
            if len(starts) >= MAX_SUGGESTIONS:
                break
        # Une valeur qui commence par ce qui est tapé passe devant une valeur
        # qui ne fait que le contenir : « disc » propose « Discovery » avant
        # « Best of Disco ».
        return (starts + contains)[:MAX_SUGGESTIONS]

    def _show_suggestions(self, items):
        self.list_suggest.clear()
        self._suggestions = items
        if not items:
            self.list_suggest.hide()
            return
        for item in items:
            plural = "s" if item["count"] > 1 else ""
            row = QListWidgetItem("%s · %s   (%d titre%s)"
                                  % (item["label"], item["value"],
                                     item["count"], plural))
            row.setData(Qt.ItemDataRole.UserRole, item["value"])
            self.list_suggest.addItem(row)
        self.list_suggest.setCurrentRow(-1)
        self._position_suggestions()
        self.list_suggest.show()
        self.list_suggest.raise_()

    def _position_suggestions(self):
        """Pose la liste juste sous le champ, à sa largeur."""
        if self.list_suggest.count() == 0:
            return
        top_left = self.input_search.mapTo(
            self, self.input_search.rect().bottomLeft())
        height = (self.list_suggest.sizeHintForRow(0)
                  * self.list_suggest.count()) + 12
        self.list_suggest.setGeometry(
            top_left.x(), top_left.y() + 4,
            self.input_search.width(), height)

    def _hide_suggestions(self):
        self.list_suggest.hide()
        self.list_suggest.clear()
        self._suggestions = []

    def _on_suggestion_clicked(self, item):
        """Un clic (ou Entrée) sur une proposition filtre aussitôt."""
        value = item.data(Qt.ItemDataRole.UserRole) or item.text()
        self._hide_suggestions()
        self.input_search.blockSignals(True)
        self.input_search.setText(value)
        self.input_search.blockSignals(False)
        self._filter_timer.stop()
        self._apply_query()

    def eventFilter(self, source, event):
        """Flèches et Échap pilotent les propositions depuis le champ."""
        if source is self.input_search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape and self.list_suggest.isVisible():
                self._hide_suggestions()
                return True
            if (self.list_suggest.isVisible()
                    and key in (Qt.Key.Key_Down, Qt.Key.Key_Up)):
                count = self.list_suggest.count()
                step = 1 if key == Qt.Key.Key_Down else -1
                row = self.list_suggest.currentRow() + step
                self.list_suggest.setCurrentRow(max(0, min(row, count - 1)))
                return True
        return super().eventFilter(source, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.list_suggest.isVisible():
            self._position_suggestions()

    # ------------------------------------------------------------------
    def _haystack(self, entry):
        """Tous les tags d'un titre en une seule chaîne comparable.

        Calculée une fois par titre et conservée : c'est elle que la
        recherche parcourt, et la recalculer à chaque lettre tapée sur trois
        mille titres se verrait à l'écran.
        """
        cached = entry.get("_haystack")
        if cached is None:
            parts = [str(entry.get(field) or "") for field, _ in SEARCH_FIELDS]
            parts.append(os.path.basename(entry.get("path") or ""))
            cached = canonical(" ".join(parts))
            entry["_haystack"] = cached
        return cached

    def _selectable(self, entry):
        """Ce titre a-t-il sa place dans la liste ?

        Un titre disparu du disque mais présent dans le seau reste
        écoutable : il n'est masqué que si le nuage l'ignore aussi. Les
        propositions s'appuient sur la même règle — proposer une valeur qui
        n'afficherait aucune ligne serait une fausse piste.
        """
        if entry["exists"] or self.cb_missing.isChecked():
            return True
        return entry.get("state") == library_mirror.FRESH

    def _matches(self, entry, tokens):
        haystack = self._haystack(entry)
        return all(token in haystack for token in tokens)

    def _apply_query(self):
        """Applique la frappe courante et réaffiche le tableau."""
        self._query = self.input_search.text().strip()
        tokens = [token for token in canonical(self._query).split() if token]
        rows = []
        for entry in self._entries:
            if not self._selectable(entry):
                continue
            if tokens and not self._matches(entry, tokens):
                continue
            rows.append(entry)
        self._rows = rows
        self._render()

    # ==================================================================
    # Affichage
    # ==================================================================
    def _on_header_clicked(self, column):
        if column not in SORT_KEYS:
            return
        current, order = self._sort
        if column == current:
            order = (Qt.SortOrder.DescendingOrder
                     if order == Qt.SortOrder.AscendingOrder
                     else Qt.SortOrder.AscendingOrder)
        else:
            order = Qt.SortOrder.AscendingOrder
        self._sort = (column, order)
        self._render()

    def _sorted_rows(self):
        column, order = self._sort
        key = SORT_KEYS.get(column, "artist")

        def sort_key(entry):
            value = entry.get(key)
            if key in ("duration", "cloud_order"):
                return (float(value or 0),)
            return (canonical(str(value or "")) or "￿",)

        rows = sorted(self._rows, key=sort_key,
                      reverse=order == Qt.SortOrder.DescendingOrder)
        return rows

    def _render(self):
        """Reconstruit le tableau. Les signaux sont coupés le temps du remplissage."""
        self._refresh_mirror_states()
        rows = self._sorted_rows()
        self._rows = rows

        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            self.table.setRowCount(len(rows))
            self._row_of = {entry["path"]: index
                            for index, entry in enumerate(rows)}
            for index, entry in enumerate(rows):
                self._fill_row(index, entry)
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)

        header = self.table.horizontalHeader()
        header.setSortIndicator(self._sort[0], self._sort[1])
        self._refresh_summary()
        self._refresh_gauge()
        self._refresh_controls()

    def _fill_row(self, row, entry):
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                       | Qt.ItemFlag.ItemIsEnabled)
        check.setCheckState(Qt.CheckState.Checked
                            if entry["path"] in self._checked
                            else Qt.CheckState.Unchecked)
        self.table.setItem(row, LC_CHECK, check)

        playable = entry["exists"] or entry.get("state") == library_mirror.FRESH
        tooltip = ("Écouter ce titre" if entry["exists"] else
                   "Écouter la copie en ligne (fichier absent du disque)")
        self.table.setItem(row, LC_PLAY, player.play_item(
            tooltip=tooltip if playable else
            "Fichier absent du disque, et pas de copie en ligne",
            enabled=playable))

        values = {
            LC_TITLE: entry.get("title") or os.path.basename(entry["path"]),
            LC_ARTIST: entry.get("artist") or "",
            LC_ALBUM: entry.get("album") or "",
            LC_GENRE: entry.get("genre") or "",
            LC_YEAR: entry.get("year") or "",
            LC_DURATION: mmss(entry.get("duration")),
            LC_FORMAT: entry.get("format") or "",
            LC_PATH: entry["path"],
        }
        for column, text in values.items():
            item = QTableWidgetItem(str(text))
            item.setToolTip(str(text))
            if not entry["exists"]:
                item.setForeground(QColor(TEXT_FAINT))
            self.table.setItem(row, column, item)

        self.table.setItem(row, LC_CLOUD, self._cloud_item(entry))

    def _cloud_item(self, entry):
        """Cellule NUAGE : ce que le seau détient de ce titre."""
        state = entry.get("state")
        mirror = entry.get("mirror") or {}
        if state == library_mirror.FRESH:
            text = "✓ %s" % human_bytes(mirror.get("bytes"))
            comment = ("copié tel quel (déjà à %s kb/s)"
                       if not str(mirror.get("key") or "").endswith(".opus")
                       else "réencodé en Opus à %s kb/s")
            color, tip = TERTIARY, (
                "En miroir : %s, %s.\nClé : %s"
                % (human_bytes(mirror.get("bytes")),
                   comment % mirror.get("bitrate"), mirror.get("key")))
        elif state == library_mirror.STALE:
            text, color = "à renvoyer", PRIMARY
            tip = ("Une copie existe en ligne, mais le fichier local a "
                   "changé depuis — ou le débit demandé n'est plus le même.")
        else:
            text, color = "—", TEXT_FAINT
            tip = "Ce titre n'est pas dans le seau."
        item = QTableWidgetItem(text)
        item.setForeground(QColor(color))
        item.setToolTip(tip)
        return item

    def _set_status_cell(self, entry, text, color=TEXT_MUTED):
        """Écrit dans la cellule NUAGE d'un titre, s'il est encore affiché."""
        row = self._row_of.get(entry["path"])
        if row is None:
            return
        item = QTableWidgetItem(text)
        item.setForeground(QColor(color))
        self._replace_cloud_cell(row, item)

    def _refresh_cloud_cell(self, entry):
        """Réécrit la cellule NUAGE d'un titre d'après son état courant."""
        row = self._row_of.get(entry["path"])
        if row is None:
            return
        self._replace_cloud_cell(row, self._cloud_item(entry))

    def _replace_cloud_cell(self, row, item):
        self.table.blockSignals(True)
        try:
            self.table.setItem(row, LC_CLOUD, item)
        finally:
            self.table.blockSignals(False)

    def _refresh_summary(self):
        total = len(self._entries)
        shown = len(self._rows)
        on_disk = sum(1 for entry in self._entries if entry["exists"])
        disk_bytes = sum(int(entry.get("file_size") or 0)
                         for entry in self._entries if entry["exists"])
        mirrored = [entry for entry in self._entries
                    if entry.get("state") == library_mirror.FRESH]
        mirror_bytes = sum(int((entry.get("mirror") or {}).get("bytes") or 0)
                           for entry in mirrored)
        # Poids d'origine des seuls titres en miroir : comparer les 2 Go du
        # miroir aux 14 Go du disque entier ne dirait rien du réencodage.
        source_bytes = sum(
            int((entry.get("mirror") or {}).get("source_bytes") or 0)
            for entry in mirrored)
        stale = sum(1 for entry in self._entries
                    if entry.get("state") == library_mirror.STALE)

        parts = ["%d titres affichés sur %d" % (shown, total),
                 "%d sur le disque (%s)" % (on_disk, human_bytes(disk_bytes))]
        if mirrored:
            share = (100.0 * mirror_bytes / source_bytes) if source_bytes else 0
            parts.append("%d en miroir (%s, soit %s %% du poids d'origine)"
                         % (len(mirrored), human_bytes(mirror_bytes),
                            ("%.0f" % share)))
        else:
            parts.append("aucun titre en miroir")
        if stale:
            parts.append("%d à renvoyer" % stale)
        self.lbl_status.setText(" · ".join(parts))

    def _refresh_controls(self):
        # Les deux sens du miroir se partagent la barre de progression et la
        # liste : ils ne peuvent pas tourner ensemble, et « Arrêter » vaut
        # pour celui des deux qui travaille.
        running = self._busy()
        configured = library_mirror.store_is_configured()
        self.btn_mirror.setEnabled(not running and configured)
        self.btn_restore.setEnabled(not running and configured)
        self.btn_stop.setEnabled(running)
        self.btn_refresh.setEnabled(not running)
        self.btn_mirror.setToolTip(
            "Copier les titres cochés dans l'archive, tels quels — aucun "
            "réencodage, rien n'est perdu"
            if configured else
            "Aucune archive configurée : ouvrez le volet « Archive » et "
            "indiquez le fichier où ranger les titres.")
        self.btn_restore.setToolTip(
            "Redescendre du seau les titres qui ne sont plus sur le disque. "
            "Rien n'est écrasé : seuls les fichiers absents sont écrits."
            if configured else unconfigured)

    def _unconfigured_text(self):
        """Ce qui manque pour que la destination courante soit utilisable."""
        return ("Aucune archive configurée. Ouvrez le volet « Archive » et "
                "indiquez le fichier où ranger les titres, puis "
                "« Enregistrer ».")

    def _busy(self):
        """Vrai si l'un des deux sens du miroir est en cours."""
        return any(thread is not None and thread.isRunning()
                   for thread in (self._mirror_thread, self._restore_thread))

    # ==================================================================
    # Sélection
    # ==================================================================
    def _on_item_changed(self, item):
        if item.column() != LC_CHECK:
            return
        row = item.row()
        if not 0 <= row < len(self._rows):
            return
        path = self._rows[row]["path"]
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(path)
        else:
            self._checked.discard(path)

    def _set_all_checked(self, checked):
        """Coche ou décoche les titres affichés — et eux seuls."""
        self.table.blockSignals(True)
        try:
            for row, entry in enumerate(self._rows):
                item = self.table.item(row, LC_CHECK)
                if item is None:
                    continue
                item.setCheckState(Qt.CheckState.Checked if checked
                                   else Qt.CheckState.Unchecked)
                if checked:
                    self._checked.add(entry["path"])
                else:
                    self._checked.discard(entry["path"])
        finally:
            self.table.blockSignals(False)

    def _selected_entries(self):
        """Titres cochés parmi ceux affichés, dans l'ordre du tableau."""
        return [entry for entry in self._rows
                if entry["path"] in self._checked]

    # ==================================================================
    # Écoute
    # ==================================================================
    def _on_cell_clicked(self, row, column):
        if column == LC_PLAY:
            self._play(row)

    def _on_cell_double_clicked(self, row, _column):
        self._play(row)

    def _play(self, row):
        """Ouvre le lecteur sur cette ligne, la liste affichée en file d'attente."""
        if not 0 <= row < len(self._rows):
            return
        tracks, index = [], 0
        for position, entry in enumerate(self._rows):
            track = self._track_for(entry)
            if track is None:
                continue
            if position == row:
                index = len(tracks)
            tracks.append(track)
        if not tracks:
            return
        entry = self._rows[row]
        if self._track_for(entry) is None:
            QMessageBox.information(
                self, "Bibliothèque",
                "Ce titre n'est ni sur le disque ni dans le seau : il n'y a "
                "rien à écouter.")
            return
        player.play_tracks(self, tracks, index)

    def _track_for(self, entry):
        """Entrée du lecteur : le fichier local, sinon la copie en ligne."""
        subtitle = " · ".join(part for part in (
            entry.get("format"), entry.get("year"), entry.get("genre"))
            if part)
        if entry["exists"]:
            return player.make_track(
                path=entry["path"], title=entry.get("title") or "",
                artist=entry.get("artist") or "",
                album=entry.get("album") or "", subtitle=subtitle)
        if entry.get("state") != library_mirror.FRESH:
            return None
        # Fichier disparu, copie en ligne à jour : le lecteur joue le flux
        # servi par le seau. L'URL est signée à la demande et vaut une heure.
        try:
            url = self._store().presigned_get(entry["mirror"]["key"])
        except sqlite_store.BlobError:
            return None
        return player.make_track(
            url=url, title=entry.get("title") or "",
            artist=entry.get("artist") or "", album=entry.get("album") or "",
            subtitle=(subtitle + " · copie en ligne").strip(" ·"))

    # ==================================================================
    # Menu contextuel
    # ==================================================================
    def _show_context_menu(self, point):
        row = self.table.rowAt(point.y())
        if not 0 <= row < len(self._rows):
            return
        entry = self._rows[row]
        menu = QMenu(self)
        listen = menu.addAction("Écouter dans le lecteur",
                                lambda: self._play(row))
        listen.setEnabled(entry["exists"]
                          or entry.get("state") == library_mirror.FRESH)
        folder = menu.addAction(
            "Ouvrir le dossier", lambda: reveal_in_explorer(entry["path"]))
        folder.setEnabled(entry["exists"])
        menu.addSeparator()
        send = menu.addAction("Envoyer ce titre au nuage",
                              lambda: self._mirror_paths([entry]))
        pret = library_mirror.store_is_configured()
        send.setEnabled(entry["exists"] and pret)
        back = menu.addAction("Restaurer ce titre depuis le nuage",
                              lambda: self._restore_entries([entry]))
        back.setEnabled(self._restorable(entry) and pret)
        drop = menu.addAction("Retirer du nuage",
                              lambda: self._drop_from_mirror(entry))
        drop.setEnabled(bool(entry.get("mirror")))
        menu.addSeparator()
        menu.addAction("Copier le chemin",
                       lambda: QApplication.clipboard().setText(entry["path"]))
        menu.exec(self.table.viewport().mapToGlobal(point))

    def _drop_from_mirror(self, entry):
        """Efface du seau la copie d'un titre, et la ligne qui la décrit."""
        row = entry.get("mirror")
        if not row:
            return
        try:
            store = self._store()
        except sqlite_store.BlobError as exc:
            QMessageBox.warning(self, "Archive", str(exc))
            return
        ok, message = library_mirror.remove_from_mirror(store, row)
        if not ok:
            QMessageBox.warning(self, "Archive", message)
            return
        local_db.shared().mark_deleted("archive", [row["path"]])
        entry["mirror"] = None
        self._refilter()

    # ==================================================================
    # Miroir Opus
    # ==================================================================
    def _start_mirror(self):
        """Envoie les titres cochés — ou tous ceux affichés, à défaut."""
        entries = self._selected_entries()
        if not entries:
            answer = QMessageBox.question(
                self, "Archiver",
                "Aucun titre coché. Envoyer les %d titres affichés ?"
                % len(self._rows))
            if answer != QMessageBox.StandardButton.Yes:
                return
            entries = list(self._rows)
        self._mirror_paths(entries)

    def _mirror_paths(self, entries):
        if self._busy():
            return
        if not library_mirror.store_is_configured():
            QMessageBox.information(self, "Archive", self._unconfigured_text())
            self.btn_cloud_panel.setChecked(True)
            return

        todo = [entry for entry in entries if entry["exists"]]
        if not todo:
            QMessageBox.information(
                self, "Archive",
                "Aucun des titres retenus n'est présent sur le disque : il "
                "n'y a rien à réencoder.")
            return

        known = {entry["path"]: entry.get("mirror") for entry in todo
                 if entry.get("mirror")}
        for entry in todo:
            self._set_status_cell(entry, "en attente…", TEXT_FAINT)

        self.progress.setRange(0, len(todo))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._set_cloud_state("Envoi de %d titres en cours…" % len(todo))

        self._mirror_thread = library_mirror.MirrorThread(
            [(entry["path"], entry.get("bitrate")) for entry in todo],
            known=known,
            credentials=library_mirror.store_credentials(), parent=self)
        self._mirror_thread.done.connect(self._on_mirror_done)
        self._mirror_thread.progress.connect(self._on_mirror_progress)
        self._mirror_thread.completed.connect(self._on_mirror_completed)
        self._mirror_thread.start()
        self._refresh_controls()

    def _stop_work(self):
        """Arrête l'envoi ou la restauration, selon celui qui tourne."""
        for thread in (self._mirror_thread, self._restore_thread):
            if thread is not None and thread.isRunning():
                thread.stop()
                self._set_cloud_state("Arrêt demandé : le titre en cours est "
                                      "abandonné.")

    def _on_mirror_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def _on_mirror_done(self, path, state, row, message):
        """Un titre traité : la ligne est écrite en base, la cellule mise à jour."""
        entry = self._by_path.get(path)
        if entry is None:
            return
        if row:
            # C'est le fil de l'interface qui écrit : la ligne part ainsi
            # dans la file de synchronisation comme n'importe quelle autre
            # écriture de l'application (voir ``local_db.upsert``).
            local_db.shared().upsert("archive", row)
            entry["mirror"] = dict(row)
        self._refresh_entry_state(entry)
        if state == library_mirror.MirrorThread.FAILED:
            self._set_status_cell(entry, "échec", ERROR)
            self.lbl_status.setText("%s — %s"
                                    % (os.path.basename(path), message))
        else:
            self._refresh_cloud_cell(entry)

    def _on_mirror_completed(self, sent, skipped, failed, uploaded):
        self.progress.setVisible(False)
        self._mirror_thread = None
        parts = ["%d titres envoyés (%s)" % (sent, human_bytes(uploaded))]
        if skipped:
            parts.append("%d déjà à jour" % skipped)
        if failed:
            parts.append("%d en échec" % failed)
        self._set_cloud_state(" · ".join(parts),
                              error=bool(failed) and not sent)
        self._refilter()

    # ==================================================================
    # Restauration
    # ==================================================================
    def _restorable(self, entry):
        """Ce titre peut-il redescendre du seau ?

        Il le peut si le seau en détient une copie et si le fichier qu'elle
        rendrait n'est pas déjà là. La destination n'est pas toujours le
        chemin d'origine — une copie réduite en Opus revient à côté, sous
        son extension (voir ``library_mirror.restore_target``) — et c'est bien
        elle qu'il faut regarder : c'est elle qui serait écrite.

        L'état de miroir (à jour, à renvoyer) n'entre pas en compte. Il dit
        si le seau reflète le disque ; ici le disque n'a plus rien à
        refléter, et un objet reste parfaitement restaurable même quand le
        débit demandé a changé depuis son envoi.
        """
        row = entry.get("mirror") or {}
        if not row.get("key"):
            return False
        destination, _identical = library_mirror.restore_target(
            entry["path"], row["key"])
        return not os.path.exists(destination)

    def _start_restore(self):
        """Rend les titres manquants — cochés, ou tous ceux affichés."""
        chosen = self._selected_entries()
        entries = chosen or list(self._rows)
        todo = [entry for entry in entries if self._restorable(entry)]

        if not todo:
            # Un titre disparu du disque ne s'affiche que si le nuage en a
            # une copie à jour : au débit courant, ceux dont la copie est
            # « à renvoyer » sont masqués — et ce sont peut-être justement
            # ceux qu'on cherche à récupérer. Le dire, plutôt que de laisser
            # croire qu'il n'y a rien.
            elsewhere = sum(1 for entry in self._entries
                            if self._restorable(entry))
            if elsewhere:
                QMessageBox.information(
                    self, "Restaurer",
                    "Aucun des titres affichés n'est à restaurer, mais %d le "
                    "sont ailleurs dans la bibliothèque. Cochez « Titres "
                    "absents du disque », ou videz la recherche, pour les "
                    "faire apparaître." % elsewhere)
            else:
                QMessageBox.information(
                    self, "Restaurer",
                    "Rien à restaurer : tous les titres que le seau détient "
                    "sont déjà sur le disque.")
            return

        if not chosen:
            answer = QMessageBox.question(
                self, "Restaurer",
                "Aucun titre coché. Restaurer les %d titres manquants parmi "
                "les %d affichés ?" % (len(todo), len(self._rows)))
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._restore_entries(todo)

    def _restore_entries(self, entries):
        if self._busy():
            return
        if not library_mirror.store_is_configured():
            QMessageBox.information(self, "Archive", self._unconfigured_text())
            self.btn_cloud_panel.setChecked(True)
            return

        todo = [entry for entry in entries if self._restorable(entry)]
        if not todo:
            return
        for entry in todo:
            self._set_status_cell(entry, "en attente…", TEXT_FAINT)

        self.progress.setRange(0, len(todo))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._set_cloud_state("Restauration de %d titres en cours…" % len(todo))

        self._restore_thread = library_mirror.RestoreThread(
            [entry["mirror"] for entry in todo],
            credentials=library_mirror.store_credentials(), parent=self)
        self._restore_thread.done.connect(self._on_restore_done)
        self._restore_thread.progress.connect(self._on_restore_progress)
        self._restore_thread.completed.connect(self._on_restore_completed)
        self._restore_thread.start()
        self._refresh_controls()

    def _on_restore_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def _on_restore_done(self, path, state, row, message):
        """Un titre rendu : la ligne de miroir est rafraîchie si besoin."""
        entry = self._by_path.get(path)
        if entry is None:
            return
        if row:
            # Le fichier est de retour avec une date de modification neuve :
            # sans cette empreinte remise à jour, tout ce qui vient d'être
            # restauré se donnerait pour « à renvoyer » et repartirait vers
            # le seau alors qu'il s'y trouve déjà, à l'identique.
            local_db.shared().upsert("archive", row)
            entry["mirror"] = dict(row)
            entry["stamp"] = row.get("stamp") or ""
        if state == library_mirror.RestoreThread.FAILED:
            self._set_status_cell(entry, "échec", ERROR)
            self.lbl_status.setText("%s — %s"
                                    % (os.path.basename(path), message))
        elif state == library_mirror.RestoreThread.DEGRADED:
            self._set_status_cell(entry, "converti", PRIMARY)
        elif state == library_mirror.RestoreThread.RESTORED:
            self._set_status_cell(entry, "rendu", TERTIARY)

    def _on_restore_completed(self, restored, degraded, skipped, failed,
                              received):
        self.progress.setVisible(False)
        self._restore_thread = None
        parts = ["%d titres rendus à l'identique (%s)"
                 % (restored, human_bytes(received))]
        if degraded:
            parts.append("%d convertis en MP3 depuis la copie Opus, "
                         "l'original étant perdu" % degraded)
        if skipped:
            parts.append("%d déjà sur le disque" % skipped)
        if failed:
            parts.append("%d en échec" % failed)
        self._set_cloud_state(" · ".join(parts),
                              error=bool(failed) and not restored)
        # Relecture complète : les titres rendus ont retrouvé une taille et
        # une date, et ce sont elles qui décident de leur état.
        self.reload()

