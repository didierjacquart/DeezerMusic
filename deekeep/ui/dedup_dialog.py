"""
Recherche et écran de déduplication des fichiers audio.

``duplicate_groups`` rapproche les fichiers qui paraissent être le même titre
— nom de fichier ou couple titre/artiste proches — et le dialogue affiche les
groupes obtenus. L'utilisateur coche les fichiers à supprimer ; par défaut, le
meilleur exemplaire de chaque groupe est conservé et les autres sont
présélectionnés pour suppression.

Ce rapprochement est *approximatif* : il ne demande ni tags complets ni
destination commune, et retrouve « Ain't Nobody » et « Ain’t Nobody (Loves Me
Better) - Felix Jaehn Feat. Jasmine Thompson » là où le plan de rangement,
qui compare des destinations, ne voit que deux fichiers distincts. Les deux se
complètent : l'écran « Organiser » supprime les copies dont il est certain,
cette recherche propose les autres à l'examen.

Le dialogue ne supprime rien lui-même : il renvoie la liste des chemins
choisis via ``get_files_to_delete()`` ; la suppression effective (avec
confirmation) est réalisée par l'appelant.

``LibraryDuplicateDialog`` répond au même besoin depuis l'autre bout : non
plus deux fichiers du disque rapprochés entre eux, mais une piste que la
recherche Deezer propose et le ou les fichiers que la bibliothèque contient
déjà (pastille verte ou orange de ``library_index``). Elle montre ce que
l'index a rapproché — motif, qualité, emplacement — et laisse choisir les
fichiers à supprimer pour retélécharger proprement. Elle rend aussi ses
chemins via ``get_files_to_delete()`` : là encore, l'appelant supprime.
"""

import os
import re

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QCheckBox, QApplication, QMessageBox, QTreeWidget, QTreeWidgetItem,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from deekeep.audio import reencode_scan
from deekeep.library import library_index
from deekeep.ui.styles import STYLE_SHEET


# ----------------------------------------------------------------------
# Rapprochement approximatif
# ----------------------------------------------------------------------
def canonical(text):
    """Clé normalisée pour le rapprochement approximatif."""
    s = (text or "").lower()
    # Marqueurs de copie / suffixes numériques entre parenthèses : « (1) », « (copie) »
    s = re.sub(r'[\(\[]\s*(copy|copie|\d+)\s*[\)\]]', ' ', s)
    s = re.sub(r'\b(copy|copie)\b', ' ', s)
    # Numéro de piste en tête (« 03 - », « 03. »)
    s = re.sub(r'^\s*\d{1,3}\s*[-\._]\s*', ' ', s)
    # Ne conserver que lettres et chiffres
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _keys(entry, artist_key=None):
    """Clés de rapprochement d'un fichier (nom de fichier et métadonnées)."""
    keys = set()
    base = os.path.splitext(os.path.basename(entry.get("path", "")))[0]
    from_name = canonical(base)
    if from_name:
        keys.add("f:" + from_name)
    title = canonical(entry.get("title", ""))
    artist = canonical(entry.get("artist", ""))
    if title and artist:
        keys.add("m:" + artist + "|" + title)
    # Même titre, même artiste *principal* : c'est ce qui rapproche « Ain't
    # Nobody (Loves Me Better) - Felix Jaehn » de la version signée « Felix
    # Jaehn Feat. Jasmine Thompson », que ni le nom de fichier ni le couple
    # titre/artiste ne réunissent — l'invité suffit à tout distinguer.
    if title and artist_key is not None:
        main = artist_key(entry)
        if main:
            keys.add("a:" + main + "|" + title)
    return keys


def duplicate_groups(entries, artist_key=None):
    """Groupes d'au moins deux fichiers rapprochés (union-find sur les clés).

    Deux fichiers partageant une clé forment un groupe, et la transitivité
    joue : A rapproché de B par son nom et de C par ses tags réunit les trois.

    ``artist_key`` — une fonction entrée → artiste principal normalisé — n'est
    pas obligatoire ; l'écran « Organiser » lui passe la sienne, celle qui
    range « Inna Feat. Flo Rida » chez « Inna ».
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    total = len(entries)
    for i in range(total):
        find(i)
    first_seen = {}
    for i, entry in enumerate(entries):
        for key in _keys(entry, artist_key):
            if key in first_seen:
                union(first_seen[key], i)
            else:
                first_seen[key] = i

    groups = {}
    for i in range(total):
        groups.setdefault(find(i), []).append(entries[i])
    return [group for group in groups.values() if len(group) >= 2]


class DeduplicationDialog(QDialog):
    def __init__(self, groups, parent=None, target_format=None):
        super().__init__(parent)
        self.setWindowTitle("Déduplication des fichiers")
        self.setStyleSheet(STYLE_SHEET)
        self.target_format = (target_format or "").upper()
        self.setMinimumSize(900, 560)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.resize(int(avail.width() * 0.8), int(avail.height() * 0.8))

        self.groups = groups
        self._checks = []  # (QCheckBox, path)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel("Fichiers en double")
        title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 22px; "
            "font-weight: bold; color: #00e5ff;"
        )
        layout.addWidget(title)

        n_files = sum(len(g) for g in groups)
        target_txt = f" ({self.target_format})" if self.target_format else ""
        sub = QLabel(
            f"{n_files} fichiers dans {len(groups)} groupe(s). Cochez ceux à "
            "supprimer. Par défaut, chaque groupe conserve (décoché) l'exemplaire "
            f"au format cible{target_txt}, puis celui rangé dans le dossier de "
            "l'artiste, puis l'emplacement le moins profond, puis le meilleur débit."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #849396; font-size: 12px;")
        layout.addWidget(sub)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["SUPPR.", "GROUPE", "TITRE", "ARTISTE", "FORMAT / DÉBIT", "FICHIER"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.verticalHeader().setDefaultSectionSize(46)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(2, 220)
        self.table.setColumnWidth(3, 160)
        layout.addWidget(self.table, 1)

        self._populate()

        # Barre de boutons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        btn_layout.addStretch()
        self.btn_cancel = QPushButton("Annuler")
        self.btn_cancel.setProperty("class", "control-btn")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_delete = QPushButton("Supprimer les fichiers cochés")
        self.btn_delete.setProperty("class", "danger-btn")
        self.btn_delete.clicked.connect(self._confirm_delete)
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_delete)
        layout.addLayout(btn_layout)

    @staticmethod
    def _fmt_label(item):
        fmt = item.get("format", "") or "?"
        br = item.get("bitrate", 0)
        return f"{fmt} · {br} kbps" if br else fmt

    @staticmethod
    def _norm(text):
        s = (text or "").lower()
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def _in_artist_folder(self, item):
        """Vrai si un dossier parent porte EXACTEMENT le nom de l'artiste.

        La correspondance est stricte (nom de dossier == artiste, casse et
        ponctuation ignorées) : un dossier d'album tel que « Shakira -
        Addicted To You 2012 » n'est PAS considéré comme le dossier de
        l'artiste, pas plus qu'un fourre-tout comme « Divers ».
        """
        artist = self._norm(item.get("artist", ""))
        if not artist:
            return False
        parent = os.path.dirname((item.get("path", "") or "").replace("\\", "/"))
        for comp in re.split(r"[\\/]+", parent):
            if self._norm(comp) == artist:
                return True
        return False

    def _keep_score(self, item):
        """Score de conservation (le plus élevé est gardé) :
        1) format déjà égal au format cible des paramètres ;
        2) fichier situé dans le dossier de l'artiste (ou un sous-dossier) ;
        3) emplacement le moins profond (copie « principale » de la
           bibliothèque plutôt qu'une copie enfouie, ex. dans « Divers ») ;
        4) meilleur débit ; 5) chemin le plus court.
        """
        path = (item.get("path", "") or "").replace("\\", "/")
        fmt = (item.get("format") or "").upper()
        is_target = 1 if (self.target_format and fmt == self.target_format) else 0
        in_artist = 1 if self._in_artist_folder(item) else 0
        depth = path.count("/")
        bitrate = item.get("bitrate", 0) or 0
        return (is_target, in_artist, -depth, bitrate, -len(path))

    def _best_index(self, group):
        """Indice de l'exemplaire à conserver dans un groupe de doublons."""
        best, best_score = 0, self._keep_score(group[0])
        for i in range(1, len(group)):
            score = self._keep_score(group[i])
            if score > best_score:
                best, best_score = i, score
        return best

    def _populate(self):
        row = 0
        for gi, group in enumerate(self.groups, start=1):
            keep = self._best_index(group)
            for idx, item in enumerate(group):
                self.table.insertRow(row)

                container = QWidget()
                lay = QHBoxLayout(container)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cb = QCheckBox()
                # Présélectionner tout sauf le meilleur exemplaire du groupe.
                cb.setChecked(idx != keep)
                lay.addWidget(cb)
                self.table.setCellWidget(row, 0, container)
                self._checks.append((cb, item.get("path", "")))

                grp_item = QTableWidgetItem(str(gi) + (" ★" if idx == keep else ""))
                grp_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if idx == keep:
                    grp_item.setToolTip("Exemplaire conservé par défaut (meilleure qualité)")
                self.table.setItem(row, 1, grp_item)

                self.table.setItem(row, 2, QTableWidgetItem(item.get("title", "")))
                self.table.setItem(row, 3, QTableWidgetItem(item.get("artist", "")))
                self.table.setItem(row, 4, QTableWidgetItem(self._fmt_label(item)))
                path_item = QTableWidgetItem(item.get("path", ""))
                path_item.setToolTip(item.get("path", ""))
                self.table.setItem(row, 5, path_item)
                row += 1

    def get_files_to_delete(self):
        return [path for cb, path in self._checks if cb.isChecked()]

    def _confirm_delete(self):
        paths = self.get_files_to_delete()
        if not paths:
            QMessageBox.information(
                self, "Aucune sélection", "Cochez au moins un fichier à supprimer."
            )
            return
        reply = QMessageBox.question(
            self, "Confirmer la suppression",
            f"Supprimer définitivement {len(paths)} fichier(s) du disque ?\n"
            "Cette action est irréversible.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.accept()


# ----------------------------------------------------------------------
# Doublons vus depuis la recherche (piste Deezer ↔ fichiers du disque)
# ----------------------------------------------------------------------
def human_size(size):
    """Taille lisible (« 8,4 Mo »), à la française."""
    value = float(size or 0)
    for unit in ("o", "Ko", "Mo", "Go"):
        if value < 1024 or unit == "Go":
            text = f"{value:.0f}" if unit == "o" else f"{value:.1f}"
            return f"{text} {unit}".replace(".", ",")
        value /= 1024


def file_quality(path):
    """« MP3 · 320 kbps · 8,4 Mo » pour un fichier, ce qu'on en sait sinon.

    Le débit n'est lu que pour les quelques fichiers cités par cet écran : le
    cache de ``reencode_scan`` fait que le prix n'est payé qu'une fois.
    """
    fmt = os.path.splitext(path)[1].lstrip(".").upper() or "?"
    try:
        size = os.path.getsize(path)
    except OSError:
        return "fichier introuvable"
    try:
        bitrate = reencode_scan.read_tags(path, need_bitrate=True).get("bitrate", 0)
    except Exception:
        bitrate = 0
    parts = [fmt]
    if bitrate:
        parts.append(f"{bitrate} kbps")
    parts.append(human_size(size))
    return " · ".join(parts)


def file_score(path):
    """Score de conservation d'un fichier (le plus élevé est gardé).

    Une suppression est irréversible : la qualité passe d'abord — meilleur
    débit, puis fichier le plus gros (à débit égal, la différence tient à la
    pochette ou aux tags) —, puis le rangement : l'exemplaire le moins enfoui,
    au chemin le plus court.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    try:
        bitrate = reencode_scan.read_tags(path, need_bitrate=True).get("bitrate", 0)
    except Exception:
        bitrate = 0
    normalized = (path or "").replace(os.sep, "/")
    return (bitrate or 0, size, -normalized.count("/"), -len(normalized))


class LibraryDuplicateDialog(QDialog):
    """Doublons relevés par une recherche, et fichiers à supprimer.

    ``candidates`` est une liste de couples ``{"track": …, "match": …}`` : la
    piste que Deezer propose, et le verdict de ``library_index`` qui la dit
    déjà présente (vert) ou ressemblante (orange). Chaque piste forme un
    groupe, et sous elle viennent les fichiers du disque que l'index a
    rapprochés — motif du rapprochement, qualité, emplacement.

    Un même fichier peut répondre de deux pistes voisines (deux versions d'un
    même morceau, par exemple) : il n'est montré que sous la première, faute de
    quoi deux cases décideraient du sort d'un seul fichier.

    Rien n'est coché d'avance quand un seul fichier répond d'une piste : sa
    suppression est un choix, pas une évidence — le rapprochement peut désigner
    une version que l'on garde volontiers. Quand plusieurs fichiers répondent
    de la même piste, ils sont en double entre eux : tous sont cochés sauf le
    meilleur exemplaire.
    """

    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Doublons de la bibliothèque")
        self.setStyleSheet(STYLE_SHEET)
        self.setMinimumSize(900, 520)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.resize(int(avail.width() * 0.8), int(avail.height() * 0.75))

        self._items = []           # (ligne du fichier, chemin)
        self.groups = self._build_groups(candidates)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel("Doublons de la bibliothèque")
        title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 22px; "
            "font-weight: bold; color: #00e5ff;"
        )
        layout.addWidget(title)

        n_files = sum(len(group["files"]) for group in self.groups)
        sub = QLabel(
            f"{len(self.groups)} piste(s) de la recherche répondent à "
            f"{n_files} fichier(s) déjà sur le disque. Cochez les fichiers à "
            "supprimer : la piste redevient alors sélectionnable dans la "
            "recherche, prête à être extraite de nouveau. Les pistes dont un "
            "seul fichier répond ne sont pas cochées d'avance — une "
            "ressemblance n'est pas une preuve."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #849396; font-size: 12px;")
        layout.addWidget(sub)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(
            ["PISTE DEEZER / FICHIERS DU DISQUE", "QUALITÉ", "MOTIF / EMPLACEMENT"]
        )
        self.tree.setUniformRowHeights(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(0, 420)
        layout.addWidget(self.tree, 1)

        self._populate()

        # Barre de boutons : les outils de masse à gauche, la décision à droite.
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        self.btn_check_all = QPushButton("Tout cocher")
        self.btn_check_all.setProperty("class", "control-btn")
        self.btn_check_all.clicked.connect(lambda: self._set_all(True))
        self.btn_check_none = QPushButton("Tout décocher")
        self.btn_check_none.setProperty("class", "control-btn")
        self.btn_check_none.clicked.connect(lambda: self._set_all(False))
        btn_layout.addWidget(self.btn_check_all)
        btn_layout.addWidget(self.btn_check_none)
        btn_layout.addStretch()
        self.btn_cancel = QPushButton("Fermer")
        self.btn_cancel.setProperty("class", "control-btn")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_delete = QPushButton("Supprimer les fichiers cochés")
        self.btn_delete.setProperty("class", "danger-btn")
        self.btn_delete.clicked.connect(self._confirm_delete)
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_delete)
        layout.addLayout(btn_layout)

        # Le décompte du bouton de suppression ne se branche qu'ici : les
        # cases posées par ``_populate`` émettent déjà ``itemChanged``, et le
        # bouton qu'il compte n'existait pas encore.
        self.tree.itemChanged.connect(self._on_item_changed)
        self._update_delete_button()

    # ------------------------------------------------------------------
    # Construction des groupes
    # ------------------------------------------------------------------
    @staticmethod
    def _build_groups(candidates):
        """Un groupe par piste, chaque fichier n'apparaissant qu'une fois."""
        seen = set()
        groups = []
        for candidate in candidates or ():
            track = candidate.get("track") or {}
            match = candidate.get("match")
            files = []
            for path in getattr(match, "paths", ()) or ():
                key = os.path.normcase(os.path.normpath(path))
                if key in seen:
                    continue
                seen.add(key)
                files.append(path)
            if files:
                groups.append({"track": track, "match": match, "files": files})
        return groups

    def _populate(self):
        for group in self.groups:
            track = group["track"]
            match = group["match"]
            state = getattr(match, "state", library_index.STATE_SIMILAR)
            glyph = library_index.STATE_GLYPHS.get(state, "")
            parent_item = QTreeWidgetItem(self.tree)
            parent_item.setText(0, f"{glyph}  {track.get('title', '')}")
            parent_item.setText(1, " — ".join(
                part for part in (track.get("artist", ""), track.get("album", ""))
                if part
            ))
            parent_item.setText(2, getattr(match, "reason", ""))
            parent_item.setForeground(
                0, QColor(library_index.STATE_COLORS.get(state, "#bac9cc")))
            # La piste n'est pas cochable : elle n'est pas sur le disque, ce
            # sont ses fichiers que l'on supprime.
            parent_item.setFlags(Qt.ItemFlag.ItemIsEnabled)

            # Plusieurs fichiers pour une seule piste : ils sont en double
            # entre eux, et le meilleur exemplaire est celui qu'on garde.
            files = group["files"]
            keep = max(files, key=file_score) if len(files) > 1 else None

            for path in files:
                child = QTreeWidgetItem(parent_item)
                child.setText(0, os.path.basename(path))
                child.setText(1, file_quality(path))
                child.setText(2, os.path.dirname(path))
                child.setToolTip(0, path)
                child.setToolTip(2, path)
                child.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                )
                child.setCheckState(
                    0,
                    Qt.CheckState.Checked if (keep and path != keep)
                    else Qt.CheckState.Unchecked,
                )
                if keep and path == keep:
                    child.setToolTip(
                        1, "Meilleur exemplaire du groupe : conservé par défaut")
                self._items.append((child, path))
            parent_item.setExpanded(True)

    # ------------------------------------------------------------------
    # Sélection
    # ------------------------------------------------------------------
    def _set_all(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for item, _path in self._items:
            item.setCheckState(0, state)

    def _on_item_changed(self, _item, _column):
        self._update_delete_button()

    def _update_delete_button(self):
        count = len(self.get_files_to_delete())
        self.btn_delete.setEnabled(count > 0)
        self.btn_delete.setText(
            f"Supprimer les {count} fichier(s) cochés" if count
            else "Supprimer les fichiers cochés"
        )

    def get_files_to_delete(self):
        """Chemins des fichiers cochés (l'appelant les supprime)."""
        return [path for item, path in self._items
                if item.checkState(0) == Qt.CheckState.Checked]

    def _confirm_delete(self):
        paths = self.get_files_to_delete()
        if not paths:
            QMessageBox.information(
                self, "Aucune sélection", "Cochez au moins un fichier à supprimer."
            )
            return
        reply = QMessageBox.question(
            self, "Confirmer la suppression",
            f"Supprimer définitivement {len(paths)} fichier(s) du disque ?\n"
            "Cette action est irréversible.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.accept()
