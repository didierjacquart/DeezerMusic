"""
Schéma de la base de données DeeKeep, partagé par le cache local et la
base en ligne.

Un seul endroit décrit les tables : le cache local (SQLite, fichier
``deekeep_cache.db`` du dossier de données) et la base distante
(Turso / libSQL, dialecte SQLite lui aussi) sont créés à partir des mêmes
définitions. Toute évolution du schéma se fait ici, en incrémentant
``SCHEMA_VERSION``.

Trois colonnes techniques complètent chaque table :

    updated_at  horodatage Unix de la dernière écriture (arbitrage lors
                d'une fusion : la version la plus récente gagne) ;
    deleted     0/1, suppression logique — une ligne effacée d'un côté
                doit pouvoir se propager de l'autre, ce qu'une vraie
                suppression rendrait impossible (elle passerait pour une
                ligne « jamais vue ») ;
    h           empreinte du contenu de la ligne (30 bits, voir
                ``row_hash``), indépendante des horodatages.

L'empreinte d'une table entière, ``(COUNT(*), SUM(h))``, se calcule par
la même requête des deux côtés : deux tables de même empreinte ont, à
une collision près, le même contenu. C'est ce qui permet de détecter une
désynchronisation sans transférer les données, et sans qu'un simple écart
d'horodatage — inévitable après un envoi — passe pour une divergence.
"""

import hashlib
import json


# Version du schéma. Inscrite dans la table « schema_meta » des deux
# côtés ; une base créée par une version antérieure est reconstruite.
SCHEMA_VERSION = 1

# Masque de l'empreinte de ligne. 30 bits suffisent : la somme des
# empreintes de quelques dizaines de milliers de lignes tient très
# largement dans l'entier 64 bits de SQLite, et la probabilité qu'une
# modification passe inaperçue reste négligeable une fois combinée au
# décompte des lignes.
HASH_MASK = (1 << 30) - 1


class TableSpec:
    """Description d'une table : sa clé, ses colonnes, son rôle."""

    def __init__(self, name, pk, columns, doc="", ordered=False):
        self.name = name
        self.pk = pk
        # columns : liste de (nom, type SQL). La clé primaire en fait
        # partie ; les colonnes techniques sont ajoutées automatiquement.
        self.columns = list(columns)
        self.doc = doc
        # Table dont l'ordre des lignes compte (historique, recherches) :
        # une colonne « position » sert alors à le restituer.
        self.ordered = ordered

    @property
    def content_columns(self):
        """Colonnes participant à l'empreinte (tout sauf les techniques)."""
        return [name for name, _ in self.columns]

    @property
    def all_columns(self):
        """Colonnes de la table, techniques comprises, dans l'ordre du DDL."""
        return self.content_columns + ["updated_at", "deleted", "h"]

    def column_type(self, column):
        for name, sql_type in self.columns:
            if name == column:
                return sql_type
        return {"updated_at": "REAL", "deleted": "INTEGER", "h": "INTEGER"}[column]

    def create_sql(self):
        parts = []
        for name, sql_type in self.columns:
            if name == self.pk:
                parts.append(f"{name} {sql_type} PRIMARY KEY")
            else:
                parts.append(f"{name} {sql_type}")
        parts.append("updated_at REAL NOT NULL DEFAULT 0")
        parts.append("deleted INTEGER NOT NULL DEFAULT 0")
        parts.append("h INTEGER NOT NULL DEFAULT 0")
        body = ",\n    ".join(parts)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n    {body}\n)"


# ----------------------------------------------------------------------
# Tables
# ----------------------------------------------------------------------

TABLES = [
    TableSpec(
        "app_settings",
        pk="key",
        columns=[("key", "TEXT"), ("value", "TEXT")],
        doc="Paramètres de l'application, un par ligne : dossier de dépôt, "
            "format et qualité d'export, options et filtres de rangement, "
            "motif de renommage, seuils de l'écran Qualité. « value » "
            "contient la valeur encodée en JSON, ce qui préserve son type "
            "(booléen, entier, chaîne, liste) sans multiplier les colonnes.",
    ),
    TableSpec(
        "searches",
        pk="query",
        columns=[("query", "TEXT"), ("position", "INTEGER")],
        doc="Historique des recherches. « position » restitue l'ordre "
            "d'affichage (0 = la plus récente).",
        ordered=True,
    ),
    TableSpec(
        "covers",
        pk="sha1",
        columns=[
            ("sha1", "TEXT"),
            ("mime", "TEXT"),
            ("bytes", "INTEGER"),
            ("width", "INTEGER"),
            ("height", "INTEGER"),
            ("data", "BLOB"),
        ],
        doc="Jaquettes, dédoublonnées par empreinte SHA-1 de l'image : les "
            "titres d'un même album partagent une seule ligne. Sur la "
            "bibliothèque de référence, 3 300 fichiers ne portent que "
            "1 461 images distinctes — deux fois moins d'octets à stocker.",
    ),
    TableSpec(
        "tracks",
        pk="path",
        columns=[
            ("path", "TEXT"),
            ("title", "TEXT"),
            ("artist", "TEXT"),
            ("album", "TEXT"),
            ("album_artist", "TEXT"),
            ("genre", "TEXT"),
            ("year", "TEXT"),
            ("track_no", "TEXT"),
            ("duration", "REAL"),
            ("bitrate", "INTEGER"),
            ("sample_rate", "INTEGER"),
            ("channels", "INTEGER"),
            ("codec", "TEXT"),
            ("ext", "TEXT"),
            ("file_size", "INTEGER"),
            ("file_mtime", "REAL"),
            ("cover_sha1", "TEXT"),
            # Provenance : « library » (trouvé sur le disque), « history »
            # (connu du seul historique, fichier absent), « both ».
            ("source", "TEXT"),
            ("in_history", "INTEGER"),
            ("history_pos", "INTEGER"),
            ("history_cover_sha1", "TEXT"),
        ],
        doc="Un titre par fichier audio de la bibliothèque : tags, "
            "caractéristiques d'encodage et renvoi vers sa jaquette. Les "
            "entrées de l'historique de téléchargement dont le fichier a "
            "disparu du disque y figurent aussi (source = « history ») : "
            "l'historique reste consultable.",
    ),
    TableSpec(
        "quality",
        pk="path",
        columns=[
            ("path", "TEXT"),
            ("stamp", "TEXT"),
            ("measures", "TEXT"),
            ("defects", "TEXT"),
        ],
        doc="Mesures de l'écran « Qualité », reprises du cache local. "
            "« stamp » est l'empreinte date+taille du fichier mesuré : elle "
            "invalide la mesure dès que le fichier change. « measures » et "
            "« defects » sont des documents JSON.",
    ),
    TableSpec(
        "opus_mirror",
        pk="path",
        columns=[
            ("path", "TEXT"),
            ("sha1", "TEXT"),
            ("stamp", "TEXT"),
            ("key", "TEXT"),
            ("bytes", "INTEGER"),
            ("bitrate", "INTEGER"),
            ("source_bytes", "INTEGER"),
            ("uploaded_at", "REAL"),
        ],
        doc="Miroir Opus : pour chaque fichier de la bibliothèque envoyé au "
            "seau de stockage objet, la clé sous laquelle il y est rangé et "
            "le poids qu'il y occupe. Le flux audio, lui, n'est **pas** "
            "stocké ici — 14 Go de musique ne tiennent pas dans les 5 Go de "
            "l'offre Turso, et une base SQLite ne sait pas servir un "
            "morceau par tranches (voir ``blob_store`` et ``opus_mirror``). "
            "« stamp » est l'empreinte date+taille du fichier source : elle "
            "signale un titre remplacé sur le disque, dont la copie en "
            "ligne est devenue périmée. « sha1 » est celle de son contenu, "
            "et donne la clé de l'objet — deux copies du même fichier "
            "n'occupent qu'une place.",
    ),
    TableSpec(
        "reference_durations",
        pk="key",
        columns=[
            ("key", "TEXT"),
            ("durations", "TEXT"),
            ("fetched_at", "REAL"),
        ],
        doc="Durées de référence relevées chez Deezer et MusicBrainz, "
            "indexées par « artiste|titre » normalisé.",
    ),
]

TABLES_BY_NAME = {spec.name: spec for spec in TABLES}

# Ordre d'écriture : les jaquettes avant les titres, qui les référencent ;
# le miroir Opus après eux, chacune de ses lignes désignant un titre.
TABLE_ORDER = ["app_settings", "searches", "covers", "tracks", "quality",
               "opus_mirror", "reference_durations"]

# Table de service, présente des deux côtés mais jamais synchronisée :
# elle porte la version du schéma et l'identifiant de l'installation.
SCHEMA_META_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_meta ("
    "    key TEXT PRIMARY KEY,"
    "    value TEXT"
    ")"
)

# Bookkeeping de la synchronisation. Purement local : il décrit l'état du
# dernier échange réussi et n'a pas de sens sur la base distante.
SYNC_STATE_SQL = (
    "CREATE TABLE IF NOT EXISTS sync_state ("
    "    table_name TEXT PRIMARY KEY,"
    # Empreinte commune constatée lors du dernier échange réussi.
    "    synced_count INTEGER NOT NULL DEFAULT -1,"
    "    synced_sum INTEGER NOT NULL DEFAULT -1,"
    "    synced_at REAL NOT NULL DEFAULT 0"
    ")"
)

# File d'attente des écritures locales à pousser. Elle n'est pas
# indispensable au rapprochement — l'empreinte suffit à savoir qu'il y a
# lieu d'échanger — mais elle évite de reparcourir toute une table pour
# retrouver la poignée de lignes qui a bougé.
OUTBOX_SQL = (
    "CREATE TABLE IF NOT EXISTS sync_outbox ("
    "    table_name TEXT NOT NULL,"
    "    pk TEXT NOT NULL,"
    "    queued_at REAL NOT NULL,"
    "    PRIMARY KEY (table_name, pk)"
    ")"
)

INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks (artist)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks (album)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_cover ON tracks (cover_sha1)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_history ON tracks (in_history)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_updated ON tracks (updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_quality_updated ON quality (updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_mirror_sha1 ON opus_mirror (sha1)",
    "CREATE INDEX IF NOT EXISTS idx_mirror_updated ON opus_mirror (updated_at)",
]


def shared_ddl():
    """Instructions de création communes aux deux bases, dans l'ordre."""
    statements = [SCHEMA_META_SQL]
    statements += [TABLES_BY_NAME[name].create_sql() for name in TABLE_ORDER]
    statements += INDEX_SQL
    return statements


def local_only_ddl():
    """Instructions propres au cache local (état de synchronisation)."""
    return [SYNC_STATE_SQL, OUTBOX_SQL]


# ----------------------------------------------------------------------
# Empreintes
# ----------------------------------------------------------------------

def _canonical(value):
    """
    Représentation stable d'une valeur pour le calcul d'empreinte.

    Les deux côtés doivent aboutir au même octet-à-octet : un flottant de
    valeur ronde est ramené à l'entier (SQLite rend parfois 1 là où l'on a
    écrit 1.0), les images sont hachées à part plutôt que recopiées, et
    None reçoit une marque propre pour ne pas se confondre avec la chaîne
    vide.
    """
    if value is None:
        return b"\x00"
    if isinstance(value, bool):
        return b"b" + (b"1" if value else b"0")
    if isinstance(value, float):
        if value == int(value):
            return b"i" + str(int(value)).encode()
        return b"f" + repr(round(value, 6)).encode()
    if isinstance(value, int):
        return b"i" + str(value).encode()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b"x" + hashlib.sha1(bytes(value)).digest()
    return b"s" + str(value).encode("utf-8", "surrogatepass")


def row_hash(spec, row):
    """
    Empreinte du contenu d'une ligne, sur 30 bits.

    ``row`` est un dictionnaire ; seules les colonnes de contenu et le
    drapeau ``deleted`` sont pris en compte. Les horodatages en sont
    exclus à dessein : deux bases au même contenu doivent afficher la
    même empreinte même si l'une a été écrite une seconde après l'autre.
    """
    digest = hashlib.blake2b(digest_size=8)
    digest.update(spec.name.encode())
    for column in spec.content_columns:
        digest.update(b"\x1f")
        digest.update(_canonical(row.get(column)))
    digest.update(b"\x1e")
    digest.update(b"1" if row.get("deleted") else b"0")
    return int.from_bytes(digest.digest(), "big") & HASH_MASK


def fingerprint_sql(table_name):
    """Requête d'empreinte d'une table, identique des deux côtés."""
    return f"SELECT COUNT(*), COALESCE(SUM(h), 0) FROM {table_name}"


def json_dump(value):
    """JSON compact et déterministe (clés triées) pour les colonnes texte."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def json_load(text, default=None):
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
