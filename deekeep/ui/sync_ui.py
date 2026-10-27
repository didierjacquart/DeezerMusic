"""
Interface de la base DeeKeep : état, synchronisation, arbitrage.

Trois pièces :

    SyncController   pilote les échanges hors du fil graphique et tient
                     l'application informée par signaux ;
    ConflictDialog   pose la question quand les deux côtés ont changé ;
    DatabasePanel    la carte « Base de données » de l'écran Paramètres.

Aucun échange réseau n'a lieu dans le fil graphique : une base en ligne
qui ne répond pas fige sinon la fenêtre le temps du délai d'attente. Le
contrôleur passe donc par un ``QThread`` par opération, et refuse d'en
lancer une seconde tant que la première n'est pas rendue.
"""

import os
import time

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QVBoxLayout, QWidget
)

from deekeep.db import cloud_db
from deekeep.db import db_init
from deekeep.db import local_db
from deekeep.db import sync_service


# Périodicité du rapprochement automatique. Cinq minutes : assez court
# pour qu'une modification faite sur une autre machine arrive vite, assez
# long pour ne peser ni sur le réseau ni sur les quotas mensuels (une
# analyse ne lit que six lignes d'empreintes).
SYNC_INTERVAL_MS = 5 * 60 * 1000

# Délai d'attente après une modification locale avant de pousser. Le temps
# de cocher trois cases d'affilée sans déclencher trois envois.
DEBOUNCE_MS = 4000

STATUS_COLORS = {
    sync_service.IN_SYNC: "#00e5ff",
    sync_service.PUSH_NEEDED: "#ffc857",
    sync_service.PULL_NEEDED: "#ffc857",
    sync_service.CONFLICT: "#ff5f6d",
    sync_service.OFFLINE: "#849396",
    sync_service.NOT_CONFIGURED: "#849396",
    sync_service.ERROR: "#ff5f6d",
}


class _Worker(QThread):
    """Exécute une opération de synchronisation hors du fil graphique."""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    note = pyqtSignal(str)

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self._job = job

    def run(self):
        try:
            result = self._job(self.note.emit)
        except Exception as exc:                      # noqa: BLE001
            # Toute exception remonte à l'interface : une panne de réseau
            # ou un schéma inattendu ne doit pas tuer le fil en silence.
            self.failed.emit(f"{type(exc).__name__} : {exc}")
        else:
            self.done.emit(result)


class SyncController(QObject):
    """
    Chef d'orchestre de la synchronisation, côté application.

    L'application appelle ``record_state`` à chaque enregistrement de sa
    configuration ; le reste — analyse, envoi, réception, alerte de
    désynchronisation — se fait tout seul.
    """

    statusChanged = pyqtSignal(str, str)   # (état, message)
    conflictFound = pyqtSignal(object)     # SyncReport
    busyChanged = pyqtSignal(bool)
    progress = pyqtSignal(str)

    def __init__(self, parent=None, auto=True):
        super().__init__(parent)
        self.db = local_db.shared()
        self.manager = sync_service.SyncManager(local=self.db)
        self.status = sync_service.NOT_CONFIGURED
        self.message = ""
        self.last_report = None
        self._worker = None
        self._conflict_pending = False

        self._timer = QTimer(self)
        self._timer.setInterval(SYNC_INTERVAL_MS)
        self._timer.timeout.connect(self.sync_now)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self.sync_now)

        self._auto = auto

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    def start(self):
        """Premier rapprochement, puis rapprochement périodique."""
        if not self.manager.is_configured():
            self._publish(sync_service.NOT_CONFIGURED,
                          "Base en ligne non configurée — "
                          "les données restent dans le cache local")
            return
        if self._auto:
            self._timer.start()
            self.sync_now()

    def stop(self):
        self._timer.stop()
        self._debounce.stop()

    def shutdown(self):
        """Attend la fin de l'opération en cours (appelé à la fermeture)."""
        self.stop()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(5000)

    @property
    def busy(self):
        return self._worker is not None and self._worker.isRunning()

    # ------------------------------------------------------------------
    # Report de l'état de l'application
    # ------------------------------------------------------------------

    def record_state(self, app_config):
        """
        Reporte les paramètres et les historiques dans le cache local.

        Rapide et sans réseau : c'est du SQLite local, et seules les lignes
        réellement modifiées sont réécrites. La synchronisation, elle, est
        différée de quelques secondes pour regrouper les modifications.
        """
        try:
            include_secrets = bool(app_config.get("cloud_sync_secrets"))
            touched = db_init.mirror_state(self.db, app_config,
                                           include_secrets=include_secrets)
        except Exception as exc:                      # noqa: BLE001
            print(f"[sync] Report dans le cache impossible : {exc}")
            return 0
        if touched and self._auto and self.manager.is_configured():
            self._debounce.start()
        return touched

    def restore_state(self, app_config):
        """
        Recharge dans la configuration de l'application ce que dit la base.

        Utilisé après une réception : les paramètres, l'historique des
        recherches et celui des téléchargements sont reconstruits depuis
        les tables. ``app_config`` est modifié sur place et le nombre de
        clés changées est renvoyé.
        """
        changed = 0

        for key, value in self.db.all_settings().items():
            if app_config.get(key) != value:
                app_config[key] = value
                changed += 1

        searches = [row["query"] for row in
                    self.db.rows("searches", order_by="position")]
        if searches and app_config.get("search_history") != searches:
            app_config["search_history"] = searches
            changed += 1

        history = self._history_from_db()
        if history and app_config.get("downloaded_history") != history:
            app_config["downloaded_history"] = history
            changed += 1

        return changed

    def _history_from_db(self):
        """Historique des téléchargements reconstitué depuis les tables."""
        import base64

        rows = self.db.query(
            "SELECT t.path, t.title, t.artist, t.album, "
            "       COALESCE(t.history_cover_sha1, t.cover_sha1) AS sha1 "
            "FROM tracks t WHERE t.in_history = 1 AND t.deleted = 0 "
            "ORDER BY t.history_pos"
        )
        history = []
        for row in rows:
            entry = {
                "title": row["title"] or "",
                "artist": row["artist"] or "",
                "album": row["album"] or "",
                "path": row["path"] or "",
            }
            if row["sha1"]:
                cover = self.db.get_row("covers", row["sha1"])
                if cover and cover.get("data"):
                    entry["cover_b64"] = base64.b64encode(
                        cover["data"]).decode("ascii")
            history.append(entry)
        return history

    # ------------------------------------------------------------------
    # Opérations
    # ------------------------------------------------------------------

    def sync_now(self):
        """Analyse puis rapproche ce qui peut l'être sans arbitrage."""
        if not self.manager.is_configured():
            self._publish(sync_service.NOT_CONFIGURED,
                          "Base en ligne non configurée")
            return False
        return self._run(lambda note: self.manager.sync(
            progress=lambda done: note(f"{done} ligne(s) transférée(s)")))

    def analyze_now(self):
        if not self.manager.is_configured():
            self._publish(sync_service.NOT_CONFIGURED,
                          "Base en ligne non configurée")
            return False
        return self._run(lambda note: self.manager.analyze())

    def resolve(self, direction):
        """Tranche une désynchronisation : « local » ou « remote »."""
        self._conflict_pending = False
        return self._run(lambda note: self.manager.resolve(
            direction,
            progress=lambda done: note(f"{done} ligne(s) transférée(s)")))

    def initialize(self, url, token, roots=None, scan=True):
        """
        Première mise en service : identifiants, schéma distant, envoi.

        Le parcours du disque est le poste le plus long — quelques minutes
        pour une bibliothèque de trois mille titres — d'où l'exécution en
        tâche de fond avec avancement.
        """
        cloud_db.save_credentials(url, token)
        self.manager.forget_client()

        def job(note):
            note("Vérification de la connexion …")
            client = self.manager.client()
            if not client.ping():
                raise cloud_db.CloudError(
                    "La base ne répond pas : vérifiez l'URL et le jeton.")
            note("Création des tables …")
            self.manager.ensure_remote_schema()
            note("Collecte des données locales …")
            db_init.build_local(roots=roots, scan=scan, verbose=False,
                                db=self.db,
                                include_secrets=bool(
                                    self.db.get_setting("cloud_sync_secrets")))
            note("Envoi vers la base en ligne …")
            return self.manager.sync(
                progress=lambda done: note(f"{done} ligne(s) envoyée(s)"))

        started = self._run(job)
        if started and self._auto:
            self._timer.start()
        return started

    def rebuild_library(self, roots=None):
        """Reparcourt le disque et met la base à jour, sans rien effacer."""
        def job(note):
            note("Parcours du disque …")
            db_init.build_local(roots=roots, scan=True, verbose=False,
                                db=self.db)
            if not self.manager.is_configured():
                return sync_service.SyncReport(
                    sync_service.NOT_CONFIGURED,
                    message="Cache local mis à jour (base en ligne non "
                            "configurée)")
            note("Envoi …")
            return self.manager.sync(
                progress=lambda done: note(f"{done} ligne(s) envoyée(s)"))
        return self._run(job)

    def test_connection(self, url, token):
        """Vérifie des identifiants sans les enregistrer."""
        def job(note):
            note("Connexion …")
            client = cloud_db.TursoClient(url, token, timeout=15, retries=1)
            client.execute("SELECT 1")
            tables = [name for name in client.table_names()
                      if not name.startswith("sqlite_")]
            client.close()
            return tables
        return self._run(job, publish=False)

    # ------------------------------------------------------------------
    # Interne
    # ------------------------------------------------------------------

    def _run(self, job, publish=True):
        if self.busy:
            return False
        worker = _Worker(job, self)
        worker.note.connect(self.progress.emit)
        if publish:
            worker.done.connect(self._on_done)
        else:
            worker.done.connect(lambda result: self.progress.emit(
                "Connexion réussie — tables : "
                + (", ".join(result) if result else "aucune (base neuve)")))
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda: self.busyChanged.emit(False))
        self._worker = worker
        self.busyChanged.emit(True)
        worker.start()
        return True

    def _on_done(self, report):
        if not isinstance(report, sync_service.SyncReport):
            return
        self.last_report = report
        self._publish(report.status, f"{report.message} — {report.summary()}")
        if report.status == sync_service.CONFLICT and not self._conflict_pending:
            self._conflict_pending = True
            self.conflictFound.emit(report)

    def _on_failed(self, message):
        self._publish(sync_service.ERROR, message)

    def _publish(self, status, message):
        self.status = status
        self.message = message
        self.statusChanged.emit(status, message)


class ConflictDialog(QDialog):
    """
    Fenêtre d'arbitrage d'une désynchronisation.

    Elle ne propose pas de fusion : les deux côtés ont changé, et fondre
    automatiquement l'un dans l'autre reviendrait à décider à la place de
    l'utilisateur quelle version de trois mille titres est la bonne. Le
    détail par table est affiché pour que le choix se fasse en
    connaissance de cause.
    """

    LOCAL = "local"
    REMOTE = "remote"

    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Désynchronisation de la base")
        self.setMinimumWidth(620)
        self.choice = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        title = QLabel("Le cache local et la base en ligne ont tous deux changé")
        title.setStyleSheet("font-size: 17px; font-weight: bold; color: #ff5f6d;")
        layout.addWidget(title)

        explain = QLabel(
            "Des modifications ont été faites des deux côtés depuis le "
            "dernier échange réussi. Aucune fusion automatique n'est "
            "tentée : choisissez la version qui fait foi. L'autre sera "
            "réalignée sur elle."
        )
        explain.setWordWrap(True)
        explain.setStyleSheet("color: #bac9cc; font-size: 13px;")
        layout.addWidget(explain)

        detail = QFrame()
        detail.setStyleSheet(
            "background-color: rgba(28, 30, 38, 0.7); border-radius: 8px;")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(14, 12, 14, 12)
        detail_layout.setSpacing(4)
        header = QLabel("Table                     local        en ligne")
        header.setStyleSheet(
            "font-family: Consolas, monospace; color: #849396; font-size: 12px;")
        detail_layout.addWidget(header)
        for name, table in report.tables.items():
            marker = "  ⚠" if table.verdict == sync_service.CONFLICT else "   "
            line = QLabel(
                f"{marker} {name:<20s} {table.local[0]:>7d} "
                f"{table.remote[0]:>13d}"
            )
            colour = ("#ff5f6d" if table.verdict == sync_service.CONFLICT
                      else "#bac9cc")
            line.setStyleSheet(
                f"font-family: Consolas, monospace; color: {colour}; "
                "font-size: 12px;")
            detail_layout.addWidget(line)
        layout.addWidget(detail)

        note = QLabel(
            "Le nombre affiché est celui des lignes de chaque table, "
            "pierres tombales comprises."
        )
        note.setStyleSheet("color: #849396; font-size: 11px;")
        layout.addWidget(note)

        buttons = QHBoxLayout()
        btn_later = QPushButton("Plus tard")
        btn_later.setProperty("class", "control-btn")
        btn_later.clicked.connect(self.reject)

        btn_local = QPushButton("Le local fait foi  →  écraser la base")
        btn_local.setProperty("class", "control-btn")
        btn_local.clicked.connect(lambda: self._choose(self.LOCAL))

        btn_remote = QPushButton("La base fait foi  →  écraser le local")
        btn_remote.setProperty("class", "control-btn")
        btn_remote.clicked.connect(lambda: self._choose(self.REMOTE))

        buttons.addWidget(btn_later)
        buttons.addStretch()
        buttons.addWidget(btn_local)
        buttons.addWidget(btn_remote)
        layout.addLayout(buttons)

    def _choose(self, direction):
        target = ("la base en ligne" if direction == self.LOCAL
                  else "le cache local")
        confirm = QMessageBox.question(
            self, "Confirmer",
            f"Les données de {target} qui diffèrent seront remplacées.\n"
            "Confirmer ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.choice = direction
            self.accept()


class DatabasePanel(QWidget):
    """
    Carte « Base de données » de l'écran Paramètres.

    Elle montre l'état de la synchronisation, permet de saisir les
    identifiants de la base en ligne, de la mettre en service, de forcer
    un échange et de reparcourir le disque.
    """

    def __init__(self, controller, parent=None, root_provider=None):
        super().__init__(parent)
        self.controller = controller
        self._root_provider = root_provider

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("Base de données")
        title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 26px; "
            "font-weight: bold; color: #00e5ff;")
        layout.addWidget(title)

        # Style limité à la carte (voir la carte d'exportation) : sans
        # sélecteur, il s'appliquerait aussi aux libellés et aux champs.
        card = QWidget()
        card.setObjectName("databaseCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setStyleSheet(
            "QWidget#databaseCard { background-color: rgba(28, 30, 38, 0.60); "
            "border: 1px solid rgba(255, 255, 255, 0.08); "
            "border-radius: 12px; }")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(14)

        intro = QLabel(
            "Paramètres, historiques, tags, jaquettes et mesures de qualité "
            "sont conservés dans une base Turso (offre gratuite, 5 Go). Un "
            "cache local en garde une copie : l'application reste utilisable "
            "hors ligne, et les écritures repartent au rétablissement."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #bac9cc; font-size: 12px;")
        card_layout.addWidget(intro)

        # --- État ---
        self.lbl_status = QLabel()
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("font-size: 14px; font-weight: bold;")
        card_layout.addWidget(self.lbl_status)

        self.lbl_detail = QLabel()
        self.lbl_detail.setWordWrap(True)
        self.lbl_detail.setStyleSheet("color: #bac9cc; font-size: 12px;")
        card_layout.addWidget(self.lbl_detail)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)          # défilement indéterminé
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        card_layout.addWidget(self.progress)

        # --- Identifiants ---
        creds = cloud_db.load_credentials()

        url_layout = QHBoxLayout()
        lbl_url = QLabel("URL de la base :")
        lbl_url.setStyleSheet("font-size: 13px;")
        self.input_url = QLineEdit(creds["url"])
        self.input_url.setPlaceholderText(
            "libsql://ma-base-mon-org.turso.io")
        url_layout.addWidget(lbl_url)
        url_layout.addWidget(self.input_url, 1)
        card_layout.addLayout(url_layout)

        token_layout = QHBoxLayout()
        lbl_token = QLabel("Jeton d'accès :")
        lbl_token.setStyleSheet("font-size: 13px;")
        self.input_token = QLineEdit(creds["token"])
        self.input_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_token.setPlaceholderText(
            "jeton créé avec « turso db tokens create »")
        token_layout.addWidget(lbl_token)
        token_layout.addWidget(self.input_token, 1)
        card_layout.addLayout(token_layout)

        help_text = QLabel(
            "Créer la base, gratuitement : compte sur turso.tech (connexion "
            "GitHub, sans carte bancaire), puis « Create Database ». L'URL et "
            "le jeton figurent sur la fiche de la base."
        )
        help_text.setWordWrap(True)
        help_text.setStyleSheet("color: #849396; font-size: 11px;")
        card_layout.addWidget(help_text)

        # --- Actions ---
        actions = QHBoxLayout()
        self.btn_test = QPushButton("Tester")
        self.btn_test.setProperty("class", "control-btn")
        self.btn_test.clicked.connect(self._on_test)

        self.btn_init = QPushButton("Mettre en service")
        self.btn_init.setProperty("class", "control-btn")
        self.btn_init.setToolTip(
            "Crée les tables sur la base en ligne, relève la bibliothèque "
            "sur le disque et envoie tout.")
        self.btn_init.clicked.connect(self._on_init)

        self.btn_sync = QPushButton("Synchroniser")
        self.btn_sync.setProperty("class", "control-btn")
        self.btn_sync.clicked.connect(self.controller.sync_now)

        self.btn_rescan = QPushButton("Reparcourir le disque")
        self.btn_rescan.setProperty("class", "control-btn")
        self.btn_rescan.setToolTip(
            "Relit les tags, jaquettes et caractéristiques des fichiers du "
            "dossier de dépôt.")
        self.btn_rescan.clicked.connect(self._on_rescan)

        for button in (self.btn_test, self.btn_init, self.btn_sync,
                       self.btn_rescan):
            actions.addWidget(button)
        actions.addStretch()
        card_layout.addLayout(actions)

        layout.addWidget(card)

        controller.statusChanged.connect(self._on_status)
        controller.busyChanged.connect(self._on_busy)
        controller.progress.connect(self._on_progress)
        self._on_status(controller.status, controller.message)
        self.refresh_detail()

    # ------------------------------------------------------------------

    def _roots(self):
        if self._root_provider is None:
            return None
        root = self._root_provider()
        return [root] if root else None

    def _on_test(self):
        url = self.input_url.text().strip()
        token = self.input_token.text().strip()
        if not url or not token:
            QMessageBox.information(self, "Base de données",
                                    "Renseignez l'URL et le jeton.")
            return
        self.controller.test_connection(url, token)

    def _on_init(self):
        url = self.input_url.text().strip()
        token = self.input_token.text().strip()
        if not url or not token:
            QMessageBox.information(self, "Base de données",
                                    "Renseignez l'URL et le jeton.")
            return
        roots = self._roots()
        target = roots[0] if roots else "le dossier de dépôt"
        confirm = QMessageBox.question(
            self, "Mettre la base en service",
            "Les tables vont être créées sur la base en ligne, puis "
            f"remplies avec :\n\n  • les paramètres et les historiques ;\n"
            f"  • les fichiers audio de {target} (tags, jaquettes, "
            "caractéristiques) ;\n  • les mesures de qualité déjà faites.\n\n"
            "Le parcours du disque peut demander quelques minutes. "
            "Continuer ?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.controller.initialize(url, token, roots=roots)

    def _on_rescan(self):
        self.controller.rebuild_library(roots=self._roots())

    def _on_status(self, status, message):
        colour = STATUS_COLORS.get(status, "#bac9cc")
        label = sync_service.STATUS_LABELS.get(status, status)
        self.lbl_status.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {colour};")
        self.lbl_status.setText(label)
        self.lbl_detail.setText(message or "")
        self.refresh_detail()

    def _on_busy(self, busy):
        self.progress.setVisible(busy)
        for button in (self.btn_test, self.btn_init, self.btn_sync,
                       self.btn_rescan):
            button.setEnabled(not busy)
        if not busy:
            self.refresh_detail()

    def _on_progress(self, message):
        self.lbl_detail.setText(message)

    def refresh_detail(self):
        """Complète le détail par le contenu du cache local."""
        db = self.controller.db
        try:
            counts = {name: db.count(name) for name in
                      ("tracks", "covers", "quality", "searches",
                       "app_settings")}
            pending = db.pending_count()
            size = db.size_bytes()
        except Exception:                             # noqa: BLE001
            return
        last = db.last_sync_at()
        when = (time.strftime("%d/%m/%Y %H:%M", time.localtime(last))
                if last else "jamais")
        text = (
            f"{counts['tracks']} titres · {counts['covers']} jaquettes · "
            f"{counts['quality']} mesures · {counts['searches']} recherches · "
            f"{counts['app_settings']} paramètres — cache "
            f"{size / 1e6:.1f} Mo, dernier échange : {when}"
        )
        if pending:
            text += f" · {pending} ligne(s) en attente d'envoi"
        self.lbl_detail.setToolTip(os.path.abspath(db.path))
        # Le détail d'avancement a la priorité tant qu'une opération tourne.
        if not self.controller.busy:
            self.lbl_detail.setText(text)
