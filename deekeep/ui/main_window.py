"""
Fenêtre principale de l'application DeeKeep.

Assemble la barre latérale de navigation et les huit pages (Musique,
Bibliothèque, Historique, Paramètres, et le groupe « Outils » : Réencoder,
Organiser, Qualité et Diagnostic), orchestre les recherches et les
téléchargements et met à jour la progression globale.

« Bibliothèque » (library_page.py), « Organiser » (organize_page.py) et
« Qualité » (quality_page.py) sont des pages comme les autres, et non des
fenêtres modales : l'analyse d'une grande bibliothèque tourne en tâche de
fond sans empêcher l'utilisateur de continuer à télécharger.
"""

import os

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QFrame, QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QCheckBox, QComboBox, QSpinBox,
    QProgressBar, QDialog, QFileDialog, QMessageBox, QSystemTrayIcon,
    QMenu, QApplication, QLineEdit
)
from PyQt6.QtCore import Qt, QTimer, QEvent, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QPixmap, QPainter, QPainterPath, QAction, QIcon

from deekeep.core import config
from deekeep.library import library_index
from deekeep.library import library_rules
from deekeep.ui import player
from deekeep.core import settings
from deekeep.db import sync_service
from deekeep.ui import sync_ui
from deekeep.ui.styles import STYLE_SHEET
from deekeep.core.utils import create_app_icon, reveal_in_explorer
from deekeep.ui.add_track_dialog import AddTrackDialog
from deekeep.workers.download_thread import TrackDownloaderThread
from deekeep.workers.image_thread import CoverLookupThread, CoverDownloadThread
from deekeep.workers.search_thread import DeezerMatchThread
from deekeep.audio.reencode_scan import LocalScanThread, AUDIO_EXTENSIONS
from deekeep.ui.library_page import LibraryPage
from deekeep.ui.organize_page import OrganizePage
from deekeep.ui.quality_page import QualityPage

# Indices des colonnes du tableau « Réencoder » (une constante par colonne
# pour éviter les décalages d'indices lors de l'ajout de colonnes).
(RC_CHECK, RC_PLAY, RC_COVER, RC_TITLE, RC_ARTIST, RC_PATH, RC_FMT,
 RC_MATCH, RC_STATUS) = range(9)

# Colonne « ÉCOUTE » du tableau principal et de l'historique : ces deux
# tableaux repèrent encore leurs colonnes par leur indice, la nouvelle est
# donc ajoutée en dernier plutôt qu'insérée (comme la colonne ACTION de
# l'onglet « Recherches effectuées »).
MAIN_PLAY_COLUMN = 8
HIST_PLAY_COLUMN = 5

# La cellule « ÉCOUTE » du tableau principal porte deux sources : le fichier
# extrait (vide tant qu'il n'existe pas) et l'extrait Deezer de 30 s, qui
# donne le titre à entendre avant même de l'extraire.
PLAY_PATH_ROLE = Qt.ItemDataRole.UserRole
PLAY_PREVIEW_ROLE = Qt.ItemDataRole.UserRole + 1


def _path_key(path):
    """Clé de comparaison d'un chemin (casse et séparateurs ignorés)."""
    return os.path.normcase(os.path.normpath(
        str(path or "").replace("/", os.sep)))


class DeeKeepApp(QMainWindow):

    # Index du dossier de dépôt reconstruit (voir ``refresh_library_index``).
    # La fenêtre de recherche s'y branche pour rafraîchir ses pastilles
    # « déjà sur le disque » sans être rouverte.
    libraryIndexChanged = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DeeKeep - Music Downloader")
        self.resize(1280, 800)
        self.setStyleSheet(STYLE_SHEET)

        self.setWindowIcon(create_app_icon())

        # Configuration interne (chargée depuis le fichier de configuration)
        self.settings = settings.load_settings()
        self.download_dir = self.settings.get(
            "download_dir", os.path.join(os.path.expanduser("~"), "Music")
        )
        self.active_downloads = {}
        # File d'attente des téléchargements de la page Musique et limite de
        # parallélisme (au plus 10 extractions simultanées).
        self._download_queue = []
        self.MAX_CONCURRENT_DOWNLOADS = 10
        # Décompte des titres par artiste du lot en cours, partagé par les
        # extractions (voir _batch_tally et library_rules.Tally).
        self._download_tally = None
        # Threads de rattrapage des jaquettes manquantes du tableau principal
        self._main_cover_threads = []
        # Cellule « ÉCOUTE » du tableau principal dont l'extrait de 30 s joue
        # (None si aucun) : elle porte le glyphe d'arrêt, et un deuxième clic
        # dessus coupe la lecture.
        self._preview_item = None
        # États de tri (colonne, ordre) des tableaux
        self._main_sort = (-1, Qt.SortOrder.AscendingOrder)
        self._hist_sort = (-1, Qt.SortOrder.AscendingOrder)
        self._searches_sort_desc = False
        self.search_history = list(self.settings.get("search_history", []))
        self.success_count = 0
        self.failed_count = 0
        # Historique de fichiers téléchargés
        self.downloaded_history = list(self.settings.get("downloaded_history", []))
        # Fichier cookies.txt pour l'identification YouTube ("" = aucun)
        self.cookies_file = self.settings.get("cookies_file", "")

        # --- État de l'écran « Réencoder » ---
        # Fichiers locaux ajoutés pour réencodage. Chaque entrée est un dict :
        # {path, title, artist, album, match, cover_bytes, cover_url, status, _uid}
        self.reencode_files = []
        self._reencode_uid = 0
        self._reencode_uid_map = {}          # _uid -> entrée de reencode_files
        self._reencode_match_threads = []
        self._reencode_cover_threads = []
        self._reencode_scan_thread = None    # analyse locale (fichiers/dossiers)
        self._reencode_workers = {}          # row_index -> TrackDownloaderThread actif
        self._reencode_queue = []            # lignes en attente de conversion
        self._reencode_batch = []            # lignes du lot courant (progression globale)
        self._reencode_sort = (-1, Qt.SortOrder.AscendingOrder)
        self._reencode_last_dir = self.download_dir
        # Filtre « déjà au format cible ».
        self._reencode_hide_at_target = False
        # Garde anti-réentrance pendant le (re)rendu du tableau (les
        # changements d'items programmatiques ne doivent pas déclencher
        # le slot itemChanged).
        self._rc_rendering = False
        # Nombre maximal de conversions simultanées.
        self.REENCODE_MAX_CONCURRENT = 10
        # Au-delà de ce nombre de fichiers ajoutés d'un coup, la recherche
        # automatique de correspondances Deezer (1 requête réseau par fichier)
        # est désactivée pour préserver la fluidité de l'interface.
        self.REENCODE_AUTOMATCH_LIMIT = 150

        # --- Confrontation au dossier de dépôt ---
        # Index des fichiers déjà rangés, construit en tâche de fond : il dit
        # à la recherche ce qu'il est inutile de retélécharger.
        self.library_index = None
        self._library_index_thread = None

        self.init_ui()
        self.init_tray()
        self.apply_loaded_settings()
        self.init_database()
        self.refresh_library_index()

    # ------------------------------------------------------------------
    # Index du dossier de dépôt (« ce titre est-il déjà sur le disque ? »)
    # ------------------------------------------------------------------

    def refresh_library_index(self):
        """(Re)lit le dossier de dépôt en tâche de fond.

        Appelée au démarrage et à chaque changement de dossier. Un parcours
        déjà en cours n'est pas doublé : il porte sur le même dossier, et les
        extractions terminées entre-temps s'ajoutent à l'index sans relecture
        (``on_download_finished``).
        """
        thread = self._library_index_thread
        if thread is not None and thread.isRunning():
            return
        thread = library_index.LibraryIndexThread(
            self.download_dir, self.downloaded_history, self
        )
        thread.ready.connect(self._on_library_index_ready)
        self._library_index_thread = thread
        thread.start()

    def _on_library_index_ready(self, index):
        """Index prêt : les pastilles du tableau principal et de la recherche."""
        self.library_index = index
        self._repair_history_paths(index)
        self._refresh_library_badges()
        self.libraryIndexChanged.emit(index)

    def on_library_files_deleted(self, paths):
        """Fichiers supprimés depuis la fenêtre de recherche.

        La déduplication de la recherche (``AddTrackDialog.review_duplicates``)
        supprime des fichiers du dossier de dépôt : l'index les oublie un à un
        plutôt que d'être reconstruit — relire l'arborescence pour quelques
        chemins connus serait payer cher un résultat déjà su —, et sa
        rediffusion remet à jour les pastilles de tous les écrans ouverts, la
        fenêtre de recherche comprise.
        """
        if self.library_index is None or not paths:
            return
        for path in paths:
            self.library_index.remove(path)
        self._refresh_library_badges()
        self.libraryIndexChanged.emit(self.library_index)

    def _repair_history_paths(self, index):
        """Rattrape les chemins d'historique laissés en arrière par un rangement.

        Ranger la bibliothèque — ou changer de dossier de dépôt — déplace les
        fichiers sans que l'historique en soit averti : ses lignes désignent
        alors des chemins vides, et le lecteur refuse de démarrer (« Fichier
        introuvable »). Le nom du fichier, lui, n'a pas bougé : quand il ne
        désigne qu'un seul fichier du dossier de dépôt, encore libre, la ligne
        y est rattachée. Un nom porté par plusieurs fichiers, ou déjà réclamé
        par une autre ligne, est laissé en place : mieux vaut une ligne muette
        qu'une ligne qui désigne le mauvais titre.
        """
        if index is None:
            return

        # Chemins déjà valides : un titre retrouvé ne peut pas se voir
        # attribuer le fichier d'une ligne qui le désigne encore.
        claimed, stale = set(), []
        for row, entry in enumerate(self.downloaded_history):
            path = entry.get("path") or "" if isinstance(entry, dict) else ""
            if not path:
                continue
            if index.has_path(path) or os.path.exists(path):
                claimed.add(_path_key(path))
            else:
                stale.append((row, entry))
        if not stale:
            return

        # Un même titre extrait deux fois figure deux fois dans l'historique,
        # sur le même chemin : les deux lignes se rattachent au même fichier,
        # là où deux lignes de chemins différents ne le peuvent pas.
        resolved = {}
        repaired = 0
        for row, entry in stale:
            found = resolved.get(_path_key(entry["path"]))
            if found is None:
                free = [candidate
                        for candidate in index.paths_named(entry["path"])
                        if _path_key(candidate) not in claimed]
                if len(free) != 1:
                    continue
                found = os.path.normpath(free[0])
                resolved[_path_key(entry["path"])] = found
                claimed.add(_path_key(found))
            entry["path"] = found
            repaired += 1
            # La ligne du tableau est mise à jour sur place : la reconstruire
            # relancerait la recherche des jaquettes de tout l'historique.
            item = self.table_history.item(row, 3)
            if item is not None:
                item.setText(found)

        if repaired:
            print(f"[historique] {repaired} chemin(s) rattrapé(s) "
                  "après déplacement de la bibliothèque.")
            self.save_config()

    def _refresh_library_badges(self):
        """Rafraîchit la pastille « déjà sur le disque » de chaque ligne."""
        for row in range(self.table_widget.rowCount()):
            self._set_library_badge(row)

    def _set_library_badge(self, row):
        """Pose sur une ligne du tableau principal le verdict du disque.

        Le tableau principal *informe* : sa case à cocher sert à retirer des
        lignes, pas à choisir ce qui s'extrait (« Tout télécharger » prend
        toute la file). La désactiver priverait donc l'utilisateur du moyen de
        retirer un doublon plutôt que de l'en empêcher ; c'est la fenêtre de
        recherche, où la case décide de l'ajout, qui refuse les titres déjà
        présents.
        """
        container = self.table_widget.cellWidget(row, 0)
        badge = container.findChild(QLabel, "library_badge") if container else None
        if badge is None:
            return
        def text(column):
            item = self.table_widget.item(row, column)
            return item.text() if item else ""
        library_index.apply_badge(badge, library_index.lookup(
            self.library_index, text(3), text(4), text(5)
        ))

    # ------------------------------------------------------------------
    # Base de données (cache local + base en ligne)
    # ------------------------------------------------------------------

    def init_database(self):
        """
        Met en route le cache local et la synchronisation en ligne.

        L'état courant est d'abord reporté dans le cache — la base doit
        décrire l'application telle qu'elle est au démarrage — puis le
        rapprochement périodique démarre. Rien de tout cela ne bloque
        l'interface : la moindre lenteur du réseau se produit dans un fil
        séparé (voir ``sync_ui.SyncController``).
        """
        self.sync_controller = sync_ui.SyncController(self)
        self.sync_controller.conflictFound.connect(self.on_sync_conflict)
        self.sync_controller.statusChanged.connect(self.on_sync_status)

        self.database_panel = sync_ui.DatabasePanel(
            self.sync_controller, root_provider=lambda: self.download_dir
        )
        self._db_panel_slot.addWidget(self.database_panel)

        self.sync_controller.record_state(self.settings)
        self.sync_controller.start()

    def on_sync_status(self, status, message):
        """Reflète l'état de la synchronisation dans la barre latérale."""
        if not hasattr(self, "lbl_sync_state"):
            return
        colour = sync_ui.STATUS_COLORS.get(status, "#849396")
        label = {
            sync_service.IN_SYNC: "Base synchronisée",
            sync_service.PUSH_NEEDED: "Envoi en attente",
            sync_service.PULL_NEEDED: "Réception en attente",
            sync_service.CONFLICT: "Base désynchronisée !",
            sync_service.OFFLINE: "Base hors ligne",
            sync_service.NOT_CONFIGURED: "Base locale seule",
            sync_service.ERROR: "Erreur de base",
        }.get(status, status)
        self.lbl_sync_state.setText(label)
        self.lbl_sync_state.setStyleSheet(
            f"color: {colour}; font-size: 11px; padding: 0 24px;")
        self.lbl_sync_state.setToolTip(message or "")

    def on_sync_conflict(self, report):
        """
        Signale une désynchronisation et applique le choix de l'utilisateur.

        Le cas ne se présente que si les deux côtés ont été modifiés depuis
        le dernier échange réussi — typiquement une seconde machine, ou une
        session menée hors ligne de part et d'autre.
        """
        dialog = sync_ui.ConflictDialog(report, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.choice:
            return
        if dialog.choice == sync_ui.ConflictDialog.REMOTE:
            # La base fait foi : une fois la réception faite, il faut
            # relire les paramètres et les historiques dans l'interface.
            self.sync_controller.busyChanged.connect(self._after_pull_resolve)
        self.sync_controller.resolve(dialog.choice)

    def _after_pull_resolve(self, busy):
        """Recharge l'interface après une résolution en faveur de la base."""
        if busy:
            return
        try:
            self.sync_controller.busyChanged.disconnect(
                self._after_pull_resolve)
        except TypeError:
            pass
        changed = self.sync_controller.restore_state(self.settings)
        if not changed:
            return
        self.search_history = list(self.settings.get("search_history", []))
        self.downloaded_history = list(self.settings.get("downloaded_history", []))
        self.download_dir = self.settings.get("download_dir", self.download_dir)
        settings.save_settings(self.settings)
        self.apply_loaded_settings()
        self.refresh_history_table()
        self.refresh_searches_table()
        self.update_stats_label()
        QMessageBox.information(
            self, "Base de données",
            f"{changed} élément(s) rechargé(s) depuis la base en ligne.")

    def save_config(self):
        """Enregistre l'état courant (paramètres + historique) sur le disque."""
        self.settings.update({
            "download_dir": self.download_dir,
            "search_history": self.search_history,
            "downloaded_history": self.downloaded_history,
        })
        # Paramètres d'exportation (uniquement si l'interface est prête)
        if hasattr(self, "combo_quality"):
            self.settings.update({
                "file_format": self.combo_format.currentText(),
                "quality": self.combo_quality.currentText(),
                "org_artist": self.cb_org_artist.isChecked(),
                "org_album": self.cb_org_album.isChecked(),
                "org_divers": self.cb_org_divers.isChecked(),
                "org_divers_min": self.spin_org_divers_min.value(),
                "youtube_browser": self.combo_yt_browser.currentText(),
                "cookies_file": self.cookies_file,
                "acoustid_key": self.input_acoustid.text().strip(),
            })
        settings.save_settings(self.settings)
        # Report dans la base : le fichier JSON reste écrit (il sert de
        # dernier recours si le cache SQLite est perdu), mais c'est la base
        # qui fait désormais référence. L'appel est local et différentiel ;
        # la synchronisation en ligne, elle, est différée de quelques
        # secondes pour regrouper une rafale de modifications.
        if getattr(self, "sync_controller", None) is not None:
            self.sync_controller.record_state(self.settings)
            if hasattr(self, "database_panel"):
                self.database_panel.refresh_detail()

    def current_export_options(self):
        """Réglages d'exportation affichés (format, qualité, identification).

        Les écrans qui réextraient un titre — « Réencoder », « Qualité » —
        doivent viser le format et le compte YouTube choisis au moment où
        l'utilisateur lance l'opération, d'où cette lecture à la demande.
        """
        browser = self.combo_yt_browser.currentText()
        return {
            "file_format": self.combo_format.currentText(),
            "quality": self.combo_quality.currentText(),
            "cookies_browser": None if browser.startswith("Aucun") else browser.lower(),
            "cookies_file": self.cookies_file,
        }

    def on_organize_options_changed(self):
        """Répercute dans les Paramètres les options changées sur « Organiser ».

        Les deux écrans exposent les mêmes réglages (sous-dossiers, dossier
        « Divers ») : la page Organiser vient d'écrire les siens dans
        ``self.settings``, on les recopie dans les widgets des Paramètres
        avant d'enregistrer, sans quoi ``save_config`` les écraserait avec
        les anciennes valeurs affichées.
        """
        if not hasattr(self, "cb_org_divers"):
            return
        for widget, key in (
            (self.cb_org_artist, "org_artist"),
            (self.cb_org_album, "org_album"),
            (self.cb_org_divers, "org_divers"),
        ):
            value = bool(self.settings.get(key, widget.isChecked()))
            if widget.isChecked() != value:
                widget.blockSignals(True)
                widget.setChecked(value)
                widget.blockSignals(False)

        minimum = int(self.settings.get("org_divers_min", 3) or 3)
        if self.spin_org_divers_min.value() != minimum:
            self.spin_org_divers_min.blockSignals(True)
            self.spin_org_divers_min.setValue(minimum)
            self.spin_org_divers_min.blockSignals(False)

        self.save_config()

    def apply_loaded_settings(self):
        """Répercute dans l'interface les valeurs chargées au démarrage."""
        # Restaurer les paramètres d'exportation.
        # setCurrentText sur le format déclenche on_format_changed qui
        # reconstruit la liste des qualités : on applique la qualité ensuite.
        self.combo_format.setCurrentText(self.settings.get("file_format", "MP3"))
        saved_quality = self.settings.get("quality")
        if saved_quality:
            idx = self.combo_quality.findText(saved_quality)
            if idx >= 0:
                self.combo_quality.setCurrentIndex(idx)
        self.cb_org_artist.setChecked(self.settings.get("org_artist", True))
        self.cb_org_album.setChecked(self.settings.get("org_album", True))
        self.cb_org_divers.setChecked(self.settings.get("org_divers", False))
        self.spin_org_divers_min.setValue(
            int(self.settings.get("org_divers_min", 3) or 3))
        self.input_acoustid.setText(self.settings.get("acoustid_key", ""))
        idx = self.combo_yt_browser.findText(
            self.settings.get("youtube_browser", "Aucun (anonyme)"))
        if idx >= 0:
            self.combo_yt_browser.setCurrentIndex(idx)

        # Sauvegarde automatique à chaque modification (branchée après
        # l'application des valeurs pour éviter des écritures inutiles au démarrage).
        self.combo_format.currentTextChanged.connect(lambda _: self.save_config())
        self.combo_quality.currentTextChanged.connect(lambda _: self.save_config())
        self.cb_org_artist.stateChanged.connect(lambda _: self.save_config())
        self.cb_org_album.stateChanged.connect(lambda _: self.save_config())
        self.cb_org_divers.stateChanged.connect(lambda _: self.save_config())
        self.spin_org_divers_min.valueChanged.connect(lambda _: self.save_config())
        self.input_acoustid.editingFinished.connect(self.save_config)
        self.combo_yt_browser.currentTextChanged.connect(lambda _: self.save_config())

        self.update_stats_label()

    # ==================== ZONE DE NOTIFICATION (TRAY) ====================

    def init_tray(self):
        """Crée l'icône de zone de notification et son menu contextuel.

        Réduire la fenêtre l'envoie dans les icônes cachées de Windows ;
        un clic sur l'icône (ou « Afficher ») la restaure, « Quitter »
        ferme réellement l'application.
        """
        self._tray_tip_shown = False
        self.tray_icon = QSystemTrayIcon(create_app_icon(), self)
        self.tray_icon.setToolTip("DeeKeep - Music Downloader")

        tray_menu = QMenu()
        action_show = QAction("Afficher DeeKeep", self)
        action_show.triggered.connect(self.restore_from_tray)
        action_quit = QAction("Quitter", self)
        action_quit.triggered.connect(self.quit_app)
        tray_menu.addAction(action_show)
        tray_menu.addSeparator()
        tray_menu.addAction(action_quit)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def on_tray_activated(self, reason):
        """Restaure la fenêtre sur clic simple ou double sur l'icône."""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore_from_tray()

    def restore_from_tray(self):
        """Réaffiche la fenêtre depuis la zone de notification."""
        self.showMaximized()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        """Quitte réellement l'application (menu « Quitter » du tray)."""
        self.close()

    def changeEvent(self, event):
        """Réduction → la fenêtre se cache dans la zone de notification."""
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self.tray_icon.isVisible()
        ):
            # hide() différé : on laisse Qt terminer la transition d'état
            # avant de retirer la fenêtre de la barre des tâches.
            QTimer.singleShot(0, self.hide)
            if not self._tray_tip_shown:
                self._tray_tip_shown = True
                self.tray_icon.showMessage(
                    "DeeKeep",
                    "L'application continue en arrière-plan. "
                    "Cliquez sur l'icône pour la rouvrir.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
        super().changeEvent(event)

    def closeEvent(self, event):
        """Sauvegarde la configuration à la fermeture de la fenêtre."""
        # Un rangement de fichiers en cours ne doit pas être coupé au milieu
        # d'un déplacement : on laisse l'utilisateur décider.
        if hasattr(self, "organize_page") and not self.organize_page.can_leave():
            reply = QMessageBox.question(
                self, "Rangement en cours",
                "Un rangement de la bibliothèque est en cours.\n"
                "Quitter maintenant attendra la fin du fichier en cours de "
                "déplacement. Continuer ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        # Un retéléchargement coupé en plein vol laisserait un titre à demi
        # remplacé : l'utilisateur décide, comme pour le rangement.
        if hasattr(self, "quality_page") and not self.quality_page.can_leave():
            reply = QMessageBox.question(
                self, "Retéléchargement en cours",
                "Des titres sont en cours de retéléchargement.\n"
                "Quitter maintenant les abandonnera. Continuer ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        self.save_config()
        if hasattr(self, "organize_page"):
            self.organize_page.stop_threads()
        if hasattr(self, "quality_page"):
            self.quality_page.stop_threads()
        for t in self._main_cover_threads:
            if t.isRunning():
                t.stop()
                t.wait()
        self._stop_reencode_threads()
        # Le parcours du dossier de dépôt ne dure que le temps de lire une
        # arborescence : on le laisse finir plutôt que de le tuer.
        if self._library_index_thread is not None:
            self._library_index_thread.wait(2000)
        # Le lecteur est une fenêtre indépendante : sans cette fermeture, son
        # analyse spectrale et sa lecture survivraient à la fenêtre principale.
        player.close_player()
        # Une synchronisation en cours a quelques secondes pour se terminer :
        # l'interrompre ne perdrait rien (la file d'attente survit dans le
        # cache) mais laisserait une transaction ouverte côté serveur.
        if getattr(self, "sync_controller", None) is not None:
            self.sync_controller.shutdown()
        self.tray_icon.hide()
        super().closeEvent(event)
        # QuitOnLastWindowClosed est désactivé (fenêtre cachable dans le
        # tray) : la fermeture de la fenêtre principale quitte explicitement.
        QApplication.instance().quit()

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)

        global_layout = QHBoxLayout(main_widget)
        global_layout.setContentsMargins(0, 0, 0, 0)
        global_layout.setSpacing(0)

        # ==================== 1. BARRE LATÉRALE GAUCHE ====================
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 24, 0, 24)
        sidebar_layout.setSpacing(5)

        # Logo
        logo_container = QWidget()
        logo_container.setStyleSheet("margin-bottom: 24px;")
        logo_layout = QHBoxLayout(logo_container)
        # Marges portées par le layout (le padding CSS d'un QWidget nu est
        # ignoré par Qt) : l'icône n'est plus collée au bord et rognée.
        logo_layout.setContentsMargins(24, 4, 24, 0)

        logo_icon = QLabel()
        # Pixmap carré rendu à sa taille native (36×36) : pas de mise à
        # l'échelle du label → aucune déformation. Le label se dimensionne
        # sur le pixmap. Les marges du layout évitent le rognage au bord.
        logo_icon.setPixmap(create_app_icon().pixmap(36, 36))

        logo_text_container = QWidget()
        logo_text_layout = QVBoxLayout(logo_text_container)
        logo_text_layout.setContentsMargins(12, 0, 0, 0)
        logo_text_layout.setSpacing(1)

        logo_title = QLabel("DeeKeep")
        logo_title.setObjectName("logo_title")
        logo_subtitle = QLabel("A U D I O P H I L E   G R A D E")
        logo_subtitle.setObjectName("logo_subtitle")

        logo_text_layout.addWidget(logo_title)
        logo_text_layout.addWidget(logo_subtitle)

        logo_layout.addWidget(logo_icon)
        logo_layout.addWidget(logo_text_container)
        sidebar_layout.addWidget(logo_container)

        # Navigation
        self.btn_musique = QPushButton("  Musique")
        self.btn_musique.setProperty("active", "true")
        self.btn_musique.setProperty("class", "nav-btn")
        self.btn_musique.clicked.connect(self.show_musique_page)

        # « Bibliothèque » se lit juste sous « Musique » : télécharger et
        # écouter ce qu'on a téléchargé sont les deux faces d'un même geste,
        # et non un outil de maintenance comme ceux du groupe « Outils ».
        self.btn_bibliotheque = QPushButton("  Bibliothèque")
        self.btn_bibliotheque.setProperty("class", "nav-btn")
        self.btn_bibliotheque.clicked.connect(self.show_bibliotheque_page)

        # Groupe « Outils » repliable : en-tête cliquable + conteneur des
        # sous-éléments. Le chevron reflète l'état plié/déplié.
        self.btn_outils_group = QPushButton()
        self.btn_outils_group.setObjectName("nav_group")
        self.btn_outils_group.setProperty("class", "nav-btn")
        self.btn_outils_group.clicked.connect(self.toggle_outils_group)

        self.btn_reencoder = QPushButton("Réencoder")
        self.btn_reencoder.setProperty("class", "nav-sub-btn")
        self.btn_reencoder.clicked.connect(self.show_reencoder_page)

        self.btn_organiser = QPushButton("Organiser")
        self.btn_organiser.setProperty("class", "nav-sub-btn")
        self.btn_organiser.clicked.connect(self.show_organiser_page)

        self.btn_qualite = QPushButton("Qualité")
        self.btn_qualite.setProperty("class", "nav-sub-btn")
        self.btn_qualite.clicked.connect(self.show_qualite_page)

        self.btn_outils = QPushButton("Diagnostic")
        self.btn_outils.setProperty("class", "nav-sub-btn")
        self.btn_outils.clicked.connect(self.show_outils_page)

        self.outils_group_container = QWidget()
        outils_group_layout = QVBoxLayout(self.outils_group_container)
        outils_group_layout.setContentsMargins(0, 0, 0, 0)
        outils_group_layout.setSpacing(0)
        outils_group_layout.addWidget(self.btn_reencoder)
        outils_group_layout.addWidget(self.btn_organiser)
        outils_group_layout.addWidget(self.btn_qualite)
        outils_group_layout.addWidget(self.btn_outils)

        self._outils_expanded = True
        self._refresh_outils_group()

        historique_container = QWidget()
        historique_layout = QHBoxLayout(historique_container)
        historique_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_historique = QPushButton("  Historique")
        self.btn_historique.setProperty("class", "nav-btn")
        self.btn_historique.clicked.connect(self.show_historique_page)

        self.badge_historique = QLabel("0")
        self.badge_historique.setObjectName("badge")

        # Facteur d'étirement sur le bouton : il occupe toute la largeur
        # restante, exactement comme les autres entrées de navigation, et le
        # badge reste collé à droite. Sans cela le bouton garde sa taille
        # naturelle et QHBoxLayout centre la ligne, décalant « Historique »
        # vers la droite par rapport à « Musique ».
        historique_layout.addWidget(self.btn_historique, 1)
        historique_layout.addWidget(self.badge_historique)
        historique_layout.addSpacing(15)

        self.btn_parametres_sidebar = QPushButton("  Paramètres")
        self.btn_parametres_sidebar.setProperty("class", "nav-btn")
        self.btn_parametres_sidebar.clicked.connect(self.show_parametres_page)

        sidebar_layout.addWidget(self.btn_musique)
        sidebar_layout.addWidget(self.btn_bibliotheque)
        sidebar_layout.addWidget(self.btn_outils_group)
        sidebar_layout.addWidget(self.outils_group_container)
        sidebar_layout.addWidget(historique_container)
        sidebar_layout.addWidget(self.btn_parametres_sidebar)

        sidebar_layout.addStretch()

        # État de la base, en pied de barre latérale : l'information doit
        # être visible depuis n'importe quel écran, une désynchronisation ne
        # devant pas attendre une visite dans les Paramètres pour se savoir.
        self.lbl_sync_state = QLabel("Base locale")
        self.lbl_sync_state.setStyleSheet(
            "color: #849396; font-size: 11px; padding: 0 24px;")
        self.lbl_sync_state.setWordWrap(True)
        self.lbl_sync_state.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_sync_state.mousePressEvent = (
            lambda event: self.show_parametres_page())
        sidebar_layout.addWidget(self.lbl_sync_state)

        # ==================== 2. ZONE DE CONTENU PRINCIPAL MULTI-PAGES ====================
        self.stacked_widget = QStackedWidget()

        # --- PAGE 1 : MUSIQUE (LISTE DE TÉLÉCHARGEMENT) ---
        musique_page = QWidget()
        musique_layout = QVBoxLayout(musique_page)
        musique_layout.setContentsMargins(40, 30, 40, 24)
        musique_layout.setSpacing(20)

        lbl_stats_header = QLabel("STATUT ACTUEL")
        lbl_stats_header.setStyleSheet("font-size: 11px; font-weight: 600; color: #849396;")
        musique_layout.addWidget(lbl_stats_header)

        self.lbl_stats = QLabel("0 fichiers téléchargés , 0 fichiers abandonnés")
        self.lbl_stats.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 26px; font-weight: bold; color: #e5e2e1;"
        )
        musique_layout.addWidget(self.lbl_stats)

        # Progression globale
        global_progress_container = QWidget()
        global_progress_layout = QVBoxLayout(global_progress_container)
        global_progress_layout.setContentsMargins(0, 0, 0, 0)
        global_progress_layout.setSpacing(5)

        lbl_global_progress = QLabel("Progression globale du traitement :")
        lbl_global_progress.setStyleSheet("font-size: 12px; color: #849396; font-weight: 600;")

        self.global_progress_bar = QProgressBar()
        self.global_progress_bar.setObjectName("global_progress_bar")
        self.global_progress_bar.setValue(0)

        global_progress_layout.addWidget(lbl_global_progress)
        global_progress_layout.addWidget(self.global_progress_bar)
        musique_layout.addWidget(global_progress_container)

        # Actions de masse
        # Même disposition sur tous les écrans (voir ``styles.py``) : la barre
        # se lit au-dessus de la liste, la manipulation de la sélection à
        # gauche, les actions à droite — l'étape qui alimente la liste en
        # pilule ajourée, puis celle qui écrit sur le disque en pilule pleine.
        action_bar_layout = QHBoxLayout()
        action_bar_layout.setContentsMargins(0, 0, 0, 0)
        action_bar_layout.setSpacing(12)

        self.btn_all_main = QPushButton("Tout cocher")
        self.btn_all_main.setProperty("class", "control-btn")
        self.btn_all_main.clicked.connect(lambda: self.set_all_checked_main(True))

        self.btn_none_main = QPushButton("Tout décocher")
        self.btn_none_main.setProperty("class", "control-btn")
        self.btn_none_main.clicked.connect(lambda: self.set_all_checked_main(False))

        self.btn_supprimer = QPushButton("Retirer la sélection")
        self.btn_supprimer.setProperty("class", "control-btn")
        self.btn_supprimer.setToolTip(
            "Retirer de la liste les lignes cochées (les fichiers déjà "
            "extraits sont conservés sur le disque)"
        )
        self.btn_supprimer.clicked.connect(self.delete_selected_tracks)

        action_bar_layout.addWidget(self.btn_all_main)
        action_bar_layout.addWidget(self.btn_none_main)
        action_bar_layout.addWidget(self.btn_supprimer)
        action_bar_layout.addStretch()

        self.btn_purge = QPushButton("Purger les téléchargés")
        self.btn_purge.setProperty("class", "control-btn")
        self.btn_purge.setToolTip("Retirer de la liste toutes les lignes téléchargées avec succès")
        self.btn_purge.clicked.connect(self.purge_successful_downloads)

        self.btn_reessayer = QPushButton("Réessayer")
        self.btn_reessayer.setProperty("class", "control-btn")
        self.btn_reessayer.setToolTip("Relancer les téléchargements en échec")
        self.btn_reessayer.clicked.connect(self.retry_failed_downloads)

        self.btn_ajouter = QPushButton("Rechercher")
        self.btn_ajouter.setProperty("class", "accent-btn")
        self.btn_ajouter.setToolTip("Chercher des titres à ajouter à la liste")
        self.btn_ajouter.clicked.connect(self.open_add_track_dialog)

        self.btn_complet = QPushButton("Extraire")
        self.btn_complet.setProperty("class", "primary-btn")
        self.btn_complet.setToolTip("Télécharger tous les titres de la liste")
        self.btn_complet.clicked.connect(self.start_all_downloads)

        action_bar_layout.addWidget(self.btn_purge)
        action_bar_layout.addWidget(self.btn_reessayer)
        action_bar_layout.addWidget(self.btn_ajouter)
        action_bar_layout.addWidget(self.btn_complet)

        musique_layout.addLayout(action_bar_layout)

        # Tableau des téléchargements
        self.table_widget = QTableWidget()
        self.table_widget.setColumnCount(9)
        self.table_widget.setHorizontalHeaderLabels(["", "JAQUETTE", "ID", "TITRE", "ARTISTE", "ALBUM", "DURÉE / PROGRESSION", "STATUT", "ÉCOUTE"])
        self.table_widget.verticalHeader().setVisible(False)
        self.table_widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table_widget.verticalHeader().setDefaultSectionSize(55)

        header = self.table_widget.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_main_header_clicked)
        self.table_widget.cellClicked.connect(self._on_main_cell_clicked)
        # Un clic dans la colonne « ÉCOUTE » joue l'extrait sans rien ouvrir ;
        # le double-clic (et le menu contextuel) ouvrent le lecteur complet.
        self.table_widget.cellDoubleClicked.connect(self._on_main_cell_double_clicked)
        self.table_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_widget.customContextMenuRequested.connect(self._show_main_menu)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(MAIN_PLAY_COLUMN, QHeaderView.ResizeMode.Fixed)

        header.setMinimumSectionSize(90)
        self.table_widget.setColumnWidth(2, 85)
        self.table_widget.setColumnWidth(4, 130)
        self.table_widget.setColumnWidth(5, 130)
        # La largeur minimale des sections de ce tableau (90) s'applique aussi
        # ici : demander moins n'aurait aucun effet.
        self.table_widget.setColumnWidth(MAIN_PLAY_COLUMN, 90)

        musique_layout.addWidget(self.table_widget)
        self.stacked_widget.addWidget(musique_page)

        # --- PAGE 2 : HISTORIQUE COMPLET (RECHERCHES & TÉLÉCHARGEMENTS) ---
        historique_page = QWidget()
        historique_layout = QVBoxLayout(historique_page)
        historique_layout.setContentsMargins(40, 30, 40, 24)
        historique_layout.setSpacing(20)

        lbl_hist_title = QLabel("Historique d'utilisation")
        lbl_hist_title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 26px; font-weight: bold; color: #00e5ff;"
        )
        historique_layout.addWidget(lbl_hist_title)

        # Système d'onglets pour séparer l'historique
        self.tab_history = QTabWidget()

        # Onglet 1 : Recherches effectuées
        tab_search_widget = QWidget()
        tab_search_layout = QVBoxLayout(tab_search_widget)

        self.table_searches = QTableWidget()
        self.table_searches.setColumnCount(4)
        self.table_searches.setHorizontalHeaderLabels(["RECHERCHE", "CATEGORIE", "JAQUETTE", "ACTION"])
        self.table_searches.verticalHeader().setVisible(False)
        self.table_searches.verticalHeader().setDefaultSectionSize(56)
        # Colonnes ajustées au contenu ; la RECHERCHE occupe l'espace restant.
        # JAQUETTE et ACTION contiennent des widgets (ResizeToContents ne
        # mesure pas les widgets) : on leur fixe une largeur explicite.
        self.table_searches.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table_searches.horizontalHeader().setSectionsClickable(True)
        self.table_searches.horizontalHeader().setSortIndicatorShown(True)
        self.table_searches.horizontalHeader().sectionClicked.connect(self._on_searches_header_clicked)
        self.table_searches.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)           # RECHERCHE
        self.table_searches.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # CATEGORIE
        self.table_searches.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)             # JAQUETTE
        self.table_searches.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)             # ACTION
        self.table_searches.setColumnWidth(2, 110)
        self.table_searches.setColumnWidth(3, 130)

        tab_search_layout.addWidget(self.table_searches)
        self.tab_history.addTab(tab_search_widget, "Recherches effectuées")

        # Onglet 2 : Fichiers extraits
        tab_files_widget = QWidget()
        tab_files_layout = QVBoxLayout(tab_files_widget)

        self.table_history = QTableWidget()
        self.table_history.setColumnCount(6)
        self.table_history.setHorizontalHeaderLabels(["TITRE", "ARTISTE", "ALBUM", "CHEMIN LOCAL", "JAQUETTE", "ÉCOUTE"])
        self.table_history.verticalHeader().setVisible(False)
        self.table_history.verticalHeader().setDefaultSectionSize(56)
        self.table_history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_history.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # Chaque colonne s'ajuste à son contenu ; le CHEMIN LOCAL est
        # privilégié en occupant tout l'espace restant (Stretch).
        hist_header = self.table_history.horizontalHeader()
        hist_header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hist_header.setSectionsClickable(True)
        hist_header.setSortIndicatorShown(True)
        hist_header.sectionClicked.connect(self._on_history_header_clicked)
        self.table_history.cellClicked.connect(self._on_history_cell_clicked)
        self.table_history.cellDoubleClicked.connect(
            self._on_history_cell_double_clicked)
        self.table_history.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_history.customContextMenuRequested.connect(self._show_history_menu)
        hist_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)  # TITRE
        hist_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # ARTISTE
        hist_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # ALBUM
        hist_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)           # CHEMIN LOCAL
        # JAQUETTE contient une vignette (widget) : largeur fixe.
        hist_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)             # JAQUETTE
        hist_header.setSectionResizeMode(HIST_PLAY_COLUMN, QHeaderView.ResizeMode.Fixed)
        self.table_history.setColumnWidth(4, 110)
        self.table_history.setColumnWidth(HIST_PLAY_COLUMN, 70)

        # Barre d'actions au-dessus de la liste, comme sur tous les écrans
        # (voir ``styles.py``) : ici aucune sélection à manipuler, les deux
        # actions se rangent donc à droite.
        btn_purge_history = QPushButton("Purger les fichiers introuvables")
        btn_purge_history.setProperty("class", "control-btn")
        btn_purge_history.setToolTip(
            "Retirer de l'historique les téléchargements dont le fichier "
            "n'existe plus sur le disque"
        )
        btn_purge_history.clicked.connect(self.purge_missing_history_files)

        btn_clear_history = QPushButton("Vider l'historique")
        btn_clear_history.setProperty("class", "control-btn")
        btn_clear_history.setToolTip(
            "Retirer toutes les entrées de l'historique (les fichiers du "
            "disque sont conservés)"
        )
        btn_clear_history.clicked.connect(self.clear_visual_history)

        hist_btns_layout = QHBoxLayout()
        hist_btns_layout.setContentsMargins(0, 0, 0, 0)
        hist_btns_layout.setSpacing(12)
        hist_btns_layout.addStretch()
        hist_btns_layout.addWidget(btn_purge_history)
        hist_btns_layout.addWidget(btn_clear_history)
        tab_files_layout.addLayout(hist_btns_layout)

        tab_files_layout.addWidget(self.table_history)

        self.tab_history.addTab(tab_files_widget, "Téléchargements réussis")

        historique_layout.addWidget(self.tab_history)
        self.stacked_widget.addWidget(historique_page)

        # --- PAGE 3 : DIAGNOSTIC ---
        outils_page = QWidget()
        outils_layout = QVBoxLayout(outils_page)
        outils_layout.setContentsMargins(40, 30, 40, 24)
        outils_layout.setSpacing(25)

        lbl_outils_title = QLabel("Diagnostic")
        lbl_outils_title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 26px; font-weight: bold; color: #00e5ff;"
        )
        outils_layout.addWidget(lbl_outils_title)

        self.txt_diagnostic = QLabel()
        self.txt_diagnostic.setStyleSheet(
            "background-color: rgba(28, 30, 38, 0.60); padding: 24px; border-radius: 12px; "
            "font-size: 13px; line-height: 1.6; border: 1px solid rgba(255, 255, 255, 0.08);"
        )
        self.update_diagnostic_info()
        outils_layout.addWidget(self.txt_diagnostic)

        outils_layout.addStretch()
        self.stacked_widget.addWidget(outils_page)

        # --- PAGE 4 : PARAMÈTRES (DÉPORTÉS) ---
        parametres_page = QWidget()
        parametres_layout = QVBoxLayout(parametres_page)
        parametres_layout.setContentsMargins(40, 30, 40, 24)
        parametres_layout.setSpacing(25)

        lbl_params_title = QLabel("Paramètres d'exportation")
        lbl_params_title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 26px; font-weight: bold; color: #00e5ff;"
        )
        parametres_layout.addWidget(lbl_params_title)

        card_widget = QWidget()
        card_widget.setStyleSheet(
            "background-color: rgba(28, 30, 38, 0.60); border: 1px solid rgba(255, 255, 255, 0.08); "
            "border-radius: 12px; padding: 20px;"
        )
        card_layout = QVBoxLayout(card_widget)
        card_layout.setSpacing(15)

        # Paramétrage du format
        fmt_layout = QHBoxLayout()
        lbl_format = QLabel("Format de fichier d'export :")
        lbl_format.setStyleSheet("font-weight: bold; font-size: 13px;")
        self.combo_format = QComboBox()
        self.combo_format.addItems(["MP3", "FLAC", "WAV", "M4A"])
        self.combo_format.currentTextChanged.connect(self.on_format_changed)
        fmt_layout.addWidget(lbl_format)
        fmt_layout.addWidget(self.combo_format, 1)
        card_layout.addLayout(fmt_layout)

        # Paramétrage de la qualité
        qty_layout = QHBoxLayout()
        lbl_quality = QLabel("Qualité d'enregistrement :")
        lbl_quality.setStyleSheet("font-weight: bold; font-size: 13px;")
        self.combo_quality = QComboBox()
        self.combo_quality.addItems(["320 kbps (HQ)", "128 kbps (Standard)", "Lossless (Qualité Source)"])
        qty_layout.addWidget(lbl_quality)
        qty_layout.addWidget(self.combo_quality, 1)
        card_layout.addLayout(qty_layout)

        # Compte YouTube : réutiliser les cookies d'un navigateur pour que
        # yt-dlp s'identifie avec le compte Google qui y est connecté.
        yt_layout = QHBoxLayout()
        lbl_yt_account = QLabel("Compte YouTube (cookies du navigateur) :")
        lbl_yt_account.setStyleSheet("font-weight: bold; font-size: 13px;")
        self.combo_yt_browser = QComboBox()
        self.combo_yt_browser.addItems([
            "Aucun (anonyme)", "Chrome", "Edge", "Firefox",
            "Brave", "Opera", "Vivaldi",
        ])
        self.combo_yt_browser.setToolTip(
            "Réutilise la session YouTube du navigateur choisi.\n"
            "Pour changer de compte Google, changez le compte connecté\n"
            "sur youtube.com dans ce navigateur.\n"
            "Attention : sous Windows, seul Firefox fonctionne par cette\n"
            "méthode (Chrome/Edge chiffrent leurs cookies). Pour Chrome,\n"
            "utilisez plutôt un fichier cookies.txt ci-dessous."
        )
        yt_layout.addWidget(lbl_yt_account)
        yt_layout.addWidget(self.combo_yt_browser, 1)
        card_layout.addLayout(yt_layout)

        # Fichier cookies.txt : alternative fiable pour Chrome/Edge, dont les
        # cookies ne sont pas lisibles directement (chiffrement app-bound).
        cookies_layout = QHBoxLayout()
        self.lbl_cookies_file = QLabel()
        self.lbl_cookies_file.setStyleSheet("color: #bac9cc; font-size: 12px;")
        self._refresh_cookies_label()
        btn_cookies = QPushButton("Choisir cookies.txt")
        btn_cookies.setProperty("class", "control-btn")
        btn_cookies.setToolTip(
            "Fichier de cookies YouTube au format Netscape, exporté depuis\n"
            "votre navigateur avec l'extension « Get cookies.txt LOCALLY ».\n"
            "Prioritaire sur le choix du navigateur ci-dessus."
        )
        btn_cookies.clicked.connect(self.select_cookies_file)
        btn_cookies_clear = QPushButton("Retirer")
        btn_cookies_clear.setProperty("class", "control-btn")
        btn_cookies_clear.clicked.connect(self.clear_cookies_file)
        cookies_layout.addWidget(self.lbl_cookies_file, 1)
        cookies_layout.addWidget(btn_cookies)
        cookies_layout.addWidget(btn_cookies_clear)
        card_layout.addLayout(cookies_layout)

        # Gestion des sous-dossiers
        self.cb_org_artist = QCheckBox("Créer un sous-dossier par Artiste")
        self.cb_org_artist.setChecked(True)
        self.cb_org_artist.setStyleSheet("font-size: 13px;")
        card_layout.addWidget(self.cb_org_artist)

        self.cb_org_album = QCheckBox("Créer un sous-dossier par Album")
        self.cb_org_album.setChecked(True)
        self.cb_org_album.setStyleSheet("font-size: 13px;")
        card_layout.addWidget(self.cb_org_album)

        # Regroupement des artistes peu représentés : évite une bibliothèque
        # criblée de dossiers ne contenant qu'un ou deux titres.
        divers_layout = QHBoxLayout()
        self.cb_org_divers = QCheckBox(
            "Regrouper dans un dossier « Divers » les artistes de moins de"
        )
        self.cb_org_divers.setStyleSheet("font-size: 13px;")
        self.cb_org_divers.setToolTip(
            "À l'organisation de la bibliothèque, les artistes comptant trop\n"
            "peu de titres ne reçoivent pas leur propre dossier : leurs\n"
            "fichiers sont rassemblés dans un dossier « Divers ».\n"
            "À l'inverse, un titre déjà dans « Divers » rejoint le dossier de\n"
            "son artiste dès que celui-ci atteint le seuil."
        )
        self.spin_org_divers_min = QSpinBox()
        self.spin_org_divers_min.setRange(2, 10)
        self.spin_org_divers_min.setValue(3)
        self.spin_org_divers_min.setSuffix(" titres")
        # Pas de largeur fixe : la carte des Paramètres impose aux champs
        # qu'elle contient une marge intérieure généreuse, qui tronquerait
        # le suffixe (« 3 titre »).
        self.spin_org_divers_min.setMinimumWidth(160)
        divers_layout.addWidget(self.cb_org_divers)
        divers_layout.addWidget(self.spin_org_divers_min)
        divers_layout.addStretch()
        card_layout.addLayout(divers_layout)

        # Clé AcoustID : sans elle, la recherche des tags manquants se limite
        # aux catalogues interrogeables par texte (Deezer, MusicBrainz).
        acoustid_layout = QHBoxLayout()
        lbl_acoustid = QLabel("Clé AcoustID :")
        lbl_acoustid.setStyleSheet("font-size: 13px;")
        self.input_acoustid = QLineEdit()
        self.input_acoustid.setPlaceholderText(
            "facultative — identifie les fichiers sans tags par empreinte acoustique")
        self.input_acoustid.setToolTip(
            "Clé d'application AcoustID, gratuite sur acoustid.org/new-application.\n"
            "Elle permet, dans « Organiser » → « Compléter les tags », d'identifier\n"
            "un fichier par l'empreinte acoustique de son signal quand ni ses tags\n"
            "ni son nom ne disent rien. Le service refuse les appels anonymes.\n"
            "Nécessite aussi fpcalc (Chromaprint) ou un ffmpeg muni du muxer\n"
            "chromaprint."
        )
        acoustid_layout.addWidget(lbl_acoustid)
        acoustid_layout.addWidget(self.input_acoustid, 1)
        card_layout.addLayout(acoustid_layout)

        # Modification de dossier
        dir_layout = QHBoxLayout()
        self.lbl_current_dir = QLabel(f"Dossier de sortie actuel : {self.download_dir}")
        self.lbl_current_dir.setStyleSheet("color: #bac9cc; font-size: 12px;")
        btn_mod_dir = QPushButton("Modifier le dossier")
        btn_mod_dir.setProperty("class", "control-btn")
        btn_mod_dir.clicked.connect(self.select_download_folder)
        dir_layout.addWidget(self.lbl_current_dir, 1)
        dir_layout.addWidget(btn_mod_dir)
        card_layout.addLayout(dir_layout)

        parametres_layout.addWidget(card_widget)

        # --- Base de données (cache local + base en ligne) ---
        # Le contrôleur n'existe pas encore (il est créé après l'interface,
        # dans init_database) : la carte est construite plus tard, ici on
        # réserve seulement sa place.
        self._db_panel_slot = QVBoxLayout()
        parametres_layout.addLayout(self._db_panel_slot)

        parametres_layout.addStretch()
        self.stacked_widget.addWidget(parametres_page)

        # --- PAGE 5 : RÉENCODER (CONVERSION DE FICHIERS LOCAUX) ---
        self._build_reencoder_page()

        # --- PAGE 6 : ORGANISER (RANGEMENT DE LA BIBLIOTHÈQUE) ---
        # Page à part entière (et non fenêtre modale) : l'analyse d'une
        # grande bibliothèque tourne en tâche de fond, l'utilisateur peut
        # revenir à la recherche entre-temps.
        self.organize_page = OrganizePage(
            self.download_dir,
            app_settings=self.settings,
            save_callback=self.on_organize_options_changed,
        )
        # Un rangement déplace les fichiers : le dossier est relu, ce qui
        # rattrape au passage les chemins de l'historique (voir
        # ``_repair_history_paths``).
        self.organize_page.libraryChanged.connect(self.refresh_library_index)
        self.stacked_widget.addWidget(self.organize_page)

        # --- PAGE 7 : QUALITÉ (DÉFAUTS SONORES DE LA BIBLIOTHÈQUE) ---
        # Les réglages d'exportation lui sont fournis à la demande : un
        # retéléchargement doit suivre le format et le compte YouTube
        # affichés au moment où il est lancé, pas au démarrage.
        self.quality_page = QualityPage(
            self.download_dir,
            app_settings=self.settings,
            save_callback=self.save_config,
            export_callback=self.current_export_options,
        )
        self.stacked_widget.addWidget(self.quality_page)

        # --- PAGE 8 : BIBLIOTHÈQUE (LISTER, CHERCHER, ÉCOUTER) ---
        # La page se sert de la base et non du disque : elle s'ouvre sans
        # rien relire, et son archive (voir ``library_mirror``) tourne en
        # tâche de fond comme les autres analyses.
        self.library_page = LibraryPage(
            app_settings=self.settings,
            save_callback=self.save_config,
        )
        self.stacked_widget.addWidget(self.library_page)

        # Fin Assemblage
        global_layout.addWidget(sidebar)
        global_layout.addWidget(self.stacked_widget)

        # L'écran de téléchargement démarre vide : l'utilisateur ajoute
        # ses propres pistes via la recherche.

    def update_diagnostic_info(self):
        import shutil
        from deekeep.core.utils import ffmpeg_location

        ytdlp_status = "Installé ✓" if config.YTDLP_AVAILABLE else "Non Détecté ✗ (Veuillez faire 'pip install yt-dlp')"
        mutagen_status = "Installé ✓" if config.MUTAGEN_AVAILABLE else "Non Détecté ✗ (Veuillez faire 'pip install mutagen')"

        if ffmpeg_location():
            ffmpeg_status = "Embarqué avec l'application ✓"
        elif shutil.which("ffmpeg"):
            ffmpeg_status = "Détecté sur le système ✓"
        else:
            ffmpeg_status = "Non Détecté ✗ (la conversion audio échouera)"

        self.txt_diagnostic.setText(
            f"<b>RAPPORT DE DIAGNOSTIC COMPATIBILITÉ</b><br/><br/>"
            f"• <b>Moteur YouTube (yt-dlp) :</b> {ytdlp_status}<br/>"
            f"• <b>Moteur de Métadonnées (mutagen) :</b> {mutagen_status}<br/>"
            f"• <b>Convertisseur audio (FFmpeg) :</b> {ffmpeg_status}<br/><br/>"
            f"• <b>Dossier d'enregistrement actuel :</b> {self.download_dir}<br/><br/>"
            f"<i>Note : Si mutagen n'est pas détecté, les jaquettes et tags ID3 ne seront pas injectés à l'intérieur des fichiers extraits.</i>"
        )

    # --- CHANGEMENT DE PAGES LATÉRALES ---
    def toggle_outils_group(self):
        """Plie ou déplie le groupe « Outils » de la barre latérale."""
        self._outils_expanded = not self._outils_expanded
        self._refresh_outils_group()

    def _refresh_outils_group(self):
        """Synchronise le chevron de l'en-tête et l'affichage des sous-éléments."""
        chevron = "▾" if self._outils_expanded else "▸"
        self.btn_outils_group.setText(f"  {chevron}  Outils")
        self.outils_group_container.setVisible(self._outils_expanded)

    def _expand_outils_group(self):
        """Déplie le groupe : un écran actif ne doit jamais rester masqué."""
        if not self._outils_expanded:
            self._outils_expanded = True
            self._refresh_outils_group()

    def reset_nav_buttons(self):
        buttons = [self.btn_musique, self.btn_bibliotheque,
                   self.btn_reencoder, self.btn_organiser,
                   self.btn_qualite, self.btn_historique, self.btn_outils,
                   self.btn_parametres_sidebar]
        for btn in buttons:
            btn.setProperty("active", "false")

        # Rafraîchir les feuilles de style de Qt
        for btn in buttons:
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def show_musique_page(self):
        self.reset_nav_buttons()
        self.btn_musique.setProperty("active", "true")
        self.btn_musique.style().polish(self.btn_musique)
        self.stacked_widget.setCurrentIndex(0)

    def show_bibliotheque_page(self):
        """Affiche la page « Bibliothèque », relue depuis la base.

        La liste est relue à chaque affichage : un téléchargement, un
        rangement ou un retéléchargement a pu changer la table des titres
        depuis la dernière visite.
        """
        self.reset_nav_buttons()
        self.btn_bibliotheque.setProperty("active", "true")
        self.btn_bibliotheque.style().polish(self.btn_bibliotheque)
        self.library_page.sync_from_settings()
        self.library_page.reload()
        self.stacked_widget.setCurrentIndex(7)

    def show_historique_page(self):
        self.reset_nav_buttons()
        self.btn_historique.setProperty("active", "true")
        self.btn_historique.style().polish(self.btn_historique)
        self.stacked_widget.setCurrentIndex(1)
        self.refresh_history_table()
        self.refresh_searches_table()

    def show_outils_page(self):
        self.reset_nav_buttons()
        self._expand_outils_group()
        self.btn_outils.setProperty("active", "true")
        self.btn_outils.style().polish(self.btn_outils)
        self.stacked_widget.setCurrentIndex(2)
        self.update_diagnostic_info()

    def show_organiser_page(self):
        """Affiche la page « Organiser la bibliothèque ».

        Les options de rangement viennent des Paramètres d'exportation ;
        la page les resynchronise à chaque affichage et y réécrit les
        changements faits sur place.
        """
        self.reset_nav_buttons()
        self._expand_outils_group()
        self.btn_organiser.setProperty("active", "true")
        self.btn_organiser.style().polish(self.btn_organiser)
        self.organize_page.sync_from_settings()
        self.stacked_widget.setCurrentIndex(5)

    def show_qualite_page(self):
        """Affiche la page « Qualité des titres »."""
        self.reset_nav_buttons()
        self._expand_outils_group()
        self.btn_qualite.setProperty("active", "true")
        self.btn_qualite.style().polish(self.btn_qualite)
        self.quality_page.sync_from_settings()
        self.stacked_widget.setCurrentIndex(6)

    def show_parametres_page(self):
        self.reset_nav_buttons()
        self.btn_parametres_sidebar.setProperty("active", "true")
        self.btn_parametres_sidebar.style().polish(self.btn_parametres_sidebar)
        self.stacked_widget.setCurrentIndex(3)

    # ==================================================================
    # ÉCRAN « RÉENCODER » (conversion de fichiers locaux)
    # ==================================================================
    def _build_reencoder_page(self):
        """Construit la page de réencodage (design proche de la recherche)."""
        reencoder_page = QWidget()
        layout = QVBoxLayout(reencoder_page)
        layout.setContentsMargins(40, 30, 40, 24)
        layout.setSpacing(18)

        lbl_title = QLabel("Réencoder des fichiers")
        lbl_title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 26px; font-weight: bold; color: #00e5ff;"
        )
        layout.addWidget(lbl_title)

        lbl_sub = QLabel(
            "Ajoutez des fichiers MP3 : une correspondance Deezer est recherchée "
            "automatiquement, puis « Convertir » réextrait chaque titre au format "
            "des paramètres et remplace le fichier dans son dossier d'origine."
        )
        lbl_sub.setWordWrap(True)
        lbl_sub.setStyleSheet("color: #849396; font-size: 12px;")
        layout.addWidget(lbl_sub)

        # Barre d'actions
        # Même disposition sur tous les écrans (voir ``styles.py``) : la barre
        # se lit au-dessus de la liste, la manipulation de la sélection à
        # gauche, les actions à droite — l'étape qui alimente la liste en
        # pilule ajourée, puis celle qui écrit sur le disque en pilule pleine.
        action_layout = QHBoxLayout()
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(12)

        self.btn_reencode_all = QPushButton("Tout cocher")
        self.btn_reencode_all.setProperty("class", "control-btn")
        self.btn_reencode_all.clicked.connect(
            lambda: self.set_all_checked_reencode(True))

        self.btn_reencode_none = QPushButton("Tout décocher")
        self.btn_reencode_none.setProperty("class", "control-btn")
        self.btn_reencode_none.clicked.connect(
            lambda: self.set_all_checked_reencode(False))

        self.btn_reencode_add = QPushButton("Ajouter des fichiers")
        self.btn_reencode_add.setProperty("class", "accent-btn")
        self.btn_reencode_add.setToolTip("Parcourir et ajouter des fichiers audio")
        self.btn_reencode_add.clicked.connect(self.reencoder_add_files)

        self.btn_reencode_add_folder = QPushButton("Ajouter un dossier")
        self.btn_reencode_add_folder.setProperty("class", "control-btn")
        self.btn_reencode_add_folder.setToolTip(
            "Ajouter tout le contenu audio d'un dossier et de ses sous-dossiers"
        )
        self.btn_reencode_add_folder.clicked.connect(self.reencoder_add_folder)

        self.btn_reencode_remove = QPushButton("Retirer la sélection")
        self.btn_reencode_remove.setProperty("class", "control-btn")
        self.btn_reencode_remove.setToolTip(
            "Retirer de la liste les fichiers cochés (les fichiers du disque "
            "sont conservés)"
        )
        self.btn_reencode_remove.clicked.connect(self.reencoder_remove_selected)

        self.btn_reencode_purge = QPushButton("Purger les convertis")
        self.btn_reencode_purge.setProperty("class", "control-btn")
        self.btn_reencode_purge.setToolTip(
            "Retirer de la liste les fichiers réencodés avec succès"
        )
        self.btn_reencode_purge.clicked.connect(self.reencoder_purge_converted)

        self.btn_reencode_convert = QPushButton("Convertir")
        self.btn_reencode_convert.setProperty("class", "primary-btn")
        self.btn_reencode_convert.setToolTip(
            "Réextraire et réencoder les fichiers sélectionnés, puis les "
            "remplacer dans leur dossier d'origine"
        )
        self.btn_reencode_convert.clicked.connect(self.reencoder_convert)

        action_layout.addWidget(self.btn_reencode_all)
        action_layout.addWidget(self.btn_reencode_none)
        action_layout.addWidget(self.btn_reencode_remove)
        action_layout.addStretch()
        action_layout.addWidget(self.btn_reencode_purge)
        action_layout.addWidget(self.btn_reencode_add_folder)
        action_layout.addWidget(self.btn_reencode_add)
        action_layout.addWidget(self.btn_reencode_convert)
        layout.addLayout(action_layout)

        # Les progressions se lisent sous la barre d'actions, comme sur
        # l'écran « Qualité » : elles rendent compte de ce que la barre a
        # lancé.
        #
        # Barre de progression indéterminée (recherche de correspondances)
        self.reencode_progress = QProgressBar()
        self.reencode_progress.setRange(0, 0)
        self.reencode_progress.setVisible(False)
        self.reencode_progress.setStyleSheet("height: 8px; border: none; background: #1c1e26;")
        layout.addWidget(self.reencode_progress)

        # Progression globale de la conversion (10 fichiers traités à la fois)
        self.reencode_global_container = QWidget()
        gpc_layout = QVBoxLayout(self.reencode_global_container)
        gpc_layout.setContentsMargins(0, 0, 0, 0)
        gpc_layout.setSpacing(5)
        self.lbl_reencode_global = QLabel("Progression globale de la conversion :")
        self.lbl_reencode_global.setStyleSheet("font-size: 12px; color: #849396; font-weight: 600;")
        self.reencode_global_bar = QProgressBar()
        self.reencode_global_bar.setObjectName("global_progress_bar")
        self.reencode_global_bar.setValue(0)
        gpc_layout.addWidget(self.lbl_reencode_global)
        gpc_layout.addWidget(self.reencode_global_bar)
        self.reencode_global_container.setVisible(False)
        layout.addWidget(self.reencode_global_container)

        # Barre d'outils secondaire : filtre « format cible ».
        #
        # La recherche de doublons vivait ici, où elle ne portait que sur les
        # fichiers ajoutés en vue d'un réencodage. Elle a rejoint l'écran
        # « Organiser », qui analyse la bibliothèque entière et sait déjà quel
        # exemplaire garder : c'est là que la question se pose.
        tools_layout = QHBoxLayout()
        tools_layout.setSpacing(12)

        self.btn_reencode_filter = QPushButton("Masquer ceux déjà au format cible")
        self.btn_reencode_filter.setProperty("class", "control-btn")
        self.btn_reencode_filter.setCheckable(True)
        self.btn_reencode_filter.setToolTip(
            "Masquer les fichiers déjà dans le format et la qualité définis "
            "dans les Paramètres (rien à réencoder)"
        )
        self.btn_reencode_filter.toggled.connect(self._on_reencode_filter_toggled)

        tools_layout.addWidget(self.btn_reencode_filter)
        tools_layout.addStretch()
        layout.addLayout(tools_layout)

        # Tableau des fichiers
        self.table_reencode = QTableWidget()
        self.table_reencode.setColumnCount(9)
        self.table_reencode.setHorizontalHeaderLabels(
            ["", "ÉCOUTE", "JAQUETTE", "TITRE", "ARTISTE", "FICHIER",
             "FORMAT / DÉBIT", "CORRESPONDANCE", "STATUT"]
        )
        self.table_reencode.verticalHeader().setVisible(False)
        # Édition désactivée sauf la case à cocher (portée par un item
        # cochable) ; le focus reste possible pour permettre de cocher.
        self.table_reencode.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_reencode.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table_reencode.verticalHeader().setDefaultSectionSize(48)
        self.table_reencode.setIconSize(QSize(40, 40))
        self.table_reencode.itemChanged.connect(self._on_reencode_item_changed)
        self.table_reencode.cellClicked.connect(self._on_reencode_cell_clicked)
        self.table_reencode.cellDoubleClicked.connect(
            self._on_reencode_cell_double_clicked)
        self.table_reencode.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_reencode.customContextMenuRequested.connect(
            self._show_reencode_menu)

        header = self.table_reencode.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_reencode_header_clicked)
        header.setSectionResizeMode(RC_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(RC_PLAY, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(RC_COVER, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(RC_TITLE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(RC_ARTIST, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(RC_PATH, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(RC_FMT, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(RC_MATCH, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(RC_STATUS, QHeaderView.ResizeMode.Fixed)
        self.table_reencode.setColumnWidth(RC_PLAY, 66)
        self.table_reencode.setColumnWidth(RC_COVER, 60)
        self.table_reencode.setColumnWidth(RC_TITLE, 200)
        self.table_reencode.setColumnWidth(RC_ARTIST, 150)
        self.table_reencode.setColumnWidth(RC_FMT, 130)
        self.table_reencode.setColumnWidth(RC_MATCH, 130)
        self.table_reencode.setColumnWidth(RC_STATUS, 220)

        layout.addWidget(self.table_reencode, 1)

        self.lbl_reencode_hint = QLabel(
            "Aucun fichier. Ajoutez des fichiers ou un dossier entier "
            "(sous-dossiers inclus) pour commencer."
        )
        self.lbl_reencode_hint.setStyleSheet("color: #5f7d84; font-size: 12px;")
        layout.addWidget(self.lbl_reencode_hint)

        self.stacked_widget.addWidget(reencoder_page)

    def show_reencoder_page(self):
        self.reset_nav_buttons()
        self._expand_outils_group()
        self.btn_reencoder.setProperty("active", "true")
        self.btn_reencoder.style().polish(self.btn_reencoder)
        self.stacked_widget.setCurrentIndex(4)

    def _reencode_busy(self):
        """Vrai si des conversions sont en cours ou en attente (indices figés)."""
        return bool(self._reencode_workers) or bool(self._reencode_queue)

    def _reencode_can_add(self):
        """Vérifie qu'aucune conversion / analyse n'est en cours avant d'ajouter."""
        if self._reencode_busy():
            QMessageBox.information(
                self, "Conversion en cours",
                "Attendez la fin des conversions avant d'ajouter des fichiers."
            )
            return False
        if self._reencode_scan_thread and self._reencode_scan_thread.isRunning():
            QMessageBox.information(
                self, "Analyse en cours",
                "Une analyse de fichiers est déjà en cours."
            )
            return False
        return True

    def reencoder_add_files(self):
        """Sélecteur de fichiers audio (sélection multiple)."""
        if not self._reencode_can_add():
            return
        start_dir = self._reencode_last_dir if os.path.isdir(self._reencode_last_dir) else self.download_dir
        exts = " ".join(f"*{e}" for e in AUDIO_EXTENSIONS)
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choisir des fichiers à réencoder", start_dir,
            f"Fichiers audio ({exts});;Fichiers MP3 (*.mp3);;Tous les fichiers (*)"
        )
        if not paths:
            return
        self._reencode_last_dir = os.path.dirname(paths[0])
        self._start_local_scan(paths)

    def reencoder_add_folder(self):
        """Sélecteur de dossier : tout le contenu audio (sous-dossiers inclus)."""
        if not self._reencode_can_add():
            return
        start_dir = self._reencode_last_dir if os.path.isdir(self._reencode_last_dir) else self.download_dir
        folder = QFileDialog.getExistingDirectory(
            self, "Choisir un dossier de musique (sous-dossiers inclus)", start_dir
        )
        if not folder:
            return
        self._reencode_last_dir = folder
        self._start_local_scan([folder])

    def _start_local_scan(self, paths):
        """Analyse fichiers/dossiers en tâche de fond (lecture des tags)."""
        self.reencode_progress.setVisible(True)
        self.btn_reencode_add.setEnabled(False)
        self.btn_reencode_add_folder.setEnabled(False)
        thread = LocalScanThread(paths)
        thread.total.connect(self._on_local_scan_total)
        thread.progress.connect(self._on_local_scan_progress)
        thread.scanned.connect(self._on_local_scanned)
        self._reencode_scan_thread = thread
        self._local_scan_total = 0
        thread.start()

    def _on_local_scan_total(self, total):
        """Le parcours des dossiers est terminé : le total est connu."""
        self._local_scan_total = total

    def _on_local_scan_progress(self, count):
        total = getattr(self, "_local_scan_total", 0)
        suffix = f" / {total}" if total else ""
        self.lbl_reencode_hint.setVisible(True)
        self.lbl_reencode_hint.setText(
            f"Analyse en cours… {count}{suffix} fichier(s) analysé(s)")

    def _on_local_scanned(self, entries):
        self.btn_reencode_add.setEnabled(True)
        self.btn_reencode_add_folder.setEnabled(True)
        self._reencode_scan_thread = None
        added = self._reencode_ingest(entries)
        # Masquer le loader si aucune recherche de correspondance n'a démarré.
        if not self._reencode_match_threads:
            self.reencode_progress.setVisible(False)
        self.lbl_reencode_hint.setVisible(not self.reencode_files)
        self.lbl_reencode_hint.setText(
            "Aucun fichier. Ajoutez des fichiers ou un dossier entier "
            "(sous-dossiers inclus) pour commencer."
        )
        if entries and added == 0:
            QMessageBox.information(
                self, "Aucun ajout",
                "Tous les fichiers trouvés sont déjà présents dans la liste."
            )

    def _reencode_ingest(self, entries):
        """Ajoute des entrées {path,title,artist,album} à la liste (dédupliquées).

        Retourne le nombre de nouvelles entrées réellement ajoutées et lance
        la recherche automatique de correspondances Deezer sur celles-ci.
        """
        existing = {os.path.abspath(f["path"]) for f in self.reencode_files}
        new_items = []
        for entry in entries:
            path = entry.get("path")
            if not path or os.path.abspath(path) in existing:
                continue
            self._reencode_uid += 1
            item = {
                "path": path,
                "title": entry.get("title", ""),
                "artist": entry.get("artist", ""),
                "album": entry.get("album", ""),
                "bitrate": entry.get("bitrate", 0),
                "format": entry.get("format", ""),
                "match": None, "cover_bytes": None, "cover_url": None,
                "status": "En attente", "selected": False, "_searched": False,
                "_uid": self._reencode_uid,
            }
            self._reencode_uid_map[item["_uid"]] = item
            self.reencode_files.append(item)
            existing.add(os.path.abspath(path))
            new_items.append(item)

        # La recherche automatique de correspondances Deezer effectue une
        # requête réseau par fichier : impraticable pour de grandes
        # bibliothèques. Au-delà d'un seuil, on la saute et l'on présélectionne
        # les fichiers (le réencodage s'appuie alors sur les tags locaux).
        auto_match = len(new_items) <= self.REENCODE_AUTOMATCH_LIMIT
        if not auto_match:
            for item in new_items:
                item["_skipped_match"] = True
                item["selected"] = True

        self._render_reencode_table()
        if new_items:
            if auto_match:
                self._launch_reencode_match(new_items)
            else:
                QMessageBox.information(
                    self, "Bibliothèque volumineuse",
                    f"{len(new_items)} fichiers ajoutés. La recherche automatique "
                    "de correspondances Deezer (jaquettes) a été désactivée pour "
                    "préserver la fluidité ; les fichiers sont présélectionnés et "
                    "seront réencodés à partir de leurs métadonnées locales.\n\n"
                    "Astuce : utilisez « Masquer ceux déjà au format cible » pour "
                    "ne convertir que le nécessaire."
                )
        return len(new_items)

    # --- Rendu du tableau ---------------------------------------------
    # Le tableau utilise uniquement des QTableWidgetItem (pas de widgets par
    # ligne) : indispensable pour rester fluide avec des milliers de fichiers.
    def _render_reencode_table(self):
        table = self.table_reencode
        self._rc_rendering = True
        table.setUpdatesEnabled(False)
        table.setRowCount(len(self.reencode_files))
        for row, item in enumerate(self.reencode_files):
            item["_row"] = row

            # Case de sélection (état coché porté par l'item, pas de widget)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(
                Qt.CheckState.Checked if item.get("selected") else Qt.CheckState.Unchecked
            )
            table.setItem(row, RC_CHECK, chk)

            table.setItem(row, RC_PLAY, player.play_item(
                "Écouter ce fichier dans le lecteur"))

            table.setItem(row, RC_TITLE, QTableWidgetItem(item.get("title", "")))
            table.setItem(row, RC_ARTIST, QTableWidgetItem(item.get("artist", "")))
            path_item = QTableWidgetItem(item.get("path", ""))
            path_item.setToolTip(item.get("path", ""))
            table.setItem(row, RC_PATH, path_item)
            table.setItem(row, RC_FMT, QTableWidgetItem(self._format_label(item)))

            self._set_cover_cell(row, item)
            self._set_match_cell(row, item)
            self._set_status_cell(row, item.get("status", "En attente"),
                                  item.get("error", ""))

        table.setUpdatesEnabled(True)
        self._rc_rendering = False

        self.lbl_reencode_hint.setVisible(not self.reencode_files)
        self._apply_reencode_filter()

    def _format_label(self, item):
        """Libellé « FORMAT · débit » (ex. « MP3 · 320 kbps »)."""
        fmt = item.get("format", "") or "?"
        br = item.get("bitrate", 0)
        return f"{fmt} · {br} kbps" if br else fmt

    def _set_cover_cell(self, row, item):
        cell = self.table_reencode.item(row, RC_COVER)
        if cell is None:
            cell = QTableWidgetItem()
            cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table_reencode.setItem(row, RC_COVER, cell)
        data = item.get("cover_bytes")
        if data:
            pix = QPixmap()
            pix.loadFromData(data)
            if not pix.isNull():
                cell.setIcon(QIcon(pix))
                cell.setText("")
                return
        cell.setText("🎵")

    def _set_match_cell(self, row, item):
        cell = self.table_reencode.item(row, RC_MATCH)
        if cell is None:
            cell = QTableWidgetItem()
            self.table_reencode.setItem(row, RC_MATCH, cell)
        if item.get("match"):
            cell.setText("✓ Trouvé")
            cell.setForeground(QColor("#5eeb7f"))
        elif item.get("_skipped_match"):
            cell.setText("—")
            cell.setForeground(QColor("#849396"))
            cell.setToolTip("Correspondance non recherchée (trop de fichiers)")
        elif item.get("_searched"):
            cell.setText("✗ Introuvable")
            cell.setForeground(QColor("#ffb4ab"))
        else:
            cell.setText("⏳")
            cell.setForeground(QColor("#849396"))

    def _set_status_cell(self, row, text, reason=""):
        """Écrit le statut d'une ligne, la cause de l'échec en infobulle.

        « Erreur » seul n'apprend rien : la raison — vidéo introuvable, tags
        qui ne mènent à rien, vérification d'âge — ne partait que sur la
        console, invisible depuis l'application. Elle s'affiche désormais au
        survol de la cellule.
        """
        cell = self.table_reencode.item(row, RC_STATUS)
        if cell is None:
            cell = QTableWidgetItem()
            self.table_reencode.setItem(row, RC_STATUS, cell)
        cell.setText(text)
        cell.setToolTip(reason or text)

    def _row_of_item(self, item):
        # Utilise l'indice mémorisé lors du rendu (O(1)) et le valide ; repli
        # sur une recherche linéaire seulement si l'indice n'est plus valable.
        row = item.get("_row", -1)
        if 0 <= row < len(self.reencode_files) and self.reencode_files[row] is item:
            return row
        try:
            return self.reencode_files.index(item)
        except ValueError:
            return -1

    def _update_reencode_row(self, item):
        """Rafraîchit les cellules dynamiques d'une ligne (jaquette/match/case)."""
        row = self._row_of_item(item)
        if row < 0:
            return
        self._set_cover_cell(row, item)
        self._set_match_cell(row, item)
        chk = self.table_reencode.item(row, RC_CHECK)
        if chk is not None:
            want = Qt.CheckState.Checked if item.get("selected") else Qt.CheckState.Unchecked
            if chk.checkState() != want:
                self._rc_rendering = True
                chk.setCheckState(want)
                self._rc_rendering = False

    # --- Recherche automatique de correspondances ---------------------
    def _launch_reencode_match(self, items):
        payload = [(it["_uid"], it.get("artist", ""), it.get("title", "")) for it in items]
        if not payload:
            return
        self.reencode_progress.setVisible(True)
        thread = DeezerMatchThread(payload)
        thread.match_ready.connect(self._on_reencode_match_ready)
        thread.finished_signal.connect(self._on_reencode_match_finished)
        self._reencode_match_threads.append(thread)
        thread.start()

    def _on_reencode_match_ready(self, uid, match):
        item = self._reencode_uid_map.get(uid)
        if not item:
            return
        item["_searched"] = True
        if match:
            item["match"] = match
            item["cover_url"] = match.get("cover_url")
            # Une correspondance trouvée présélectionne le fichier ; les
            # fichiers sans correspondance restent décochés par défaut.
            item["selected"] = True
            # Chaque jaquette est téléchargée dans son propre thread : les
            # images arrivent en parallèle et s'affichent dès qu'elles sont
            # prêtes, sans attendre la fin des autres recherches.
            self._fetch_reencode_cover(item)
        self._update_reencode_row(item)

    def _on_reencode_match_finished(self):
        # Masquer le loader lorsque plus aucun thread de recherche n'est actif.
        self._reencode_match_threads = [
            t for t in self._reencode_match_threads if t.isRunning()
        ]
        if not self._reencode_match_threads:
            self.reencode_progress.setVisible(False)

    def _fetch_reencode_cover(self, item):
        """Télécharge la jaquette d'un fichier dans un thread dédié."""
        url = item.get("cover_url")
        if not url or item.get("cover_bytes"):
            return
        thread = CoverDownloadThread([(item["_uid"], url)], size="small")
        thread.cover_ready.connect(self._on_reencode_cover_ready)
        # Nettoyer la référence au thread une fois terminé (évite l'accumulation).
        thread.finished_signal.connect(self._prune_reencode_cover_threads)
        self._reencode_cover_threads.append(thread)
        thread.start()

    def _prune_reencode_cover_threads(self):
        self._reencode_cover_threads = [
            t for t in self._reencode_cover_threads if t.isRunning()
        ]

    def _on_reencode_cover_ready(self, uid, data):
        item = self._reencode_uid_map.get(uid)
        if not item:
            return
        item["cover_bytes"] = data
        self._update_reencode_row(item)

    # --- Sélection ----------------------------------------------------
    def _on_reencode_item_changed(self, twitem):
        """Case à cocher d'une ligne modifiée par l'utilisateur."""
        if self._rc_rendering or twitem.column() != RC_CHECK:
            return
        row = twitem.row()
        if 0 <= row < len(self.reencode_files):
            self.reencode_files[row]["selected"] = (
                twitem.checkState() == Qt.CheckState.Checked
            )

    def set_all_checked_reencode(self, check):
        # N'agit que sur les lignes visibles (respecte le filtre actif).
        self._rc_rendering = True
        for row in range(self.table_reencode.rowCount()):
            if self.table_reencode.isRowHidden(row):
                continue
            if 0 <= row < len(self.reencode_files):
                self.reencode_files[row]["selected"] = check
            chk = self.table_reencode.item(row, RC_CHECK)
            if chk is not None:
                chk.setCheckState(
                    Qt.CheckState.Checked if check else Qt.CheckState.Unchecked
                )
        self._rc_rendering = False

    def reencoder_remove_selected(self):
        if self._reencode_busy():
            QMessageBox.information(
                self, "Conversion en cours",
                "Attendez la fin des conversions avant de retirer des fichiers."
            )
            return
        removed = [f for f in self.reencode_files if f.get("selected")]
        if not removed:
            return
        for f in removed:
            self._reencode_uid_map.pop(f.get("_uid"), None)
        self.reencode_files = [f for f in self.reencode_files if not f.get("selected")]
        self._render_reencode_table()

    def reencoder_purge_converted(self):
        """Retire de la liste les fichiers réencodés avec succès."""
        if self._reencode_busy():
            QMessageBox.information(
                self, "Conversion en cours",
                "Attendez la fin des conversions avant de purger."
            )
            return
        converted = [f for f in self.reencode_files if "✓" in f.get("status", "")]
        if not converted:
            QMessageBox.information(self, "Purge", "Aucun fichier converti à purger.")
            return
        for f in converted:
            self._reencode_uid_map.pop(f.get("_uid"), None)
        self.reencode_files = [
            f for f in self.reencode_files if "✓" not in f.get("status", "")
        ]
        self._render_reencode_table()
        QMessageBox.information(
            self, "Purge",
            f"{len(converted)} fichier(s) converti(s) retiré(s) de la liste."
        )

    # --- Tri ----------------------------------------------------------
    def _on_reencode_header_clicked(self, col):
        if self._reencode_busy():
            return  # indices figés pendant les conversions
        keys = {
            RC_TITLE: "title", RC_ARTIST: "artist", RC_PATH: "path",
            RC_FMT: "format", RC_MATCH: "match",
        }
        if col not in keys:
            return
        key = keys[col]

        prev_col, prev_order = self._reencode_sort
        if col == prev_col:
            order = (
                Qt.SortOrder.DescendingOrder
                if prev_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            order = Qt.SortOrder.AscendingOrder
        self._reencode_sort = (col, order)
        reverse = order == Qt.SortOrder.DescendingOrder

        if key == "match":
            sort_key = lambda f: 1 if f.get("match") else 0
        elif key == "format":
            # Trier par format puis par débit (regroupe MP3 320, MP3 128, etc.).
            sort_key = lambda f: ((f.get("format") or "").lower(), f.get("bitrate") or 0)
        else:
            sort_key = lambda f: (f.get(key) or "").lower()
        self.reencode_files.sort(key=sort_key, reverse=reverse)

        self._render_reencode_table()
        self.table_reencode.horizontalHeader().setSortIndicator(col, order)

    # --- Conversion ---------------------------------------------------
    def reencoder_convert(self):
        if self._reencode_busy():
            QMessageBox.information(
                self, "Conversion en cours",
                "Des conversions sont déjà en cours."
            )
            return
        if not config.YTDLP_AVAILABLE:
            QMessageBox.warning(
                self, "Extraction indisponible",
                "yt-dlp n'est pas installé. Installez-le via : pip install yt-dlp"
            )
            return

        # Lignes sélectionnées et visibles (le filtre « déjà au format cible »
        # exclut donc les fichiers masqués de la conversion).
        selected_rows = [
            row for row, item in enumerate(self.reencode_files)
            if item.get("selected") and not self.table_reencode.isRowHidden(row)
        ]
        if not selected_rows:
            QMessageBox.information(
                self, "Aucune sélection",
                "Sélectionnez au moins un fichier à réencoder (visible)."
            )
            return

        # Les fichiers sans correspondance préalable (grande bibliothèque)
        # interrogent Deezer pendant la conversion pour récupérer jaquette et
        # métadonnées : aucune confirmation nécessaire, tout est récupéré.

        # Préparer le lot : file d'attente + progression globale, puis
        # démarrer les premières conversions (10 au maximum en parallèle).
        self._reencode_batch = list(selected_rows)
        self._reencode_queue = list(selected_rows)
        for row in selected_rows:
            self.reencode_files[row]["status"] = "En attente..."
            self.reencode_files[row]["_pct"] = 0
            self._set_status_cell(row, "En attente...")
        self.reencode_global_container.setVisible(True)
        self.reencode_global_bar.setValue(0)
        self._set_reencode_controls_enabled(False)
        self._reencode_pump()

    def _reencode_pump(self):
        """Démarre des conversions tant qu'un emplacement est libre (max 10)."""
        while (self._reencode_queue
               and len(self._reencode_workers) < self.REENCODE_MAX_CONCURRENT):
            row = self._reencode_queue.pop(0)
            self._launch_reencode_worker(row)

    def _launch_reencode_worker(self, row):
        item = self.reencode_files[row]
        match = item.get("match") or {}

        # Métadonnées Deezer si disponibles, sinon celles du fichier local.
        title = match.get("title") or item.get("title") or ""
        artist = match.get("artist") or item.get("artist") or ""
        album = match.get("album") or item.get("album") or ""
        track_id = match.get("id", "")

        save_dir = os.path.dirname(item["path"])
        cover_bytes = item.get("cover_bytes")
        file_format = self.combo_format.currentText()
        quality = self.combo_quality.currentText()
        browser_choice = self.combo_yt_browser.currentText()
        cookies_browser = None if browser_choice.startswith("Aucun") else browser_choice.lower()

        # Sans correspondance préalable (grande bibliothèque), on résout la
        # jaquette et les métadonnées Deezer au moment de la conversion : le
        # débit étant limité à 5 conversions simultanées, cela ne sature pas
        # le réseau tout en récupérant « tout » pour chaque fichier.
        resolve = not bool(match)

        item["status"] = "Initialisation..."
        self._set_reencode_status(row, 5, "Initialisation...")

        worker = TrackDownloaderThread(
            row, track_id, title, artist, album, save_dir,
            file_format, quality, cover_bytes,
            # Réencoder convertit sur place : le fichier ne change ni de
            # dossier ni de bibliothèque, seul son format évolue. Ranger ici
            # déplacerait des fichiers que l'utilisateur n'a désignés que
            # pour leur format — c'est le rôle d'« Organiser ».
            layout=None,
            cookies_browser=cookies_browser, cookies_file=self.cookies_file,
            original_path=item["path"], resolve_metadata=resolve,
        )
        worker.progress_signal.connect(self._on_reencode_progress)
        worker.finished_signal.connect(self._on_reencode_finished)
        self._reencode_workers[row] = worker
        worker.start()

    def _set_reencode_status(self, row, percent, text):
        if 0 <= row < len(self.reencode_files):
            self.reencode_files[row]["_pct"] = percent
        self._set_status_cell(row, text)

    def _on_reencode_progress(self, row, percent, text):
        if 0 <= row < len(self.reencode_files):
            self.reencode_files[row]["status"] = text
            self.reencode_files[row]["_pct"] = percent
        self._set_status_cell(row, f"{text} — {percent}%")
        self._update_reencode_global_progress()

    def _update_reencode_global_progress(self):
        """Moyenne de progression des lignes du lot de conversion courant."""
        rows = self._reencode_batch
        if not rows:
            return
        total = sum(
            self.reencode_files[r].get("_pct", 0)
            for r in rows if 0 <= r < len(self.reencode_files)
        )
        self.reencode_global_bar.setValue(int(total / len(rows)))

    def _on_reencode_finished(self, row, success, msg, track_info):
        if 0 <= row < len(self.reencode_files):
            item = self.reencode_files[row]
            item["_pct"] = 100 if success else 0
            # Le fichier réencodé remplace éventuellement l'original : mettre
            # à jour le chemin affiché sur le nouveau fichier produit.
            new_path = track_info.get("path") if track_info else None
            if success and new_path:
                item["path"] = new_path
                path_item = self.table_reencode.item(row, RC_PATH)
                if path_item:
                    path_item.setText(new_path)
                    path_item.setToolTip(new_path)
                # Le fichier produit adopte le format/débit cible : refléter
                # la nouvelle valeur affichée.
                item["format"] = self.combo_format.currentText().upper()
                item["bitrate"] = self._target_bitrate()
                fmt_item = self.table_reencode.item(row, RC_FMT)
                if fmt_item:
                    fmt_item.setText(self._format_label(item))

                # Métadonnées / jaquette récupérées pendant la conversion :
                # refléter le titre, l'artiste, la jaquette et la correspondance.
                new_title = track_info.get("title")
                new_artist = track_info.get("artist")
                if new_title:
                    item["title"] = new_title
                    ti = self.table_reencode.item(row, RC_TITLE)
                    if ti:
                        ti.setText(new_title)
                if new_artist:
                    item["artist"] = new_artist
                    ai = self.table_reencode.item(row, RC_ARTIST)
                    if ai:
                        ai.setText(new_artist)
                cover_b64 = track_info.get("cover_b64")
                if cover_b64:
                    try:
                        import base64 as _b64
                        item["cover_bytes"] = _b64.b64decode(cover_b64)
                    except Exception:
                        pass
                    item["match"] = item.get("match") or {"resolved": True}
                    item["_skipped_match"] = False
                    self._set_cover_cell(row, item)
                    self._set_match_cell(row, item)
            item["status"] = "Réencodé ✓" if success else "Erreur"
            # La cause de l'échec reste attachée à la ligne : elle survit au
            # tri comme au filtrage, qui reconstruisent le tableau.
            item["error"] = "" if success else msg

        self._set_status_cell(row, "Réencodé ✓" if success else "Erreur",
                              "" if success else msg)
        if not success:
            print(f"[Réencodage ligne {row}] {msg}")

        self._reencode_workers.pop(row, None)
        self._update_reencode_global_progress()

        # Démarrer la conversion suivante en file d'attente, s'il en reste.
        self._reencode_pump()

        # Lot terminé : plus aucun worker actif ni en attente.
        if not self._reencode_workers and not self._reencode_queue:
            self._set_reencode_controls_enabled(True)

    def _set_reencode_controls_enabled(self, enabled):
        for btn in (self.btn_reencode_add, self.btn_reencode_remove,
                    self.btn_reencode_purge, self.btn_reencode_convert):
            btn.setEnabled(enabled)

    # --- Filtre « déjà au format cible » ------------------------------
    def _target_bitrate(self):
        """Débit cible en kbps d'après les paramètres (0 = sans objet/lossless)."""
        quality = self.combo_quality.currentText()
        if "128" in quality:
            return 128
        if "320" in quality:
            return 320
        return 0

    def _is_at_target(self, item):
        """Vrai si le fichier est déjà exactement au format et à la qualité cibles.

        Le débit doit *correspondre* au débit cible (et non simplement l'égaler
        ou le dépasser) : avec une cible à 128 kbps, un fichier à 320 kbps
        reste à convertir (rééchantillonnage vers le bas) et ne doit donc pas
        être masqué. Une petite tolérance couvre les arrondis (VBR).
        """
        target_fmt = self.combo_format.currentText().upper()
        if (item.get("format") or "").upper() != target_fmt:
            return False
        if target_fmt in ("FLAC", "WAV"):
            return True  # sans perte : le format seul suffit
        target = self._target_bitrate()
        if target == 0:
            return True
        return abs((item.get("bitrate") or 0) - target) <= 5

    def _apply_reencode_filter(self):
        hide = self._reencode_hide_at_target
        for row in range(self.table_reencode.rowCount()):
            if 0 <= row < len(self.reencode_files):
                at_target = self._is_at_target(self.reencode_files[row])
                self.table_reencode.setRowHidden(row, hide and at_target)

    def _on_reencode_filter_toggled(self, checked):
        self._reencode_hide_at_target = checked
        self.btn_reencode_filter.setText(
            "Afficher tous les fichiers" if checked
            else "Masquer ceux déjà au format cible"
        )
        self._apply_reencode_filter()

    def _stop_reencode_threads(self):
        threads = list(self._reencode_match_threads) + list(self._reencode_cover_threads)
        if self._reencode_scan_thread is not None:
            threads.append(self._reencode_scan_thread)
        for thread in threads:
            if thread.isRunning():
                thread.stop()
                thread.wait(2000)
        for worker in list(self._reencode_workers.values()):
            if worker.isRunning():
                worker.wait(2000)

    def on_format_changed(self, file_format):
        self.combo_quality.clear()
        if file_format in ["FLAC", "WAV"]:
            self.combo_quality.addItem("Lossless (Qualité Source)")
        else:
            self.combo_quality.addItems(["320 kbps (HQ)", "128 kbps (Standard)"])

    def refresh_searches_table(self):
        # Arrêter d'éventuels threads de jaquettes de recherche encore actifs
        for t in getattr(self, "_search_cover_threads", []):
            if t.isRunning():
                t.wait(2000)
        self._search_cover_threads = []

        self.table_searches.setRowCount(0)
        for row, query in enumerate(self.search_history):
            self.table_searches.insertRow(row)
            self.table_searches.setItem(row, 0, QTableWidgetItem(query))
            self.table_searches.setItem(row, 1, QTableWidgetItem("Recherche Globale"))

            # Jaquette : pochette du premier résultat de la recherche.
            self.table_searches.setCellWidget(row, 2, self._make_search_cover(query))

            # Hauteur de ligne explicite : garantit assez de place pour
            # centrer le bouton sans que ses coins arrondis soient rognés.
            self.table_searches.setRowHeight(row, 58)

            # Bouton "Relancer" centré dans un conteneur.
            container = QWidget()
            hl = QHBoxLayout(container)
            hl.setContentsMargins(0, 0, 0, 0)
            btn_relaunch = QPushButton("Relancer")
            btn_relaunch.setFixedSize(96, 32)
            btn_relaunch.setStyleSheet(
                "QPushButton {"
                "  background-color: #00e5ff; color: #00363d; border: none;"
                "  padding: 0px 16px; border-radius: 16px; font-weight: bold; font-size: 12px;"
                "}"
                "QPushButton:hover { background-color: #5cefff; }"
            )
            btn_relaunch.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_relaunch.clicked.connect(lambda checked, q=query: self.relaunch_search_from_history(q))
            hl.addWidget(btn_relaunch, alignment=Qt.AlignmentFlag.AlignCenter)
            self.table_searches.setCellWidget(row, 3, container)

    def _make_search_cover(self, query):
        """Vignette d'une recherche : pochette du premier résultat Deezer."""
        cover = QLabel()
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover.setFixedSize(46, 46)
        cover.setText("⏳")
        cover.setStyleSheet("background-color: #1c1e26; border-radius: 4px; color: #849396; font-size: 16px;")

        thread = CoverLookupThread("", query, None)
        thread.target_label = cover
        thread.target_item = None
        thread.result_signal.connect(self._on_history_cover_ready)
        self._search_cover_threads.append(thread)
        thread.start()
        return cover

    def relaunch_search_from_history(self, query):
        self.show_musique_page()
        # Comme pour une recherche neuve : le dossier de dépôt est relu, et la
        # fenêtre dit ce qui s'y trouve déjà (cf. ``open_add_track_dialog``).
        self.refresh_library_index()
        dialog = AddTrackDialog(self, self.search_history, self.library_index)
        dialog.input_search.setText(query)
        # Déclencher la recherche une fois la boucle d'événements du dialogue démarrée
        QTimer.singleShot(0, dialog.start_search)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_tracks = dialog.get_selected_tracks()
            self.search_history = dialog.search_history
            for track in selected_tracks:
                if track and track["id"]:
                    self.add_table_row(
                        track["id"],
                        track["title"],
                        track["artist"],
                        track["album"],
                        track["duration"],
                        track.get("cover_bytes"),
                        track.get("cover_url"),
                        preview_url=track.get("preview") or ""
                    )
            self._fetch_missing_covers()
        self.refresh_searches_table()
        self.update_stats_label()
        self.save_config()

    def refresh_history_table(self):
        # Arrêter d'éventuels threads de jaquettes encore actifs
        self._stop_history_cover_threads()
        self._hist_cover_threads = []

        self.table_history.setRowCount(0)
        for row, item in enumerate(self.downloaded_history):
            self.table_history.insertRow(row)
            self.table_history.setItem(row, 0, QTableWidgetItem(item.get("title", "")))
            self.table_history.setItem(row, 1, QTableWidgetItem(item.get("artist", "")))
            self.table_history.setItem(row, 2, QTableWidgetItem(item.get("album", "")))
            self.table_history.setItem(row, 3, QTableWidgetItem(item.get("path", "")))

            self.table_history.setCellWidget(row, 4, self._make_history_cover(item))
            self.table_history.setItem(
                row, HIST_PLAY_COLUMN,
                player.play_item("Écouter ce titre dans le lecteur"))

    def _make_history_cover(self, item):
        """Cellule 'JAQUETTE' : miniature de la pochette.

        Ordre de résolution : jaquette déjà stockée (base64) → pochette
        intégrée au fichier local (hors-ligne) → recherche Deezer en tâche
        de fond. Un clic agrandit l'image.
        """
        import base64

        cover = QLabel()
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover.setFixedSize(46, 46)
        cover.setCursor(Qt.CursorShape.PointingHandCursor)
        cover.setToolTip("Cliquer pour agrandir")
        cover.mousePressEvent = lambda event, it=item: self.show_cover_popup(it)

        # 1) Jaquette déjà stockée
        data = None
        if item.get("cover_b64"):
            try:
                data = base64.b64decode(item["cover_b64"])
            except Exception:
                data = None
        # 2) Pochette intégrée au fichier local
        if not data:
            data = self._read_embedded_cover(item.get("path", ""))
            if data:
                try:
                    item["cover_b64"] = base64.b64encode(data).decode("ascii")
                except Exception:
                    pass
        if data:
            self._set_thumb(cover, data)
            return cover

        # 3) Recherche asynchrone sur Deezer (loader en attendant)
        cover.setText("⏳")
        cover.setStyleSheet("background-color: #1c1e26; border-radius: 4px; color: #849396; font-size: 16px;")
        thread = CoverLookupThread(item.get("artist", ""), item.get("title", ""), item.get("cover_url"))
        # Le label et l'entrée cibles voyagent avec le thread : le slot
        # (méthode liée de self, dans le thread GUI) les relira via sender().
        thread.target_label = cover
        thread.target_item = item
        thread.result_signal.connect(self._on_history_cover_ready)
        self._hist_cover_threads.append(thread)
        thread.start()
        return cover

    def _set_thumb(self, label, data, size=46, radius=6):
        label.setPixmap(self._rounded_pixmap(data, size, radius))
        label.setText("")
        label.setStyleSheet("background: transparent;")

    def _rounded_pixmap(self, data, size, radius):
        """Recadre l'image en carré centré et arrondit réellement ses coins."""
        src = QPixmap()
        src.loadFromData(data)
        if src.isNull():
            return src
        # Recadrage centré en carré (évite toute déformation)
        side = min(src.width(), src.height())
        if src.width() != src.height():
            x = (src.width() - side) // 2
            y = (src.height() - side) // 2
            src = src.copy(x, y, side, side)
        src = src.scaled(
            size, size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        rounded = QPixmap(size, size)
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, size, size, radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, src)
        painter.end()
        return rounded

    def _on_history_cover_ready(self, data):
        """Slot exécuté dans le thread GUI (méthode liée → connexion en file)."""
        import base64
        thread = self.sender()
        label = getattr(thread, "target_label", None)
        item = getattr(thread, "target_item", None)
        if label is None:
            return
        if data:
            self._set_thumb(label, data)
            if item is not None:
                try:
                    item["cover_b64"] = base64.b64encode(data).decode("ascii")
                except Exception:
                    pass
        else:
            label.setText("♪")
            label.setStyleSheet("background-color: #1c1e26; border-radius: 4px; color: #849396; font-size: 16px;")

    def _read_embedded_cover(self, path):
        """Extrait la pochette intégrée d'un fichier audio local (mutagen)."""
        if not config.MUTAGEN_AVAILABLE or not path or not os.path.exists(path):
            return None
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext == ".mp3":
                tags = config.ID3(path)
                for key in tags.keys():
                    if key.startswith("APIC"):
                        return tags[key].data
            elif ext == ".flac":
                flac = config.FLAC(path)
                if flac.pictures:
                    return flac.pictures[0].data
        except Exception:
            return None
        return None

    def _stop_history_cover_threads(self):
        for t in getattr(self, "_hist_cover_threads", []):
            if t.isRunning():
                t.wait(2000)

    def _set_popup_cover(self, label, data):
        pix = QPixmap()
        pix.loadFromData(data)
        label.setPixmap(pix.scaled(
            320, 320,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        label.setText("")
        label.setStyleSheet("border-radius: 8px;")

    def _on_popup_cover_ready(self, data):
        thread = self.sender()
        label = getattr(thread, "target_label", None)
        if label is None:
            return
        if data:
            self._set_popup_cover(label, data)
        else:
            label.setText("Aucune jaquette disponible")

    def show_cover_popup(self, item):
        """Affiche la jaquette d'un fichier de l'historique dans une fenêtre."""
        import base64
        cover_b64 = item.get("cover_b64")
        title = item.get("title", "")
        artist = item.get("artist", "")

        dialog = QDialog(self)
        dialog.setWindowTitle(f"{title} — {artist}")
        dialog.setStyleSheet(STYLE_SHEET)
        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(14)

        img_label = QLabel()
        img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_label.setFixedSize(320, 320)

        # Résolution : base64 stocké → pochette intégrée au fichier → Deezer.
        data = None
        if cover_b64:
            try:
                data = base64.b64decode(cover_b64)
            except Exception:
                data = None
        if not data:
            data = self._read_embedded_cover(item.get("path", ""))

        if data:
            self._set_popup_cover(img_label, data)
        else:
            img_label.setText("Chargement de la jaquette…")
            img_label.setStyleSheet(
                "background-color: #1c1e26; border-radius: 8px; color: #849396; font-size: 14px;"
            )
            self._cover_lookup = CoverLookupThread(artist, title, item.get("cover_url"))
            self._cover_lookup.target_label = img_label
            self._cover_lookup.result_signal.connect(self._on_popup_cover_ready)
            self._cover_lookup.start()

        caption = QLabel(f"<b>{title}</b><br/>{artist}")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setStyleSheet("color: #e5e2e1; font-size: 14px;")

        btn_open = QPushButton("Ouvrir le dossier")
        btn_open.setProperty("class", "control-btn")
        btn_open.clicked.connect(lambda: self.open_local_folder(item.get("path", "")))

        lay.addWidget(img_label, alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(caption)
        lay.addWidget(btn_open)
        dialog.exec()

    def open_local_folder(self, filepath):
        """Ouvre l'explorateur sur le fichier, sélectionné dans son dossier."""
        try:
            reveal_in_explorer(filepath)
        except Exception as e:
            QMessageBox.critical(self, "Erreur", f"Impossible d'ouvrir le dossier : {str(e)}")

    def purge_missing_history_files(self):
        """Purge de l'historique : retire les entrées dont le fichier local
        n'existe plus (supprimé ou déplacé depuis le téléchargement)."""
        before = len(self.downloaded_history)
        self.downloaded_history = [
            it for it in self.downloaded_history
            if it.get("path") and os.path.exists(it["path"])
        ]
        purged = before - len(self.downloaded_history)

        if purged:
            self.refresh_history_table()
            self.save_config()
            self.update_stats_label()
            QMessageBox.information(
                self, "Purge",
                f"{purged} entrée(s) purgée(s) : fichier(s) introuvable(s) sur le disque."
            )
        else:
            QMessageBox.information(
                self, "Purge",
                "Rien à purger : tous les fichiers de l'historique existent sur le disque."
            )

    def clear_visual_history(self):
        self.downloaded_history.clear()
        self.refresh_history_table()
        self.save_config()
        self.update_stats_label()

    def add_table_row(self, track_id, title, artist, album, duration, cover_bytes=None,
                      cover_url=None, local_path="", preview_url=""):
        row_pos = self.table_widget.rowCount()
        self.table_widget.insertRow(row_pos)

        # Case à cocher robuste via un Widget centré, précédée de la pastille
        # « déjà sur le disque ? » (voir ``_set_library_badge``).
        container = QWidget()
        lay = QHBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(library_index.make_badge())
        cb = QCheckBox()
        lay.addWidget(cb)
        container.setLayout(lay)
        self.table_widget.setCellWidget(row_pos, 0, container)

        # Jaquette
        cover_label = QLabel()
        cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover_label.setFixedSize(45, 45)
        cover_label.setStyleSheet("border-radius: 4px; background-color: #1c1e26;")
        cover_label.setProperty("cover_data", cover_bytes)
        cover_label.setProperty("cover_url", cover_url)

        if cover_bytes:
            pix = QPixmap()
            pix.loadFromData(cover_bytes)
            cover_label.setPixmap(pix.scaled(45, 45, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            cover_label.setText("🎵")
            cover_label.setStyleSheet("color: #849396; font-size: 16px; border-radius: 4px; background-color: #1c1e26;")

        self.table_widget.setCellWidget(row_pos, 1, cover_label)

        # Métadonnées
        self.table_widget.setItem(row_pos, 2, QTableWidgetItem(track_id))
        self.table_widget.setItem(row_pos, 3, QTableWidgetItem(title))
        self.table_widget.setItem(row_pos, 4, QTableWidgetItem(artist))
        self.table_widget.setItem(row_pos, 5, QTableWidgetItem(album))

        # Progression spécifique et bouton Annuler local
        prog_container = QWidget()
        prog_lay = QHBoxLayout(prog_container)
        prog_lay.setContentsMargins(0, 0, 0, 0)
        prog_lay.setSpacing(5)

        prog_bar = QProgressBar()
        prog_bar.setFormat(f"{duration} - %p%")
        prog_bar.setProperty("duration", duration)
        prog_bar.setValue(0)
        prog_lay.addWidget(prog_bar, 1)

        btn_cancel = QPushButton("X")
        btn_cancel.setFixedSize(20, 20)
        btn_cancel.setStyleSheet("""
            background-color: #93000a;
            color: #ffdad6;
            border: none;
            border-radius: 10px;
            font-weight: bold;
            font-size: 9px;
        """)
        btn_cancel.setToolTip("Annuler ce téléchargement")
        btn_cancel.setVisible(False)
        btn_cancel.clicked.connect(lambda checked, tid=track_id: self.cancel_download_by_id(tid))

        prog_lay.addWidget(btn_cancel)
        prog_container.setLayout(prog_lay)

        self.table_widget.setCellWidget(row_pos, 6, prog_container)

        # Statut
        status_item = QTableWidgetItem("En attente")
        status_item.setForeground(QColor("#bac9cc"))
        self.table_widget.setItem(row_pos, 7, status_item)

        # Écoute : l'extrait Deezer en attendant le fichier, le fichier dès
        # qu'il existe (voir ``_set_main_play_cell``). ``local_path`` n'est
        # renseigné que lors d'une reconstruction (tri), pour ne pas perdre ce
        # qui est déjà sur le disque.
        self._set_main_play_cell(row_pos, local_path, preview_url)

        # La pastille se lit dans les cellules qu'on vient de remplir.
        self._set_library_badge(row_pos)

    # ------------------------------------------------------------------
    # Lecture (module player)
    # ------------------------------------------------------------------
    def _set_main_play_cell(self, row, path, preview=None):
        """Pose le bouton d'écoute d'une ligne du tableau principal.

        Deux sources, dans cet ordre : le fichier extrait s'il existe — le
        lecteur complet s'ouvre alors, l'historique en file d'attente —, sinon
        l'extrait Deezer de 30 s, qui permet d'entendre le titre avant de
        l'extraire (c'est aussi ce que propose la fenêtre de recherche). Le
        bouton ne reste éteint que si Deezer ne donne aucun extrait.

        Les deux sources sont mémorisées dans la cellule : le tableau
        principal ne garde pas d'objet par ligne, et le tri le reconstruit de
        zéro. ``preview`` à ``None`` conserve l'extrait déjà mémorisé — c'est
        le cas d'une extraction qui aboutit, qui n'apporte que le chemin.
        """
        old = self.table_widget.item(row, MAIN_PLAY_COLUMN)
        if preview is None:
            preview = (old.data(PLAY_PREVIEW_ROLE) if old else "") or ""
        # La cellule remplacée est peut-être celle dont l'extrait joue : le
        # glyphe de la nouvelle est au repos, la lecture doit l'être aussi.
        if old is not None and old is self._preview_item:
            player.stop_preview()

        if path:
            tooltip = "Écouter le fichier extrait"
        elif preview:
            tooltip = ("Écouter l'extrait de 30 secondes "
                       "(le titre n'est pas encore extrait)")
        else:
            tooltip = ("Titre pas encore extrait, et Deezer n'en donne "
                       "aucun extrait")
        item = player.play_item(tooltip, enabled=bool(path or preview))
        item.setData(PLAY_PATH_ROLE, path or "")
        item.setData(PLAY_PREVIEW_ROLE, preview)
        self.table_widget.setItem(row, MAIN_PLAY_COLUMN, item)

    def _on_main_cell_clicked(self, row, column):
        """Clic dans la colonne « ÉCOUTE » du tableau principal."""
        if column != MAIN_PLAY_COLUMN:
            return
        item = self.table_widget.item(row, MAIN_PLAY_COLUMN)
        if item is None:
            return
        path = item.data(PLAY_PATH_ROLE) or ""
        if path:
            self._play_local(path)
            return
        preview = item.data(PLAY_PREVIEW_ROLE) or ""
        if preview:
            self._toggle_main_preview(item, preview)

    def _toggle_main_preview(self, item, url):
        """Lance l'extrait de 30 s d'une ligne, ou l'arrête s'il joue déjà."""
        if item is self._preview_item:
            player.stop_preview()
            return
        if not player.available():
            QMessageBox.warning(self, "Lecteur", player.unavailable_reason())
            return

        # L'extrait précédent est arrêté d'abord : son rappel de fin remet le
        # glyphe de sa ligne, sans toucher à celui qui démarre.
        player.stop_preview()
        self._preview_item = item
        player.set_item_playing(item, True)
        if not player.play_preview(url, self._on_main_preview_stopped):
            self._on_main_preview_stopped()

    def _on_main_preview_stopped(self):
        """Extrait terminé (fin, erreur, arrêt) : glyphe de la ligne au repos."""
        item, self._preview_item = self._preview_item, None
        if item is not None:
            player.set_item_playing(item, False)

    def _on_history_cell_clicked(self, row, column):
        """Clic dans la colonne « ÉCOUTE » de l'historique des extractions."""
        if column != HIST_PLAY_COLUMN:
            return
        if 0 <= row < len(self.downloaded_history):
            self._play_history(self.downloaded_history[row])

    def _on_reencode_cell_clicked(self, row, column):
        """Clic dans la colonne « ÉCOUTE » de l'écran « Réencoder »."""
        if column != RC_PLAY:
            return
        if 0 <= row < len(self.reencode_files):
            self._play_reencode(self.reencode_files[row]["_uid"])

    # ------------------------------------------------------------------
    # Ouverture du lecteur complet : double-clic et menu contextuel
    # ------------------------------------------------------------------
    # Le clic simple de la colonne « ÉCOUTE » reste réservé à l'écoute
    # immédiate — l'extrait de 30 s de la page Musique ne doit rien ouvrir.
    # Le lecteur complet, lui, s'ouvre par un geste explicite, le même dans
    # tous les écrans : double-clic sur la ligne, ou menu contextuel (c'est
    # déjà le cas des pages « Qualité » et « Organiser »).
    def _on_main_cell_double_clicked(self, row, column):
        # La colonne « ÉCOUTE » garde sa règle : un clic lance l'extrait, un
        # second l'arrête. Y ouvrir la fenêtre au double-clic reviendrait à
        # lancer l'extrait pour l'interrompre aussitôt.
        if column == MAIN_PLAY_COLUMN:
            return
        self._play_main_row(row)

    def _on_history_cell_double_clicked(self, row, _column):
        if 0 <= row < len(self.downloaded_history):
            self._play_history(self.downloaded_history[row])

    def _on_reencode_cell_double_clicked(self, row, _column):
        if 0 <= row < len(self.reencode_files):
            self._play_reencode(self.reencode_files[row]["_uid"])

    def _play_main_row(self, row):
        """Ouvre le lecteur sur une ligne de la page Musique.

        Le fichier extrait s'il existe — l'historique en file d'attente —,
        sinon l'extrait de 30 s : la fenêtre lui donne la position, le volume
        et la jaquette, là où le clic simple se contente du son.
        """
        item = self.table_widget.item(row, MAIN_PLAY_COLUMN)
        if item is None:
            return
        path = item.data(PLAY_PATH_ROLE) or ""
        if path:
            self._play_local(path)
            return
        preview = item.data(PLAY_PREVIEW_ROLE) or ""
        if not preview:
            return
        cover = self.table_widget.cellWidget(row, 1)
        player.play_track(self, player.make_track(
            url=preview,
            title=self._cell_text(row, 3),
            artist=self._cell_text(row, 4),
            album=self._cell_text(row, 5),
            cover=cover.property("cover_data") if cover else None,
            subtitle="Extrait de 30 secondes — le titre n'est pas encore extrait",
        ))

    def _cell_text(self, row, column):
        item = self.table_widget.item(row, column)
        return item.text() if item is not None else ""

    def _show_main_menu(self, point):
        """Menu contextuel d'une ligne de la page Musique."""
        row = self.table_widget.rowAt(point.y())
        if row < 0:
            return
        item = self.table_widget.item(row, MAIN_PLAY_COLUMN)
        path = (item.data(PLAY_PATH_ROLE) if item else "") or ""
        preview = (item.data(PLAY_PREVIEW_ROLE) if item else "") or ""

        menu = QMenu(self)
        listen = menu.addAction("Écouter dans le lecteur",
                                lambda: self._play_main_row(row))
        listen.setEnabled(bool(path or preview))
        if item is not None and item is self._preview_item:
            menu.addAction("Arrêter l'extrait", player.stop_preview)
        else:
            extract = menu.addAction(
                "Écouter l'extrait de 30 s (sans fenêtre)",
                lambda: self._toggle_main_preview(item, preview))
            # L'extrait ne sert qu'à ce qui n'est pas encore extrait : dès
            # que le fichier existe, c'est lui qu'on écoute.
            extract.setEnabled(bool(preview) and not path)
        folder = menu.addAction("Ouvrir le dossier",
                                lambda: self.open_local_folder(path))
        folder.setEnabled(bool(path))
        menu.exec(self.table_widget.viewport().mapToGlobal(point))

    def _show_history_menu(self, point):
        """Menu contextuel de l'onglet « Téléchargements réussis »."""
        row = self.table_history.rowAt(point.y())
        if not 0 <= row < len(self.downloaded_history):
            return
        entry = self.downloaded_history[row]
        path = entry.get("path") or ""

        menu = QMenu(self)
        listen = menu.addAction("Écouter dans le lecteur",
                               lambda: self._play_history(entry))
        listen.setEnabled(bool(path))
        folder = menu.addAction("Ouvrir le dossier",
                                lambda: self.open_local_folder(path))
        folder.setEnabled(bool(path))
        menu.exec(self.table_history.viewport().mapToGlobal(point))

    def _show_reencode_menu(self, point):
        """Menu contextuel d'une ligne de l'écran « Réencoder »."""
        row = self.table_reencode.rowAt(point.y())
        if not 0 <= row < len(self.reencode_files):
            return
        entry = self.reencode_files[row]
        path = entry.get("path") or ""

        menu = QMenu(self)
        listen = menu.addAction("Écouter dans le lecteur",
                                lambda: self._play_reencode(entry["_uid"]))
        listen.setEnabled(bool(path))
        folder = menu.addAction("Ouvrir le dossier",
                                lambda: self.open_local_folder(path))
        folder.setEnabled(bool(path))
        menu.exec(self.table_reencode.viewport().mapToGlobal(point))

    def _play_local(self, path):
        """Écoute un fichier extrait, l'historique en file d'attente."""
        for entry in self.downloaded_history:
            if entry.get("path") == path:
                self._play_history(entry)
                return
        # Ligne extraite puis purgée de l'historique : le fichier reste
        # jouable, seul.
        player.play_track(self, player.make_track(
            path=path, title=os.path.basename(path)))

    def _play_history(self, item):
        """Ouvre le lecteur sur un titre extrait, l'historique en file d'attente."""
        tracks, index = [], 0
        for entry in self.downloaded_history:
            path = entry.get("path") or ""
            if not path:
                continue
            if entry is item:
                index = len(tracks)
            tracks.append(player.make_track(
                path=path,
                title=entry.get("title", ""),
                artist=entry.get("artist", ""),
                album=entry.get("album", ""),
                cover=self._history_cover_bytes(entry),
            ))
        player.play_tracks(self, tracks, index)

    @staticmethod
    def _history_cover_bytes(entry):
        """Jaquette déjà stockée dans l'historique (base64), ou None.

        Rien n'est cherché en ligne ici : à défaut, le lecteur lit la
        pochette intégrée au fichier.
        """
        import base64

        data = entry.get("cover_b64")
        if not data:
            return None
        try:
            return base64.b64decode(data)
        except Exception:
            return None

    def _play_reencode(self, uid):
        """Ouvre le lecteur sur un fichier de l'écran « Réencoder ».

        La file reprend les lignes visibles : un fichier masqué par le filtre
        « déjà au format voulu » n'a pas à s'enchaîner sans avoir été demandé.
        """
        target = self._reencode_uid_map.get(uid)
        tracks, index = [], 0
        for entry in self.reencode_files:
            path = entry.get("path") or ""
            if not path:
                continue
            row = self._row_of_item(entry)
            if row >= 0 and self.table_reencode.isRowHidden(row):
                continue
            if entry is target:
                index = len(tracks)
            tracks.append(player.make_track(
                path=path,
                title=entry.get("title", "") or os.path.basename(path),
                artist=entry.get("artist", ""),
                album=entry.get("album", ""),
                cover=entry.get("cover_bytes"),
                subtitle="%s  ·  %s" % (self._format_label(entry), path),
            ))
        player.play_tracks(self, tracks, index)

    def _fetch_missing_covers(self):
        """Rattrapage des jaquettes absentes du tableau principal.

        Les pistes validées avant la fin du téléchargement des vignettes du
        dialogue de recherche arrivent sans image (placeholder 🎵) : on la
        récupère ici en tâche de fond à partir de leur ``cover_url``.
        """
        pending = []
        labels = []
        for row in range(self.table_widget.rowCount()):
            label = self.table_widget.cellWidget(row, 1)
            if (isinstance(label, QLabel)
                    and not label.property("cover_data")
                    and label.property("cover_url")):
                pending.append((len(labels), label.property("cover_url")))
                labels.append(label)
        if not pending:
            return
        thread = CoverDownloadThread(pending, size="small")
        thread.cover_ready.connect(
            lambda idx, data, labels=labels: self._on_main_cover_ready(labels, idx, data)
        )
        self._main_cover_threads.append(thread)
        thread.start()

    def _on_main_cover_ready(self, labels, idx, data):
        if not (0 <= idx < len(labels)):
            return
        label = labels[idx]
        try:
            pix = QPixmap()
            pix.loadFromData(data)
            label.setPixmap(pix.scaled(
                45, 45, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            ))
            label.setText("")
            label.setStyleSheet("border-radius: 4px; background-color: #1c1e26;")
            # La jaquette sera aussi intégrée au fichier lors du téléchargement.
            label.setProperty("cover_data", data)
        except RuntimeError:
            pass  # la ligne a été supprimée entre-temps

    # ==================================================================
    # Tri des tableaux (clic sur les en-têtes de colonnes)
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

    def _toggle_sort_order(self, state_attr, col):
        """Bascule asc/desc si on reclique la même colonne, sinon asc."""
        prev_col, prev_order = getattr(self, state_attr)
        order = (
            Qt.SortOrder.DescendingOrder
            if col == prev_col and prev_order == Qt.SortOrder.AscendingOrder
            else Qt.SortOrder.AscendingOrder
        )
        setattr(self, state_attr, (col, order))
        return order

    def _on_main_header_clicked(self, col):
        """Tri du tableau principal.

        Les lignes mêlent items et widgets (case, jaquette, progression) :
        on capture leur état, on trie, puis on reconstruit le tableau.
        Désactivé pendant les téléchargements (les workers repèrent leur
        ligne par son index, qui changerait avec le tri).
        """
        if col < 2 or col == MAIN_PLAY_COLUMN:
            # Case à cocher, jaquette, écoute : rien à trier.
            return
        if self.active_downloads:
            QMessageBox.information(
                self, "Tri indisponible",
                "Le tri est désactivé pendant des téléchargements en cours."
            )
            return

        # Le tableau est reconstruit de zéro : la cellule qui porte l'extrait
        # en cours n'existera plus pour rendre son glyphe.
        player.stop_preview()

        order = self._toggle_sort_order("_main_sort", col)

        rows = []
        for row in range(self.table_widget.rowCount()):
            cb_container = self.table_widget.cellWidget(row, 0)
            cb = cb_container.findChild(QCheckBox) if cb_container else None
            cover = self.table_widget.cellWidget(row, 1)
            prog_container = self.table_widget.cellWidget(row, 6)
            prog_bar = prog_container.findChild(QProgressBar) if prog_container else None
            status_item = self.table_widget.item(row, 7)
            play_item = self.table_widget.item(row, MAIN_PLAY_COLUMN)
            rows.append({
                "checked": bool(cb and cb.isChecked()),
                "play_path": (play_item.data(PLAY_PATH_ROLE)
                              if play_item else "") or "",
                "play_preview": (play_item.data(PLAY_PREVIEW_ROLE)
                                 if play_item else "") or "",
                "cover_data": cover.property("cover_data") if cover else None,
                "cover_url": cover.property("cover_url") if cover else None,
                "id": self.table_widget.item(row, 2).text(),
                "title": self.table_widget.item(row, 3).text(),
                "artist": self.table_widget.item(row, 4).text(),
                "album": self.table_widget.item(row, 5).text(),
                "duration": (prog_bar.property("duration") if prog_bar else "") or "",
                "progress": prog_bar.value() if prog_bar else 0,
                "status": status_item.text() if status_item else "",
                "status_color": (status_item.foreground().color()
                                 if status_item else QColor("#bac9cc")),
            })

        def sort_key(r):
            if col == 2:
                try:
                    return int(r["id"])
                except ValueError:
                    return 0
            if col == 6:
                return self._duration_seconds(r["duration"])
            field = {3: "title", 4: "artist", 5: "album", 7: "status"}[col]
            return r[field].lower()

        rows.sort(key=sort_key, reverse=(order == Qt.SortOrder.DescendingOrder))
        self.table_widget.horizontalHeader().setSortIndicator(col, order)

        self.table_widget.setRowCount(0)
        for r in rows:
            self.add_table_row(r["id"], r["title"], r["artist"], r["album"],
                               r["duration"], r["cover_data"], r["cover_url"],
                               r["play_path"], r["play_preview"])
            row_pos = self.table_widget.rowCount() - 1
            if r["checked"]:
                container = self.table_widget.cellWidget(row_pos, 0)
                cb = container.findChild(QCheckBox) if container else None
                if cb:
                    cb.setChecked(True)
            prog_container = self.table_widget.cellWidget(row_pos, 6)
            prog_bar = prog_container.findChild(QProgressBar) if prog_container else None
            if prog_bar:
                prog_bar.setValue(r["progress"])
            status_item = self.table_widget.item(row_pos, 7)
            status_item.setText(r["status"])
            status_item.setForeground(r["status_color"])

    def _on_history_header_clicked(self, col):
        """Tri de l'onglet « Fichiers extraits » (la jaquette est exclue)."""
        keys = {0: "title", 1: "artist", 2: "album", 3: "path"}
        if col not in keys:
            return
        order = self._toggle_sort_order("_hist_sort", col)
        self.downloaded_history.sort(
            key=lambda it: (it.get(keys[col]) or "").lower(),
            reverse=(order == Qt.SortOrder.DescendingOrder),
        )
        self.table_history.horizontalHeader().setSortIndicator(col, order)
        self.refresh_history_table()

    def _on_searches_header_clicked(self, col):
        """Tri de l'onglet « Recherches effectuées » (colonne RECHERCHE)."""
        if col != 0:
            return
        self._searches_sort_desc = not self._searches_sort_desc
        self.search_history.sort(key=str.lower, reverse=self._searches_sort_desc)
        self.table_searches.horizontalHeader().setSortIndicator(
            0,
            Qt.SortOrder.DescendingOrder if self._searches_sort_desc
            else Qt.SortOrder.AscendingOrder,
        )
        self.refresh_searches_table()

    def cancel_download_by_id(self, track_id):
        for row in range(self.table_widget.rowCount()):
            id_item = self.table_widget.item(row, 2)
            if id_item and id_item.text() == track_id:
                # Ligne encore en file d'attente : la retirer sans la lancer.
                if row in self._download_queue:
                    self._download_queue.remove(row)
                    status_item = self.table_widget.item(row, 7)
                    if status_item:
                        status_item.setText("Annulé")
                        status_item.setForeground(QColor("#ffb4ab"))
                if row in self.active_downloads:
                    worker = self.active_downloads[row]
                    worker.terminate()
                    worker.wait()
                    del self.active_downloads[row]

                    status_item = self.table_widget.item(row, 7)
                    if status_item:
                        status_item.setText("Annulé")
                        status_item.setForeground(QColor("#ffb4ab"))

                    # Cacher le bouton d'annulation
                    container = self.table_widget.cellWidget(row, 6)
                    if container:
                        btn = container.findChild(QPushButton)
                        if btn:
                            btn.setVisible(False)

                    self.update_global_progress()
                    break

    def open_add_track_dialog(self):
        # Le dossier a pu bouger depuis le dernier parcours (rangement,
        # retéléchargement, ajout hors de DeeKeep) : c'est ici que la réponse
        # compte, donc ici qu'on la rafraîchit. La fenêtre s'ouvre sans
        # attendre, avec l'index précédent, et se met à jour dès le nouveau.
        self.refresh_library_index()
        dialog = AddTrackDialog(self, self.search_history, self.library_index)
        result = dialog.exec()

        # L'historique de recherche est conservé quel que soit le résultat
        # (le dialogue le modifie sur place, on le récupère explicitement).
        self.search_history = dialog.search_history

        if result == QDialog.DialogCode.Accepted:
            selected_tracks = dialog.get_selected_tracks()
            for track in selected_tracks:
                if track and track["id"]:
                    self.add_table_row(
                        track["id"],
                        track["title"],
                        track["artist"],
                        track["album"],
                        track["duration"],
                        track.get("cover_bytes"),
                        track.get("cover_url"),
                        preview_url=track.get("preview") or ""
                    )
            self._fetch_missing_covers()

        self.refresh_searches_table()
        self.update_stats_label()
        self.save_config()

    def purge_successful_downloads(self):
        """Retire du tableau toutes les lignes téléchargées avec succès.

        Bloqué pendant des téléchargements actifs : les workers repèrent
        leur ligne par son index, qui serait décalé par la suppression.
        """
        if self.active_downloads:
            QMessageBox.information(
                self, "Purge indisponible",
                "Attendez la fin des téléchargements en cours avant de purger."
            )
            return

        # Une ligne purgée peut être celle dont l'extrait joue.
        player.stop_preview()

        purged = 0
        for row in reversed(range(self.table_widget.rowCount())):
            status_item = self.table_widget.item(row, 7)
            if status_item and status_item.text().startswith("Téléchargé"):
                self.table_widget.removeRow(row)
                purged += 1

        if purged:
            self.update_global_progress()
            QMessageBox.information(
                self, "Purge",
                f"{purged} téléchargement(s) réussi(s) retiré(s) de la liste."
            )
        else:
            QMessageBox.information(self, "Purge", "Aucun téléchargement réussi à purger.")

    def delete_selected_tracks(self):
        # Une ligne supprimée peut être celle dont l'extrait joue.
        player.stop_preview()

        deleted_count = 0
        for row in reversed(range(self.table_widget.rowCount())):
            widget = self.table_widget.cellWidget(row, 0)
            if widget:
                cb = widget.findChild(QCheckBox)
                if cb and cb.isChecked():
                    self.table_widget.removeRow(row)
                    deleted_count += 1

        if deleted_count > 0:
            self.success_count = 0
            self.failed_count = 0
            self.update_stats_label()
            self.update_global_progress()
            QMessageBox.information(self, "Retrait", f"{deleted_count} élément(s) retiré(s) de votre liste.")

    def set_all_checked_main(self, checked):
        check_state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.table_widget.rowCount()):
            widget = self.table_widget.cellWidget(row, 0)
            if widget:
                cb = widget.findChild(QCheckBox)
                if cb:
                    cb.setCheckState(check_state)

    def _refresh_cookies_label(self):
        if self.cookies_file:
            self.lbl_cookies_file.setText(f"Cookies YouTube : {self.cookies_file}")
        else:
            self.lbl_cookies_file.setText("Cookies YouTube : aucun fichier")

    def select_cookies_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Fichier cookies.txt (format Netscape)",
            os.path.join(os.path.expanduser("~"), "Downloads"),
            "Cookies (*.txt);;Tous les fichiers (*)",
        )
        if path:
            self.cookies_file = path
            self._refresh_cookies_label()
            self.save_config()

    def clear_cookies_file(self):
        self.cookies_file = ""
        self._refresh_cookies_label()
        self.save_config()

    def select_download_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Dossier d'exportation", self.download_dir)
        if folder:
            self.download_dir = folder
            self.lbl_current_dir.setText(f"Dossier de sortie actuel : {self.download_dir}")
            # Les pages « Organiser » et « Qualité » ciblent par défaut le
            # dossier d'export.
            if hasattr(self, "organize_page"):
                self.organize_page.set_root(folder)
            if hasattr(self, "quality_page"):
                self.quality_page.set_root(folder)
            # L'index de l'ancien dossier ne dit plus rien du nouveau.
            self.library_index = None
            self._refresh_library_badges()
            self.refresh_library_index()
            self.save_config()
            QMessageBox.information(self, "Dossier configuré", f"Musique enregistrée dans :\n{folder}")

    def update_stats_label(self):
        self.lbl_stats.setText(
            f"<span style='color:#e5e2e1;'>{self.success_count}</span> "
            f"<span style='color:#bac9cc; font-size:15px; font-weight:normal;'>fichiers téléchargés</span> "
            f"<span style='color:#3b494c;'>|</span> "
            f"<span style='color:#e5e2e1;'>{self.failed_count}</span> "
            f"<span style='color:#bac9cc; font-size:15px; font-weight:normal;'>fichiers abandonnés</span>"
        )
        # Le badge reflète l'activité totale de l'historique (recherches + fichiers).
        total_historique = len(self.search_history) + len(self.downloaded_history)
        self.badge_historique.setText(str(total_historique))

    def update_global_progress(self):
        total_rows = self.table_widget.rowCount()
        if total_rows == 0:
            self.global_progress_bar.setValue(0)
            return

        sum_progress = 0
        for row in range(total_rows):
            container = self.table_widget.cellWidget(row, 6)
            if container:
                prog_bar = container.findChild(QProgressBar)
                if prog_bar:
                    sum_progress += prog_bar.value()

        global_avg = int(sum_progress / total_rows)
        self.global_progress_bar.setValue(global_avg)

    def start_all_downloads(self):
        if not config.MUTAGEN_AVAILABLE:
            QMessageBox.warning(
                self, "Diagnostic Tags",
                "La bibliothèque 'mutagen' n'est pas installée. Les tags et pochettes ne pourront pas être intégrés.\n"
                "Installez-la via : pip install mutagen"
            )

        for row in range(self.table_widget.rowCount()):
            status_text = self.table_widget.item(row, 7).text()
            if status_text == "En attente" or any(
                k in status_text for k in ("Abandonné", "Erreur", "Annulé")
            ):
                self._enqueue_download(row)
        self._pump_downloads()

    def retry_failed_downloads(self):
        for row in range(self.table_widget.rowCount()):
            status_text = self.table_widget.item(row, 7).text()
            if "Erreur" in status_text or "Abandonné" in status_text or "Annulé" in status_text:
                self._enqueue_download(row)
        self._pump_downloads()

    def _batch_tally(self):
        """Décompte par artiste du lot d'extraction en cours.

        Un lot neuf — plus rien en file ni en cours — repart d'une lecture
        fraîche du disque : les titres du lot précédent y sont écrits, et
        comptent désormais comme les autres.
        """
        if (self._download_tally is None
                or self._download_tally.root != self.download_dir
                or not (self.active_downloads or self._download_queue)):
            self._download_tally = library_rules.Tally(self.download_dir)
        return self._download_tally

    def _enqueue_download(self, row_index):
        """Ajoute une ligne à la file d'attente (si pas déjà active/en file)."""
        if row_index in self.active_downloads or row_index in self._download_queue:
            return
        # L'artiste est réservé dès la mise en file, avant que le moindre fil
        # ne démarre : les quarante titres d'un même artiste se comptent
        # ensemble, là où dix extractions parallèles se voyaient chacune
        # seules et partaient toutes dans « Divers ».
        artist_item = self.table_widget.item(row_index, 4)
        self._batch_tally().reserve(artist_item.text() if artist_item else "")
        self._download_queue.append(row_index)
        status_item = self.table_widget.item(row_index, 7)
        if status_item:
            status_item.setText("En file d'attente")
            status_item.setForeground(QColor("#bac9cc"))

    def _pump_downloads(self):
        """Lance des téléchargements tant qu'un emplacement est libre (max 10)."""
        while (self._download_queue
               and len(self.active_downloads) < self.MAX_CONCURRENT_DOWNLOADS):
            row = self._download_queue.pop(0)
            self.launch_single_download(row)

    def launch_single_download(self, row_index):
        track_id = self.table_widget.item(row_index, 2).text()
        title = self.table_widget.item(row_index, 3).text()
        artist = self.table_widget.item(row_index, 4).text()
        album = self.table_widget.item(row_index, 5).text()

        cover_label = self.table_widget.cellWidget(row_index, 1)
        cover_bytes = cover_label.property("cover_data") if cover_label else None

        file_format = self.combo_format.currentText()
        quality = self.combo_quality.currentText()
        # "Aucun (anonyme)" → None ; sinon la clé yt-dlp est le nom en minuscules.
        browser_choice = self.combo_yt_browser.currentText()
        cookies_browser = None if browser_choice.startswith("Aucun") else browser_choice.lower()

        # Afficher le bouton d'annulation local
        container = self.table_widget.cellWidget(row_index, 6)
        if container:
            btn = container.findChild(QPushButton)
            if btn:
                btn.setVisible(True)

        worker = TrackDownloaderThread(
            row_index, track_id, title, artist, album, self.download_dir,
            file_format, quality, cover_bytes,
            # Le titre est rangé et nommé selon les règles de l'écran
            # « Organiser » : c'est là qu'elles se règlent, une seule fois.
            layout=library_rules.rules_from_settings(self.settings),
            # Le décompte du lot, déjà réservé par _enqueue_download : il est
            # relu tel quel, jamais reconstruit ici — le rebâtir perdrait les
            # réservations des lignes encore en file.
            tally=self._download_tally,
            cookies_browser=cookies_browser, cookies_file=self.cookies_file
        )
        worker.progress_signal.connect(self.on_download_progress)
        worker.finished_signal.connect(self.on_download_finished)

        self.active_downloads[row_index] = worker
        worker.start()

    def on_download_progress(self, row_index, percentage, status_text):
        status_item = self.table_widget.item(row_index, 7)
        status_item.setText(status_text)
        status_item.setForeground(QColor("#00e5ff"))

        container = self.table_widget.cellWidget(row_index, 6)
        if container:
            prog_bar = container.findChild(QProgressBar)
            if prog_bar:
                prog_bar.setValue(percentage)

        self.update_global_progress()

    def on_download_finished(self, row_index, success, result_msg, track_info):
        status_item = self.table_widget.item(row_index, 7)
        container = self.table_widget.cellWidget(row_index, 6)
        prog_bar = None

        # Masquer le bouton d'annulation
        if container:
            btn = container.findChild(QPushButton)
            if btn:
                btn.setVisible(False)
            prog_bar = container.findChild(QProgressBar)

        # Nombre de tentatives effectuées par le worker (pour l'affichage).
        worker = self.active_downloads.get(row_index)
        attempts = getattr(worker, "attempts", 1) if worker else 1

        if success:
            label = "Téléchargé ✓"
            if attempts > 1:
                label += f" ({attempts} essais)"
            status_item.setText(label)
            status_item.setForeground(QColor("#5eeb7f"))
            if container and prog_bar:
                prog_bar.setValue(100)
            self.success_count += 1
            if track_info:
                self.downloaded_history.append(track_info)
                self.save_config()
                self._set_main_play_cell(row_index, track_info.get("path", ""))
                # Le fichier existe : il entre dans l'index sans relecture du
                # dossier, et les lignes encore en file le savent aussitôt.
                if self.library_index is not None:
                    self.library_index.add(
                        track_info.get("path", ""), track_info.get("title", ""),
                        track_info.get("artist", ""), track_info.get("album", ""),
                    )
                    self._refresh_library_badges()
                    self.libraryIndexChanged.emit(self.library_index)
        else:
            # Un refus définitif — vidéo introuvable, vérification d'âge —
            # s'arrête à la première tentative : parler d'abandon après des
            # essais qui n'ont pas eu lieu induirait en erreur. La cause
            # exacte, elle, s'affiche au survol.
            status_item.setText(
                f"Erreur / Abandonné ({attempts} essais)" if attempts > 1
                else "Erreur"
            )
            status_item.setToolTip(result_msg or "")
            status_item.setForeground(QColor("#ffb4ab"))
            if container and prog_bar:
                prog_bar.setValue(0)
            self.failed_count += 1
            print(f"[Erreur Row {row_index}]: {result_msg}")

        self.update_stats_label()
        self.update_global_progress()

        if row_index in self.active_downloads:
            del self.active_downloads[row_index]

        # Libérer un emplacement : démarrer le téléchargement suivant en file.
        self._pump_downloads()