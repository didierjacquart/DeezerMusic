"""
Index du dossier de dépôt : « ce titre est-il déjà sur le disque ? »

Rien n'empêchait jusqu'ici de retélécharger un titre déjà rangé : la
recherche affiche ce que Deezer répond, sans regarder la bibliothèque. Ce
module relève les fichiers du dossier de dépôt et répond, pour un titre
candidat, l'un de trois états :

===========  ========  ==================================================
``PRESENT``  vert      le titre est déjà là : même titre, même artiste
``SIMILAR``  orange    un fichier lui ressemble assez pour qu'il faille
                       regarder — artiste principal identique mais invité
                       différent, autre version (remix, live, edit), même
                       titre dans le même album, ou titre approchant
``ABSENT``   gris      rien de tel sur le disque
===========  ========  ==================================================

Un titre **présent** voit sa case de sélection désactivée : la recherche ne
propose pas d'extraire ce que la bibliothèque contient déjà. Un titre
**orange** reste sélectionnable, mais décoché — la ressemblance n'est pas une
preuve, et seul l'utilisateur sait si « Titanium (Radio Edit) » remplace le
fichier qu'il a déjà.

D'où viennent les métadonnées
-----------------------------
Trois sources, aucune ne lisant les tags des fichiers : relire les tags de
plusieurs milliers de fichiers demande des dizaines de secondes, ce qui est le
travail de l'écran « Organiser », pas d'une recherche.

* l'**historique de téléchargement** donne le titre, l'artiste et l'album
  exacts — tels que Deezer les a fournis — des fichiers extraits par DeeKeep ;
* le **cache local** (table ``tracks``) donne les tags des fichiers relevés
  par la collecte de la base de données, s'il en a déjà été fait une ;
* le **chemin du fichier** renseigne tous les autres. DeeKeep nomme lui-même
  ce qu'il extrait (« Titre - Artiste ») et le range par artiste puis album :
  le chemin porte donc l'essentiel. Quand le nom du dossier ne permet pas de
  trancher quel côté du tiret est l'artiste, les deux lectures sont indexées —
  mieux vaut un doublon signalé en orange qu'un titre extrait deux fois.

Le parcours de l'arborescence ne coûte que quelques dizaines de millisecondes
mais il se fait tout de même en tâche de fond (``LibraryIndexThread``) : ni
l'ouverture de la fenêtre ni celle de la recherche ne l'attendent. Tant que
l'index n'est pas prêt, les pastilles affichent « ⏳ » et rien n'est décoché.
"""

import os
import re
import unicodedata
from collections import namedtuple
from difflib import SequenceMatcher

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import QLabel

from deekeep.audio import reencode_scan


# ----------------------------------------------------------------------
# États et apparence de la pastille
# ----------------------------------------------------------------------
STATE_PRESENT = "present"
STATE_SIMILAR = "similar"
STATE_ABSENT = "absent"
STATE_UNKNOWN = "unknown"

# Palette « Neon Nocturne » (cf. styles.py) : vert des succès, orange des
# avertissements, gris des informations discrètes.
STATE_COLORS = {
    STATE_PRESENT: "#5eeb7f",
    STATE_SIMILAR: "#ffcf7a",
    STATE_ABSENT: "#3b494c",
    STATE_UNKNOWN: "#5f7d84",
}

STATE_GLYPHS = {
    STATE_PRESENT: "✓",
    STATE_SIMILAR: "≈",
    STATE_ABSENT: "○",
    STATE_UNKNOWN: "⏳",
}

# Nombre de fichiers cités dans l'infobulle d'une pastille.
TOOLTIP_PATHS = 5


Match = namedtuple("Match", "state reason paths")

ABSENT = Match(STATE_ABSENT, "Absent du dossier de dépôt", ())
UNKNOWN = Match(STATE_UNKNOWN, "Lecture du dossier de dépôt en cours…", ())


# ----------------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------------
# Marqueurs explicites d'invité : ce qui suit n'appartient ni au titre ni à
# l'artiste principal. Même liste que l'écran « Organiser ».
_FEAT_RE = re.compile(
    r"(?i)\s*\b(?:feat\.?|featuring|ft\.?|avec|with|vs\.?|versus)\s+"
)

_BRACKETS_RE = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")

# Mots qui ne décrivent qu'une variante d'un même morceau : le groupe entre
# parenthèses (ou le suffixe après un tiret) qui les contient ne distingue pas
# deux titres, il distingue deux versions du même titre.
_VARIANT_RE = re.compile(
    r"(?i)\b(?:remix(?:es|ed)?|remaster(?:ed|isation|ise|isé|isée)?|live"
    r"|version|edit|mix|instrumental|acoustic|acoustique|unplugged|extended"
    r"|radio|single|album|original|explicit|clean|cover|karaoke|karaoké|demo"
    r"|mono|stereo|stéréo|bonus|deluxe|edition|édition|maxi|club|dub"
    r"|bootleg|reprise|inédit|inedit|remake|rework|slowed)\b"
)

# Mots vides d'un nom d'artiste : ils joignent ou décorent, ils ne
# distinguent pas. « Flo Rida & Ke$ha » et « Ke$ha, Flo Rida » doivent donner
# le même ensemble de mots.
_ARTIST_NOISE = frozenset({
    "and", "et", "the", "les", "los", "feat", "featuring", "ft", "with",
    "avec", "vs", "versus", "presents", "present", "presente", "dj",
})


def canonical(text):
    """Forme comparable d'un libellé : sans accents, sans ponctuation.

    Les accents, la casse et la ponctuation ne distinguent pas deux titres
    (« Ain't Nobody » et « Ain’t Nobody » sont le même) : tout est ramené à
    des mots en minuscules séparés par une espace. Deux substitutions
    précèdent le nettoyage, faute de quoi elles seraient perdues : « & »
    devient un mot (« and », mot vide côté artiste) et « $ » redevient un
    « s » (« Ke$ha » et « Kesha » sont le même artiste).
    """
    text = unicodedata.normalize("NFKD", str(text or "")).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("&", " and ").replace("$", "s")
    # Marqueurs de copie et numéro de piste en tête (« 03 - », « (1) »)
    text = re.sub(r"[\(\[]\s*(?:copy|copie|\d+)\s*[\)\]]", " ", text)
    text = re.sub(r"^\s*\d{1,3}\s*[-\._]\s*", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_key(title):
    """Clé d'un titre : les mentions d'invité écartées.

    « Titanium (feat. Sia) » et « Titanium » sont le même morceau ; Deezer
    signe le premier, le fichier extrait porte souvent le second.
    """
    cleaned = _BRACKETS_RE.sub(
        lambda m: " " if _FEAT_RE.search(m.group(0)) else m.group(0),
        str(title or ""),
    )
    return canonical(_FEAT_RE.split(cleaned, 1)[0] or cleaned)


def base_key(title):
    """Clé d'un titre débarrassé de sa version (remix, live, edit…).

    C'est elle qui rapproche « Titanium » de « Titanium (Radio Edit) » : deux
    fichiers différents, mais un seul morceau — d'où l'orange plutôt que le
    vert.
    """
    text = _BRACKETS_RE.sub(
        lambda m: " " if (_VARIANT_RE.search(m.group(0))
                          or _FEAT_RE.search(m.group(0))) else m.group(0),
        str(title or ""),
    )
    parts = re.split(r"\s+-\s+", text)
    while len(parts) > 1 and _VARIANT_RE.search(parts[-1]):
        parts.pop()
    return canonical(" - ".join(parts))


def artist_tokens(artist):
    """Mots significatifs d'un nom d'artiste (ordre et invités indifférents)."""
    return frozenset(
        word for word in canonical(artist).split() if word not in _ARTIST_NOISE
    )


def artist_key(artist):
    """Clé d'un artiste : ses mots significatifs, triés."""
    return " ".join(sorted(artist_tokens(artist)))


def main_artist_key(artist):
    """Clé de l'artiste *principal* : ce qui précède le premier « feat. ».

    « Flo Rida Feat. Ke$ha » et « Flo Rida » ont la même : c'est ce qui
    rapproche deux signatures d'un même morceau, que le couple titre/artiste
    complet ne réunit pas.
    """
    return artist_key(_FEAT_RE.split(str(artist or ""), 1)[0])


def _overlap(left, right):
    """Part des mots communs à deux ensembles (0 si l'un est vide)."""
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _ratio(left, right):
    """Ressemblance de deux chaînes, de 0 à 1."""
    return SequenceMatcher(None, left, right).ratio()


# Seuils du rapprochement approchant. Ils ne servent qu'à l'orange : une
# ressemblance n'est jamais tenue pour une présence.
TITLE_OVERLAP_MIN = 0.5     # part de mots communs ouvrant la comparaison fine
TITLE_RATIO_MIN = 0.85      # ressemblance des titres retenue comme proche
ARTIST_OVERLAP_MIN = 0.5    # part de mots communs tenant deux artistes proches


# ----------------------------------------------------------------------
# Lecture du dossier de dépôt
# ----------------------------------------------------------------------
def _match_key(path):
    """Clé de rapprochement d'un chemin (casse ignorée, comme ``db_init``)."""
    return os.path.normcase(os.path.normpath(str(path or "")))


def _split_stem(stem, folder_artist):
    """Titre et artiste lus dans un nom de fichier « Titre - Artiste ».

    Le nom du dossier tranche quand il peut : un fichier rangé sous
    « Coldplay » et nommé « Coldplay - Yellow » a bien « Yellow » pour titre,
    malgré la convention inverse de DeeKeep. Sans dossier d'artiste
    exploitable, la convention s'applique et la lecture inverse est renvoyée
    en second, pour être indexée elle aussi.
    """
    parts = re.split(r"\s+-\s+", stem)
    if len(parts) < 2:
        return (stem, folder_artist), None
    left = " - ".join(parts[:-1]).strip()
    right = parts[-1].strip()
    if folder_artist:
        folder = canonical(folder_artist)
        if canonical(right) == folder:
            return (left, right), None
        if canonical(left) == folder:
            return (right, left), None
    # Convention DeeKeep, la lecture inverse gardée en réserve.
    return (left, right), (right, left)


def _from_path(path, root):
    """Titre, artiste et album déduits du chemin d'un fichier.

    Renvoie une liste de lectures : une seule d'ordinaire, deux quand le nom
    du dossier ne dit pas de quel côté du tiret se trouve l'artiste.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    folder_artist = folder_album = ""
    try:
        parts = os.path.relpath(path, root).split(os.sep) if root else []
    except ValueError:                      # disques différents sous Windows
        parts = []
    if len(parts) >= 2:
        # « Divers » est le fourre-tout des artistes peu représentés : il ne
        # nomme personne (cf. library_rules.DIVERS_FOLDER).
        if canonical(parts[0]) != "divers":
            folder_artist = parts[0]
        if len(parts) >= 3:
            folder_album = parts[1]
    primary, alternate = _split_stem(stem, folder_artist)
    readings = [primary]
    if alternate and alternate != primary:
        readings.append(alternate)
    return [
        {"title": title, "artist": artist, "album": folder_album, "stem": stem}
        for title, artist in readings
    ]


def _tags_from_history(history):
    """Métadonnées des fichiers extraits par DeeKeep, par chemin."""
    tags = {}
    for entry in history or ():
        path = (entry or {}).get("path") or ""
        if path and (entry.get("title") or entry.get("artist")):
            tags[_match_key(path)] = {
                "title": entry.get("title") or "",
                "artist": entry.get("artist") or "",
                "album": entry.get("album") or "",
            }
    return tags


def _tags_from_cache():
    """Métadonnées relevées par la collecte de la base de données, par chemin.

    Le cache peut être absent, vide ou verrouillé : l'index se contente alors
    des autres sources, il n'a pas à faire échouer une recherche.
    """
    try:
        from deekeep.db import local_db
        rows = local_db.shared().query(
            "SELECT path, title, artist, album FROM tracks WHERE deleted = 0"
        )
    except Exception:
        return {}
    tags = {}
    for row in rows:
        path = row["path"] or ""
        if path and (row["title"] or row["artist"]):
            tags[_match_key(path)] = {
                "title": row["title"] or "",
                "artist": row["artist"] or "",
                "album": row["album"] or "",
            }
    return tags


# ----------------------------------------------------------------------
# Index
# ----------------------------------------------------------------------
class LibraryIndex:
    """Ce que le dossier de dépôt contient, prêt à être interrogé.

    Un index se construit une fois (``build``) puis se complète au fil des
    extractions (``add``) : une bibliothèque de plusieurs milliers de titres
    n'a pas à être relue parce qu'un fichier s'y est ajouté.
    """

    def __init__(self, root="", entries=()):
        self.root = root or ""
        self._entries = []          # lectures indexées (une ou deux par fichier)
        self._paths = set()         # clés des fichiers présents
        self._by_filename = {}      # nom de fichier (casse ignorée) -> chemins
        self._by_pair = {}          # artiste|titre -> lectures
        self._by_main = {}          # artiste principal|titre -> lectures
        self._by_title = {}         # titre -> lectures
        self._by_base = {}          # titre sans version -> lectures
        self._by_stem = {}          # nom de fichier -> lectures
        self._by_token = {}         # mot du titre -> lectures
        for entry in entries:
            self._ingest(entry)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def build(cls, root, history=(), use_cache=True):
        """Index du dossier ``root`` (voir l'en-tête du module)."""
        index = cls(root)
        if not root or not os.path.isdir(root):
            return index
        tags = _tags_from_cache() if use_cache else {}
        # L'historique passe après le cache : ses métadonnées viennent de
        # Deezer, celles du cache d'une relecture des tags du fichier.
        tags.update(_tags_from_history(history))
        for path in reencode_scan.iter_audio_files(root):
            index._add_file(path, tags.get(_match_key(path)))
        return index

    def _add_file(self, path, known=None):
        """Indexe un fichier, ses métadonnées connues ou déduites du chemin."""
        self._paths.add(_match_key(path))
        self._by_filename.setdefault(
            os.path.basename(path).lower(), []).append(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        if known and (known.get("title") or known.get("artist")):
            readings = [{
                "title": known.get("title") or "",
                "artist": known.get("artist") or "",
                "album": known.get("album") or "",
                "stem": stem,
            }]
        else:
            readings = _from_path(path, self.root)
        for reading in readings:
            reading["path"] = path
            self._ingest(reading)

    def add(self, path, title="", artist="", album=""):
        """Ajoute un fichier fraîchement extrait, sans relire le dossier."""
        if not path:
            return
        known = {"title": title, "artist": artist, "album": album}
        self._add_file(path, known if (title or artist) else None)

    def _ingest(self, reading):
        """Range une lecture (titre, artiste, album, fichier) dans les tables."""
        title = reading.get("title") or ""
        artist = reading.get("artist") or ""
        entry = {
            "path": reading.get("path") or "",
            "title": title,
            "artist": artist,
            "album": reading.get("album") or "",
            "title_key": title_key(title),
            "base_key": base_key(title),
            "album_key": canonical(reading.get("album")),
            "artist_tokens": artist_tokens(artist),
            "artist_key": artist_key(artist),
            "main_key": main_artist_key(artist),
            "stem_key": canonical(reading.get("stem")),
        }
        if not entry["title_key"]:
            return
        self._entries.append(entry)
        self._index(entry)

    def _index(self, entry):
        """Range une lecture constituée dans les tables de recherche."""
        self._by_pair.setdefault(
            entry["artist_key"] + "|" + entry["title_key"], []).append(entry)
        self._by_main.setdefault(
            entry["main_key"] + "|" + entry["title_key"], []).append(entry)
        self._by_title.setdefault(entry["title_key"], []).append(entry)
        if entry["base_key"]:
            self._by_base.setdefault(entry["base_key"], []).append(entry)
        if entry["stem_key"]:
            self._by_stem.setdefault(entry["stem_key"], []).append(entry)
        for token in set(entry["title_key"].split()):
            self._by_token.setdefault(token, []).append(entry)

    def remove(self, path):
        """Oublie un fichier que l'on vient de supprimer du disque.

        Les tables de recherche sont reposées depuis les lectures restantes :
        toutes partagent les mêmes entrées, et retirer l'une d'elles de chacune
        demanderait plus de code que de tout refaire. Une déduplication porte
        sur quelques fichiers, jamais sur des milliers.

        Renvoie vrai si le fichier était bien indexé.
        """
        key = _match_key(path)
        if key not in self._paths:
            return False
        self._paths.discard(key)
        name = os.path.basename(str(path or "").replace("/", os.sep)).lower()
        kept = [p for p in self._by_filename.get(name, ())
                if _match_key(p) != key]
        if kept:
            self._by_filename[name] = kept
        else:
            self._by_filename.pop(name, None)
        self._entries = [e for e in self._entries
                         if _match_key(e["path"]) != key]
        self._by_pair = {}
        self._by_main = {}
        self._by_title = {}
        self._by_base = {}
        self._by_stem = {}
        self._by_token = {}
        for entry in self._entries:
            self._index(entry)
        return True

    # ------------------------------------------------------------------
    # Interrogation
    # ------------------------------------------------------------------
    def __len__(self):
        """Nombre de fichiers indexés."""
        return len(self._paths)

    def paths_named(self, filename):
        """Fichiers indexés portant ce nom (casse ignorée).

        Sert à retrouver un titre dont le chemin noté a vieilli : la
        bibliothèque a pu être déplacée, le nom du fichier n'a pas bougé
        (cf. ``DeeKeepApp._repair_history_paths``).
        """
        return list(self._by_filename.get(
            os.path.basename(str(filename or "").replace("/", os.sep)).lower(),
            ()))

    def has_path(self, path):
        """Vrai si ce chemin fait partie des fichiers indexés."""
        return _match_key(path) in self._paths

    def lookup(self, title, artist, album=""):
        """État d'un titre candidat vis-à-vis du disque (voir ``Match``).

        Les rapprochements sont essayés du plus sûr au plus lâche, et le
        premier qui aboutit tranche : un titre déjà présent n'a pas à être
        comparé de façon approchante.
        """
        tkey = title_key(title)
        if not tkey or not self._entries:
            return ABSENT
        akey = artist_key(artist)
        query_tokens = artist_tokens(artist)
        mkey = main_artist_key(artist)
        alkey = canonical(album)

        # 1. Même titre, même artiste : le titre est là.
        found = self._by_pair.get(akey + "|" + tkey)
        if found:
            return self._match(
                STATE_PRESENT, "Déjà présent (même titre, même artiste)", found)

        # 2. Le fichier porte exactement le nom qu'une extraction lui donnerait.
        for stem in (canonical(f"{title} - {artist}"),
                     canonical(f"{artist} - {title}")):
            found = self._by_stem.get(stem) if stem else None
            if found:
                return self._match(
                    STATE_PRESENT, "Déjà présent (fichier du même nom)", found)

        # 3. Même titre, même artiste principal : l'invité seul diffère.
        found = self._by_main.get(mkey + "|" + tkey) if mkey else None
        if found:
            return self._match(
                STATE_SIMILAR,
                "Doublon probable : même titre, même artiste principal "
                "(l'invité diffère)",
                found,
            )

        # 4. Même titre : artiste proche, ou même album.
        same_title = self._by_title.get(tkey, ())
        close = [e for e in same_title
                 if _overlap(query_tokens, e["artist_tokens"]) >= ARTIST_OVERLAP_MIN]
        if close:
            return self._match(
                STATE_SIMILAR, "Doublon possible : même titre, artiste similaire",
                close)
        if alkey:
            same_album = [e for e in same_title if e["album_key"] == alkey]
            if same_album:
                return self._match(
                    STATE_SIMILAR, "Doublon possible : même titre, même album",
                    same_album)

        # 5. Même morceau, autre version (remix, live, edit…).
        bkey = base_key(title)
        if bkey:
            versions = [
                e for e in self._by_base.get(bkey, ())
                if (mkey and e["main_key"] == mkey)
                or _overlap(query_tokens, e["artist_tokens"]) >= ARTIST_OVERLAP_MIN
            ]
            if versions:
                return self._match(
                    STATE_SIMILAR,
                    "Doublon possible : autre version du même titre", versions)

        # 6. Recherche approchante : titre voisin et artiste compatible.
        approximate = self._approximate(tkey, query_tokens, mkey)
        if approximate:
            return self._match(
                STATE_SIMILAR,
                "Doublon possible : titre proche, artiste compatible",
                approximate)

        return ABSENT

    def _approximate(self, tkey, query_tokens, mkey):
        """Fichiers dont le titre approche celui demandé, artiste compatible.

        Les candidats sont d'abord réunis par les *mots* du titre — l'index
        inversé en donne une poignée, là où comparer un à un les milliers de
        titres de la bibliothèque coûterait une seconde par recherche. La
        comparaison fine ne porte que sur eux.
        """
        words = set(tkey.split())
        seen = set()
        candidates = []
        for word in words:
            for entry in self._by_token.get(word, ()):
                marker = id(entry)
                if marker in seen:
                    continue
                seen.add(marker)
                if _overlap(words, set(entry["title_key"].split())) >= TITLE_OVERLAP_MIN:
                    candidates.append(entry)
        found = []
        for entry in candidates:
            artist_close = (
                (mkey and entry["main_key"] == mkey)
                or _overlap(query_tokens, entry["artist_tokens"]) >= ARTIST_OVERLAP_MIN
            )
            if artist_close and _ratio(tkey, entry["title_key"]) >= TITLE_RATIO_MIN:
                found.append(entry)
        return found

    @staticmethod
    def _match(state, reason, entries):
        """Résultat rendu à l'appelant : état, motif, fichiers en cause."""
        paths = []
        for entry in entries:
            if entry["path"] and entry["path"] not in paths:
                paths.append(entry["path"])
        return Match(state, reason, tuple(paths))


# ----------------------------------------------------------------------
# Construction en tâche de fond
# ----------------------------------------------------------------------
class LibraryIndexThread(QThread):
    """Construit l'index sans bloquer l'interface.

    Un échec (dossier illisible, disque absent) n'annonce pas de panne : il
    rend un index vide, et les pastilles retombent sur « absent » — l'écran
    reste utilisable, il ne prévient simplement de rien.
    """

    ready = pyqtSignal(object)

    def __init__(self, root, history=(), parent=None):
        super().__init__(parent)
        self.root = root
        # Copie : l'historique de l'appelant peut bouger pendant le parcours.
        self.history = list(history or ())

    def run(self):
        try:
            index = LibraryIndex.build(self.root, self.history)
        except Exception:
            index = LibraryIndex(self.root)
        self.ready.emit(index)


# ----------------------------------------------------------------------
# Pastille commune aux tableaux
# ----------------------------------------------------------------------
def lookup(index, title, artist, album=""):
    """État d'un titre, l'index pouvant n'être pas encore construit."""
    if index is None:
        return UNKNOWN
    return index.lookup(title, artist, album)


def make_badge():
    """Pastille d'état, à poser à côté de la case de sélection d'une ligne."""
    badge = QLabel()
    badge.setObjectName("library_badge")
    # Largeur figée : les quatre glyphes n'ont pas la même, et la case à
    # cocher voisine se décalerait d'une ligne à l'autre.
    badge.setFixedWidth(18)
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    apply_badge(badge, UNKNOWN)
    return badge


def apply_badge(badge, match):
    """Met une pastille à l'état donné (glyphe, couleur et infobulle)."""
    badge.setText(STATE_GLYPHS.get(match.state, ""))
    badge.setStyleSheet(
        f"color: {STATE_COLORS.get(match.state, '#5f7d84')}; "
        "font-size: 13px; font-weight: bold; background: transparent;"
    )
    badge.setToolTip(tooltip(match))


def tooltip(match):
    """Infobulle d'une pastille : le motif, puis les fichiers en cause."""
    lines = [match.reason]
    for path in match.paths[:TOOLTIP_PATHS]:
        lines.append("• " + path)
    extra = len(match.paths) - TOOLTIP_PATHS
    if extra > 0:
        lines.append(f"… et {extra} autre(s)")
    return "\n".join(lines)
