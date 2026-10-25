"""
Miroir de la bibliothèque : une copie intacte, et le chemin du retour.

Ce module a eu une vie antérieure. Il réencodait la bibliothèque en Opus
pour la faire tenir dans les dix gigaoctets gratuits d'un hébergeur, et tout
y était arbitrage : quel débit, quelle perte, quelle part de la collection.
La mesure a fini par trancher contre lui — 14 Go de musique déjà en MP3 128,
aucun palier qui tienne à la fois dans l'offre et dans la fidélité. La copie
vit désormais dans une archive locale (voir ``sqlite_store``), où le
gigaoctet n'est pas facturé.

Il n'y a donc **plus de réglage**. Un fichier entre tel quel, octet pour
octet, et ressort identique — c'est vérifié à l'empreinte à chaque
restauration. Ni débit, ni réencodage, ni « sauf pour ceux qui ».

Deux empreintes font tout le travail :

  * l'**empreinte date+taille** (``stamp``) dit si le fichier a changé depuis
    sa dernière copie. Un titre retéléchargé par l'écran Qualité est ainsi
    repéré, et lui seul est réexpédié ;
  * l'**empreinte SHA-1** de son contenu donne la clé de l'objet. Deux copies
    du même fichier, dans deux dossiers, n'occupent qu'une place dans
    l'archive ; un objet déjà présent n'est pas réécrit, ce qui rend une
    copie interrompue reprenable à peu de frais ; et c'est elle, relue après
    écriture, qui prouve qu'une restauration a rendu l'original.

La jaquette n'est pas recopiée. La table ``covers`` la détient déjà,
dédoublonnée, et elle revient avec le fichier puisque celui-ci est intact.
"""

import hashlib
import os
import time

from PyQt6.QtCore import QThread, pyqtSignal

from deekeep.core import config
from deekeep.db import sqlite_store


# Rayon des objets dans l'archive. Tous y sont : il n'existe plus qu'une
# sorte d'objet, le fichier d'origine. Le rayon demeure parce que les clés
# déjà en base le portent, et qu'une archive constituée n'a pas à être
# réécrite pour une question de nommage.
SOURCE_SHELF = "audio/source/"

# États d'un titre vis-à-vis du miroir.
ABSENT = "absent"          # jamais copié
FRESH = "fresh"            # copié, et le fichier n'a pas bougé depuis
STALE = "stale"            # copié, mais le fichier a changé depuis


def canonical_path(path):
    """Chemin normalisé, casse conservée : la clé de la table des titres."""
    return os.path.normpath(str(path or "").replace("/", os.sep))


def stamp(path):
    """Empreinte date+taille d'un fichier, ou "" s'il est absent."""
    try:
        info = os.stat(path)
    except OSError:
        return ""
    return "%d:%d" % (int(info.st_mtime), info.st_size)


def file_sha1(path, chunk=1 << 20):
    """Empreinte SHA-1 du contenu d'un fichier, lue par tranches de 1 Mo."""
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def probe_bitrate(path):
    """Débit réel d'un fichier, en kb/s, ou 0 s'il reste indéchiffrable.

    La table des titres ne le connaît pas toujours : sur la bibliothèque de
    référence, deux cents lignes portent un débit nul — durée comprise —
    pour des titres entrés sans que leurs en-têtes aient jamais été lus.

    Le débit ne décide plus de rien : rien n'est réencodé. Il reste consigné
    dans la ligne du miroir, et sert à l'écran Bibliothèque pour afficher un
    format complet plutôt qu'un « MP3 » sans chiffre.
    """
    if config.mutagen is None:
        return 0
    try:
        info = getattr(config.mutagen.File(path), "info", None)
        if info is None:
            return 0
        bitrate = getattr(info, "bitrate", 0) or 0
        if bitrate > 0:
            return int(round(bitrate / 1000.0))
        length = getattr(info, "length", 0) or 0
        if length > 0:
            return int(round(os.path.getsize(path) * 8 / length / 1000.0))
    except Exception:
        # Fichier tronqué, en-tête introuvable : le débit reste inconnu, ce
        # qui n'empêche en rien de l'archiver tel quel.
        pass
    return 0


# Type de contenu des fichiers, par extension. L'archive n'en fait rien
# aujourd'hui ; un magasin qui servirait des requêtes HTTP en aurait besoin.
CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/wav", ".wma": "audio/x-ms-wma", ".aiff": "audio/aiff",
    ".aif": "audio/aiff", ".alac": "audio/mp4",
}


def object_key(prefix, sha1, extension=".mp3"):
    """Clé de l'objet dans l'archive, dérivée de l'empreinte du contenu."""
    extension = (extension or "").lower()
    return "%s%s%s/%s%s" % (prefix, SOURCE_SHELF, sha1[:2], sha1, extension)


def is_source_copy(key):
    """Vrai si l'objet est le fichier d'origine.

    Il l'est toujours désormais. La fonction demeure parce qu'une archive
    constituée du temps du miroir Opus pouvait contenir les deux, et qu'une
    clé ancienne doit encore pouvoir se lire pour ce qu'elle est.
    """
    return SOURCE_SHELF in (key or "")


def restore_target(path, key):
    """Où écrire l'objet restauré : (destination, est-ce l'original ?).

    L'archive ne contient que des originaux : le fichier retrouve son chemin
    exact, et la bibliothèque est reconstituée à l'identique.

    Le second terme n'est plus qu'un reliquat utile : une clé héritée du
    miroir Opus désignerait une réduction, et le dire permet à la
    restauration de ne pas la faire passer pour l'original.
    """
    if is_source_copy(key):
        return path, True
    base, extension = os.path.splitext(path)
    return (path if extension.lower() == ".mp3" else base + ".mp3"), False


def state_of(row, current_stamp):
    """État du miroir pour un titre, d'après sa ligne et son fichier."""
    if not row or not row.get("key"):
        return ABSENT
    if current_stamp and row.get("stamp") and row["stamp"] != current_stamp:
        return STALE
    return FRESH


def open_store(credentials=None):
    """Le magasin où vit l'archive.

    ``credentials`` porte son emplacement. Il est lu par l'interface et
    passé au fil de travail : celui-ci ne touche pas aux fichiers de
    configuration, et n'a donc pas à les verrouiller.
    """
    return sqlite_store.SqliteStore(credentials or sqlite_store.load_config())


def store_is_configured():
    """L'archive est-elle utilisable en l'état ?"""
    return sqlite_store.is_configured()


def store_credentials():
    """Ce qu'il faut passer à ``open_store``."""
    return sqlite_store.load_config()


def remove_from_mirror(store, row):
    """Efface de l'archive l'objet d'un titre. Rend (succès, message)."""
    key = (row or {}).get("key") or ""
    if not key:
        return True, ""
    try:
        store.delete(key)
    except sqlite_store.BlobError as exc:
        return False, str(exc)
    return True, ""


class MirrorThread(QThread):
    """Copie une liste de fichiers dans l'archive, tels quels.

    Le travail est mené fichier par fichier. L'intérêt du fil séparé n'est
    pas la vitesse — c'est que l'interface reste vivante, et que la liste
    s'écoute pendant que l'archive se constitue.
    """

    # (chemin, état, ligne ou None, message) — une émission par fichier.
    done = pyqtSignal(str, str, object, str)
    # (traités, total)
    progress = pyqtSignal(int, int)
    # (copiés, ignorés, échecs, octets écrits)
    completed = pyqtSignal(int, int, int, int)

    # Suites possibles pour un fichier, telles qu'annoncées par ``done``.
    SENT = "sent"              # copié dans l'archive
    REUSED = "reused"          # contenu déjà présent (doublon, ou reprise)
    SKIPPED = "skipped"        # déjà à jour, rien à faire
    FAILED = "failed"          # échec, message à l'appui

    def __init__(self, items, known=None, credentials=None, parent=None):
        super().__init__(parent)
        # ``items`` : des chemins, ou des couples (chemin, débit) — le débit
        # est accepté pour la compatibilité des appels, et seulement
        # consigné.
        self._items = [(entry[0], entry[1]) if isinstance(entry, (tuple, list))
                       else (entry, 0) for entry in items]
        # Lignes déjà en base, indexées par chemin : de quoi sauter ce qui
        # est à jour sans interroger l'archive.
        self._known = dict(known or {})
        self._credentials = credentials
        self._stop = False

    def stop(self):
        """Demande l'arrêt : le fichier en cours finit, les suivants non."""
        self._stop = True

    def run(self):
        total = len(self._items)
        try:
            store = open_store(self._credentials)
        except sqlite_store.BlobError as exc:
            for path, _rate in self._items:
                self.done.emit(path, self.FAILED, None, str(exc))
            self.completed.emit(0, 0, total, 0)
            return

        sent = reused = skipped = failed = 0
        written = 0

        for index, (path, source_rate) in enumerate(self._items, start=1):
            if self._stop:
                break
            state, row, message = self._mirror_one(store, path, source_rate)
            if state == self.SENT:
                sent += 1
                written += int((row or {}).get("bytes") or 0)
            elif state == self.REUSED:
                reused += 1
            elif state == self.SKIPPED:
                skipped += 1
            else:
                failed += 1
            self.done.emit(path, state, row, message)
            self.progress.emit(index, total)

        self.completed.emit(sent, reused + skipped, failed, written)

    # ------------------------------------------------------------------
    def _mirror_one(self, store, path, source_rate):
        """Traite un fichier. Rend (état, ligne ou None, message)."""
        if not os.path.exists(path):
            return self.FAILED, None, "Fichier absent du disque."

        current = stamp(path)
        known = self._known.get(canonical_path(path))
        if state_of(known, current) == FRESH:
            return self.SKIPPED, None, "Déjà dans l'archive."

        # L'empreinte est toujours relue : deux fichiers distincts peuvent
        # partager date et taille, et un cache indexé là-dessus rangerait un
        # titre sous la clé d'un autre.
        try:
            sha1 = file_sha1(path)
        except OSError as exc:
            return self.FAILED, None, f"Lecture impossible : {exc}"

        extension = os.path.splitext(path)[1].lower()
        key = object_key(store.prefix, sha1, extension)
        row = {
            "path": canonical_path(path),
            "sha1": sha1,
            "stamp": current,
            "key": key,
            "bitrate": int(source_rate or 0) or probe_bitrate(path),
            "source_bytes": os.path.getsize(path),
            "uploaded_at": time.time(),
        }

        # Objet déjà là : ni lecture ni écriture. C'est le cas d'une
        # deuxième copie du même fichier, et celui d'une copie interrompue
        # qu'on relance.
        try:
            existing = store.head(key)
        except sqlite_store.BlobError as exc:
            return self.FAILED, None, str(exc)
        if existing:
            row["bytes"] = existing["bytes"]
            return self.REUSED, row, "Déjà dans l'archive."

        if self._stop:
            return self.FAILED, None, "Copie interrompue."

        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            return self.FAILED, None, f"Lecture impossible : {exc}"
        try:
            store.put(key, data, content_type=CONTENT_TYPES.get(
                extension, "application/octet-stream"))
        except sqlite_store.BlobError as exc:
            return self.FAILED, None, str(exc)

        row["bytes"] = len(data)
        return self.SENT, row, ""


class RestoreThread(QThread):
    """Ressort de l'archive les titres que le disque n'a plus.

    Le chemin inverse du miroir, et celui qui lui donne sa raison d'être :
    une réinstallation, un disque changé, un dossier effacé par mégarde, et
    la bibliothèque se reconstitue **à l'identique**.

    Deux garanties :

      * le fichier revient **octet pour octet**, et son empreinte SHA-1 est
        revérifiée après écriture — c'est la ligne du miroir qui la détient.
        Une copie qui ne se vérifie pas est effacée plutôt que gardée :
        mieux vaut un titre manquant, qu'on sait aller rechercher, qu'un
        titre corrompu qui se donne pour restauré ;
      * **rien n'est jamais écrasé.** Un titre dont le fichier est déjà là
        est passé, sans même une requête à l'archive.
    """

    # (chemin d'origine, état, ligne de miroir à réécrire ou None, message)
    done = pyqtSignal(str, str, object, str)
    # (traités, total)
    progress = pyqtSignal(int, int)
    # (rendus, ignorés, échecs, octets lus)
    completed = pyqtSignal(int, int, int, int)

    RESTORED = "restored"      # fichier d'origine rendu, empreinte vérifiée
    SKIPPED = "skipped"        # le fichier est déjà sur le disque
    FAILED = "failed"          # échec, message à l'appui

    def __init__(self, rows, credentials=None, parent=None):
        super().__init__(parent)
        # ``rows`` : les lignes de la table du miroir des titres à rendre.
        # Tout y est — le chemin visé, la clé de l'objet et l'empreinte
        # attendue — sans avoir à relire quoi que ce soit.
        self._rows = [dict(row) for row in rows]
        self._credentials = credentials
        self._stop = False

    def stop(self):
        """Demande l'arrêt : le titre en cours finit, les suivants non."""
        self._stop = True

    def run(self):
        total = len(self._rows)
        try:
            store = open_store(self._credentials)
        except sqlite_store.BlobError as exc:
            for row in self._rows:
                self.done.emit(row.get("path") or "", self.FAILED, None,
                               str(exc))
            self.completed.emit(0, 0, total, 0)
            return

        restored = skipped = failed = 0
        received = 0

        for index, row in enumerate(self._rows, start=1):
            if self._stop:
                break
            state, updated, message, size = self._restore_one(store, row)
            if state == self.RESTORED:
                restored += 1
                received += size
            elif state == self.SKIPPED:
                skipped += 1
            else:
                failed += 1
            self.done.emit(row.get("path") or "", state, updated, message)
            self.progress.emit(index, total)

        self.completed.emit(restored, skipped, failed, received)

    # ------------------------------------------------------------------
    def _restore_one(self, store, row):
        """Rend un titre. Retourne (état, ligne à réécrire, message, octets)."""
        path = row.get("path") or ""
        key = row.get("key") or ""
        if not path or not key:
            return self.FAILED, None, "Ligne de miroir incomplète.", 0

        destination, identical = restore_target(path, key)
        if not identical:
            # Une clé héritée du miroir Opus : l'archive ne contient plus
            # rien de tel, et rendre une réduction sous le nom de l'original
            # serait un mensonge. Mieux vaut le dire.
            return (self.FAILED, None,
                    "Cette ligne désigne une ancienne copie réduite, absente "
                    "de l'archive : le titre est à recopier depuis le disque.",
                    0)

        # Rien n'est écrasé : la restauration comble des trous, elle ne
        # remplace pas ce qui est là.
        if os.path.exists(destination):
            return self.SKIPPED, None, "Déjà sur le disque.", 0

        folder = os.path.dirname(destination)
        if folder and not os.path.isdir(folder):
            # Reconstituer un disque, c'est aussi refaire l'arborescence :
            # les dossiers d'album ont disparu avec les fichiers.
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                return self.FAILED, None, f"Dossier impossible à créer : {exc}", 0

        try:
            size = store.download(key, destination)
        except sqlite_store.BlobError as exc:
            return self.FAILED, None, str(exc), 0
        except OSError as exc:
            return self.FAILED, None, f"Écriture impossible : {exc}", 0

        # La copie doit se prouver. L'empreinte du fichier d'origine est en
        # base depuis la copie ; celle du fichier rendu doit lui être
        # identique, sans quoi ce n'est pas le même fichier — et un titre
        # corrompu qui se donne pour restauré est pire qu'un titre manquant,
        # qu'on sait au moins aller rechercher.
        expected = (row.get("sha1") or "").lower()
        if expected:
            trouble = ("L'empreinte du fichier rendu ne correspond pas à "
                       "celle attendue : la copie a été écartée.")
            try:
                actual = file_sha1(destination)
            except OSError as exc:
                actual, trouble = "", f"Relecture impossible : {exc}"
            if actual != expected:
                try:
                    os.remove(destination)
                except OSError:
                    pass
                return self.FAILED, None, trouble, 0

        # Le fichier est de retour, mais avec une date de modification
        # neuve : sans cette mise à jour de l'empreinte date+taille, tout ce
        # qu'on vient de restaurer se donnerait pour « à recopier », et une
        # bibliothèque entière repartirait vers l'archive où elle se trouve
        # déjà, à l'identique.
        updated = dict(row)
        updated["stamp"] = stamp(destination)
        return self.RESTORED, updated, "", size


class CheckThread(QThread):
    """Vérifie que l'archive s'ouvre et accepte une écriture.

    Dans un fil séparé : un chemin sur un disque endormi met quelques
    secondes à répondre, et la fenêtre ne doit pas rester figée pendant ce
    temps.
    """

    checked = pyqtSignal(bool, str)

    def __init__(self, credentials=None, parent=None):
        super().__init__(parent)
        self._credentials = credentials

    def run(self):
        try:
            open_store(self._credentials).check()
        except sqlite_store.BlobError as exc:
            self.checked.emit(False, str(exc))
            return
        self.checked.emit(True, "Archive accessible.")


class UsageThread(QThread):
    """Compte ce que l'archive contient réellement."""

    measured = pyqtSignal(int, int, str)

    def __init__(self, credentials=None, parent=None):
        super().__init__(parent)
        self._credentials = credentials

    def run(self):
        try:
            count, total = open_store(self._credentials).usage()
        except sqlite_store.BlobError as exc:
            self.measured.emit(0, 0, str(exc))
            return
        self.measured.emit(count, total, "")
