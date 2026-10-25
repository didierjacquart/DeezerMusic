"""
Règles de rangement de la bibliothèque, partagées par tous les écrans.

L'écran « Organiser » définit où un titre doit se trouver et comment il doit
s'appeler : sous-dossier par artiste, sous-dossier par album, motif de
nommage, regroupement des artistes peu représentés dans « Divers ». Ces
réglages ne valaient jusqu'ici que pour les fichiers *déjà* sur le disque :
une extraction rangeait selon ses propres conventions, si bien qu'un titre
fraîchement téléchargé pouvait atterrir ailleurs que là où « Organiser »
l'aurait mis, et se retrouvait déplacé à l'analyse suivante.

Ce module porte la règle une seule fois. ``organize_page`` s'en sert pour
bâtir son plan ; ``download_thread`` s'en sert pour choisir la destination de
chaque fichier extrait — téléchargement de la page Musique comme
retéléchargement de la page Qualité.

Un seul écart assumé entre le plan de rangement et une extraction :
« Renommer les fichiers » décide du sort des fichiers *déjà nommés*. Un
fichier qui n'existe pas encore doit bien recevoir un nom : l'option décochée,
c'est la convention historique de DeeKeep (« Titre - Artiste ») qui
s'applique — voir ``download_pattern``.

Le décompte de la règle « Divers », lui, doit voir la même chose des deux
côtés. « Organiser » compte les titres d'un artiste sur tout le lot analysé
d'un coup, écritures variantes réunies (« Inna », « Inna Feat. Flo Rida »).
Une extraction, elle, décidait titre par titre sur le seul dossier de
l'artiste tel qu'il existait à cet instant : la page Musique lançant dix
extractions en parallèle, les dix premiers titres d'un artiste nouveau se
voyaient chacun seul et partaient tous dans « Divers » — les dix suivants
trouvant toujours le dossier vide, un album entier y passait, pour en
ressortir à l'analyse suivante. ``Tally`` porte ce décompte : il réunit ce
que le disque contient déjà et ce que le lot en cours va y écrire.
"""

import os
import re
import threading

from deekeep.library import name_pattern
from deekeep.audio import reencode_scan

from deekeep.core.utils import sanitize_filename


# Nom du dossier fourre-tout des artistes peu représentés.
DIVERS_FOLDER = "Divers"

# Marqueurs explicites d'invité : ce qui suit n'appartient pas à l'artiste
# principal du titre, et est toujours écarté.
_FEAT_RE = re.compile(
    r"(?i)\s+(?:feat\.?|featuring|ft\.?|vs\.?|versus|with|avec"
    r"|présente|presente|presents?)\s+"
)

# Séparateurs ambigus : ils peuvent joindre deux artistes (« Calvin Harris &
# Sam Smith ») comme faire partie d'un nom (« Earth, Wind & Fire »). Ils ne
# sont coupés que si ce qui précède est déjà un artiste connu.
_JOIN_RE = re.compile(r"(?i)\s*(?:&|\+|,|/|\bx\b|\band\b|\bet\b)\s*")

# Séparateur du nom de fichier « Titre - Artiste » de DeeKeep.
_STEM_SPLIT_RE = re.compile(r"\s+-\s+")


def safe_component(name):
    """Nom de dossier/fichier utilisable sur tous les systèmes.

    S'appuie sur ``sanitize_filename`` — la même fonction que le
    téléchargement — pour que le rangement d'un fichier existant vise
    exactement le nom qu'une nouvelle extraction lui aurait donné
    (« AC/DC » donne « ACDC » de part et d'autre).

    D'où l'absence de compactage des espaces : retirer le « / » de
    « Whip / Nae Nae » laisse deux espaces, que le téléchargement conserve.
    Les recoller ici ferait voir un renommage à chaque analyse, invisible à
    l'œil et jamais définitif puisque le téléchargement les recréerait.

    S'y ajoutent la neutralisation des caractères de contrôle et la
    suppression des points et espaces finaux, interdits sous Windows.
    """
    cleaned = re.sub(r"[\x00-\x1f]+", " ", sanitize_filename(name or ""))
    return cleaned.strip().rstrip(". ")


def artist_key(name):
    """Clé de regroupement d'un artiste : son nom de dossier, casse ignorée."""
    return safe_component(name or "").casefold()


def main_artist(name, known):
    """Artiste principal d'un nom de tag, invités écartés.

    Les marqueurs explicites d'invité (« feat. », « with », « vs. ») sont
    toujours coupés : « Madonna Featuring Justin Timberlake and Timbaland »
    rejoint « Madonna ».

    Les séparateurs ambigus (« & », « , ») ne le sont que si ce qui précède
    figure déjà seul dans ``known``, l'ensemble des artistes connus :
    « Calvin Harris & Sam Smith » rejoint « Calvin Harris », mais « Earth,
    Wind & Fire » — dont aucun titre n'est signé « Earth » — reste entier.

    ``known`` vient des artistes du lot analysé côté « Organiser », des
    dossiers déjà présents dans la bibliothèque côté extraction (voir
    ``Tally.known``) : la même règle, sur ce que chaque écran peut savoir
    sans relire les tags de milliers de fichiers.
    """
    base = _FEAT_RE.split(name, 1)[0].strip() or (name or "").strip()
    for match in _JOIN_RE.finditer(base):
        prefix = base[:match.start()].strip()
        key = artist_key(prefix)
        if key and key in known and key != artist_key(base):
            return prefix
    return base


def stem_artist(filename):
    """Artiste lu dans un nom de fichier « Titre - Artiste » de DeeKeep.

    Chaîne vide si le nom ne suit pas la convention : mieux vaut ne compter
    personne que compter un titre pour un artiste inventé.
    """
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    parts = _STEM_SPLIT_RE.split(stem)
    return parts[-1].strip() if len(parts) > 1 else ""


# ----------------------------------------------------------------------
# Lecture des réglages
# ----------------------------------------------------------------------
def rules_from_settings(settings):
    """Règles de rangement telles qu'enregistrées par l'écran « Organiser ».

    Les clés ``org_*`` de la configuration sont la seule source : changer une
    case sur l'écran Organiser change du même coup la destination des
    téléchargements, sans réglage à répéter ailleurs.
    """
    settings = settings or {}
    return {
        "artist": bool(settings.get("org_artist", True)),
        "album": bool(settings.get("org_album", True)),
        "rename": bool(settings.get("org_rename", False)),
        "pattern": settings.get("org_pattern") or name_pattern.DEFAULT_PATTERN,
        "divers": bool(settings.get("org_divers", False)),
        "divers_min": max(1, int(settings.get("org_divers_min", 3) or 3)),
    }


def download_pattern(rules):
    """Motif de nommage appliqué à un fichier qui n'existe pas encore.

    Voir l'en-tête du module : sans renommage demandé, un fichier existant
    garde son nom, mais un fichier extrait doit en recevoir un — celui de la
    convention historique.
    """
    if not rules:
        return name_pattern.DEFAULT_PATTERN
    if rules.get("rename"):
        return rules.get("pattern") or name_pattern.DEFAULT_PATTERN
    return name_pattern.DEFAULT_PATTERN


# ----------------------------------------------------------------------
# Destination d'un fichier extrait
# ----------------------------------------------------------------------
def _norm(path):
    """Chemin absolu comparable (casse ignorée, comme partout ailleurs)."""
    return os.path.normcase(os.path.abspath(path or ""))


class Tally:
    """Ce que la bibliothèque comptera par artiste, lot d'extraction compris.

    C'est le pendant, côté extraction, de l'``ArtistIndex`` d'« Organiser » :
    réunir les titres d'un même artiste pour décider une bonne fois s'il
    atteint le seuil de la règle « Divers ». Trois apports se cumulent :

    - les titres déjà rangés dans le dossier de l'artiste ;
    - ceux qui traînent hors d'un dossier d'artiste — dans « Divers » ou à la
      racine — et que leur nom de fichier lui attribue. « Organiser » les
      compte pour lui (ils quittent « Divers » dès qu'il atteint le seuil) ;
      les ignorer ferait indéfiniment tomber ses titres suivants dans
      « Divers », d'où l'analyse suivante les sortirait aussitôt ;
    - **les titres du lot en cours**, réservés par l'appelant avant que le
      premier fil d'extraction ne démarre (voir ``reserve``). Sans eux, dix
      extractions parallèles se voient chacune seule.

    Rien ici n'ouvre un fichier : seuls les noms de dossiers et de fichiers
    sont lus, ce qui tient en quelques dizaines de millisecondes là où relire
    les tags de la bibliothèque demanderait des dizaines de secondes.

    Un même objet est partagé par tous les fils d'un lot : les décomptes sont
    donc pris sous verrou, et le disque n'est parcouru qu'une fois par
    artiste.
    """

    def __init__(self, root):
        self.root = root or ""
        self._lock = threading.RLock()
        self._folders = None    # clé d'artiste -> dossier de premier niveau
        self._loose = None      # clé d'artiste -> titres hors dossier d'artiste
        self._counted = {}      # clé d'artiste -> titres déjà sur le disque
        self._pending = {}      # clé d'artiste -> titres réservés par le lot

    # ------------------------------------------------------------------
    # Lecture du disque (une seule fois, à la demande)
    # ------------------------------------------------------------------
    def _scan_folders(self):
        """Dossiers de premier niveau : le nom de chacun désigne un artiste."""
        if self._folders is not None:
            return self._folders
        folders = {}
        try:
            with os.scandir(self.root) as entries:
                for entry in entries:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    key = artist_key(entry.name)
                    # « Divers » ne nomme personne : c'est le fourre-tout.
                    if key and key != DIVERS_FOLDER.casefold():
                        folders.setdefault(key, entry.name)
        except OSError:
            pass
        self._folders = folders
        return folders

    def _scan_loose(self):
        """Titres hors dossier d'artiste, comptés par artiste.

        Seuls « Divers » et la racine sont regardés : ailleurs, un fichier est
        déjà dans le dossier d'un artiste, qui le compte.
        """
        if self._loose is not None:
            return self._loose
        loose = {}
        for folder in (self.root, os.path.join(self.root, DIVERS_FOLDER)):
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        try:
                            if not entry.is_file():
                                continue
                        except OSError:
                            continue
                        if not entry.name.lower().endswith(
                                reencode_scan.AUDIO_EXTENSIONS):
                            continue
                        key = self._key(stem_artist(entry.name))
                        if key:
                            loose[key] = loose.get(key, 0) + 1
            except OSError:
                continue
        self._loose = loose
        return loose

    def _key(self, artist):
        """Clé de regroupement d'un nom d'artiste, invités écartés.

        Toutes les écritures d'un même artiste tombent sur la même clé
        (« Earth, Wind & Fire With the Emotions » rejoint « Earth, Wind &
        Fire »), qu'elles viennent d'un tag, d'un nom de dossier ou d'un nom
        de fichier : c'est ce qui fait compter ses titres ensemble.
        """
        return artist_key(main_artist((artist or "").strip(),
                                      self._scan_folders()))

    def _on_disk(self, key):
        """Titres de cet artiste déjà écrits, dossier et fourre-tout compris."""
        if key in self._counted:
            return self._counted[key]
        total = self._scan_loose().get(key, 0)
        folder = self._scan_folders().get(key)
        if folder:
            total += sum(
                1 for _ in reencode_scan.iter_audio_files(
                    os.path.join(self.root, folder)))
        self._counted[key] = total
        return total

    def _excluded(self, key, exclude):
        """1 si le fichier que l'extraction remplace est compté pour ``key``.

        Une réextraction remplace un fichier existant : le laisser dans le
        décompte lui ferait atteindre le seuil grâce à lui-même, et un titre
        isolé ne quitterait jamais « Divers ».
        """
        if not exclude:
            return 0
        path = _norm(exclude)
        folder = self._scan_folders().get(key)
        if folder:
            prefix = _norm(os.path.join(self.root, folder)) + os.sep
            if path.startswith(prefix):
                return 1
        parent = os.path.dirname(path)
        if parent in (_norm(self.root), _norm(os.path.join(self.root, DIVERS_FOLDER))):
            return 1 if self._key(stem_artist(exclude)) == key else 0
        return 0

    # ------------------------------------------------------------------
    # Interrogation
    # ------------------------------------------------------------------
    def known(self):
        """Artistes que la bibliothèque nomme déjà par un dossier.

        C'est le ``known`` de ``main_artist`` : un séparateur ambigu n'est
        coupé que si ce qui précède a déjà son dossier.
        """
        with self._lock:
            return set(self._scan_folders())

    def folder(self, artist):
        """Nom du dossier que ce nom d'artiste doit rejoindre.

        L'écriture retenue est celle du dossier existant quand il y en a un :
        « Organiser » range sous une seule écriture, une extraction ne doit
        pas en ouvrir une seconde à côté.
        """
        with self._lock:
            base = main_artist((artist or "").strip(), self._scan_folders())
            return (self._scan_folders().get(artist_key(base))
                    or safe_component(base))

    def reserve(self, artist, count=1):
        """Annonce ``count`` titre(s) de cet artiste à extraire dans ce lot.

        À appeler dès la mise en file, avant tout démarrage : c'est ce qui
        permet aux extractions parallèles d'un même artiste de se compter
        ensemble plutôt que chacune pour une.
        """
        with self._lock:
            key = self._key(artist)
            if key:
                self._pending[key] = self._pending.get(key, 0) + count

    def count(self, artist, exclude=None):
        """Titres que l'artiste comptera une fois le lot écrit."""
        with self._lock:
            key = self._key(artist)
            if not key:
                return 0
            on_disk = max(0, self._on_disk(key) - self._excluded(key, exclude))
            # Le titre en cours n'est pas encore écrit : il compte pour un —
            # et avec lui tout le lot réservé, le cas échéant.
            return on_disk + max(self._pending.get(key, 0), 1)


def target_dir(root, tags, rules, exclude=None, tally=None):
    """Dossier visé par les règles pour une piste, sous ``root``.

    Sans artiste connu, le titre reste à la racine : le placer dans un
    dossier vide ou dans « Divers » sur la foi d'un tag absent créerait un
    rangement que l'analyse d'« Organiser » signalerait aussitôt comme
    non rangeable.

    ``tally`` est le décompte du lot en cours (voir ``Tally``). Sans lui, le
    titre est décompté seul — ce qui ne vaut que pour une extraction isolée.
    """
    if not rules or not rules.get("artist"):
        return root

    name = (tags.get("artist") or "").strip()
    if not name:
        return root

    if tally is None:
        tally = Tally(root)
    artist = tally.folder(name)
    if not artist:
        return root

    if rules.get("divers"):
        if tally.count(name, exclude=exclude) < rules.get("divers_min", 3):
            # « Divers » est plat : ni dossier d'artiste, ni dossier d'album.
            return os.path.join(root, DIVERS_FOLDER)

    path = os.path.join(root, artist)
    album = safe_component((tags.get("album") or "").strip())
    if rules.get("album") and album:
        path = os.path.join(path, album)
    return path


def target_stem(tags, rules):
    """Nom de fichier (sans extension) visé par les règles pour une piste.

    Un motif ne rendant rien d'exploitable — tags absents, balises toutes
    vides — retombe sur « Titre - Artiste », puis sur le seul titre : une
    extraction réussie ne doit jamais produire un fichier sans nom.
    """
    stem = safe_component(name_pattern.format_name(download_pattern(rules), tags))
    if stem:
        return stem
    title = (tags.get("title") or "").strip()
    artist = (tags.get("artist") or "").strip()
    for fallback in (f"{title} - {artist}" if title and artist else "", title, artist):
        stem = safe_component(fallback)
        if stem:
            return stem
    return "piste"


def destination(root, tags, rules, extension, exclude=None, tally=None):
    """Chemin complet visé par les règles pour une piste extraite.

    ``tags`` porte les métadonnées de la piste sous les clés attendues par
    ``name_pattern`` (title, artist, album, date, track…) ; ``extension``
    comprend son point (« .mp3 ») ; ``exclude`` désigne le fichier remplacé ;
    ``tally`` le décompte par artiste du lot en cours (voir ``Tally``).
    """
    return os.path.join(
        target_dir(root, tags, rules, exclude=exclude, tally=tally),
        target_stem(tags, rules) + extension,
    )
