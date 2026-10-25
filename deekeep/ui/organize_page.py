"""
Écran « Organiser la bibliothèque ».

Range les fichiers audio déjà présents sur le disque selon la même
convention que les téléchargements : sous-dossiers « Artiste / Album »
construits à partir des tags, et nommage piloté par un motif (%titre%,
%artiste%, %piste2%…) choisi dans la liste des balises disponibles.

L'écran est une page de la fenêtre principale (comme « Réencoder ») et
non une fenêtre modale : l'analyse d'une grande bibliothèque peut durer,
l'utilisateur reste libre de naviguer pendant ce temps.

L'analyse tourne dans un thread dédié et remplit l'arborescence au fil de
l'eau (dossiers, sous-dossiers, fichiers). Rien n'est déplacé tant que
l'utilisateur n'a pas validé, et chaque ligne — ou chaque dossier — peut
être décochée.

Le rangement réunit le plus de titres possible sous un même artiste : les
écritures multiples d'un même nom (« Madonna », « Madonna Featuring Justin
Timberlake and Timbaland ») sont ramenées à l'artiste principal, et seuls
les dossiers formant un album à part entière — musique de film,
compilation multi-artistes — échappent à ce regroupement. Les artistes qui
restent sous le seuil sont rassemblés dans « Divers », dossier analysé
comme le reste de la bibliothèque : ses titres en ressortent dès que leur
artiste atteint le seuil.

Traitements activables séparément :
    - créer les sous-dossiers Artiste et/ou Album ;
    - renommer les fichiers selon un motif de nommage ;
    - regrouper les artistes peu représentés dans un dossier « Divers »
      (et en ressortir les titres dont l'artiste atteint le seuil) ;
    - ne conserver qu'un exemplaire des titres présents en plusieurs
      copies, le mieux encodé, et supprimer les autres ;
    - supprimer les dossiers devenus vides après déplacement ;
    - signaler (au lieu d'ignorer) les fichiers dont les tags manquent.

Ces règles ne valent pas que pour l'écran : ``library_rules`` les porte, et
toute extraction les suit — un titre téléchargé se pose là où l'analyse
l'aurait mis.

S'y ajoute, hors du plan, la **recherche de doublons** (``dedup_dialog``) :
un rapprochement approximatif des fichiers analysés, plus large que l'option
« Ne garder qu'un exemplaire » et qui ne supprime donc rien sans examen. Elle
vivait sur l'écran « Réencoder », où elle ne voyait que les fichiers ajoutés
en vue d'une conversion ; sa place est ici, où la bibliothèque entière est
sous les yeux.
"""

import os
import re
import shutil

from collections import Counter

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QComboBox, QSpinBox, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QAbstractItemView, QCheckBox, QMessageBox, QProgressBar, QFileDialog,
    QToolButton, QMenu, QDialog,
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QBrush

from deekeep.ui import dedup_dialog
from deekeep.library import name_pattern
from deekeep.core.utils import reveal_in_explorer
from deekeep.ui import player
from deekeep.audio import reencode_scan
from deekeep.audio import tag_lookup
from deekeep.ui import tag_lookup_dialog
from deekeep.audio.reencode_scan import LocalScanThread

# Le nom du fourre-tout, la mise au propre d'un nom de dossier et la lecture
# de l'artiste principal valent pour tout ce qui range des fichiers,
# extractions comprises : ils vivent dans ``library_rules``, que la page
# importe plutôt que d'en tenir sa copie — une extraction qui compterait ses
# artistes autrement rangerait autrement.
from deekeep.library.library_rules import (
    DIVERS_FOLDER, artist_key, main_artist, safe_component)

# Reconnaissance d'un dossier formant un album à part entière (voir
# ``collection_folders``).
COLLECTION_MIN_ARTISTS = 3       # compilation : artistes différents attendus
SOUNDTRACK_MIN_ARTISTS = 2       # musique de film : deux interprètes suffisent
COLLECTION_NAME_RATIO = 0.6      # part des titres dont l'album nomme le dossier
COLLECTION_DOMINANT_RATIO = 0.5  # au-delà, l'album est celui d'un seul artiste

# Un album dont le titre porte l'un de ces marqueurs est une musique de film.
SOUNDTRACK_RE = re.compile(
    r"(?i)(soundtrack|motion picture|original score|film score"
    r"|bande[\s-]*originale|musique (?:du|de|des|extraite))"
)

# Artistes d'album ne désignant personne (compilations).
_VARIOUS_NAMES = {
    "various", "various artists", "va", "artistes varies", "artistes variés",
    "divers", "compilation", "compilations", "multi interpretes",
    "multi interprètes", "unknown artist", "artiste inconnu",
}

# Écart de taille en deçà duquel deux exemplaires d'un titre sont tenus pour
# de qualité équivalente (la différence ne tient qu'aux tags ou à la pochette).
DUPLICATE_SIZE_RATIO = 0.97

# Au-delà de ce nombre de fichiers, la recherche des tags demande
# confirmation : elle interroge le catalogue un fichier à la fois.
TAGS_LOOKUP_WARN = 50

# Durée observée d'une recherche complète pour un fichier (trois requêtes :
# la piste, ses détails, son album — ce dernier mis en cache). Ne sert qu'à
# annoncer un ordre de grandeur.
SECONDS_PER_LOOKUP = 1.2

# Couleurs d'état de la colonne « Action » (palette Neon Nocturne).
COLOR_MOVE = QColor("#00e5ff")
COLOR_INPLACE = QColor("#5f7d84")
COLOR_WARN = QColor("#ffcf7a")
COLOR_DIR = QColor("#bac9cc")
COLOR_ALBUM = QColor("#8fb7c4")
COLOR_DUPLICATE = QColor("#ff8a80")


def same_path(a, b):
    """Vrai si deux chemins désignent le même emplacement (casse ignorée)."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def equivalent_name(current, target):
    """Vrai si deux noms de fichier ne diffèrent que par des espaces multiples.

    Retirer un caractère interdit d'un tag (« Whip / Nae Nae ») laisse deux
    espaces là où une autre écriture du même nom n'en a qu'un. Renommer pour
    cela seul produirait une ligne de plan que rien ne distingue à l'écran,
    et qui reviendrait à chaque analyse.
    """
    return re.sub(r"\s+", " ", current) == re.sub(r"\s+", " ", target)


def is_inside(path, folder):
    """Vrai si ``path`` se trouve dans ``folder`` (à n'importe quel niveau)."""
    prefix = os.path.normcase(os.path.abspath(folder)) + os.sep
    return os.path.normcase(os.path.abspath(path)).startswith(prefix)


def _is_compilation_flag(value):
    """Vrai si la valeur du tag « compilation » (TCMP) vaut oui."""
    return str(value or "").strip().lower() in ("1", "true", "yes", "oui")


def words(value):
    """Texte réduit à ses mots, minuscules, pour comparer deux noms.

    Ponctuation et écarts d'espacement disparaissent, les lettres accentuées
    sont conservées : « Angels Par Delà Les Nuages » et « Angels par delà
    les nuages » deviennent une seule et même chaîne.
    """
    return " ".join(re.findall(r"[^\W_]+", (value or "").casefold(), re.UNICODE))


def name_matches(folder, album):
    """Vrai si un nom de dossier et un titre d'album se désignent l'un l'autre.

    La comparaison porte sur des mots entiers, dans les deux sens : le
    dossier « Dirty Dancing » reconnaît l'album « More Dirty Dancing: More
    Original Music From the Hit Motion Picture “Dirty Dancing” », et le
    dossier « Anais - The Love Album (2008) » reconnaît l'album « The Love
    Album ». Un dossier d'artiste (« Madonna ») ne reconnaît pas pour autant
    l'album « Who's That Girl Soundtrack ».
    """
    left, right = words(folder), words(album)
    if not left or not right:
        return False
    return f" {left} " in f" {right} " or f" {right} " in f" {left} "


def is_soundtrack_album(album):
    """Vrai si le titre de l'album annonce une musique de film."""
    return bool(SOUNDTRACK_RE.search(album or ""))


def is_various(name):
    """Vrai si ce nom d'artiste ne désigne personne (« Artistes Variés »)."""
    return words(name) in _VARIOUS_NAMES


def entry_artist(entry):
    """Artiste porté par un fichier, avec repli sur l'artiste d'album.

    Les fichiers d'une compilation mal étiquetée n'ont parfois que l'artiste
    d'album ; encore faut-il qu'il nomme quelqu'un, ce que « Artistes
    Variés » ne fait pas.
    """
    artist = (entry.get("artist") or "").strip()
    if artist:
        return artist
    album_artist = (entry.get("albumartist") or "").strip()
    return "" if is_various(album_artist) else album_artist


class ArtistIndex:
    """Regroupe les écritures d'un même artiste sur une analyse donnée.

    Un artiste apparaît sous plusieurs formes selon les fichiers (« Inna »,
    « Inna Feat. Flo Rida »). Les compter séparément éparpille ses titres et
    en envoie une partie dans « Divers » alors que son dossier existe :
    l'index ramène toutes ces formes à une clé unique et retient, comme nom
    de dossier, l'écriture la plus fréquente.
    """

    def __init__(self, entries):
        raw = Counter()
        for entry in entries:
            name = entry_artist(entry)
            if name:
                raw[name] += 1
        # Seuls les noms figurant tels quels dans la bibliothèque autorisent
        # la coupe d'un séparateur ambigu (voir ``main_artist``).
        known = {artist_key(name) for name in raw}
        known.discard("")

        self._keys = {}
        spellings = {}
        for name, count in raw.items():
            base = main_artist(name, known)
            key = artist_key(base)
            if not key:                     # nom réduit à des caractères interdits
                base, key = name, artist_key(name)
            self._keys[name] = key
            spellings.setdefault(key, Counter())[base] += count

        # ``sorted`` avant ``max`` : à fréquence égale, le nom retenu ne
        # dépend pas de l'ordre de lecture des fichiers.
        self._folders = {
            key: safe_component(max(sorted(names), key=names.get))
            for key, names in spellings.items()
        }

    def key(self, name):
        """Clé de l'artiste ``name`` (chaîne vide si le nom est inutilisable)."""
        name = (name or "").strip()
        if not name:
            return ""
        key = self._keys.get(name)
        return key if key is not None else artist_key(name)

    def entry_key(self, entry):
        """Clé de l'artiste d'un fichier analysé."""
        return self.key(entry_artist(entry))

    def folder(self, key):
        """Nom du dossier de l'artiste désigné par ``key``."""
        return self._folders.get(key, "")


def collection_folders(entries, root, index):
    """Dossiers formant un album à part entière : à ne pas défaire.

    Une musique de film (« Footloose », « Dirty Dancing ») ou une
    compilation (« Zen 2 ») est un tout : la ranger par artiste
    l'éparpillerait en autant de dossiers d'un seul titre, et le dossier
    d'album disparaîtrait.

    Un tel dossier se reconnaît d'abord à son nom, que reprend l'album de
    ses fichiers — c'est le film ou la compilation qui le nomme. S'y ajoute,
    au choix :
        - un album annoncé comme musique de film (« Original Motion Picture
          Soundtrack ») interprété par plusieurs artistes ;
        - au moins ``COLLECTION_MIN_ARTISTS`` artistes dont aucun ne
          rassemble la moitié des titres, ou un balisage « compilation ».

    L'exigence du nom écarte les dossiers de genre (« ELECTRO ») et les
    fourre-tout (« CD1 », « Various ») remplis d'albums disparates, que
    l'utilisateur veut au contraire voir rangés par artiste. L'absence
    d'artiste dominant écarte le best-of d'un seul artiste dont quelques
    titres sont signés avec un invité (« The Very Best of Deborah Harry and
    Blondie »), qui doit lui rejoindre le dossier de son artiste.
    """
    by_dir = {}
    for entry in entries:
        by_dir.setdefault(os.path.dirname(entry.get("path", "")), []).append(entry)

    divers_root = os.path.join(root, DIVERS_FOLDER)
    protected = set()
    for dirpath, items in by_dir.items():
        # La racine analysée n'est pas un album, et « Divers » est justement
        # le fourre-tout que l'on veut continuer à trier.
        if same_path(dirpath, root) or same_path(dirpath, divers_root):
            continue
        if is_inside(dirpath, divers_root):
            continue

        folder = os.path.basename(dirpath)
        named = [item for item in items if name_matches(folder, item.get("album"))]
        if not named:
            continue

        artists = Counter(index.entry_key(item) for item in items)
        artists.pop("", None)
        # Un album cohérent : le nom du dossier n'est pas celui d'un titre
        # égaré parmi les albums d'un artiste.
        coherent = len(named) >= COLLECTION_NAME_RATIO * len(items)
        multi = len(artists) >= COLLECTION_MIN_ARTISTS

        if any(is_soundtrack_album(item.get("album")) for item in named):
            # Une musique de film n'a pas besoin de réunir trois artistes,
            # mais il en faut plus d'un : un dossier d'un seul interprète est
            # son album, pas une bande originale à préserver — le titre de
            # « Burlesque » rangé sous Christina Aguilera doit la suivre.
            if len(artists) >= SOUNDTRACK_MIN_ARTISTS and (coherent or multi):
                protected.add(os.path.normcase(os.path.abspath(dirpath)))
            continue

        if not coherent or not multi:
            continue

        total = sum(artists.values())
        dominant = artists.most_common(1)[0][1] / total
        flagged = sum(
            1 for item in items
            if _is_compilation_flag(item.get("compilation"))
            or is_various(item.get("albumartist"))
        )
        if dominant < COLLECTION_DOMINANT_RATIO or flagged > len(items) / 2:
            protected.add(os.path.normcase(os.path.abspath(dirpath)))
    return protected


def best_copy(group, divers_root):
    """Exemplaire à conserver dans un groupe de doublons.

    La qualité passe d'abord — une suppression est irréversible : meilleur
    débit, puis fichier le plus gros (à débit égal, la différence tient à la
    pochette ou aux tags, autant garder le plus complet). À qualité
    équivalente, le mieux rangé : hors « Divers », le moins enfoui, le chemin
    le plus court, enfin l'ordre alphabétique pour que le choix ne dépende
    pas de l'ordre de lecture des fichiers.
    """
    quality = {id(entry): copy_quality(entry.get("path") or "") for entry in group}
    largest = max(size for _bitrate, size in quality.values())

    def rank(entry):
        path = entry.get("path") or ""
        bitrate, size = quality[id(entry)]
        return (
            -bitrate,
            0 if size >= DUPLICATE_SIZE_RATIO * largest else 1,
            -size,
            1 if is_inside(path, divers_root) else 0,
            path.count(os.sep),
            len(path),
            path.casefold(),
        )

    return min(group, key=rank)


def copy_quality(path):
    """(débit en kbps, taille en octets) d'un fichier, (0, 0) s'il est illisible.

    Le débit n'est pas lu lors de l'analyse (inutile au rangement, et coûteux
    car il faut parcourir le flux MPEG) : il ne l'est que pour les quelques
    fichiers en double, dont il départage les exemplaires. Le cache de
    ``reencode_scan`` fait que le prix n'est payé qu'une fois.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0, 0
    try:
        bitrate = reencode_scan.read_tags(path, need_bitrate=True).get("bitrate", 0)
    except Exception:
        bitrate = 0
    return bitrate or 0, size


def default_options():
    """Options de rangement par défaut (structure attendue par ``build_plan``)."""
    return {
        "artist": True,
        "album": True,
        "rename": False,
        "pattern": name_pattern.DEFAULT_PATTERN,
        "divers": False,
        "divers_min": 3,
        "keep_albums": True,
        "dedup": True,
    }


def artist_counts(entries, index):
    """Titres par artiste : compteur indexé par clé d'artiste.

    Les écritures d'un même nom comptent ensemble (« toto » et « Toto »,
    « Inna » et « Inna Feat. Flo Rida ») : c'est ce total qui décide du
    regroupement dans « Divers ».
    """
    counts = Counter(index.entry_key(entry) for entry in entries)
    counts.pop("", None)
    return counts


def artist_folder_count(root, folder, analyzed):
    """Fichiers audio déjà rangés dans « root/Artiste », hors lot analysé.

    Les fichiers de l'analyse en cours sont exclus (ils sont déjà comptés
    par ``artist_counts``) : ne restent que ceux dont le plan n'a pas
    connaissance, par exemple quand l'analyse ne porte que sur un
    sous-dossier de la bibliothèque.
    """
    if not folder:
        return 0
    path_root = os.path.join(root, folder)
    if not os.path.isdir(path_root):
        return 0
    total = 0
    for path in reencode_scan.iter_audio_files(path_root):
        if os.path.normcase(os.path.abspath(path)) not in analyzed:
            total += 1
    return total


def effective_artist_counts(entries, root, index, threshold, analyzed=None):
    """Nombre de titres par artiste, dossier existant sur le disque compris.

    Sert à décider du regroupement dans « Divers ». La règle joue dans les
    deux sens : un artiste qui n'atteint pas le seuil y est rassemblé, mais
    un titre qui s'y trouve déjà en ressort dès que son artiste atteint le
    seuil — y compris grâce aux fichiers qu'abrite déjà son dossier.

    ``entries`` ne contient que les fichiers que le plan peut déplacer ;
    ``analyzed`` couvre l'analyse entière (albums préservés compris) pour
    qu'aucun fichier ne soit compté deux fois.
    """
    counts = artist_counts(entries, index)
    if analyzed is None:
        analyzed = {
            os.path.normcase(os.path.abspath(entry.get("path") or ""))
            for entry in entries
        }
    for key, count in list(counts.items()):
        if count < threshold:
            counts[key] = count + artist_folder_count(
                root, index.folder(key), analyzed)
    return counts


def duplicate_copies(entries, targets, org_album, divers_root):
    """Exemplaires en trop d'un même titre : {chemin normalisé -> exemplaire gardé}.

    Deux fichiers sont deux exemplaires du même titre quand le plan les
    envoie sur le même fichier de destination, c'est-à-dire quand ils
    partagent artiste principal, album (si les sous-dossiers d'album sont
    demandés) et nom de fichier visé. Le nom visé suffit sans connaître la
    destination complète : que l'artiste rejoigne son dossier ou « Divers »,
    les exemplaires d'un groupe le suivent ensemble.
    """
    groups = {}
    for entry, target in zip(entries, targets):
        # Un fichier que le plan ne peut pas ranger ne sert pas de référence :
        # on ne supprime jamais un exemplaire alors que celui qu'on garde
        # resterait, faute de tags, là où il se trouve.
        if target is None or target["blocked"]:
            continue
        key = (
            target["artist_id"],
            safe_component(target["album"]).casefold() if org_album else "",
            target["name"].casefold(),
        )
        groups.setdefault(key, []).append(entry)

    extra = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        kept = best_copy(group, divers_root)
        for entry in group:
            if entry is not kept:
                path = entry.get("path") or ""
                extra[os.path.normcase(os.path.abspath(path))] = kept.get("path") or ""
    return extra


def build_plan(entries, root, options):
    """Calcule le rangement de chaque fichier analysé.

    Retourne (results, stats) où ``results`` suit l'ordre de ``entries``.
    Chaque résultat est un dict {entry, src, status, dst, missing} avec
    ``status`` valant :
        « move »      : le fichier doit être déplacé/renommé (``dst``) ;
        « inplace »   : il est déjà exactement au bon endroit ;
        « album »     : il appartient à un album préservé, laissé intact ;
        « duplicate » : un autre exemplaire du même titre est conservé et
                        celui-ci est à supprimer (``dst`` désigne l'exemplaire
                        retenu) ;
        « untagged »  : il manque un tag indispensable (``missing``).
    """
    org_artist = options.get("artist", True)
    org_album = options.get("album", True)
    rename = options.get("rename", False)
    pattern = options.get("pattern") or name_pattern.DEFAULT_PATTERN
    use_divers = options.get("divers", False) and org_artist
    divers_min = max(1, int(options.get("divers_min", 3) or 3))
    dedup = options.get("dedup", True)

    # Un même artiste écrit de plusieurs façons doit compter — et se ranger —
    # comme un seul, sans quoi le regroupement par artiste reste incomplet.
    index = ArtistIndex(entries)

    # Les albums à part entière (musiques de film, compilations) sont laissés
    # intacts : ils ne comptent donc ni pour le rangement, ni dans le
    # décompte des artistes. Leurs titres ne sont pas non plus candidats à la
    # déduplication : le même titre peut légitimement figurer dans une bande
    # originale et dans la discographie de son interprète.
    protected = (collection_folders(entries, root, index)
                 if options.get("keep_albums", True) else set())

    def in_protected_album(path):
        return os.path.normcase(os.path.abspath(os.path.dirname(path))) in protected

    divers_root = os.path.join(root, DIVERS_FOLDER)

    # Première passe : artiste et nom de fichier visés. Ils ne dépendent pas
    # du décompte des artistes, ce qui permet de reconnaître les doublons
    # avant de compter — un titre en trois exemplaires ne doit pas valoir
    # trois titres à son artiste.
    targets = []
    for entry in entries:
        src = entry.get("path", "")
        if protected and in_protected_album(src):
            targets.append(None)
            continue

        # Le dossier visé est celui de l'artiste principal, écriture retenue
        # par l'index : « Inna Feat. Flo Rida » rejoint « Inna ».
        artist_id = index.entry_key(entry)
        album = (entry.get("album") or "").strip()
        target = {
            "artist_id": artist_id,
            "artist": index.folder(artist_id),
            "album": album,
            "name": os.path.basename(src),
            "no_artist": bool(org_artist and not index.folder(artist_id)),
            "no_tags": [],
            "no_pattern": False,
        }
        if rename and not target["no_artist"]:
            # Un motif dont un tag manque et que le nom de fichier comblerait
            # rallongerait ce nom à chaque passe : le fichier est signalé,
            # jamais renommé.
            target["no_tags"] = name_pattern.missing_required_tags(pattern, entry)
            if not target["no_tags"]:
                stem = safe_component(name_pattern.format_name(pattern, entry))
                if stem:
                    target["name"] = stem + os.path.splitext(src)[1]
                else:
                    target["no_pattern"] = True
        # L'album manquant ne condamne le fichier que s'il ne part pas dans
        # « Divers », ce qu'on ne saura qu'après le décompte des artistes ;
        # pour la déduplication, le doute suffit à s'abstenir.
        target["blocked"] = bool(
            target["no_artist"] or target["no_tags"] or target["no_pattern"]
            or (org_album and not album)
        )
        targets.append(target)

    duplicates = (duplicate_copies(entries, targets, org_album, divers_root)
                  if dedup else {})

    movable = [
        entry for entry, target in zip(entries, targets)
        if target is not None
        and os.path.normcase(os.path.abspath(entry.get("path") or "")) not in duplicates
    ]
    analyzed = {
        os.path.normcase(os.path.abspath(entry.get("path") or ""))
        for entry in entries
    }
    counts = (effective_artist_counts(movable, root, index, divers_min, analyzed)
              if use_divers else {})

    results = []
    stats = {"move": 0, "inplace": 0, "untagged": 0, "duplicate": 0,
             "divers": 0, "from_divers": 0, "album": 0, "album_dirs": len(protected)}

    for entry, target in zip(entries, targets):
        src = entry.get("path", "")

        if target is None:
            stats["album"] += 1
            results.append({
                "entry": entry, "src": src, "status": "album",
                "dst": "", "missing": [],
            })
            continue

        kept = duplicates.get(os.path.normcase(os.path.abspath(src)))
        if kept is not None:
            stats["duplicate"] += 1
            results.append({
                "entry": entry, "src": src, "status": "duplicate",
                "dst": kept, "missing": [],
            })
            continue

        artist_id = target["artist_id"]
        artist = target["artist"]
        album = target["album"]
        name = target["name"]

        # Un artiste sous le seuil part dans « Divers » : ni sous-dossier
        # d'artiste, ni sous-dossier d'album, l'album n'est donc pas requis.
        to_divers = bool(
            use_divers and artist_id and counts.get(artist_id, 0) < divers_min
        )

        missing = []
        if target["no_artist"]:
            missing.append("artiste")
        missing.extend(target["no_tags"])
        if org_album and not album and not to_divers:
            missing.append("album")
        if not missing and target["no_pattern"]:
            missing.append("motif de nommage sans valeur")

        if missing:
            stats["untagged"] += 1
            results.append({
                "entry": entry, "src": src, "status": "untagged",
                "dst": "", "missing": missing,
            })
            continue

        target_dir = root
        if to_divers:
            target_dir = os.path.join(target_dir, DIVERS_FOLDER)
        else:
            if org_artist:
                target_dir = os.path.join(target_dir, artist)
            if org_album:
                target_dir = os.path.join(target_dir, safe_component(album))

        dst = os.path.join(target_dir, name)
        if (same_path(os.path.dirname(src), target_dir)
                and equivalent_name(os.path.basename(src), name)):
            stats["inplace"] += 1
            results.append({
                "entry": entry, "src": src, "status": "inplace",
                "dst": dst, "missing": [],
            })
            continue

        stats["move"] += 1
        if to_divers:
            stats["divers"] += 1
        elif is_inside(src, divers_root):
            # Le titre quitte « Divers » : son artiste a atteint le seuil.
            stats["from_divers"] += 1
        results.append({
            "entry": entry, "src": src, "status": "move",
            "dst": dst, "missing": [],
        })

    return results, stats


def prune_empty_dirs(root):
    """Supprime récursivement les dossiers vides sous ``root`` (root exclu).

    Le parcours est ascendant (``topdown=False``) afin qu'un dossier ne
    contenant que des sous-dossiers vides devienne vide à son tour et
    soit supprimé dans la même passe.
    """
    removed = []
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if same_path(dirpath, root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed.append(dirpath)
        except OSError:
            pass
    return removed


class OrganizeApplyThread(QThread):
    """Exécute les déplacements, renommages et suppressions de doublons.

    Les suppressions passent en premier : l'exemplaire conservé d'un titre
    vise parfois l'emplacement qu'occupe encore un doublon, et le
    déplacement échouerait sur un fichier de même nom déjà présent.
    """

    progress = pyqtSignal(int, str)   # nombre traité, nom du fichier
    done = pyqtSignal(list, list)     # échecs [(src, message)], dossiers supprimés

    def __init__(self, operations, root, remove_empty):
        super().__init__()
        self.operations = sorted(
            operations, key=lambda op: 0 if op["status"] == "duplicate" else 1
        )
        self.root = root
        self.remove_empty = remove_empty

    @staticmethod
    def _delete(src):
        """Supprime un exemplaire en trop. Retourne (succès, message d'erreur)."""
        try:
            os.remove(src)
            return True, ""
        except OSError as exc:
            return False, str(exc)

    @staticmethod
    def _move(src, dst):
        """Déplace un fichier. Retourne (succès, message d'erreur éventuel)."""
        if same_path(src, dst):
            # Même emplacement : il ne reste qu'un éventuel changement de
            # casse, qu'un simple renommage suffit à appliquer (les
            # systèmes Windows ne distinguent pas les deux noms).
            if os.path.abspath(src) == os.path.abspath(dst):
                return True, ""
            try:
                os.rename(src, dst)
                return True, ""
            except OSError as exc:
                return False, str(exc)

        if os.path.exists(dst):
            return False, "un fichier porte déjà ce nom à destination"
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            # shutil.move (et non os.replace) : la destination peut se
            # trouver sur un autre volume que la source.
            shutil.move(src, dst)
            return True, ""
        except OSError as exc:
            return False, str(exc)

    def run(self):
        failures = []
        for done_count, op in enumerate(self.operations, start=1):
            if op["status"] == "duplicate":
                ok, msg = self._delete(op["src"])
            else:
                ok, msg = self._move(op["src"], op["dst"])
            if not ok:
                failures.append((op["src"], msg))
            self.progress.emit(done_count, os.path.basename(op["src"]))

        removed = prune_empty_dirs(self.root) if self.remove_empty else []
        self.done.emit(failures, removed)


class OrganizePage(QWidget):
    """Analyse une bibliothèque, propose un plan de rangement, l'applique."""

    # Émis quand un rangement a remué le dossier de dépôt. La fenêtre
    # principale s'y branche pour relire le dossier : l'historique des
    # extractions note des chemins, et un fichier déplacé les périme.
    libraryChanged = pyqtSignal()

    # Colonnes de l'arborescence.
    COL_NAME, COL_ARTIST, COL_ALBUM, COL_ACTION = range(4)

    # Rôle portant le résultat de plan attaché à un item « fichier ».
    ROLE_RESULT = int(Qt.ItemDataRole.UserRole)
    # Rôle portant le chemin du fichier : l'arborescence sert aussi à
    # l'écouter, et reconstituer le chemin depuis les libellés des parents
    # serait fragile (un dossier peut porter le nom d'un séparateur).
    ROLE_PATH = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, root, app_settings=None, save_callback=None, parent=None):
        super().__init__(parent)
        self.root = root
        self._settings = app_settings if app_settings is not None else {}
        self._save_callback = save_callback

        self._scan_thread = None
        self._apply_thread = None
        self._entries = []          # fichiers lus lors de la dernière analyse
        self._results = []          # plan calculé (aligné sur _entries)
        self._plan_by_src = {}      # chemin normalisé -> résultat de plan
        self._op_items = []         # (QTreeWidgetItem, résultat) à déplacer
        self._dir_items = {}        # chemin normalisé -> item de dossier
        self._file_items = {}       # chemin normalisé -> item de fichier
        self._loading = False       # garde pendant le remplissage de l'arbre
        self._scan_total = 0
        self._updating_checks = False   # garde anti-réentrance des cases
        # Fichiers dont l'utilisateur a lui-même posé la case : la sélection
        # automatique (« Cocher ce qui est à faire ») ne les touche plus, un
        # changement d'option ne doit pas lui reprendre son choix.
        self._touched_checks = set()
        self._scanning = False
        self._applying = False

        # Recalcul différé : saisir un motif de nommage caractère par
        # caractère ne doit ni recalculer le plan ni réécrire la
        # configuration à chaque frappe.
        self._replan_timer = QTimer(self)
        self._replan_timer.setSingleShot(True)
        self._replan_timer.setInterval(350)
        self._replan_timer.timeout.connect(self._commit_options)

        self._build_ui()
        self.sync_from_settings()

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 24)
        layout.setSpacing(14)

        title = QLabel("Organiser la bibliothèque")
        title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 26px; "
            "font-weight: bold; color: #00e5ff;"
        )
        layout.addWidget(title)

        sub = QLabel(
            "Range les fichiers déjà présents selon leurs tags, comme le fait "
            "le téléchargement, en réunissant le plus de titres possible sous "
            "un même artiste. L'analyse ne modifie rien : le plan ci-dessous "
            "n'est appliqué qu'après validation, dossier par dossier ou "
            "fichier par fichier."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #849396; font-size: 12px;")
        layout.addWidget(sub)

        # --- Dossier traité ---
        dir_layout = QHBoxLayout()
        self.lbl_dir = QLabel()
        self.lbl_dir.setStyleSheet("color: #bac9cc; font-size: 12px;")
        # Les réglages sont nombreux et se règlent une fois : ils vivent dans
        # un volet que l'on déplie au besoin, pour laisser la hauteur — et la
        # largeur — de la fenêtre au plan de rangement.
        self.btn_filters = QToolButton()
        self.btn_filters.setCheckable(True)
        self.btn_filters.setToolTip("Afficher ou masquer les filtres de rangement")
        self.btn_filters.toggled.connect(self._on_filters_toggled)
        btn_dir = QPushButton("Modifier le dossier")
        btn_dir.setProperty("class", "control-btn")
        btn_dir.clicked.connect(self._choose_dir)
        dir_layout.addWidget(self.lbl_dir, 1)
        dir_layout.addWidget(self.btn_filters)
        dir_layout.addWidget(btn_dir)
        layout.addLayout(dir_layout)
        self._refresh_dir_label()

        # Rappel des filtres actifs quand le volet est replié : leur effet est
        # trop important pour être invisible.
        self.lbl_filters = QLabel()
        self.lbl_filters.setWordWrap(True)
        self.lbl_filters.setStyleSheet("color: #849396; font-size: 12px;")
        layout.addWidget(self.lbl_filters)

        # --- Volet des filtres de rangement ---
        self.filters_panel = QWidget()
        self.filters_panel.setObjectName("filtersPanel")
        # Sélecteur nommé : sans lui, le fond de la carte serait aussi
        # appliqué à chacune des cases qu'elle contient.
        self.filters_panel.setStyleSheet(
            "QWidget#filtersPanel { background-color: #1c1e26; "
            "border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 12px; }"
        )
        panel_layout = QVBoxLayout(self.filters_panel)
        panel_layout.setContentsMargins(20, 16, 20, 16)
        panel_layout.setSpacing(12)

        options_layout = QGridLayout()
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setHorizontalSpacing(24)
        options_layout.setVerticalSpacing(10)

        self.cb_artist = QCheckBox("Sous-dossier par Artiste")
        self.cb_album = QCheckBox("Sous-dossier par Album")
        self.cb_rename = QCheckBox("Renommer les fichiers")
        self.cb_empty = QCheckBox("Supprimer les dossiers vides")
        self.cb_untagged = QCheckBox("Signaler les fichiers sans tags")
        self.cb_keep_albums = QCheckBox("Préserver musiques de film et compilations")
        self.cb_keep_albums.setToolTip(
            "Un dossier dont le nom est celui de l'album de ses fichiers\n"
            "(« Footloose », « Dirty Dancing », « Zen 2 ») est laissé intact\n"
            "dès que cet album est une musique de film, ou qu'il réunit au\n"
            "moins trois artistes dont aucun ne domine : le ranger par\n"
            "artiste l'éparpillerait en autant de dossiers d'un seul titre.\n"
            "Un dossier de genre ou un fourre-tout (« CD1 », « Various »),\n"
            "dont les albums diffèrent, reste rangé par artiste — de même\n"
            "que le best-of d'un artiste unique, invités compris."
        )
        self.cb_dedup = QCheckBox("Ne garder qu'un exemplaire")
        self.cb_dedup.setToolTip(
            "Quand plusieurs copies d'un même titre visent le même\n"
            "emplacement, une seule est conservée — le meilleur débit, puis\n"
            "le fichier le plus complet — et rangée selon les règles\n"
            "habituelles (dossier de l'artiste, ou « Divers » si son artiste\n"
            "n'atteint pas le seuil). Les autres sont supprimées du disque.\n"
            "Les albums préservés ne sont pas concernés : un titre peut\n"
            "figurer à la fois dans une bande originale et chez son artiste."
        )
        # Filtre d'affichage du plan, et non de rangement : il reste hors du
        # volet, à portée de main pendant la lecture de l'arborescence.
        self.cb_hide_ok = QCheckBox("Masquer ce qui est déjà en place")

        # Trois colonnes : les libellés restent lisibles en entier, ce que la
        # ligne unique ne permettait plus.
        grid = (
            (self.cb_artist, self.cb_album, self.cb_rename),
            (self.cb_keep_albums, self.cb_dedup, self.cb_empty),
            (self.cb_untagged,),
        )
        for row, line in enumerate(grid):
            for column, cb in enumerate(line):
                cb.setStyleSheet("font-size: 13px;")
                options_layout.addWidget(cb, row, column)
        options_layout.setColumnStretch(len(grid[0]), 1)
        panel_layout.addLayout(options_layout)

        # --- Motif de nommage (visible seulement si « Renommer ») ---
        self.rename_row = QWidget()
        rename_layout = QHBoxLayout(self.rename_row)
        rename_layout.setContentsMargins(0, 0, 0, 0)
        rename_layout.setSpacing(10)

        lbl_pattern = QLabel("Modèle de nom :")
        lbl_pattern.setStyleSheet("font-size: 13px; font-weight: 600;")
        rename_layout.addWidget(lbl_pattern)

        # Éditable : les motifs courants sont proposés dans la liste, mais
        # l'utilisateur peut composer le sien librement.
        self.combo_pattern = QComboBox()
        self.combo_pattern.setEditable(True)
        self.combo_pattern.addItems(name_pattern.PRESETS)
        # Sans cette limite, le plus long motif de la liste imposerait sa
        # largeur au volet, et le volet à la fenêtre entière.
        self.combo_pattern.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo_pattern.setMinimumContentsLength(24)
        self.combo_pattern.setToolTip(
            "Motif de nommage. Les balises %…% sont remplacées par les tags\n"
            "du fichier ; une balise vide disparaît du nom.\n"
            "Les balises « _00 » forcent deux chiffres : %piste_00% donne 07."
        )
        rename_layout.addWidget(self.combo_pattern, 1)

        self.btn_tags = QToolButton()
        self.btn_tags.setText("Insérer une balise  ▾")
        self.btn_tags.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_tags.setToolTip("Choisir une balise à insérer dans le modèle")
        self.btn_tags.setMenu(self._build_tag_menu())
        rename_layout.addWidget(self.btn_tags)

        panel_layout.addWidget(self.rename_row)

        self.lbl_preview = QLabel()
        self.lbl_preview.setStyleSheet("color: #849396; font-size: 12px;")
        self.lbl_preview.setWordWrap(True)
        panel_layout.addWidget(self.lbl_preview)

        # --- Regroupement « Divers » ---
        divers_layout = QHBoxLayout()
        divers_layout.setContentsMargins(0, 0, 0, 0)
        divers_layout.setSpacing(10)
        self.cb_divers = QCheckBox(
            f"Regrouper dans « {DIVERS_FOLDER} » les artistes de moins de"
        )
        self.cb_divers.setStyleSheet("font-size: 13px;")
        self.cb_divers.setToolTip(
            f"Le décompte porte sur l'artiste principal : « Inna » et « Inna\n"
            f"Feat. Flo Rida » comptent — et se rangent — ensemble.\n"
            f"La règle joue dans les deux sens : le dossier « {DIVERS_FOLDER} »\n"
            f"est analysé comme le reste de la bibliothèque, et un titre qui\n"
            f"s'y trouve en ressort dès que son artiste atteint le seuil, en\n"
            f"comptant les titres que contient déjà son dossier."
        )
        # Un compteur plutôt qu'une liste de chiffres : la valeur porte son
        # unité (« 3 titres ») et se lit sans recomposer la phrase.
        self.spin_divers_min = QSpinBox()
        self.spin_divers_min.setRange(2, 10)
        self.spin_divers_min.setValue(3)
        self.spin_divers_min.setSuffix(" titres")
        self.spin_divers_min.setMinimumWidth(120)
        lbl_divers = QLabel("(option enregistrée dans les Paramètres)")
        lbl_divers.setStyleSheet("color: #849396; font-size: 12px;")
        divers_layout.addWidget(self.cb_divers)
        divers_layout.addWidget(self.spin_divers_min)
        divers_layout.addWidget(lbl_divers)
        divers_layout.addStretch()
        panel_layout.addLayout(divers_layout)

        # --- Retour à la sélection automatique ---
        # Une action, non un filtre : elle est pourtant à sa place ici, sous
        # les règles dont la sélection découle — et la barre d'actions était
        # déjà pleine à la largeur par défaut de la fenêtre.
        pending_layout = QHBoxLayout()
        pending_layout.setContentsMargins(0, 0, 0, 0)
        pending_layout.setSpacing(10)
        # L'état des cases à l'issue de l'analyse : c'est celui que l'écran
        # pose de lui-même, le bouton ne fait que le rendre après coup.
        self.btn_pending = QPushButton("Cocher ce qui est à faire")
        self.btn_pending.setProperty("class", "control-btn")
        self.btn_pending.setToolTip(
            "Cocher les fichiers que le plan déplace, renomme ou supprime, et\n"
            "ceux auxquels un tag manque ; décocher le reste.\n"
            "Un fichier déjà en place dont tous les tags sont là n'attend rien\n"
            "de cet écran : le laisser coché ne faisait que noyer ce qui\n"
            "réclame une décision.\n"
            "C'est la sélection posée après chaque analyse ; ce bouton y\n"
            "revient, et oublie les cases posées à la main."
        )
        self.btn_pending.setEnabled(False)
        self.btn_pending.clicked.connect(self._check_pending)
        lbl_pending = QLabel(
            "décoche les fichiers déjà en place dont les tags sont complets"
        )
        lbl_pending.setStyleSheet("color: #849396; font-size: 12px;")
        pending_layout.addWidget(self.btn_pending)
        pending_layout.addWidget(lbl_pending)
        pending_layout.addStretch()
        panel_layout.addLayout(pending_layout)

        layout.addWidget(self.filters_panel)

        # Toute option modifiant le plan le recalcule sur-le-champ à partir
        # des fichiers déjà lus (aucune relecture disque nécessaire).
        for cb in (self.cb_artist, self.cb_album, self.cb_rename,
                   self.cb_untagged, self.cb_divers, self.cb_keep_albums,
                   self.cb_dedup):
            cb.stateChanged.connect(self._on_option_changed)
        self.spin_divers_min.valueChanged.connect(self._on_option_changed)
        self.combo_pattern.currentTextChanged.connect(self._on_option_changed)
        self.cb_hide_ok.stateChanged.connect(lambda _=None: self._apply_visibility())

        # --- Barre d'actions ---
        # Même disposition sur tous les écrans (voir ``styles.py``) : la barre
        # se lit au-dessus de la liste, la manipulation de l'affichage à
        # gauche, les actions à droite — l'analyse en pilule ajourée, puis
        # l'action qui écrit sur le disque en pilule pleine.
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(12)
        self.btn_all = QPushButton("Tout cocher")
        self.btn_all.setProperty("class", "control-btn")
        self.btn_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_none = QPushButton("Tout décocher")
        self.btn_none.setProperty("class", "control-btn")
        self.btn_none.clicked.connect(lambda: self._set_all_checked(False))
        self.btn_expand = QPushButton("Tout déplier")
        self.btn_expand.setProperty("class", "control-btn")
        self.btn_expand.clicked.connect(self._toggle_expand)
        btn_layout.addWidget(self.btn_all)
        btn_layout.addWidget(self.btn_none)
        btn_layout.addWidget(self.btn_expand)
        btn_layout.addStretch()

        # Un seul bouton, un seul passage. Les tags qui empêchent de ranger
        # un fichier et ceux qui ne manquent que partout ailleurs se
        # cherchent ensemble : ``find_tags`` demande déjà, fichier par
        # fichier, ce qui lui manque, et le catalogue ne rend les seconds
        # qu'une fois la piste identifiée — la même identification sert donc
        # aux deux. Les séparer imposait deux recherches, deux revues et deux
        # analyses pour compléter un fichier non rangeable, qui est presque
        # toujours aussi dépourvu de genre et d'année.
        self.btn_tags_lookup = QPushButton("Compléter les tags")
        self.btn_tags_lookup.setProperty("class", "control-btn")
        self.btn_tags_lookup.setToolTip(
            "Rechercher en ligne les tags manquants des fichiers cochés, et\n"
            "proposer de les écrire : le titre, l'artiste et l'album sans\n"
            "lesquels un fichier ne peut pas être rangé, comme l'artiste de\n"
            "l'album, le genre, l'année et le numéro de piste, qui\n"
            "n'empêchent rien mais dont l'absence se voit partout ailleurs —\n"
            "un motif de nommage qui réclame l'année ne rend rien, et la\n"
            "base reste muette.\n"
            "Décochez un dossier pour l'écarter : la recherche ne porte que\n"
            "sur ce qui est coché.\n"
            "Une requête par fichier concerné : comptez plusieurs minutes sur\n"
            "une grande bibliothèque. « Arrêter la recherche » l'interrompt à\n"
            "tout moment sans rien perdre de ce qui a été trouvé. Rien n'est\n"
            "écrit sans validation, et jamais par-dessus un tag existant."
        )
        self.btn_tags_lookup.setEnabled(False)
        self.btn_tags_lookup.clicked.connect(lambda: self._lookup_tags())
        btn_layout.addWidget(self.btn_tags_lookup)

        # « Ne garder qu'un exemplaire » ne supprime que les copies dont le
        # plan est sûr : celles qui visent exactement le même fichier. Cette
        # recherche-ci est approximative — elle rapproche « Ain't Nobody » et
        # « Ain’t Nobody (Loves Me Better) - Felix Jaehn Feat. Jasmine
        # Thompson » — et ne supprime donc rien sans examen : elle ouvre un
        # écran où chaque groupe se décide à la main.
        self.btn_dupes = QPushButton("Rechercher les doublons")
        self.btn_dupes.setProperty("class", "control-btn")
        self.btn_dupes.setToolTip(
            "Rapprocher les fichiers qui semblent être le même titre (noms de\n"
            "fichiers ou couples titre/artiste proches), puis choisir ceux à\n"
            "supprimer, groupe par groupe.\n"
            "Plus large que l'option « Ne garder qu'un exemplaire », qui ne\n"
            "vise que les copies dont le rangement est identique — d'où\n"
            "l'examen manuel avant toute suppression."
        )
        self.btn_dupes.setEnabled(False)
        self.btn_dupes.clicked.connect(self._find_duplicates)
        btn_layout.addWidget(self.btn_dupes)

        self.btn_scan = QPushButton("Analyser")
        self.btn_scan.setProperty("class", "accent-btn")
        self.btn_scan.setToolTip(
            "Lire les tags du dossier et construire le plan de rangement\n"
            "(aucun fichier n'est déplacé)"
        )
        self.btn_scan.clicked.connect(self._start_scan)
        self.btn_apply = QPushButton("Organiser")
        self.btn_apply.setProperty("class", "primary-btn")
        self.btn_apply.setToolTip(
            "Appliquer le plan : déplacer et renommer les éléments cochés"
        )
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._confirm_apply)
        btn_layout.addWidget(self.btn_scan)
        btn_layout.addWidget(self.btn_apply)
        layout.addLayout(btn_layout)

        # Le bilan et le filtre d'affichage se lisent ensemble, juste au-dessus
        # du plan : celui-ci ne touche pas au rangement, il n'a donc pas sa
        # place parmi les filtres du volet.
        status_layout = QHBoxLayout()
        status_layout.setContentsMargins(0, 0, 0, 0)
        self.lbl_status = QLabel(
            "Lancez l'analyse pour construire le plan de rangement."
        )
        self.lbl_status.setStyleSheet("color: #849396; font-size: 12px;")
        self.cb_hide_ok.setStyleSheet("font-size: 13px;")
        status_layout.addWidget(self.lbl_status, 1)
        status_layout.addWidget(self.cb_hide_ok)
        layout.addLayout(status_layout)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # --- Arborescence ---
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(
            ["DOSSIER / FICHIER", "ARTISTE", "ALBUM", "ACTION"]
        )
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Hauteur de ligne uniforme : indispensable à la fluidité du défilement
        # sur une bibliothèque de plusieurs milliers de fichiers.
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setAnimated(False)
        header = self.tree.header()
        header.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_ARTIST, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self.COL_ALBUM, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self.COL_ACTION, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(self.COL_ARTIST, 170)
        self.tree.setColumnWidth(self.COL_ALBUM, 170)
        self.tree.itemChanged.connect(self._on_item_changed)
        # Écoute : l'arborescence n'a pas de colonne dédiée (la place va au
        # plan de rangement), le lecteur s'ouvre donc au double-clic ou par
        # le menu contextuel.
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_menu)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.tree, 1)

    def _build_tag_menu(self):
        """Menu hiérarchique des balises disponibles (catégorie → balise)."""
        menu = QMenu(self)
        for category, items in name_pattern.TAG_CATEGORIES:
            sub = menu.addMenu(category)
            for label, token in items:
                action = sub.addAction(f"{label}\t{token}")
                action.setToolTip(token)
                action.triggered.connect(
                    lambda _checked=False, tok=token: self._insert_token(tok)
                )
        return menu

    # ------------------------------------------------------------------
    # Volet des filtres
    # ------------------------------------------------------------------
    def _on_filters_toggled(self, opened):
        """Déplie ou replie le volet, et enregistre l'état choisi."""
        self._refresh_filters_panel()
        if not self._loading:
            self._store_settings()

    def _refresh_filters_panel(self):
        """Applique l'état du volet : visibilité, libellé du bouton, résumé.

        Le résumé ne s'affiche que volet replié : déplié, les cases parlent
        d'elles-mêmes et répéter la liste ferait doublon.
        """
        opened = self.btn_filters.isChecked()
        self.btn_filters.setText("Filtres  ▴" if opened else "Filtres  ▾")
        self.filters_panel.setVisible(opened)
        self.lbl_filters.setVisible(not opened)
        if not opened:
            self.lbl_filters.setText(f"Filtres : {self._filters_summary()}")

    def _filters_summary(self):
        """Énumération des filtres actifs, pour le volet replié."""
        parts = []
        if self.cb_artist.isChecked():
            parts.append("dossier par artiste")
        if self.cb_album.isChecked():
            parts.append("dossier par album")
        if self.cb_rename.isChecked():
            parts.append(f"renommage « {self.combo_pattern.currentText()} »")
        if self.cb_divers.isChecked() and self.cb_artist.isChecked():
            parts.append(
                f"« {DIVERS_FOLDER} » sous {self.spin_divers_min.value()} titres"
            )
        if self.cb_keep_albums.isChecked():
            parts.append("musiques de film et compilations préservées")
        if self.cb_dedup.isChecked():
            parts.append("un seul exemplaire des doublons")
        if self.cb_empty.isChecked():
            parts.append("dossiers vides supprimés")
        if self.cb_untagged.isChecked():
            parts.append("fichiers sans tags signalés")
        return " · ".join(parts) if parts else "aucun"

    def _insert_token(self, token):
        """Insère une balise à la position du curseur dans le champ de motif."""
        line = self.combo_pattern.lineEdit()
        if line is None:
            return
        line.insert(token)
        line.setFocus()

    # ------------------------------------------------------------------
    # Paramètres
    # ------------------------------------------------------------------
    def sync_from_settings(self):
        """Recharge les options depuis la configuration de l'application.

        Appelée à chaque affichage de la page : les cases « Artiste » /
        « Album » / « Divers » des Paramètres restent la référence.
        """
        self._loading = True
        try:
            self.cb_artist.setChecked(self._settings.get("org_artist", True))
            self.cb_album.setChecked(self._settings.get("org_album", True))
            self.cb_rename.setChecked(self._settings.get("org_rename", False))
            self.cb_empty.setChecked(self._settings.get("org_remove_empty", True))
            self.cb_untagged.setChecked(self._settings.get("org_report_untagged", True))
            self.cb_keep_albums.setChecked(self._settings.get("org_keep_albums", True))
            self.cb_dedup.setChecked(self._settings.get("org_dedup", True))
            self.cb_divers.setChecked(self._settings.get("org_divers", False))

            pattern = self._settings.get("org_pattern") or name_pattern.DEFAULT_PATTERN
            self.combo_pattern.setCurrentText(name_pattern.modernize(pattern))

            self.spin_divers_min.setValue(
                int(self._settings.get("org_divers_min", 3) or 3))
            self.btn_filters.setChecked(
                bool(self._settings.get("org_filters_open", False)))
        finally:
            self._loading = False
        self._refresh_option_widgets()

    def _store_settings(self):
        """Écrit les options dans la configuration (sauvegarde différée)."""
        self._settings.update({
            "org_artist": self.cb_artist.isChecked(),
            "org_album": self.cb_album.isChecked(),
            "org_rename": self.cb_rename.isChecked(),
            "org_pattern": self.combo_pattern.currentText(),
            "org_remove_empty": self.cb_empty.isChecked(),
            "org_report_untagged": self.cb_untagged.isChecked(),
            "org_keep_albums": self.cb_keep_albums.isChecked(),
            "org_dedup": self.cb_dedup.isChecked(),
            "org_divers": self.cb_divers.isChecked(),
            "org_divers_min": self.spin_divers_min.value(),
            "org_filters_open": self.btn_filters.isChecked(),
        })
        if self._save_callback is not None:
            self._save_callback()

    def current_options(self):
        """Options de rangement telles qu'affichées à l'écran."""
        return {
            "artist": self.cb_artist.isChecked(),
            "album": self.cb_album.isChecked(),
            "rename": self.cb_rename.isChecked(),
            "pattern": self.combo_pattern.currentText(),
            "divers": self.cb_divers.isChecked(),
            "divers_min": self.spin_divers_min.value(),
            "keep_albums": self.cb_keep_albums.isChecked(),
            "dedup": self.cb_dedup.isChecked(),
        }

    def _refresh_option_widgets(self):
        """Grise ce qui n'a pas de sens selon les options cochées."""
        # Le modèle de nom reste visible même quand le renommage est
        # désactivé : le masquer le rendait introuvable, et l'utilisateur ne
        # pouvait pas voir ce que la case « Renommer » allait appliquer.
        renaming = self.cb_rename.isChecked()
        self.rename_row.setEnabled(renaming)
        self.lbl_preview.setEnabled(renaming)
        # « Divers » remplace le dossier d'artiste : sans sous-dossier par
        # artiste, l'option n'a rien à regrouper.
        self.cb_divers.setEnabled(self.cb_artist.isChecked())
        self.spin_divers_min.setEnabled(
            self.cb_artist.isChecked() and self.cb_divers.isChecked()
        )
        self._refresh_filters_panel()
        self._refresh_preview()

    def _refresh_preview(self):
        """Aperçu du motif appliqué au premier fichier analysé."""
        self.lbl_preview.setStyleSheet("color: #849396; font-size: 12px;")
        if not self.cb_rename.isChecked():
            self.lbl_preview.setText(
                "Renommage désactivé : les fichiers gardent leur nom actuel. "
                "Cochez « Renommer les fichiers » pour appliquer ce modèle."
            )
            return

        pattern = self.combo_pattern.currentText()
        unknown = name_pattern.unknown_tokens(pattern)
        if unknown:
            self.lbl_preview.setText(
                "Balise inconnue : " + ", ".join(unknown)
                + " — elle sera recopiée telle quelle."
            )
            self.lbl_preview.setStyleSheet("color: #ffcf7a; font-size: 12px;")
            return

        self.lbl_preview.setStyleSheet("color: #849396; font-size: 12px;")
        if not self._entries:
            self.lbl_preview.setText(
                "Aperçu disponible après l'analyse. Exemple de balises : "
                "%titre%, %artiste%, %album%, %piste_00%, %annee%."
            )
            return

        entry = self._entries[0]
        ext = os.path.splitext(entry.get("path", ""))[1]
        current = os.path.basename(entry.get("path", ""))
        absent = name_pattern.missing_required_tags(pattern, entry)
        if absent:
            # Sans le tag, le motif reprendrait le nom actuel et l'allongerait
            # à chaque rangement : le fichier est signalé, pas renommé.
            self.lbl_preview.setText(
                f"Aperçu : {current}  →  inchangé "
                f"({', '.join(absent)} absent des tags : fichier signalé)"
            )
            return
        name = safe_component(name_pattern.format_name(pattern, entry))
        self.lbl_preview.setText(
            f"Aperçu : {current}  →  "
            f"{name + ext if name else '(nom vide : fichier signalé)'}"
        )

    def _on_option_changed(self, *_args):
        """Une option a changé : l'aperçu suit tout de suite, le plan après."""
        if self._loading:
            return
        self._refresh_option_widgets()
        self._replan_timer.start()

    def _commit_options(self):
        """Enregistre les options et recalcule le plan (appel différé)."""
        self._store_settings()
        self._rebuild_plan()

    # ------------------------------------------------------------------
    # Dossier analysé
    # ------------------------------------------------------------------
    def _refresh_dir_label(self):
        self.lbl_dir.setText(f"Dossier analysé : {self.root}")
        self.lbl_dir.setToolTip(self.root)

    def set_root(self, root):
        """Change le dossier ciblé (suit le dossier d'export par défaut)."""
        if same_path(root, self.root):
            return
        self.root = root
        self._refresh_dir_label()
        self._clear_plan("Dossier modifié : relancez l'analyse.")

    def _choose_dir(self):
        if self._busy():
            return
        chosen = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier à organiser", self.root
        )
        if chosen:
            self.set_root(chosen)

    # ------------------------------------------------------------------
    # Analyse
    # ------------------------------------------------------------------
    def _busy(self):
        """Vrai si une analyse ou un rangement est en cours.

        Des drapeaux plutôt que ``isRunning()`` : un QThread reste « en
        cours » quelques instants après avoir émis son dernier signal, et
        le traitement de ce signal doit justement pouvoir recalculer le plan.
        """
        return self._scanning or self._applying

    def _start_scan(self):
        if self._busy():
            return
        if not os.path.isdir(self.root):
            QMessageBox.warning(
                self, "Dossier introuvable",
                f"Le dossier suivant n'existe pas :\n{self.root}"
            )
            return

        # Un recalcul de plan encore en attente porterait sur l'analyse
        # précédente, que l'on s'apprête à jeter.
        self._replan_timer.stop()
        self._clear_plan("Analyse en cours…")
        self._scanning = True
        self.btn_scan.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)   # indéterminé tant que le total est inconnu

        # Les débits ne servent pas au rangement : les ignorer évite de
        # parcourir le flux MPEG de chaque fichier (analyse ~2× plus rapide).
        self._scan_thread = LocalScanThread([self.root], need_bitrate=False)
        self._scan_thread.total.connect(self._on_scan_total)
        self._scan_thread.progress.connect(self._on_scan_progress)
        self._scan_thread.batch.connect(self._on_scan_batch)
        self._scan_thread.scanned.connect(self._on_scanned)
        self._scan_thread.start()

    def _on_scan_total(self, total):
        self._scan_total = total
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(0)
        self.lbl_status.setText(f"Analyse en cours… {total} fichier(s) à lire")

    def _on_scan_progress(self, count):
        self.progress.setValue(count)
        self.lbl_status.setText(
            f"Analyse en cours… {count} / {getattr(self, '_scan_total', count)} fichiers lus"
        )

    def _on_scan_batch(self, entries):
        """Ajoute un lot de fichiers à l'arborescence pendant l'analyse."""
        self._entries.extend(entries)
        # Aucune case n'est encore posée : inutile de recalculer l'état des
        # dossiers à chaque ligne ajoutée (ce serait quadratique).
        self._updating_checks = True
        self.tree.setUpdatesEnabled(False)
        try:
            for entry in entries:
                self._add_file_item(entry)
        finally:
            self.tree.setUpdatesEnabled(True)
            self._updating_checks = False

    def _on_scanned(self, entries):
        self._scanning = False
        self.progress.setVisible(False)
        self.btn_scan.setEnabled(True)
        # Les lots ont déjà rempli l'arbre ; ``entries`` fait foi en cas
        # d'analyse interrompue (liste partielle) comme en cas de succès.
        self._entries = entries
        if not entries:
            self._clear_plan("Aucun fichier audio trouvé dans ce dossier.")
            return
        self._refresh_preview()
        self._rebuild_plan()

    # ------------------------------------------------------------------
    # Arborescence
    # ------------------------------------------------------------------
    def _dir_item(self, dirpath):
        """Item de dossier correspondant à ``dirpath``, créé au besoin.

        Les dossiers intermédiaires sont créés de proche en proche jusqu'au
        dossier analysé, qui forme la racine visible de l'arbre.
        """
        key = os.path.normcase(os.path.abspath(dirpath))
        item = self._dir_items.get(key)
        if item is not None:
            return item

        root_key = os.path.normcase(os.path.abspath(self.root))
        if key == root_key:
            item = QTreeWidgetItem(self.tree, [self.root, "", "", ""])
            item.setForeground(self.COL_NAME, QBrush(COLOR_DIR))
            item.setExpanded(True)
        else:
            parent_path = os.path.dirname(dirpath)
            # Sécurité : un chemin hors du dossier analysé (lien, volume
            # différent) est rattaché à la racine plutôt que de boucler.
            if not parent_path or same_path(parent_path, dirpath):
                parent = self._dir_item(self.root)
            else:
                parent = self._dir_item(parent_path)
            item = QTreeWidgetItem(parent, [os.path.basename(dirpath), "", "", ""])
            item.setForeground(self.COL_NAME, QBrush(COLOR_DIR))

        item.setData(self.COL_NAME, self.ROLE_RESULT, None)
        self._dir_items[key] = item
        return item

    def _add_file_item(self, entry):
        """Crée la ligne d'un fichier sous son dossier.

        Tout fichier analysé est cochable. La case ne désigne pas la seule
        exécution du plan : c'est le choix de la ligne, et la recherche des
        tags complémentaires s'y tient elle aussi — un fichier déjà rangé n'a
        rien à déplacer mais peut très bien manquer de genre. Sans case, il
        n'y aurait aucun moyen de restreindre cette recherche à un artiste ou
        à un dossier.

        Elle naît cochée : l'analyse en cours n'a pas encore dit ce que ce
        fichier attend, et c'est le plan qui posera la sélection réelle (voir
        ``_set_default_checks``).
        """
        path = entry.get("path", "")
        parent = self._dir_item(os.path.dirname(path))
        item = QTreeWidgetItem(parent, [
            os.path.basename(path),
            entry.get("artist", ""),
            entry.get("album", ""),
            "",
        ])
        item.setData(self.COL_NAME, self.ROLE_PATH, path)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(self.COL_NAME, Qt.CheckState.Checked)
        self._file_items[os.path.normcase(os.path.abspath(path))] = item
        return item

    def _is_file(self, item):
        """Vrai si cette ligne est un fichier (et non un dossier).

        Le chemin n'est porté que par les lignes de fichier : c'est un test
        plus sûr que l'absence d'enfants, qu'un dossier vide partagerait.
        """
        return bool(item.data(self.COL_NAME, self.ROLE_PATH))

    def _is_checked(self, item):
        return item.checkState(self.COL_NAME) == Qt.CheckState.Checked

    def _annotate(self, result):
        """Renseigne la colonne « Action » de la ligne d'un fichier."""
        item = self._file_items.get(
            os.path.normcase(os.path.abspath(result["src"]))
        )
        if item is None:
            return

        status = result["status"]
        item.setData(self.COL_NAME, self.ROLE_RESULT, status)

        if status == "move":
            # Un simple renommage affiche le nouveau nom seul : répéter le
            # chemin complet, identique à la source, ne montrait pas ce qui
            # change (« R.E.M..mp3 » → « R.E.M.mp3 »).
            if same_path(os.path.dirname(result["src"]),
                         os.path.dirname(result["dst"])):
                action = f"Renommer  →  {os.path.basename(result['dst'])}"
            else:
                action = self._rel(result["dst"])
            item.setText(self.COL_ACTION, action)
            item.setToolTip(self.COL_ACTION, result["dst"])
            item.setForeground(self.COL_ACTION, QBrush(COLOR_MOVE))
            self._op_items.append((item, result))
        elif status == "duplicate":
            # L'exemplaire conservé est lui-même rangé : c'est son emplacement
            # final qui intéresse l'utilisateur, non celui qu'il quitte.
            kept = self._final_path(result["dst"])
            item.setText(
                self.COL_ACTION,
                f"Doublon à supprimer  ·  exemplaire gardé : {self._rel(kept)}"
            )
            item.setToolTip(
                self.COL_ACTION,
                f"Ce fichier sera supprimé du disque.\nExemplaire conservé :\n{kept}"
            )
            item.setForeground(self.COL_ACTION, QBrush(COLOR_DUPLICATE))
            self._op_items.append((item, result))
        elif status in ("inplace", "album"):
            item.setText(
                self.COL_ACTION,
                "Déjà en place" if status == "inplace" else "Album conservé"
            )
            item.setForeground(self.COL_ACTION, QBrush(COLOR_INPLACE))
            item.setForeground(self.COL_NAME, QBrush(COLOR_INPLACE))
            if status == "album":
                # Le dossier porte l'explication : c'est lui l'unité conservée.
                parent = item.parent()
                if parent is not None and not parent.text(self.COL_ACTION):
                    parent.setText(
                        self.COL_ACTION,
                        "Musique de film / compilation — laissée intacte"
                    )
                    parent.setForeground(self.COL_ACTION, QBrush(COLOR_ALBUM))
        else:
            missing = ", ".join(result["missing"])
            item.setText(self.COL_ACTION, f"Tag manquant : {missing}")
            item.setToolTip(
                self.COL_ACTION,
                "Complétez les tags de ce fichier pour pouvoir le ranger."
            )
            item.setForeground(self.COL_ACTION, QBrush(COLOR_WARN))

    def _final_path(self, path):
        """Emplacement qu'occupera ``path`` une fois le plan appliqué."""
        result = self._plan_by_src.get(os.path.normcase(os.path.abspath(path or "")))
        if result is None:
            return path
        return result.get("dst") or result.get("src") or path

    def _is_operation(self, item):
        """Vrai si cette ligne est un fichier que le plan propose de traiter.

        Déplacement, renommage ou suppression d'un doublon : ce sont les
        lignes que « Organiser » exécutera. Toutes les lignes de fichier
        portent une case, celles-ci sont les seules dont la case commande un
        mouvement sur le disque (voir ``selected_operations``).
        """
        return (item.childCount() == 0
                and item.data(self.COL_NAME, self.ROLE_RESULT)
                in ("move", "duplicate"))

    def _refresh_dir_checkboxes(self, item):
        """Pose la case d'un dossier d'après ses fichiers.

        Retourne (nombre de fichiers, nombre de cochés) pour la branche. La
        case du dossier est cochée, décochée ou partielle selon eux ; un
        dossier sans aucun fichier n'affiche pas de case.

        Tous les fichiers comptent, non les seuls que le plan déplace : la
        case sert aussi à choisir ce que la recherche des tags
        complémentaires couvrira, et un dossier entièrement rangé doit
        pouvoir se décocher. Qt sait tenir cet état seul
        (``ItemIsAutoTristate``), mais compterait aussi les lignes de dossier
        intermédiaires — d'où ce calcul maison, qui ne retient que les
        fichiers.
        """
        total = checked = 0
        for index in range(item.childCount()):
            child = item.child(index)
            if not self._is_file(child):
                sub_total, sub_checked = self._refresh_dir_checkboxes(child)
                total += sub_total
                checked += sub_checked
            else:
                total += 1
                if self._is_checked(child):
                    checked += 1

        if not total:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(self.COL_NAME, Qt.ItemDataRole.CheckStateRole, None)
            return 0, 0

        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        if checked == 0:
            state = Qt.CheckState.Unchecked
        elif checked == total:
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        item.setCheckState(self.COL_NAME, state)
        return total, checked

    def _set_subtree_checked(self, item, state):
        """Coche/décoche tous les fichiers sous ``item``."""
        for index in range(item.childCount()):
            child = item.child(index)
            if not self._is_file(child):
                self._set_subtree_checked(child, state)
            else:
                child.setCheckState(self.COL_NAME, state)

    def _on_item_changed(self, item, column):
        """Propage une case cochée : un dossier entraîne toute sa branche.

        Les ancêtres sont ensuite recalculés pour refléter l'état partiel.
        """
        if column != self.COL_NAME or self._updating_checks:
            return
        self._mark_touched(item)
        self._updating_checks = True
        try:
            if item.childCount():
                state = item.checkState(self.COL_NAME)
                if state != Qt.CheckState.PartiallyChecked:
                    self._set_subtree_checked(item, state)
            for index in range(self.tree.topLevelItemCount()):
                self._refresh_dir_checkboxes(self.tree.topLevelItem(index))
        finally:
            self._updating_checks = False
        # Les deux boutons dépendent de ce qui est coché : décocher le dernier
        # fichier concerné doit les éteindre sur le champ, sans quoi ils
        # ouvriraient un écran vide.
        self._refresh_action_buttons()

    # ------------------------------------------------------------------
    # Écoute (module player)
    # ------------------------------------------------------------------
    def _show_tree_menu(self, point):
        """Menu contextuel d'une ligne : écouter, ou ouvrir le dossier."""
        item = self.tree.itemAt(point)
        if item is None:
            return
        path = item.data(self.COL_NAME, self.ROLE_PATH)
        menu = QMenu(self)
        listen = menu.addAction("Écouter dans DeeKeep",
                                lambda: self._play_item(item))
        listen.setEnabled(bool(path))
        folder = path if path else None
        menu.addAction("Ouvrir le dossier",
                       lambda: self._open_folder(folder))
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _on_item_double_clicked(self, item, _column):
        self._play_item(item)

    def _play_item(self, item):
        """Ouvre le lecteur sur le fichier d'une ligne, la branche en file.

        La file d'attente reprend les fichiers visibles du dossier affiché :
        c'est l'unité à laquelle on écoute une bibliothèque en cours de
        rangement — « qu'est-ce que contient ce dossier, au fait ».
        """
        path = item.data(self.COL_NAME, self.ROLE_PATH)
        if not path:
            return
        parent = item.parent()
        siblings = ([parent.child(index) for index in range(parent.childCount())]
                    if parent is not None else [item])
        tracks, index = [], 0
        for sibling in siblings:
            sibling_path = sibling.data(self.COL_NAME, self.ROLE_PATH)
            if not sibling_path or sibling.isHidden():
                continue
            if sibling is item:
                index = len(tracks)
            tracks.append(player.make_track(
                path=sibling_path,
                title=sibling.text(self.COL_NAME),
                artist=sibling.text(self.COL_ARTIST),
                album=sibling.text(self.COL_ALBUM),
                subtitle=sibling_path,
            ))
        player.play_tracks(self, tracks, index)

    @staticmethod
    def _open_folder(path):
        if path:
            reveal_in_explorer(path)

    def _apply_visibility(self):
        """Applique le filtre « masquer ce qui est déjà en place »."""
        hide_ok = self.cb_hide_ok.isChecked()
        self.tree.setUpdatesEnabled(False)
        try:
            for index in range(self.tree.topLevelItemCount()):
                self._apply_visibility_to(self.tree.topLevelItem(index), hide_ok)
        finally:
            self.tree.setUpdatesEnabled(True)

    def _apply_visibility_to(self, item, hide_ok):
        """Masque récursivement les lignes sans intérêt. Retourne la visibilité."""
        if item.childCount():
            visible = False
            for index in range(item.childCount()):
                visible = self._apply_visibility_to(item.child(index), hide_ok) or visible
            item.setHidden(hide_ok and not visible)
            return visible or not hide_ok

        status = item.data(self.COL_NAME, self.ROLE_RESULT)
        keep = status not in ("inplace", "album")
        item.setHidden(hide_ok and not keep)
        return keep

    def _toggle_expand(self):
        """Déplie ou replie tout l'arbre (bouton à double effet)."""
        if self.btn_expand.text() == "Tout déplier":
            self.tree.expandAll()
            self.btn_expand.setText("Tout replier")
        else:
            self.tree.collapseAll()
            for index in range(self.tree.topLevelItemCount()):
                self.tree.topLevelItem(index).setExpanded(True)
            self.btn_expand.setText("Tout déplier")

    # ------------------------------------------------------------------
    # Plan de rangement
    # ------------------------------------------------------------------
    def _rebuild_plan(self):
        """Recalcule le plan à partir des fichiers déjà analysés."""
        if not self._entries or self._busy():
            return

        results, stats = build_plan(self._entries, self.root, self.current_options())
        self._results = results
        self._op_items = []
        # Indexé par source : la ligne d'un doublon doit pouvoir nommer
        # l'emplacement final de l'exemplaire conservé (voir ``_final_path``).
        self._plan_by_src = {
            os.path.normcase(os.path.abspath(result["src"])): result
            for result in results
        }

        # Les milliers de modifications qui suivent ne doivent pas déclencher
        # le recalcul des dossiers ligne par ligne : il est fait une fois, à
        # la fin.
        self._updating_checks = True
        self.tree.setUpdatesEnabled(False)
        try:
            self._reset_annotations()
            show_untagged = self.cb_untagged.isChecked()
            for result in results:
                if result["status"] == "untagged" and not show_untagged:
                    continue
                self._annotate(result)
            self._set_default_checks()
            for index in range(self.tree.topLevelItemCount()):
                self._refresh_dir_checkboxes(self.tree.topLevelItem(index))
        finally:
            self.tree.setUpdatesEnabled(True)
            self._updating_checks = False

        self._apply_visibility()

        parts = [
            f"{len(self._entries)} fichiers analysés",
            f"{stats['move']} à ranger",
            f"{stats['inplace']} déjà en place",
        ]
        if stats["duplicate"]:
            parts.append(f"{stats['duplicate']} doublon(s) à supprimer")
        if stats["divers"]:
            parts.append(f"{stats['divers']} vers « {DIVERS_FOLDER} »")
        if stats["from_divers"]:
            parts.append(f"{stats['from_divers']} sortent de « {DIVERS_FOLDER} »")
        if stats["album"]:
            parts.append(
                f"{stats['album']} dans {stats['album_dirs']} album(s) préservé(s)"
            )
        if stats["untagged"]:
            parts.append(f"{stats['untagged']} sans tags exploitables")
        self.lbl_status.setText(" · ".join(parts))
        self.btn_apply.setEnabled(bool(self._op_items))
        self.btn_pending.setEnabled(True)
        self._refresh_action_buttons()
        self.btn_dupes.setEnabled(len(self._entries) > 1)

    def _refresh_action_buttons(self):
        """Active la recherche de tags d'après ce qui reste à chercher.

        Recalculé aussi à chaque case cochée : la recherche ne porte que sur
        les fichiers cochés, et sans eux le bouton n'aurait rien à chercher.
        """
        self.btn_tags_lookup.setEnabled(bool(self._lookup_entries()))

    def _reset_annotations(self):
        """Efface les annotations d'un plan précédent (options modifiées).

        Les cases posées à la main, elles, sont laissées telles quelles :
        elles disent ce que l'utilisateur a choisi de traiter, ce qu'un
        changement d'option ne remet pas en cause. Les recocher toutes lui
        reprendrait en silence la sélection qu'il vient de faire — et rendrait
        à la recherche des tags la bibliothèque entière. Les autres suivent le
        nouveau plan (voir ``_set_default_checks``).
        """
        for item in self._file_items.values():
            item.setData(self.COL_NAME, self.ROLE_RESULT, None)
            item.setText(self.COL_ACTION, "")
            item.setToolTip(self.COL_ACTION, "")
            item.setForeground(self.COL_ACTION, QBrush(COLOR_INPLACE))
            item.setForeground(self.COL_NAME, QBrush(COLOR_DIR))
        for item in self._dir_items.values():
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(self.COL_NAME, Qt.ItemDataRole.CheckStateRole, None)
            item.setText(self.COL_ACTION, "")

    def _clear_plan(self, message):
        self.tree.clear()
        self._dir_items = {}
        self._file_items = {}
        self._touched_checks = set()
        self._op_items = []
        self._results = []
        self._plan_by_src = {}
        self._entries = []
        self.btn_apply.setEnabled(False)
        self.btn_pending.setEnabled(False)
        self.btn_tags_lookup.setEnabled(False)
        self.btn_dupes.setEnabled(False)
        self.btn_expand.setText("Tout déplier")
        self.lbl_status.setText(message)

    def _rel(self, path):
        """Chemin affiché, relatif au dossier analysé quand c'est possible."""
        try:
            rel = os.path.relpath(path, self.root)
            return rel if not rel.startswith("..") else path
        except ValueError:      # volumes différents sous Windows
            return path

    def _set_all_checked(self, checked):
        """Coche ou décoche tous les fichiers analysés, plan ou non.

        Le choix est celui de l'utilisateur : il tient jusqu'à la prochaine
        analyse, et une option modifiée entre-temps ne le défait pas (voir
        ``_mark_touched``).
        """
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._updating_checks = True
        self.tree.setUpdatesEnabled(False)
        try:
            for key, item in self._file_items.items():
                item.setCheckState(self.COL_NAME, state)
                self._touched_checks.add(key)
            for index in range(self.tree.topLevelItemCount()):
                self._refresh_dir_checkboxes(self.tree.topLevelItem(index))
        finally:
            self.tree.setUpdatesEnabled(True)
            self._updating_checks = False
        self._refresh_action_buttons()

    def _check_pending(self):
        """Revient à la sélection que l'analyse pose d'elle-même.

        Les cases posées à la main sont oubliées : ce bouton est justement ce
        par quoi l'utilisateur renonce à sa propre sélection.
        """
        if not self._results:
            return
        self._touched_checks.clear()
        self._updating_checks = True
        self.tree.setUpdatesEnabled(False)
        try:
            self._set_default_checks()
            for index in range(self.tree.topLevelItemCount()):
                self._refresh_dir_checkboxes(self.tree.topLevelItem(index))
        finally:
            self.tree.setUpdatesEnabled(True)
            self._updating_checks = False
        self._refresh_action_buttons()

    def _default_checked(self, result):
        """Vrai si ce fichier a quelque chose à attendre de cet écran.

        Un déplacement, un renommage ou un doublon à supprimer : « Organiser »
        les traite, ils sont cochés. Un tag manquant, indispensable ou
        complémentaire : la recherche peut encore le trouver, ils sont cochés
        aussi. Reste le fichier déjà en place dont tous les tags sont là — la
        recherche l'ignorerait et le plan n'a rien à en faire : sa case ne
        disait rien de plus que « rien à faire », et noyait les quelques
        lignes qui réclament une décision.
        """
        if result["status"] in ("inplace", "album"):
            return tag_lookup.needs_extras(result["entry"])
        return True

    def _set_default_checks(self):
        """Pose les cases que l'utilisateur n'a pas touchées.

        Appelée la garde ``_updating_checks`` déjà posée, et suivie du recalcul
        des cases de dossier : elle ne touche qu'aux lignes de fichier.

        Le plan est recalculé à chaque option modifiée, et la sélection suit :
        cocher « dossier par artiste » fait apparaître des milliers de
        déplacements, qui doivent l'être eux aussi. Ce qui a été coché ou
        décoché à la main, en revanche, est laissé intact.
        """
        for result in self._results:
            key = os.path.normcase(os.path.abspath(result["src"]))
            if key in self._touched_checks:
                continue
            item = self._file_items.get(key)
            if item is None:
                continue
            item.setCheckState(
                self.COL_NAME,
                Qt.CheckState.Checked if self._default_checked(result)
                else Qt.CheckState.Unchecked
            )

    def _mark_touched(self, item):
        """Retient les fichiers dont la case vient d'être posée à la main.

        Un dossier coché vaut pour toute sa branche, comme la propagation de
        la case elle-même.
        """
        if self._is_file(item):
            path = item.data(self.COL_NAME, self.ROLE_PATH)
            self._touched_checks.add(os.path.normcase(os.path.abspath(path)))
            return
        for index in range(item.childCount()):
            self._mark_touched(item.child(index))

    # ------------------------------------------------------------------
    # Recherche des doublons
    # ------------------------------------------------------------------
    def _find_duplicates(self):
        """Rapproche les fichiers analysés et ouvre l'écran de déduplication."""
        if self._busy():
            QMessageBox.information(
                self, "Analyse en cours",
                "Attendez la fin de l'analyse avant de chercher les doublons.")
            return
        if not self._entries:
            QMessageBox.information(
                self, "Doublons", "Lancez d'abord l'analyse du dossier.")
            return

        # L'index d'artistes est celui du plan de rangement : les écritures
        # d'un même nom se rapprochent ici comme elles se rangent là.
        index = ArtistIndex(self._entries)
        groups = dedup_dialog.duplicate_groups(
            self._entries, artist_key=index.entry_key)
        if not groups:
            QMessageBox.information(
                self, "Doublons",
                "Aucun fichier ne semble être la copie d'un autre.")
            return

        # L'analyse de rangement se passe du débit (elle gagne à ne pas
        # parcourir le flux MPEG de chaque fichier) ; le choix de l'exemplaire
        # à garder, lui, en dépend. Il n'est lu que pour les fichiers des
        # groupes, quelques dizaines au lieu de milliers.
        for group in groups:
            for entry in group:
                if not entry.get("bitrate"):
                    entry["bitrate"] = copy_quality(entry.get("path", ""))[0]

        dialog = dedup_dialog.DeduplicationDialog(
            groups, self, target_format=self._settings.get("file_format", ""))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._delete_duplicates(dialog.get_files_to_delete())

    def _delete_duplicates(self, paths):
        """Supprime les fichiers choisis et recalcule le plan sans eux."""
        if not paths:
            return
        deleted, errors = 0, []
        for path in paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
                deleted += 1
            except OSError as exc:
                errors.append(f"{os.path.basename(path)} : {exc}")

        # Les fichiers supprimés ne doivent plus peser sur le plan : ils
        # comptaient pour leur artiste, et donc dans la règle « Divers ».
        gone = {os.path.normcase(os.path.abspath(p)) for p in paths}
        self._entries = [
            entry for entry in self._entries
            if os.path.normcase(os.path.abspath(entry.get("path", ""))) not in gone
        ]
        for key in gone:
            item = self._file_items.pop(key, None)
            if item is not None:
                parent = item.parent()
                if parent is not None:
                    parent.removeChild(item)
                else:
                    index = self.tree.indexOfTopLevelItem(item)
                    if index >= 0:
                        self.tree.takeTopLevelItem(index)
        reencode_scan.clear_tag_cache()
        self._rebuild_plan()

        message = f"{deleted} fichier(s) supprimé(s)."
        if errors:
            message += "\n\nErreurs :\n" + "\n".join(errors[:10])
        QMessageBox.information(self, "Déduplication", message)

    # ------------------------------------------------------------------
    # Recherche des tags manquants
    # ------------------------------------------------------------------
    def _lookup_entries(self):
        """Fichiers cochés qu'une recherche en ligne peut compléter.

        Deux manques y conduisent, et le même passage les traite : les tags
        sans lesquels le plan ne sait pas ranger le fichier — titre, artiste,
        album — et les tags complémentaires, qui n'empêchent rien mais
        manquent partout ailleurs. Le premier manque se lit dans le plan et
        non dans les tags : c'est lui qui sait si l'album est requis par les
        options en cours. Le second se lit dans le fichier, que le plan
        sache le ranger ou non.

        Ceux dont la case est décochée en sont écartés, comme pour toute
        autre action de l'écran. L'ordre est celui de l'analyse : la revue se
        remplit dans l'ordre de l'arborescence.
        """
        blocked = {
            os.path.normcase(os.path.abspath(result["entry"].get("path", "")))
            for result in self._results if result["status"] == "untagged"
        }
        kept = []
        for entry in self._entries:
            key = os.path.normcase(os.path.abspath(entry.get("path", "")))
            if key not in blocked and not tag_lookup.needs_extras(entry):
                continue
            if self._entry_checked(entry):
                kept.append(entry)
        return kept

    def _entry_checked(self, entry):
        """Vrai si la ligne de ce fichier est cochée dans l'arborescence.

        Un fichier que l'analyse a relevé sans que l'arbre le montre — lot
        encore en cours d'affichage — est tenu pour coché : c'est l'état que
        sa ligne prendra en naissant.
        """
        key = os.path.normcase(os.path.abspath(entry.get("path", "")))
        item = self._file_items.get(key)
        return item is None or self._is_checked(item)

    def _lookup_tags(self):
        """Cherche les tags manquants en ligne, puis propose de les écrire.

        Un seul passage pour les deux manques : ``find_tags`` demande pour
        chaque fichier ce qui lui manque, et lui seul décide des services à
        solliciter — MusicBrainz et l'empreinte acoustique ne savent rien des
        tags complémentaires, et ne sont donc pas appelés pour un fichier
        auquel il ne manque qu'un genre.
        """
        if self._busy():
            return
        entries = self._lookup_entries()
        if not entries:
            QMessageBox.information(
                self, "Rien à compléter",
                "Aucun fichier coché ne manque de tag : ils portent tous "
                "leur titre, leur artiste et leur album, ainsi que leur "
                "artiste d'album, leur genre, leur année et leur numéro de "
                "piste."
            )
            return

        # Une requête par fichier : sur une bibliothèque entière, l'attente
        # se compte en minutes et mérite d'être annoncée avant de commencer.
        # Elle n'est pas subie pour autant — la revue s'arrête quand on veut
        # et garde ce qu'elle a déjà trouvé.
        if len(entries) > TAGS_LOOKUP_WARN:
            reply = QMessageBox.question(
                self, "Recherche longue",
                f"{len(entries)} fichiers cochés n'ont pas tous leurs "
                f"tags.\n\nLe catalogue est interrogé fichier par fichier : "
                f"comptez de l'ordre de "
                f"{max(1, round(len(entries) * SECONDS_PER_LOOKUP / 60))} "
                f"minute(s). La recherche s'interrompt à tout moment — les "
                f"correspondances déjà trouvées restent écrivables, les "
                f"fichiers non encore interrogés sont laissés tels quels et "
                f"un passage suivant les reprendra. Rien n'est écrit sans "
                f"validation."
                f"\n\nLancer la recherche ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        dialog = tag_lookup_dialog.TagLookupDialog(
            entries, self.root, self._settings.get("acoustid_key", ""), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        accepted = dialog.get_accepted()
        written, failures = 0, []
        for path, values, _proposal in accepted:
            ok, message = tag_lookup.write_tags(path, values)
            if ok:
                written += 1
            else:
                failures.append((path, message))

        lines = [f"{written} fichier(s) complété(s) sur {len(accepted)}."]
        if failures:
            lines.append(f"\n{len(failures)} échec(s) :")
            for path, message in failures[:10]:
                lines.append(f"  • {os.path.basename(path)} — {message}")
            if len(failures) > 10:
                lines.append(f"  … et {len(failures) - 10} autre(s).")
        QMessageBox.information(self, "Tags complétés", "\n".join(lines))

        if written:
            # Les tags ont changé sur le disque : le cache est caduc et le
            # plan doit être reconstruit sur les nouvelles valeurs.
            reencode_scan.clear_tag_cache()
            self._start_scan()

    # ------------------------------------------------------------------
    # Application du plan
    # ------------------------------------------------------------------
    def selected_operations(self):
        return [
            result for item, result in self._op_items
            if item.checkState(self.COL_NAME) == Qt.CheckState.Checked
        ]

    def _confirm_apply(self):
        if self._busy():
            return
        operations = self.selected_operations()
        if not operations:
            QMessageBox.information(
                self, "Aucune sélection", "Cochez au moins un fichier à ranger."
            )
            return

        moves = [op for op in operations if op["status"] == "move"]
        dups = [op for op in operations if op["status"] == "duplicate"]

        lines = [f"Déplacer {len(moves)} fichier(s) dans :\n{self.root}"]
        if dups:
            # La suppression est irréversible : elle est annoncée à part, en
            # toutes lettres, et jamais noyée dans le décompte des rangements.
            lines.append(
                f"\nSupprimer définitivement {len(dups)} doublon(s) du disque, "
                "dont un autre exemplaire est conservé."
            )
        if self.cb_empty.isChecked():
            lines.append("\nLes dossiers devenus vides seront supprimés.")
        reply = QMessageBox.question(
            self, "Confirmer le rangement", "".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._applying = True
        self._set_controls_enabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(operations))
        self.progress.setValue(0)

        self._apply_thread = OrganizeApplyThread(
            operations, self.root, self.cb_empty.isChecked()
        )
        self._apply_thread.progress.connect(self._on_apply_progress)
        self._apply_thread.done.connect(
            lambda failures, removed: self._on_apply_done(
                operations, failures, removed)
        )
        self._apply_thread.start()

    def _on_apply_progress(self, count, name):
        self.progress.setValue(count)
        self.lbl_status.setText(f"Rangement… {count} — {name}")

    def _on_apply_done(self, operations, failures, removed):
        self._applying = False
        self.progress.setVisible(False)
        self._set_controls_enabled(True)

        failed = {os.path.normcase(src) for src, _msg in failures}

        def succeeded(status):
            ops = [op for op in operations if op["status"] == status]
            done = sum(1 for op in ops
                       if os.path.normcase(op["src"]) not in failed)
            return done, len(ops)

        moved, to_move = succeeded("move")
        deleted, to_delete = succeeded("duplicate")
        lines = [f"{moved} fichier(s) rangé(s) sur {to_move}."]
        if to_delete:
            lines.append(f"{deleted} doublon(s) supprimé(s) sur {to_delete}.")
        if removed:
            lines.append(f"{len(removed)} dossier(s) vide(s) supprimé(s).")
        if failures:
            lines.append(f"\n{len(failures)} échec(s) :")
            for src, msg in failures[:10]:
                lines.append(f"  • {os.path.basename(src)} — {msg}")
            if len(failures) > 10:
                lines.append(f"  … et {len(failures) - 10} autre(s).")

        # Les chemins analysés n'existent plus : le cache de tags est caduc.
        reencode_scan.clear_tag_cache()
        self._clear_plan("Rangement terminé : relancez l'analyse pour vérifier.")
        if moved or deleted:
            self.libraryChanged.emit()
        QMessageBox.information(self, "Rangement terminé", "\n".join(lines))

    def _set_controls_enabled(self, enabled):
        for widget in (self.btn_scan, self.btn_apply, self.btn_all, self.btn_none,
                       self.btn_pending, self.btn_expand, self.btn_tags_lookup,
                       self.btn_dupes,
                       self.cb_artist, self.cb_album,
                       self.cb_rename, self.cb_empty, self.cb_untagged,
                       self.cb_keep_albums, self.cb_dedup, self.cb_divers,
                       self.spin_divers_min, self.combo_pattern, self.btn_tags):
            widget.setEnabled(enabled)
        if enabled:
            self._refresh_option_widgets()

    # ------------------------------------------------------------------
    def can_leave(self):
        """Vrai si l'application peut se fermer sans interrompre un rangement."""
        return self._apply_thread is None or not self._apply_thread.isRunning()

    def stop_threads(self):
        """Interrompt l'analyse en cours (appelé à la fermeture de l'application)."""
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._scan_thread.stop()
            self._scan_thread.wait(3000)
        if self._apply_thread is not None and self._apply_thread.isRunning():
            # Un déplacement ne s'interrompt pas en plein vol : on attend la
            # fin de l'opération courante pour ne pas perdre de fichier.
            self._apply_thread.wait(10000)
