"""
Archive locale sans perte : la bibliothèque entière dans une base SQLite.

Le seau distant avait imposé un compromis — dix gigaoctets gratuits contre
quatorze de musique, donc réduire, donc perdre. Ce module le supprime en
changeant de destination : une base posée sur un disque à soi ne facture pas
le gigaoctet, et les fichiers y entrent **tels quels**. Aucun réencodage,
aucun Opus, aucune génération perdue ; ce qui ressort de l'archive est
exactement ce qui y est entré, empreinte SHA-1 à l'appui.

Le magasin expose ``put``, ``head``, ``get``, ``download``, ``delete``,
``iter_objects``, ``usage``, ``check`` et un attribut ``prefix``. Ce contrat
était celui d'un client S3 du temps où la copie partait chez un hébergeur ;
il a survécu à son départ parce qu'il décrit bien ce qu'on attend d'un
magasin, et ``MirrorThread`` comme ``RestoreThread`` s'y adossent sans savoir
ce qu'il y a derrière.

**Un fichier, pas une arborescence.** C'est ce qui permet de déménager
l'archive d'un disque à l'autre en la copiant — et c'est prévu : FAT32
refusant tout fichier de plus de 4 Go, l'archive vit pour l'instant sur une
partition NTFS en attendant un disque dédié. Changer de place, ce sera
changer un chemin dans la configuration.

**Le contenu est dédoublonné par empreinte.** Deux copies du même titre dans
deux dossiers n'occupent qu'une place : la clé de l'objet dérive du SHA-1 de
son contenu (voir ``library_mirror.object_key``), et un objet déjà présent n'est
pas réécrit. Une initialisation interrompue se reprend donc à peu de frais.

Pourquoi SQLite plutôt que des fichiers posés dans un dossier : la question a
été tranchée par l'usage visé. Une archive n'a pas à être parcourue à la
main, elle a à être **complète et vérifiable**. Une base donne la transaction
— un titre entre entièrement ou pas du tout — et le compte exact de ce
qu'elle contient sans parcourir un disque. Elle se déplace d'un seul geste.
Le prix est qu'il faut cet outil pour en ressortir un titre ; c'est à cela
que sert ``RestoreThread``, et l'écran Bibliothèque qui le commande.

Les gros contenus ne transitent pas par la mémoire quand on peut l'éviter :
``download`` lit le blob par tranches via ``Connection.blobopen`` (Python
3.11 et au-delà), et retombe sur une lecture simple si l'interface manque.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time

from deekeep.core import settings as app_settings


class BlobError(RuntimeError):
    """Échec d'un échange avec le magasin."""


class NotConfigured(BlobError):
    """Aucun magasin n'est enregistré."""


# Préfixe des clés. Tout ce que l'application range vit dessous : une archive
# reste utilisable pour autre chose.
DEFAULT_PREFIX = "deekeep/"


def normalize_prefix(prefix):
    """Préfixe de clé : sans « / » de tête, avec « / » de queue."""
    value = (prefix or "").strip().strip("/")
    return (value + "/") if value else ""


# Nom par défaut du fichier d'archive.
DEFAULT_FILENAME = "deekeep-archive.db"

# Taille de tranche pour les lectures et écritures de blob.
CHUNK = 1 << 20

SCHEMA = (
    # Le contenu, dédoublonné par clé (donc par empreinte du contenu).
    "CREATE TABLE IF NOT EXISTS objects ("
    "    key    TEXT PRIMARY KEY,"
    "    bytes  INTEGER NOT NULL,"
    "    stored_at REAL NOT NULL,"
    "    data   BLOB NOT NULL"
    ")",
    # Quelques repères sur l'archive elle-même : version, date de création.
    "CREATE TABLE IF NOT EXISTS archive_meta ("
    "    key TEXT PRIMARY KEY,"
    "    value TEXT"
    ")",
)

SCHEMA_VERSION = "1"


CONFIG_FILENAME = "archive.json"


def config_path():
    return os.path.join(app_settings.data_dir(), CONFIG_FILENAME)


def load_config():
    """Emplacement de l'archive, et préfixe des clés."""
    data = {"root": "", "prefix": DEFAULT_PREFIX}
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                for key in data:
                    if stored.get(key):
                        data[key] = str(stored[key])
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[sqlite_store] Lecture de {path} impossible : {exc}")
    data["root"] = os.environ.get("DEEKEEP_ARCHIVE") or data["root"]
    return data


def save_config(root, prefix=DEFAULT_PREFIX):
    """Écrit l'emplacement dans le dossier de données (écriture atomique).

    Déménager l'archive — vers la partition NTFS d'un disque plus grand, par
    exemple — se résume à copier le fichier puis à repasser ici.
    """
    path = config_path()
    tmp_path = path + ".tmp"
    payload = {"root": os.path.abspath(root) if root else "",
               "prefix": normalize_prefix(prefix)}
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def is_configured():
    return bool((load_config().get("root") or "").strip())


def free_bytes(path):
    """Place libre sur le volume qui porte ``path``, ou 0 s'il est injoignable."""
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return 0
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def same_device_as(path, other):
    """Vrai si les deux chemins vivent sur le même volume.

    Une archive posée sur le disque de la bibliothèque protège d'un
    effacement par mégarde, pas d'une panne : les deux disparaissent
    ensemble. L'écran doit pouvoir le dire.
    """
    try:
        a = os.path.splitdrive(os.path.abspath(path))[0].upper()
        b = os.path.splitdrive(os.path.abspath(other))[0].upper()
    except (OSError, ValueError):
        return False
    return bool(a) and a == b


class SqliteStore:
    """Archive SQLite sans perte : un titre par blob, dédoublonné."""

    def __init__(self, config=None):
        data = config or {}
        path = (data.get("root") or data.get("path") or "").strip()
        if not path:
            raise NotConfigured(
                "Aucune archive locale n'est configurée : indiquez le fichier "
                "où ranger les titres."
            )
        # Un dossier donné à la place d'un fichier : on y crée le fichier par
        # défaut. C'est ce qu'on écrit spontanément quand on pense « où », et
        # non « sous quel nom ».
        if os.path.isdir(path) or not os.path.splitext(path)[1]:
            path = os.path.join(path, DEFAULT_FILENAME)
        self.path = os.path.abspath(path)
        self.prefix = normalize_prefix(data.get("prefix") or DEFAULT_PREFIX)
        self._connection = None

    # ------------------------------------------------------------------
    # Connexion
    # ------------------------------------------------------------------
    def _db(self):
        if self._connection is not None:
            return self._connection
        folder = os.path.dirname(self.path)
        try:
            if folder:
                os.makedirs(folder, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=30)
        except (OSError, sqlite3.Error) as exc:
            raise BlobError(f"Archive inaccessible : {exc}") from exc
        try:
            # WAL : l'écoute d'un titre peut lire pendant qu'une archivage
            # écrit, sans que l'un bloque l'autre.
            connection.execute("PRAGMA journal_mode=WAL")
            # L'archive est une copie : une perte de courant peut coûter la
            # dernière transaction, jamais la base. « normal » épargne un
            # vidage disque par titre, ce qui se voit sur trois mille.
            connection.execute("PRAGMA synchronous=NORMAL")
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO archive_meta(key, value) VALUES(?, ?)",
                ("schema_version", SCHEMA_VERSION))
            connection.execute(
                "INSERT OR IGNORE INTO archive_meta(key, value) VALUES(?, ?)",
                ("created_at", str(time.time())))
            connection.commit()
        except sqlite3.Error as exc:
            connection.close()
            raise BlobError(f"Archive illisible : {exc}") from exc
        self._connection = connection
        return connection

    def close(self):
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    # ------------------------------------------------------------------
    # Opérations
    # ------------------------------------------------------------------
    def put(self, key, data, content_type=None):
        """Range un objet. Rend sa taille, en guise d'ETag.

        ``INSERT OR REPLACE`` : réécrire une clé existante n'est pas une
        erreur, et la clé dérivant du contenu, cela ne peut pas écraser un
        contenu différent.
        """
        db = self._db()
        try:
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO objects(key, bytes, stored_at, data)"
                    " VALUES(?, ?, ?, ?)",
                    (key, len(data), time.time(), sqlite3.Binary(data)))
        except sqlite3.Error as exc:
            raise BlobError(f"Écriture de {key} impossible : {exc}") from exc
        return str(len(data))

    def head(self, key):
        """Taille d'un objet, ou None s'il est absent."""
        db = self._db()
        try:
            row = db.execute("SELECT bytes FROM objects WHERE key = ?",
                             (key,)).fetchone()
        except sqlite3.Error as exc:
            raise BlobError(f"Lecture de {key} impossible : {exc}") from exc
        if row is None:
            return None
        return {"bytes": int(row[0]), "etag": ""}

    def get(self, key):
        """Contenu d'un objet (octets)."""
        db = self._db()
        try:
            row = db.execute("SELECT data FROM objects WHERE key = ?",
                             (key,)).fetchone()
        except sqlite3.Error as exc:
            raise BlobError(f"Lecture de {key} impossible : {exc}") from exc
        if row is None:
            raise BlobError(f"Lecture de {key} refusée : objet absent de "
                            f"l'archive.")
        return bytes(row[0])

    def download(self, key, destination, chunk=CHUNK):
        """Écrit un objet dans un fichier. Rend le nombre d'octets écrits.

        Même garantie qu'ailleurs : écriture dans un temporaire du dossier de
        destination, mis en place d'un coup. Une restauration interrompue ne
        laisse pas de moignon.
        """
        db = self._db()
        row = db.execute("SELECT rowid, bytes FROM objects WHERE key = ?",
                         (key,)).fetchone()
        if row is None:
            raise BlobError(f"Lecture de {key} refusée : objet absent de "
                            f"l'archive.")
        rowid = int(row[0])

        folder = os.path.dirname(destination) or "."
        handle, temporary = tempfile.mkstemp(prefix=".dk_", dir=folder)
        written = 0
        try:
            with os.fdopen(handle, "wb") as output:
                try:
                    blob = db.blobopen("objects", "data", rowid, readonly=True)
                except (AttributeError, sqlite3.Error):
                    # Interface absente (Python < 3.11) : lecture d'un bloc.
                    data = self.get(key)
                    output.write(data)
                    written = len(data)
                else:
                    with blob:
                        total = len(blob)
                        while written < total:
                            block = blob.read(chunk)
                            if not block:
                                break
                            output.write(block)
                            written += len(block)
            os.replace(temporary, destination)
        except BaseException:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
        return written

    def delete(self, key):
        """Efface un objet. Un objet déjà absent n'est pas une erreur."""
        db = self._db()
        try:
            with db:
                db.execute("DELETE FROM objects WHERE key = ?", (key,))
        except sqlite3.Error as exc:
            raise BlobError(f"Effacement de {key} impossible : {exc}") from exc
        return True

    def iter_objects(self, prefix=None):
        """Parcourt les objets : (clé, taille)."""
        wanted = self.prefix if prefix is None else (prefix or "")
        db = self._db()
        if wanted:
            # ``LIKE`` avec échappement : un « _ » dans le préfixe ne doit pas
            # valoir « n'importe quel caractère ».
            motif = wanted.replace("\\", "\\\\").replace("%", "\\%") \
                          .replace("_", "\\_") + "%"
            query = ("SELECT key, bytes FROM objects WHERE key LIKE ? "
                     "ESCAPE '\\' ORDER BY key")
            cursor = db.execute(query, (motif,))
        else:
            cursor = db.execute("SELECT key, bytes FROM objects ORDER BY key")
        for key, size in cursor:
            yield key, int(size)

    def usage(self, prefix=None):
        """(nombre d'objets, octets occupés) sous le préfixe de DeeKeep."""
        count = total = 0
        for _key, size in self.iter_objects(prefix):
            count += 1
            total += size
        return count, total

    def check(self):
        """Vérifie que l'archive s'ouvre et accepte une écriture."""
        db = self._db()
        try:
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO archive_meta(key, value) "
                    "VALUES(?, ?)", ("checked_at", str(time.time())))
        except sqlite3.Error as exc:
            raise BlobError(f"Archive inutilisable : {exc}") from exc
        return True

    def presigned_get(self, key, expires=None):
        """Une archive ne sert pas de flux : le lecteur doit passer ailleurs.

        Le seau distant rend ici une URL que le lecteur ouvre directement.
        Un blob SQLite n'a pas d'adresse ; plutôt que de rendre quelque chose
        d'inutilisable, on le dit.
        """
        raise BlobError(
            "L'archive locale ne diffuse pas de flux : un titre doit être "
            "restauré sur le disque pour être écouté.")

    # ------------------------------------------------------------------
    # Entretien
    # ------------------------------------------------------------------
    def file_bytes(self):
        """Poids du fichier d'archive sur le disque, WAL compris."""
        total = 0
        for suffixe in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffixe)
            except OSError:
                pass
        return total

    def vacuum(self):
        """Compacte l'archive après des effacements. Rend les octets gagnés."""
        avant = self.file_bytes()
        db = self._db()
        try:
            db.execute("VACUUM")
        except sqlite3.Error as exc:
            raise BlobError(f"Compactage impossible : {exc}") from exc
        return max(0, avant - self.file_bytes())


def open_store(config=None):
    """Archive configurée, ou ``NotConfigured`` s'il n'y en a pas."""
    return SqliteStore(config or load_config())
