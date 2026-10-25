"""
Cache local de la base DeeKeep (SQLite).

Fichier ``deekeep_cache.db``, dans le dossier de données de
l'utilisateur, à côté de la configuration. Il porte exactement le même
schéma que la base en ligne (voir ``db_schema``), ce qui donne deux
avantages :

  * l'application lit toujours le cache, jamais le réseau : afficher
    trois mille titres avec leurs jaquettes se fait en millisecondes,
    quel que soit l'état de la liaison ;
  * la base en ligne peut être indisponible — panne, absence de réseau,
    quota du mois épuisé — sans que l'application cesse de fonctionner :
    les écritures s'accumulent dans la file ``sync_outbox`` et repartent
    au rétablissement.

Le cache n'est pas jetable : c'est lui qui détient les écritures en
attente. Il est en revanche reconstructible à partir de la base en ligne
(``sync_service.pull_all``) ou du disque et des anciens fichiers JSON
(``db_init``) s'il vient à manquer.

Toutes les écritures passent par ``upsert`` / ``mark_deleted``, qui
tiennent à jour l'horodatage, l'empreinte de ligne et la file d'attente :
aucun module ne doit écrire dans les tables synchronisées par un SQL
direct, sans quoi la détection de désynchronisation perdrait pied.
"""

import os
import sqlite3
import threading
import time

from deekeep.db import db_schema
from deekeep.core import settings as app_settings


CACHE_FILENAME = "deekeep_cache.db"


def cache_path():
    return os.path.join(app_settings.data_dir(), CACHE_FILENAME)


class LocalDB:
    """Cache SQLite, partagé par tous les fils de l'application."""

    def __init__(self, path=None):
        self.path = path or cache_path()
        # Un seul objet connexion pour toute l'application, protégé par un
        # verrou : SQLite accepte les accès de plusieurs fils dès lors
        # qu'ils ne se chevauchent pas, et le cache ne subit jamais assez
        # d'écritures concurrentes pour que la sérialisation se remarque.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL : un lecteur n'attend pas l'écrivain, l'interface reste
            # fluide pendant qu'une synchronisation écrit en tâche de fond.
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
        self.ensure_schema()

    # ------------------------------------------------------------------
    # Schéma
    # ------------------------------------------------------------------

    def ensure_schema(self):
        """Crée les tables manquantes et enregistre la version du schéma."""
        with self._lock, self._conn:
            for statement in db_schema.shared_ddl():
                self._conn.execute(statement)
            for statement in db_schema.local_only_ddl():
                self._conn.execute(statement)
        stored = self.get_meta("schema_version")
        if stored is None:
            self.set_meta("schema_version", str(db_schema.SCHEMA_VERSION))
        elif int(stored) != db_schema.SCHEMA_VERSION:
            # Le cache est reconstructible : plutôt que d'écrire des
            # migrations pour un fichier qui se retélécharge, on le vide.
            self.reset_data()
            self.set_meta("schema_version", str(db_schema.SCHEMA_VERSION))

    def reset_data(self):
        """Vide les tables synchronisées et l'état de synchronisation."""
        with self._lock, self._conn:
            for name in db_schema.TABLE_ORDER:
                self._conn.execute(f"DELETE FROM {name}")
            self._conn.execute("DELETE FROM sync_state")
            self._conn.execute("DELETE FROM sync_outbox")

    def get_meta(self, key, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, None if value is None else str(value)),
            )

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------

    def query(self, sql, params=()):
        """Requête libre ; renvoie des lignes indexables par nom de colonne."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def get_row(self, table, pk_value):
        spec = db_schema.TABLES_BY_NAME[table]
        rows = self.query(
            f"SELECT * FROM {table} WHERE {spec.pk} = ?", (pk_value,)
        )
        return dict(rows[0]) if rows else None

    def rows(self, table, include_deleted=False, order_by=None):
        """Toutes les lignes d'une table, sous forme de dictionnaires."""
        clause = "" if include_deleted else " WHERE deleted = 0"
        order = f" ORDER BY {order_by}" if order_by else ""
        return [dict(row)
                for row in self.query(f"SELECT * FROM {table}{clause}{order}")]

    def count(self, table, include_deleted=False):
        clause = "" if include_deleted else " WHERE deleted = 0"
        return self.query(f"SELECT COUNT(*) AS n FROM {table}{clause}")[0]["n"]

    def fingerprint(self, table):
        """Empreinte (nombre de lignes, somme des empreintes) de la table."""
        row = self.query(db_schema.fingerprint_sql(table))[0]
        return (int(row[0]), int(row[1]))

    def fingerprints(self):
        return {name: self.fingerprint(name) for name in db_schema.TABLE_ORDER}

    # ------------------------------------------------------------------
    # Écriture
    # ------------------------------------------------------------------

    def upsert(self, table, row, queue=True, updated_at=None, commit=True):
        """
        Insère ou met à jour une ligne.

        ``row`` est un dictionnaire de colonnes de contenu. L'horodatage et
        l'empreinte sont calculés ici ; la clé est ajoutée à la file
        d'attente d'envoi sauf si ``queue`` est faux (cas d'une ligne
        reçue de la base en ligne, qu'il ne faut évidemment pas lui
        renvoyer).
        """
        return self.upsert_many(table, [row], queue=queue,
                                updated_at=updated_at, commit=commit)

    def upsert_many(self, table, rows, queue=True, updated_at=None,
                    commit=True):
        """Version en lot d'``upsert`` — une seule transaction."""
        spec = db_schema.TABLES_BY_NAME[table]
        columns = spec.all_columns
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(
            f"{name} = excluded.{name}" for name in columns if name != spec.pk
        )
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT({spec.pk}) DO UPDATE SET {assignments}"
        )

        now = time.time()
        payload, queued = [], []
        for row in rows:
            record = dict(row)
            record.setdefault("deleted", 0)
            record["deleted"] = 1 if record["deleted"] else 0
            record["updated_at"] = (
                updated_at if updated_at is not None
                else record.get("updated_at") or now
            )
            record["h"] = db_schema.row_hash(spec, record)
            payload.append([record.get(name) for name in columns])
            queued.append((table, record.get(spec.pk), now))

        with self._lock:
            self._conn.executemany(sql, payload)
            if queue:
                self._conn.executemany(
                    "INSERT INTO sync_outbox (table_name, pk, queued_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(table_name, pk) DO UPDATE "
                    "SET queued_at = excluded.queued_at",
                    queued,
                )
            if commit:
                self._conn.commit()
        return len(payload)

    def mark_deleted(self, table, pk_values, queue=True):
        """
        Marque des lignes comme supprimées (pierre tombale).

        La ligne reste en base, vidée de son contenu : c'est ce qui permet
        à la suppression de se propager à l'autre côté. Le contenu est
        effacé pour ne pas garder d'octets inutiles — une jaquette de
        200 Ko n'a pas à survivre à sa suppression.
        """
        spec = db_schema.TABLES_BY_NAME[table]
        if isinstance(pk_values, str) or not hasattr(pk_values, "__iter__"):
            pk_values = [pk_values]
        rows = []
        for value in pk_values:
            record = {name: None for name in spec.content_columns}
            record[spec.pk] = value
            record["deleted"] = 1
            rows.append(record)
        return self.upsert_many(table, rows, queue=queue)

    def hard_delete(self, table, pk_values, queue=False):
        """Suppression réelle, sans pierre tombale (usage : purge locale)."""
        spec = db_schema.TABLES_BY_NAME[table]
        if isinstance(pk_values, str) or not hasattr(pk_values, "__iter__"):
            pk_values = [pk_values]
        values = [(value,) for value in pk_values]
        with self._lock, self._conn:
            self._conn.executemany(
                f"DELETE FROM {table} WHERE {spec.pk} = ?", values
            )
            if queue:
                self._conn.executemany(
                    "DELETE FROM sync_outbox WHERE table_name = ? AND pk = ?",
                    [(table, value[0]) for value in values],
                )
        return len(values)

    # ------------------------------------------------------------------
    # File d'attente d'envoi
    # ------------------------------------------------------------------

    def pending(self, table=None, limit=None):
        """Clés en attente d'envoi, les plus anciennes d'abord."""
        sql = "SELECT table_name, pk FROM sync_outbox"
        params = []
        if table:
            sql += " WHERE table_name = ?"
            params.append(table)
        sql += " ORDER BY queued_at"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(row["table_name"], row["pk"]) for row in self.query(sql, params)]

    def pending_count(self):
        return self.query("SELECT COUNT(*) AS n FROM sync_outbox")[0]["n"]

    def clear_pending(self, entries=None):
        """Retire des clés de la file (après envoi réussi)."""
        with self._lock, self._conn:
            if entries is None:
                self._conn.execute("DELETE FROM sync_outbox")
            else:
                self._conn.executemany(
                    "DELETE FROM sync_outbox WHERE table_name = ? AND pk = ?",
                    list(entries),
                )

    def queue_all(self, tables=None):
        """
        Met toutes les lignes en file d'attente.

        Sert au premier envoi et à la résolution d'un conflit tranché en
        faveur du local : la base en ligne doit alors tout recevoir.
        """
        now = time.time()
        with self._lock, self._conn:
            for name in tables or db_schema.TABLE_ORDER:
                spec = db_schema.TABLES_BY_NAME[name]
                self._conn.execute(
                    "INSERT INTO sync_outbox (table_name, pk, queued_at) "
                    f"SELECT ?, {spec.pk}, ? FROM {name} "
                    "WHERE true ON CONFLICT(table_name, pk) DO NOTHING",
                    (name, now),
                )

    # ------------------------------------------------------------------
    # État de synchronisation
    # ------------------------------------------------------------------

    def synced_fingerprint(self, table):
        """
        Empreinte constatée lors du dernier échange réussi.

        ``(-1, -1)`` signifie « jamais synchronisé » : impossible de savoir
        quel côté a bougé, un premier rapprochement complet est nécessaire.
        """
        rows = self.query(
            "SELECT synced_count, synced_sum FROM sync_state "
            "WHERE table_name = ?", (table,)
        )
        if not rows:
            return (-1, -1)
        return (int(rows[0]["synced_count"]), int(rows[0]["synced_sum"]))

    def set_synced_fingerprint(self, table, fingerprint):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sync_state "
                "(table_name, synced_count, synced_sum, synced_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(table_name) DO UPDATE SET "
                "synced_count = excluded.synced_count, "
                "synced_sum = excluded.synced_sum, "
                "synced_at = excluded.synced_at",
                (table, int(fingerprint[0]), int(fingerprint[1]), time.time()),
            )

    def last_sync_at(self):
        rows = self.query("SELECT MIN(synced_at) AS t FROM sync_state")
        return rows[0]["t"] or 0.0

    # ------------------------------------------------------------------
    # Paramètres
    # ------------------------------------------------------------------

    def get_setting(self, key, default=None):
        row = self.get_row("app_settings", key)
        if not row or row.get("deleted"):
            return default
        return db_schema.json_load(row.get("value"), default)

    def set_setting(self, key, value, queue=True):
        return self.upsert(
            "app_settings",
            {"key": key, "value": db_schema.json_dump(value)},
            queue=queue,
        )

    def set_settings(self, mapping, queue=True):
        """Écrit plusieurs paramètres ; ne touche que ceux qui changent."""
        rows = []
        for key, value in mapping.items():
            encoded = db_schema.json_dump(value)
            current = self.get_row("app_settings", key)
            if current and not current.get("deleted") \
                    and current.get("value") == encoded:
                continue
            rows.append({"key": key, "value": encoded})
        if not rows:
            return 0
        return self.upsert_many("app_settings", rows, queue=queue)

    def all_settings(self):
        """Tous les paramètres, sous forme de dictionnaire Python."""
        result = {}
        for row in self.rows("app_settings"):
            result[row["key"]] = db_schema.json_load(row.get("value"))
        return result

    # ------------------------------------------------------------------
    # Divers
    # ------------------------------------------------------------------

    def vacuum(self):
        """Compacte le fichier (après une purge de jaquettes, par exemple)."""
        with self._lock:
            self._conn.isolation_level = None
            try:
                self._conn.execute("VACUUM")
            finally:
                self._conn.isolation_level = ""

    def size_bytes(self):
        try:
            total = os.path.getsize(self.path)
        except OSError:
            return 0
        # Le journal WAL peut contenir une part notable des données.
        for suffix in ("-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def close(self):
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()


# Instance partagée, créée à la demande : les modules de l'interface
# n'ont pas à se passer l'objet de main en main.
_INSTANCE = None
_INSTANCE_LOCK = threading.Lock()


def shared():
    """Cache local partagé par toute l'application."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = LocalDB()
        return _INSTANCE
