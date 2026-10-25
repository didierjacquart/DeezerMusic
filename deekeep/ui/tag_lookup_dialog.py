"""
Écran de revue des tags trouvés en ligne.

Affiche, pour chaque fichier dont un tag manque, la proposition rapportée
par ``tag_lookup`` : les valeurs trouvées, les champs qu'elles combleraient,
la durée comparée et la raison de la correspondance.

Rien n'est écrit sans validation. Les correspondances sûres sont cochées
d'office, les correspondances probables laissées décochées : elles reposent
sur un indice plus faible (durée très différente, titre reconnu seul) et
méritent un coup d'œil. Le dialogue n'écrit rien lui-même ; il renvoie la
liste des propositions retenues via ``get_accepted()``.

Un seul passage pour les deux manques : les tags sans lesquels un fichier ne
peut pas être rangé — titre, artiste, album — et les tags complémentaires,
artiste de l'album, genre, année, numéro de piste. C'est
``tag_lookup.find_tags`` qui décide, fichier par fichier, ce qu'il faut
demander et à qui : un fichier auquel il ne manque qu'un genre n'est demandé
qu'à Deezer, seul à le porter, et la colonne « source » le dit ligne par
ligne. Les tags complémentaires ne se lisent pas dans la colonne des tags
trouvés : c'est celle des champs comblés qui en montre les valeurs, seul
endroit où elles se lisent.

Une requête par fichier : sur une bibliothèque entière, la recherche dure
de longues minutes. Rien n'oblige à les attendre — « Arrêter la recherche »
l'interrompt sans fermer l'écran, et l'écriture est ouverte dès la première
correspondance trouvée. Les fichiers non encore interrogés sont simplement
laissés tels quels ; un passage suivant les reprendra.
"""

import os

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QCheckBox, QProgressBar, QMessageBox, QApplication,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor

from deekeep.audio import acoustid_lookup
from deekeep.audio import tag_lookup
from deekeep.ui.styles import STYLE_SHEET

COLOR_SURE = QColor("#00e5ff")
COLOR_MAYBE = QColor("#ffcf7a")
COLOR_NONE = QColor("#5f7d84")


class TagLookupDialog(QDialog):
    """Cherche les tags manquants en ligne et propose de les écrire."""

    (COL_WRITE, COL_FILE, COL_CURRENT, COL_FOUND, COL_FILLS, COL_SOURCE,
     COL_WHY) = range(7)

    def __init__(self, entries, root, acoustid_key="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Compléter les tags")
        self.setStyleSheet(STYLE_SHEET)
        self.setMinimumSize(1000, 560)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.resize(int(avail.width() * 0.8), int(avail.height() * 0.8))

        self.entries = entries
        self.root = root
        self.acoustid_key = acoustid_key
        self._checks = {}      # rang -> (QCheckBox, proposition)
        self._rows = {}        # rang -> ligne du tableau
        self._thread = None
        self._examined = 0      # fichiers réellement interrogés

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)

        title = QLabel("Tags manquants")
        title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 22px; "
            "font-weight: bold; color: #00e5ff;"
        )
        layout.addWidget(title)

        sub = QLabel(
            f"{len(entries)} fichier(s) ne portent pas tous leurs tags : le "
            "titre, l'artiste ou l'album sans lesquels le plan ne sait pas "
            "les ranger, ou l'artiste de l'album, le genre, l'année et le "
            "numéro de piste, qui manquent partout ailleurs. Chacun est "
            "cherché sur Deezer d'après ses tags existants, son nom de "
            "fichier et son dossier, puis sur MusicBrainz si Deezer ne le "
            "reconnaît pas et qu'un tag indispensable lui manque ; la durée "
            "du fichier confirme qu'il s'agit bien du même enregistrement. "
            "La piste identifiée, le catalogue rend aussi son rang dans "
            "l'album, le genre et l'année de la parution. Les "
            "correspondances sûres sont cochées, les probables vous sont "
            "laissées. Seuls les champs vides seront écrits — aucun tag "
            "existant n'est remplacé."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #849396; font-size: 12px;")
        layout.addWidget(sub)

        # L'empreinte acoustique dépend d'un outil et d'une clé extérieurs :
        # dire lequel manque évite de croire la recherche complète. Elle ne
        # sert qu'aux fichiers muets, ceux dont un tag indispensable manque :
        # un fichier auquel il ne manque qu'un genre porte déjà de quoi le
        # retrouver dans le catalogue, et n'y recourt jamais.
        ready, message = acoustid_lookup.availability(acoustid_key)
        lbl_fp = QLabel(
            ("Empreinte acoustique disponible en dernier recours — " + message)
            if ready else
            ("Empreinte acoustique indisponible — " + message)
        )
        lbl_fp.setWordWrap(True)
        lbl_fp.setStyleSheet(
            "font-size: 12px; color: %s;" % ("#849396" if ready else "#ffcf7a"))
        layout.addWidget(lbl_fp)

        self.lbl_status = QLabel("Recherche en cours…")
        self.lbl_status.setStyleSheet("color: #849396; font-size: 12px;")
        layout.addWidget(self.lbl_status)

        self.progress = QProgressBar()
        self.progress.setRange(0, len(entries))
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["ÉCRIRE", "FICHIER", "TAGS ACTUELS", "TAGS TROUVÉS",
             "CHAMPS COMBLÉS", "SOURCE", "CORRESPONDANCE"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.verticalHeader().setDefaultSectionSize(46)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(self.COL_WRITE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_FILE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self.COL_CURRENT, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self.COL_FOUND, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_FILLS, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self.COL_SOURCE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_WHY, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(self.COL_FILE, 260)
        self.table.setColumnWidth(self.COL_CURRENT, 190)
        self.table.setColumnWidth(self.COL_FILLS, 260)
        self.table.setColumnWidth(self.COL_WHY, 260)
        layout.addWidget(self.table, 1)

        # Corriger un tag existant est plus engageant que combler un vide :
        # un fichier peut légitimement venir d'une autre édition que celle
        # trouvée. D'où une option, décochée, et l'affichage des champs
        # concernés ligne par ligne.
        self.cb_replace = QCheckBox(
            "Corriger aussi les tags que la correspondance contredit")
        self.cb_replace.setStyleSheet("font-size: 13px;")
        self.cb_replace.setToolTip(
            "Par défaut, seuls les champs vides sont renseignés.\n"
            "Cochez pour remplacer aussi les tags qui contredisent la\n"
            "correspondance retenue — un artiste erroné, par exemple.\n"
            "Les champs concernés sont signalés « ⟳ » dans le tableau."
        )
        self.cb_replace.stateChanged.connect(lambda _=None: self._refresh_fields())
        layout.addWidget(self.cb_replace)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        self.btn_all = QPushButton("Tout cocher")
        self.btn_all.setProperty("class", "control-btn")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none = QPushButton("Tout décocher")
        self.btn_none.setProperty("class", "control-btn")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        btn_layout.addWidget(self.btn_all)
        btn_layout.addWidget(self.btn_none)
        # Interroger le catalogue pour quelques milliers de fichiers prend
        # de longues minutes, et rien n'oblige à les attendre : ce bouton
        # arrête la recherche sans quitter l'écran, les correspondances
        # déjà trouvées restant proposées à l'écriture.
        self.btn_stop = QPushButton("Arrêter la recherche")
        self.btn_stop.setProperty("class", "control-btn")
        self.btn_stop.setToolTip(
            "Interrompt la recherche sans quitter cet écran.\n"
            "Les correspondances déjà trouvées restent écrivables ; les\n"
            "fichiers non encore interrogés sont laissés tels quels."
        )
        self.btn_stop.clicked.connect(self._stop_search)
        btn_layout.addWidget(self.btn_stop)
        btn_layout.addStretch()
        self.btn_cancel = QPushButton("Annuler")
        self.btn_cancel.setProperty("class", "control-btn")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_write = QPushButton("Écrire les tags cochés")
        self.btn_write.setProperty("class", "primary-btn")
        self.btn_write.setEnabled(False)
        self.btn_write.clicked.connect(self._confirm)
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_write)
        layout.addLayout(btn_layout)

        self._start()

    # ------------------------------------------------------------------
    def _start(self):
        # ``extras`` toujours : la recherche couvre les deux manques. Elle ne
        # coûte rien de plus aux fichiers qui n'en ont pas besoin —
        # ``find_tags`` ne demande au catalogue les tags complémentaires que
        # des fichiers auxquels ils manquent.
        self._thread = tag_lookup.TagLookupThread(
            self.entries, self.acoustid_key, True)
        self._thread.found.connect(self._on_found)
        self._thread.progress.connect(self._on_progress)
        self._thread.done.connect(self._on_done)
        self._thread.start()

    def _rel(self, path):
        try:
            rel = os.path.relpath(path, self.root)
            return rel if not rel.startswith("..") else path
        except ValueError:
            return path

    @staticmethod
    def _current_tags(entry):
        """Tags déjà présents, tels qu'ils s'affichent dans la revue."""
        parts = []
        if entry.get("title_tagged") and entry.get("title"):
            parts.append(str(entry["title"]))
        for field in ("artist", "album"):
            value = str(entry.get(field) or "").strip()
            if value:
                parts.append(value)
        return " · ".join(parts) if parts else "(aucun)"

    def _on_found(self, index, proposal):
        entry = self.entries[index]
        row = self.table.rowCount()
        self.table.insertRow(row)

        if proposal is not None:
            container = QWidget()
            box = QHBoxLayout(container)
            box.setContentsMargins(0, 0, 0, 0)
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            check = QCheckBox()
            check.setChecked(bool(proposal["sure"]))
            box.addWidget(check)
            self.table.setCellWidget(row, self.COL_WRITE, container)
            self._checks[index] = (check, proposal)
            self._rows[index] = row
            # Écrire n'exige pas d'attendre la fin de la recherche : dès
            # qu'une correspondance est là, elle peut partir sur le disque.
            self.btn_write.setEnabled(True)

        item_file = QTableWidgetItem(self._rel(entry.get("path", "")))
        item_file.setToolTip(entry.get("path", ""))
        self.table.setItem(row, self.COL_FILE, item_file)
        self.table.setItem(row, self.COL_CURRENT,
                           QTableWidgetItem(self._current_tags(entry)))

        if proposal is None:
            found = QTableWidgetItem("aucune correspondance trouvée")
            found.setForeground(QBrush(COLOR_NONE))
            self.table.setItem(row, self.COL_FOUND, found)
            self.table.setItem(row, self.COL_FILLS, QTableWidgetItem("—"))
            # Dire ce qui a réellement été interrogé, et cela dépend du
            # fichier : celui auquel il ne manque qu'un tag complémentaire
            # n'a été demandé qu'à Deezer, seul à les porter — ni MusicBrainz
            # ni l'empreinte n'en savent rien (voir ``tag_lookup._propose``).
            blocked = bool(tag_lookup.missing_fields(entry))
            if blocked:
                tried = "Deezer + MusicBrainz"
                if acoustid_lookup.availability(self.acoustid_key)[0]:
                    tried += " + AcoustID"
            else:
                tried = "Deezer"
            source = QTableWidgetItem(tried)
            source.setForeground(QBrush(COLOR_NONE))
            self.table.setItem(row, self.COL_SOURCE, source)
            why = QTableWidgetItem(
                "tags, nom de fichier et dossier trop pauvres" if blocked else
                "le catalogue ne reconnaît pas cet enregistrement")
            why.setForeground(QBrush(COLOR_NONE))
            self.table.setItem(row, self.COL_WHY, why)
            return

        found = QTableWidgetItem(
            f"{proposal['title']} · {proposal['artist']} · {proposal['album']}")
        found.setForeground(QBrush(COLOR_SURE if proposal["sure"] else COLOR_MAYBE))
        found.setToolTip(
            f"Titre : {proposal['title']}\nArtiste : {proposal['artist']}\n"
            f"Album : {proposal['album']}\n"
            f"Durée du fichier : {proposal['duration']} s — "
            f"durée trouvée : {proposal['seconds']} s\n"
            f"Source : {proposal.get('source', '')}\n"
            f"Recherche : {proposal['query']}"
        )
        self.table.setItem(row, self.COL_FOUND, found)

        self.table.setItem(row, self.COL_FILLS, QTableWidgetItem(""))
        self._refresh_fields_cell(index)
        self.table.setItem(row, self.COL_SOURCE,
                           QTableWidgetItem(proposal.get("source", "")))

        why = QTableWidgetItem(
            f"{'sûre' if proposal['sure'] else 'probable'} ({proposal['score']:.2f}) — "
            f"{proposal['reason']}")
        why.setForeground(QBrush(COLOR_SURE if proposal["sure"] else COLOR_MAYBE))
        self.table.setItem(row, self.COL_WHY, why)

    # Libellés courts : la colonne en aligne jusqu'à six, valeurs comprises.
    FIELD_LABELS = {
        "title": "titre", "artist": "artiste", "album": "album",
        "albumartist": "artiste album", "genre": "genre", "date": "année",
        "track": "piste", "tracktotal": "total pistes", "disc": "disque",
    }

    def _field_text(self, field, proposal):
        """Libellé d'un champ comblé, sa valeur en regard s'il le faut.

        Les trois tags de base se lisent déjà dans la colonne « tags
        trouvés » ; les complémentaires, non — les afficher ici est le seul
        moyen de savoir ce que la case cochée va écrire.
        """
        label = self.FIELD_LABELS.get(field, field)
        if field in ("title", "artist", "album"):
            return label
        value = str((proposal.get("values") or {}).get(field) or "")
        return f"{label} = {value}" if value else label

    def _refresh_fields_cell(self, index):
        """Réécrit la colonne des champs touchés pour une proposition."""
        row = self._rows.get(index)
        item = self.table.item(row, self.COL_FILLS) if row is not None else None
        if item is None:
            return
        _check, proposal = self._checks[index]
        fields = [self._field_text(field, proposal)
                  for field in proposal["fills"]]
        parts = [" · ".join(fields)]
        # La colonne est trop étroite pour six champs : l'infobulle les
        # reprend un par ligne, seule façon de tout relire sans l'élargir.
        hint = ["Tags écrits :"] + ["  • " + text for text in fields]

        corrects = proposal.get("corrects") or []
        if corrects:
            marked = ", ".join(self.FIELD_LABELS.get(f, f) for f in corrects)
            if self.cb_replace.isChecked():
                parts.append("⟳ " + marked)
                hint.append("Tags corrigés : " + marked)
            else:
                parts.append("(⟳ %s ignoré)" % marked)
                hint.append(
                    "La correspondance contredit : " + marked
                    + "\nCochez « Corriger aussi les tags… » pour les remplacer."
                )
        # Trois espaces entre les groupes : le point médian sépare déjà les
        # champs entre eux, et « genre = Pop · ⟳ artiste » se lirait comme
        # une seule énumération.
        item.setText("   ".join(p for p in parts if p))
        item.setToolTip("\n".join(hint))
        item.setForeground(QBrush(
            COLOR_MAYBE if corrects and self.cb_replace.isChecked() else COLOR_SURE))

    def _refresh_fields(self):
        """Répercute l'option de correction sur toutes les lignes."""
        for index in self._checks:
            self._refresh_fields_cell(index)

    def _stop_search(self):
        """Interrompt la recherche ; l'écran et ses résultats restent là.

        Le thread s'arrête au fichier suivant et émet ``done`` : c'est lui
        qui clôt l'affichage. Ne pas l'attendre ici — la requête en cours
        peut durer quelques secondes et l'écran doit rester utilisable.
        """
        if self._thread is None or not self._thread.isRunning():
            return
        self.btn_stop.setEnabled(False)
        self._thread.stop()
        self.lbl_status.setText(
            "Arrêt de la recherche… %d / %d fichiers examinés"
            % (self._examined, len(self.entries)))

    def _on_progress(self, count):
        self._examined = count
        self.progress.setValue(count)
        # Après un arrêt demandé — le bouton est alors éteint — le libellé
        # annonce l'interruption : le dernier fichier encore en vol ne doit
        # pas le remplacer par « recherche en cours ».
        if self.btn_stop.isEnabled():
            self.lbl_status.setText(
                f"Recherche en cours… {count} / {len(self.entries)} fichiers")

    def _on_done(self):
        self.progress.setVisible(False)
        self.btn_stop.setVisible(False)
        sure = sum(1 for _c, p in self._checks.values() if p["sure"])
        maybe = len(self._checks) - sure
        # Sans réponse et non examiné ne sont pas la même chose : après un
        # arrêt, compter les fichiers jamais interrogés comme des échecs
        # laisserait croire que le catalogue les a refusés.
        silent = max(0, self._examined - len(self._checks))
        left = max(0, len(self.entries) - self._examined)
        parts = [f"{sure} correspondance(s) sûre(s)", f"{maybe} probable(s)",
                 f"{silent} sans réponse"]
        if left:
            parts.append(f"{left} non examiné(s) — recherche arrêtée")
        self.lbl_status.setText(" · ".join(parts))
        self.btn_write.setEnabled(bool(self._checks))

    def _set_all(self, checked):
        for check, _proposal in self._checks.values():
            check.setChecked(checked)

    # ------------------------------------------------------------------
    def get_accepted(self):
        """Propositions retenues : [(chemin, {champ: valeur}, proposition)].

        Les champs à corriger n'y figurent que si l'option l'autorise.
        """
        accepted = []
        for index, (check, proposal) in sorted(self._checks.items()):
            if not check.isChecked():
                continue
            values = dict(proposal["values"])
            if self.cb_replace.isChecked():
                values.update(proposal.get("replaces") or {})
            accepted.append(
                (self.entries[index].get("path", ""), values, proposal))
        return accepted

    def _confirm(self):
        accepted = self.get_accepted()
        if not accepted:
            QMessageBox.information(
                self, "Aucune sélection",
                "Cochez au moins une correspondance à écrire."
            )
            return
        fields = sum(len(values) for _p, values, _prop in accepted)
        corrected = sum(
            len(set(values) & set(prop.get("replaces") or {}))
            for _p, values, prop in accepted
        )
        lines = [f"Écrire {fields} tag(s) dans {len(accepted)} fichier(s) ?"]
        left = max(0, len(self.entries) - self._examined)
        if left:
            lines.append(
                f"\nLa recherche n'a pas parcouru toute la liste : "
                f"{left} fichier(s) n'ont pas été interrogés et resteront "
                "tels quels."
            )
        if corrected:
            lines.append(
                f"\nDont {corrected} tag(s) existant(s) remplacé(s) par une valeur "
                "différente — l'ancienne valeur sera perdue."
            )
        else:
            lines.append("\nSeuls des champs vides sont renseignés ; aucun tag "
                         "existant n'est remplacé.")
        reply = QMessageBox.question(
            self, "Confirmer l'écriture", "".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            # Écrire met fin à la recherche : les tags vont changer sur le
            # disque, les propositions suivantes porteraient sur un état
            # périmé.
            if self._thread is not None and self._thread.isRunning():
                self._thread.stop()
                self._thread.wait(3000)
            self.accept()

    def closeEvent(self, event):
        if self._thread is not None and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(3000)
        super().closeEvent(event)

    def reject(self):
        if self._thread is not None and self._thread.isRunning():
            self._thread.stop()
            self._thread.wait(3000)
        super().reject()
