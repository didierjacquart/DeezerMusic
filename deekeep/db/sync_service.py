"""
Rapprochement du cache local et de la base DeeKeep en ligne.

Principe
--------
Chaque table porte une empreinte de contenu ``(nombre de lignes, somme
des empreintes de ligne)``, calculée par la même requête des deux côtés
(voir ``db_schema``). Le cache local retient en outre l'empreinte
constatée lors du dernier échange réussi. Trois valeurs suffisent alors à
savoir quoi faire, sans transférer une seule donnée :

    locale == distante                  → rien à faire ;
    locale == dernière connue           → seul le distant a bougé : on tire ;
    distante == dernière connue         → seul le local a bougé : on pousse ;
    les trois diffèrent                 → les deux côtés ont bougé :
                                          DÉSYNCHRONISATION, l'utilisateur
                                          tranche.

C'est volontairement l'utilisateur qui tranche, et non une règle
automatique du type « la date la plus récente gagne » : une fusion
silencieuse peut réécrire trois mille titres sur la foi d'une horloge mal
réglée, alors qu'un conflit n'apparaît qu'après une modification des deux
côtés — situation rare, où le choix vaut d'être posé.

Transferts
----------
Ni l'envoi ni la réception ne recopient tout. On échange d'abord l'index
des lignes — clé, empreinte, horodatage, drapeau de suppression — qui
pèse quelques dizaines de kilo-octets, puis les seules lignes qui
diffèrent. Sur la table des jaquettes, dont le contenu approche la
centaine de mégaoctets, la différence est décisive.

Les suppressions voyagent sous forme de pierres tombales (ligne conservée,
vidée, ``deleted = 1``) : une ligne réellement effacée d'un côté
passerait pour une ligne « jamais vue » de l'autre et reviendrait au
rapprochement suivant.
"""

import time

from deekeep.db import cloud_db
from deekeep.db import db_schema
from deekeep.db import local_db


# --- États possibles d'une table, et de l'ensemble -------------------

NOT_CONFIGURED = "not_configured"   # aucun identifiant de base en ligne
OFFLINE = "offline"                 # base injoignable
IN_SYNC = "in_sync"                 # les deux côtés sont identiques
PUSH_NEEDED = "push_needed"         # seul le local a changé
PULL_NEEDED = "pull_needed"         # seul le distant a changé
CONFLICT = "conflict"               # les deux ont changé : désynchronisation
ERROR = "error"

STATUS_LABELS = {
    NOT_CONFIGURED: "Base en ligne non configurée",
    OFFLINE: "Base en ligne injoignable — le cache local prend le relais",
    IN_SYNC: "Synchronisé",
    PUSH_NEEDED: "Modifications locales à envoyer",
    PULL_NEEDED: "Modifications distantes à recevoir",
    CONFLICT: "Désynchronisation : les deux côtés ont changé",
    ERROR: "Erreur de synchronisation",
}

# Taille des pages d'index et de contenu. Les jaquettes se comptent en
# dizaines de kilo-octets pièce : leurs lignes voyagent par petits
# paquets, quand celles des titres passent par centaines.
INDEX_PAGE = 4000
ROW_PAGE = {"covers": 24}
DEFAULT_ROW_PAGE = 400


class TableReport:
    """Constat dressé pour une table lors d'une analyse."""

    def __init__(self, table, local_fp, remote_fp, synced_fp, verdict):
        self.table = table
        self.local = local_fp
        self.remote = remote_fp
        self.synced = synced_fp
        self.verdict = verdict
        self.pushed = 0
        self.pulled = 0

    @property
    def label(self):
        return STATUS_LABELS.get(self.verdict, self.verdict)

    def __repr__(self):
        return (f"<{self.table} {self.verdict} local={self.local} "
                f"distant={self.remote} référence={self.synced}>")


class SyncReport:
    """Résultat d'une analyse ou d'une synchronisation."""

    def __init__(self, status, tables=None, message="", error=None):
        self.status = status
        self.tables = tables or {}
        self.message = message or STATUS_LABELS.get(status, "")
        self.error = error
        self.at = time.time()

    @property
    def conflicts(self):
        return [name for name, report in self.tables.items()
                if report.verdict == CONFLICT]

    @property
    def pushed(self):
        return sum(report.pushed for report in self.tables.values())

    @property
    def pulled(self):
        return sum(report.pulled for report in self.tables.values())

    def summary(self):
        if self.status == CONFLICT:
            return ("Désynchronisation détectée sur : "
                    + ", ".join(self.conflicts))
        if self.status in (OFFLINE, NOT_CONFIGURED, ERROR):
            return self.message
        parts = []
        if self.pushed:
            parts.append(f"{self.pushed} ligne(s) envoyée(s)")
        if self.pulled:
            parts.append(f"{self.pulled} ligne(s) reçue(s)")
        return ", ".join(parts) if parts else "Déjà à jour"


class SyncManager:
    """
    Pilote les échanges entre le cache local et la base en ligne.

    L'objet est réutilisable et sans état durable : tout ce qui doit
    survivre à l'exécution est dans le cache local (file d'attente,
    empreintes de référence).
    """

    def __init__(self, local=None, client=None):
        self.local = local or local_db.shared()
        self._client = client
        self.last_report = None

    # ------------------------------------------------------------------
    # Connexion
    # ------------------------------------------------------------------

    def client(self):
        """Client de la base en ligne, créé à la demande."""
        if self._client is None:
            self._client = cloud_db.TursoClient.from_credentials()
        return self._client

    def forget_client(self):
        """Oublie le client (après un changement d'identifiants)."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    def is_configured(self):
        return self._client is not None or cloud_db.is_configured()

    # ------------------------------------------------------------------
    # Schéma distant
    # ------------------------------------------------------------------

    def ensure_remote_schema(self, progress=None):
        """
        Crée les tables manquantes sur la base en ligne.

        Appelable à volonté : toutes les instructions sont en
        « IF NOT EXISTS ». Si la base porte un schéma d'une version
        antérieure, la version est simplement relevée — le contenu, lui,
        n'est jamais détruit ici.
        """
        client = self.client()
        statements = [(sql, []) for sql in db_schema.shared_ddl()]
        client.batch(statements, progress=progress)
        client.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ["schema_version", str(db_schema.SCHEMA_VERSION)],
        )
        return True

    def remote_schema_version(self):
        try:
            result = self.client().execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            )
        except cloud_db.CloudError:
            return None
        value = result.scalar()
        return int(value) if value is not None else None

    def remote_fingerprints(self):
        """Empreintes des tables distantes, en un seul aller-retour."""
        client = self.client()
        statements = [(db_schema.fingerprint_sql(name), [])
                      for name in db_schema.TABLE_ORDER]
        results = client._run(statements)
        fingerprints = {}
        for name, result in zip(db_schema.TABLE_ORDER, results):
            row = result.rows[0] if result.rows else (0, 0)
            fingerprints[name] = (int(row[0] or 0), int(row[1] or 0))
        return fingerprints

    # ------------------------------------------------------------------
    # Analyse
    # ------------------------------------------------------------------

    def analyze(self):
        """
        Compare les deux côtés sans rien transférer.

        Renvoie un ``SyncReport`` dont le statut global est le cas le plus
        grave rencontré : un conflit sur une seule table met l'ensemble en
        conflit.
        """
        if not self.is_configured():
            return SyncReport(NOT_CONFIGURED)

        try:
            remote = self.remote_fingerprints()
        except cloud_db.CloudError as exc:
            message = str(exc)
            # Une table absente n'est pas une panne : la base est neuve.
            if "no such table" in message.lower():
                try:
                    self.ensure_remote_schema()
                    remote = self.remote_fingerprints()
                except cloud_db.CloudError as retry_exc:
                    return SyncReport(OFFLINE, message=str(retry_exc),
                                      error=retry_exc)
            else:
                return SyncReport(OFFLINE, message=message, error=exc)

        tables = {}
        for name in db_schema.TABLE_ORDER:
            local_fp = self.local.fingerprint(name)
            remote_fp = remote[name]
            synced_fp = self.local.synced_fingerprint(name)
            tables[name] = TableReport(
                name, local_fp, remote_fp, synced_fp,
                _verdict(local_fp, remote_fp, synced_fp),
            )

        verdicts = {report.verdict for report in tables.values()}
        if CONFLICT in verdicts:
            status = CONFLICT
        elif PUSH_NEEDED in verdicts and PULL_NEEDED in verdicts:
            # Rien de contradictoire : une table part, une autre arrive.
            status = PUSH_NEEDED
        elif PUSH_NEEDED in verdicts:
            status = PUSH_NEEDED
        elif PULL_NEEDED in verdicts:
            status = PULL_NEEDED
        else:
            status = IN_SYNC

        report = SyncReport(status, tables)
        self.last_report = report
        return report

    # ------------------------------------------------------------------
    # Synchronisation
    # ------------------------------------------------------------------

    def sync(self, progress=None):
        """
        Rapproche les deux côtés pour tout ce qui peut l'être sans arbitrage.

        Les tables en conflit sont laissées en l'état et signalées dans le
        rapport : c'est à l'utilisateur de choisir un sens, via
        ``resolve``.
        """
        report = self.analyze()
        if report.status in (NOT_CONFIGURED, OFFLINE, ERROR):
            return report

        try:
            for name, table_report in report.tables.items():
                if table_report.verdict == PUSH_NEEDED:
                    table_report.pushed = self.push_table(name, progress=progress)
                elif table_report.verdict == PULL_NEEDED:
                    table_report.pulled = self.pull_table(name, progress=progress)
                elif table_report.verdict == IN_SYNC:
                    # Contenus identiques : la file d'attente porte des
                    # écritures déjà présentes en ligne, on l'allège.
                    self._settle(name)
        except cloud_db.CloudError as exc:
            failed = SyncReport(OFFLINE, report.tables, message=str(exc),
                                error=exc)
            self.last_report = failed
            return failed

        # Les envois et réceptions ont pu changer les empreintes : on
        # reprend le constat pour rendre un état juste.
        final = self.analyze()
        for name, table_report in final.tables.items():
            if name in report.tables:
                table_report.pushed = report.tables[name].pushed
                table_report.pulled = report.tables[name].pulled
        self.last_report = final
        return final

    def resolve(self, direction, tables=None, progress=None):
        """
        Tranche une désynchronisation dans le sens demandé.

        ``direction`` vaut « local » (le disque de cette machine fait foi,
        la base en ligne est réalignée) ou « remote » (l'inverse). Sans
        ``tables``, toutes les tables en conflit du dernier constat sont
        traitées.
        """
        if direction not in ("local", "remote"):
            raise ValueError("direction attendue : « local » ou « remote »")

        if tables is None:
            source = self.last_report or self.analyze()
            tables = source.conflicts or list(db_schema.TABLE_ORDER)

        for name in tables:
            if direction == "local":
                self.push_table(name, full=True, progress=progress)
            else:
                self.pull_table(name, full=True, progress=progress)

        report = self.analyze()
        self.last_report = report
        return report

    # ------------------------------------------------------------------
    # Envoi
    # ------------------------------------------------------------------

    def push_table(self, table, full=False, progress=None):
        """
        Envoie les lignes locales vers la base en ligne.

        ``full`` réaligne complètement la table distante sur la locale :
        toutes les lignes sont réécrites et celles qui n'existent que côté
        distant sont supprimées. Sans ``full``, seules les lignes de la
        file d'attente partent.
        """
        spec = db_schema.TABLES_BY_NAME[table]
        client = self.client()

        if full:
            keys = [row[spec.pk] for row in
                    self.local.rows(table, include_deleted=True)]
        else:
            keys = [pk for _, pk in self.local.pending(table)]
            # Une ligne peut avoir été mise en file puis effacée du cache
            # sans pierre tombale : on ne cherche pas à l'envoyer.
            keys = [key for key in keys if key is not None]

        sent = 0
        if keys:
            columns = spec.all_columns
            placeholders = ", ".join("?" for _ in columns)
            assignments = ", ".join(f"{name} = excluded.{name}"
                                    for name in columns if name != spec.pk)
            sql = (
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT({spec.pk}) DO UPDATE SET {assignments}"
            )
            statements = []
            for key in keys:
                row = self.local.get_row(table, key)
                if row is None:
                    continue
                statements.append((sql, [row.get(name) for name in columns]))
            sent = client.batch(statements, progress=progress)

        if full:
            sent += self._delete_remote_extras(table)

        self.local.clear_pending([(table, key) for key in keys])
        self._settle(table)
        return sent

    def _delete_remote_extras(self, table):
        """Supprime en ligne les lignes absentes du cache local."""
        spec = db_schema.TABLES_BY_NAME[table]
        remote_keys = set(self._remote_index(table))
        local_keys = {row[spec.pk] for row in
                      self.local.rows(table, include_deleted=True)}
        extras = sorted(remote_keys - local_keys)
        if not extras:
            return 0
        client = self.client()
        statements = []
        for chunk in _chunks(extras, cloud_db.MAX_BOUND_PARAMS):
            marks = ", ".join("?" for _ in chunk)
            statements.append(
                (f"DELETE FROM {table} WHERE {spec.pk} IN ({marks})", chunk)
            )
        client.batch(statements)
        return len(extras)

    # ------------------------------------------------------------------
    # Réception
    # ------------------------------------------------------------------

    def pull_table(self, table, full=False, progress=None):
        """
        Rapatrie les lignes distantes qui manquent ou diffèrent.

        ``full`` aligne complètement la table locale sur la distante,
        suppression des lignes surnuméraires comprise.
        """
        spec = db_schema.TABLES_BY_NAME[table]
        remote_index = self._remote_index(table)
        local_index = {
            row[spec.pk]: (row["h"], row["updated_at"])
            for row in self.local.rows(table, include_deleted=True)
        }

        wanted = [key for key, (h, _) in remote_index.items()
                  if key not in local_index or local_index[key][0] != h]

        received = 0
        page = ROW_PAGE.get(table, DEFAULT_ROW_PAGE)
        for chunk in _chunks(wanted, min(page, cloud_db.MAX_BOUND_PARAMS)):
            rows = self._remote_rows(table, chunk)
            if rows:
                # queue=False : ces lignes viennent de la base en ligne,
                # les lui renvoyer n'aurait aucun sens.
                self.local.upsert_many(table, rows, queue=False)
                received += len(rows)
            if progress is not None:
                progress(received)

        if full:
            extras = [key for key in local_index if key not in remote_index]
            if extras:
                self.local.hard_delete(table, extras, queue=True)
                received += len(extras)
            self.local.clear_pending([(table, key) for key in local_index])

        self._settle(table)
        return received

    def _remote_index(self, table):
        """Index des lignes distantes : clé -> (empreinte, horodatage)."""
        spec = db_schema.TABLES_BY_NAME[table]
        client = self.client()
        index = {}
        offset = 0
        while True:
            result = client.execute(
                f"SELECT {spec.pk}, h, updated_at FROM {table} "
                f"ORDER BY {spec.pk} LIMIT {INDEX_PAGE} OFFSET {offset}"
            )
            for key, h, updated_at in result.rows:
                index[key] = (int(h or 0), float(updated_at or 0))
            if len(result.rows) < INDEX_PAGE:
                break
            offset += INDEX_PAGE
        return index

    def _remote_rows(self, table, keys):
        """Contenu complet des lignes distantes désignées."""
        if not keys:
            return []
        spec = db_schema.TABLES_BY_NAME[table]
        columns = spec.all_columns
        marks = ", ".join("?" for _ in keys)
        result = self.client().execute(
            f"SELECT {', '.join(columns)} FROM {table} "
            f"WHERE {spec.pk} IN ({marks})",
            list(keys),
        )
        return result.dicts()

    # ------------------------------------------------------------------
    # Interne
    # ------------------------------------------------------------------

    def _settle(self, table):
        """
        Enregistre l'empreinte de référence si les deux côtés concordent.

        Sans cette étape, la prochaine analyse ne saurait plus quel côté a
        bougé et conclurait à un conflit à la moindre modification.
        """
        try:
            remote_fp = self.remote_fingerprints()[table]
        except cloud_db.CloudError:
            return
        local_fp = self.local.fingerprint(table)
        if local_fp == remote_fp:
            self.local.set_synced_fingerprint(table, local_fp)
            self.local.clear_pending(
                [(table, pk) for _, pk in self.local.pending(table)]
            )

    # ------------------------------------------------------------------
    # Informations
    # ------------------------------------------------------------------

    def remote_stats(self):
        """Nombre de lignes et poids approché de la base en ligne."""
        client = self.client()
        statements = [(f"SELECT COUNT(*) FROM {name}", [])
                      for name in db_schema.TABLE_ORDER]
        statements.append(
            ("SELECT COALESCE(SUM(bytes), 0) FROM covers WHERE deleted = 0", [])
        )
        results = client._run(statements)
        stats = {name: int(result.scalar(0) or 0)
                 for name, result in zip(db_schema.TABLE_ORDER, results)}
        stats["cover_bytes"] = int(results[-1].scalar(0) or 0)
        return stats


def _verdict(local_fp, remote_fp, synced_fp):
    """Diagnostic d'une table à partir de ses trois empreintes."""
    if local_fp == remote_fp:
        return IN_SYNC

    never_synced = synced_fp == (-1, -1)
    if never_synced:
        # Premier contact : un côté vide ne peut rien réclamer.
        if remote_fp[0] == 0:
            return PUSH_NEEDED
        if local_fp[0] == 0:
            return PULL_NEEDED
        return CONFLICT

    local_moved = local_fp != synced_fp
    remote_moved = remote_fp != synced_fp
    if local_moved and remote_moved:
        return CONFLICT
    if local_moved:
        return PUSH_NEEDED
    if remote_moved:
        return PULL_NEEDED
    # Les deux valent la référence sans être égales : impossible en
    # théorie, on préfère alerter plutôt que d'écraser au hasard.
    return CONFLICT


def _chunks(items, size):
    items = list(items)
    for start in range(0, len(items), max(1, size)):
        yield items[start:start + size]
