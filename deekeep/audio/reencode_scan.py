"""
Analyse locale des fichiers audio (écrans « Réencoder » et « Organiser »).

Le parcours récursif d'un dossier (potentiellement une bibliothèque entière)
et la lecture des métadonnées de chaque fichier sont réalisés dans un thread
dédié afin que l'interface reste fluide, même pour des milliers de fichiers.

Trois optimisations rendent l'analyse rapide sur de grandes bibliothèques :

    - le parcours utilise ``os.scandir`` (une seule syscall par entrée, le
      type de l'entrée étant déjà connu) plutôt que ``os.walk`` + ``isdir`` ;
    - la lecture des tags est répartie sur un pool de threads : l'attente
      disque d'un fichier est mise à profit pour en lire un autre ;
    - quand le débit n'est pas nécessaire (rangement de fichiers), les MP3
      sont lus via ``ID3`` en limitant les trames décodées : la pochette
      embarquée (souvent plusieurs centaines de Ko) n'est pas décodée et le
      flux MPEG n'est pas parcouru pour en déduire le débit.

S'y ajoute un cache mémoire indexé par (chemin, date de modification,
taille) : relancer une analyse sur le même dossier est quasi instantané.

``read_audio_tags`` est aussi utilisée directement pour des fichiers isolés.
"""

import os

from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QThread, pyqtSignal

from deekeep.core import config

# Extensions audio prises en charge lors de l'ajout de fichiers / dossiers.
AUDIO_EXTENSIONS = (
    ".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma",
)

# Nombre de lectures de tags menées de front. La tâche est dominée par
# l'attente disque : quelques threads suffisent à saturer un SSD, et rester
# modeste évite d'écrouler un disque mécanique par des accès désordonnés.
SCAN_WORKERS = 8

# Trames ID3 réellement exploitées. Toutes les autres (pochette APIC en
# tête) sont ignorées à la lecture, ce qui divise le temps d'analyse.
_WANTED_ID3 = (
    "TIT2", "TPE1", "TPE2", "TALB", "TRCK", "TPOS", "TDRC", "TYER",
    "TDAT", "TIME", "TCON", "TCOM", "TPUB", "TCMP", "COMM",
)

_KNOWN_ID3_FRAMES = None
if config.MUTAGEN_AVAILABLE:
    try:
        from mutagen.id3 import Frames as _ID3_FRAMES, Frames_2_2 as _ID3_FRAMES_22

        _KNOWN_ID3_FRAMES = {
            name: cls for name, cls in _ID3_FRAMES.items() if name in _WANTED_ID3
        }
        # Les tags ID3v2.2 portent des noms de trames à trois lettres (TT2,
        # TP1...) ; mutagen ne les convertit qu'après lecture, il faut donc
        # les déclarer aussi. Chaque classe 2.2 hérite de son équivalent 2.4.
        _KNOWN_ID3_FRAMES.update({
            name: cls for name, cls in _ID3_FRAMES_22.items()
            if cls.__bases__ and cls.__bases__[0].__name__ in _WANTED_ID3
        })
    except Exception:      # version de mutagen inattendue : repli lecture complète
        _KNOWN_ID3_FRAMES = None


# ----------------------------------------------------------------------
# Cache mémoire des tags
# ----------------------------------------------------------------------
# path -> (mtime, taille, dict de tags). Invalidé dès que le fichier change
# sur le disque ; purgé en bloc au-delà d'une taille déraisonnable.
_TAG_CACHE = {}
_TAG_CACHE_MAX = 100000


def clear_tag_cache():
    """Vide le cache de tags (après un rangement, les chemins ont changé)."""
    _TAG_CACHE.clear()


def _cache_key(path):
    """(mtime, taille) du fichier, ou None s'il est illisible."""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# ----------------------------------------------------------------------
# Lecture des métadonnées
# ----------------------------------------------------------------------
def _info_bitrate(audio, path):
    """Débit en kbps à partir d'un objet mutagen déjà ouvert (0 si inconnu)."""
    try:
        info = getattr(audio, "info", None)
        if info is None:
            return 0
        bitrate = getattr(info, "bitrate", 0) or 0
        if bitrate > 0:
            return int(round(bitrate / 1000))
        length = getattr(info, "length", 0) or 0
        if length > 0:
            return int(round((os.path.getsize(path) * 8) / length / 1000))
    except Exception:
        pass
    return 0


def _text(frames, key):
    """Premier texte d'une trame ID3, ou chaîne vide."""
    frame = frames.get(key) if frames else None
    if frame is not None and getattr(frame, "text", None):
        return str(frame.text[0])
    return ""


def _split_num(value):
    """Découpe « 3/12 » en ("3", "12") ; ("", "") si la valeur est vide."""
    if not value:
        return "", ""
    text = str(value)
    if "/" in text:
        num, _, total = text.partition("/")
        return num.strip(), total.strip()
    return text.strip(), ""


def _first(mapping, *keys):
    """Première valeur non vide parmi ``keys`` dans un dict de tags mutagen."""
    for key in keys:
        try:
            value = mapping.get(key)
        except Exception:
            value = None
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        text = str(value).strip()
        if text:
            return text
    return ""


def _empty_tags():
    return {
        "title": "", "artist": "", "album": "", "albumartist": "",
        "track": "", "tracktotal": "", "disc": "", "disctotal": "",
        "date": "", "genre": "", "composer": "", "publisher": "",
        "comment": "", "compilation": "", "bitrate": 0,
        # Le titre vient-il vraiment des tags, ou du nom de fichier faute de
        # mieux ? Un renommage qui l'ignorerait rallongerait le nom à chaque
        # passe (voir ``read_tags``).
        "title_tagged": False,
    }


def _read_id3(path):
    """Trames ID3 utiles d'un MP3, sans toucher au flux MPEG (None si absent)."""
    frames = config.ID3()
    try:
        frames.load(path, known_frames=_KNOWN_ID3_FRAMES, load_v1=False)
    except Exception:
        return None
    return frames


def _read_mp3(path, need_bitrate, tags):
    """Tags d'un MP3. Sans débit demandé, seules les trames utiles sont lues."""
    frames = None
    if need_bitrate or _KNOWN_ID3_FRAMES is None:
        try:
            audio = config.MP3(path)
            frames = audio.tags
            tags["bitrate"] = _info_bitrate(audio, path)
        except Exception:
            # En-tête MPEG illisible : les tags, eux, restent souvent
            # parfaitement exploitables — on les lit directement.
            frames = _read_id3(path) if _KNOWN_ID3_FRAMES else None
    else:
        frames = _read_id3(path)

    tags["title"] = _text(frames, "TIT2")
    tags["artist"] = _text(frames, "TPE1")
    tags["album"] = _text(frames, "TALB")
    tags["albumartist"] = _text(frames, "TPE2")
    tags["track"], tags["tracktotal"] = _split_num(_text(frames, "TRCK"))
    tags["disc"], tags["disctotal"] = _split_num(_text(frames, "TPOS"))
    tags["date"] = _text(frames, "TDRC") or _text(frames, "TYER")
    tags["genre"] = _text(frames, "TCON")
    tags["composer"] = _text(frames, "TCOM")
    tags["publisher"] = _text(frames, "TPUB")
    tags["compilation"] = _text(frames, "TCMP")
    # Les commentaires sont indexés par langue (« COMM::eng ») : getall
    # récupère toutes les variantes, on garde la première non vide.
    if frames is not None:
        try:
            for comment in frames.getall("COMM"):
                text = str(comment.text[0]) if comment.text else ""
                if text.strip():
                    tags["comment"] = text
                    break
        except Exception:
            pass


def _read_vorbis(audio, tags):
    """Tags d'un conteneur à champs nommés (FLAC, OGG, Opus)."""
    tags["title"] = _first(audio, "title")
    tags["artist"] = _first(audio, "artist")
    tags["album"] = _first(audio, "album")
    tags["albumartist"] = _first(audio, "albumartist", "album artist")
    tags["track"], tags["tracktotal"] = _split_num(
        _first(audio, "tracknumber", "track"))
    tags["tracktotal"] = tags["tracktotal"] or _first(audio, "tracktotal", "totaltracks")
    tags["disc"], tags["disctotal"] = _split_num(
        _first(audio, "discnumber", "disc"))
    tags["disctotal"] = tags["disctotal"] or _first(audio, "disctotal", "totaldiscs")
    tags["date"] = _first(audio, "date", "year", "originaldate")
    tags["genre"] = _first(audio, "genre")
    tags["composer"] = _first(audio, "composer")
    tags["publisher"] = _first(audio, "organization", "label", "publisher")
    tags["comment"] = _first(audio, "comment", "description")
    tags["compilation"] = _first(audio, "compilation")


def read_tags(path, need_bitrate=True):
    """Dictionnaire de tags d'un fichier audio local (jamais None).

    Les clés sont celles de ``_empty_tags`` ; à défaut de titre, le nom de
    fichier (sans extension) sert de repli, et ``title_tagged`` reste faux
    pour que l'appelant sache que ce titre n'en est pas un. Le résultat est
    mis en cache tant que le fichier n'est pas modifié.
    """
    key = _cache_key(path)
    if key is not None:
        cached = _TAG_CACHE.get(path)
        if cached is not None and cached[0] == key and (
                cached[1] or not need_bitrate):
            return cached[2]

    tags = _empty_tags()
    ext = os.path.splitext(path)[1].lower()

    if config.MUTAGEN_AVAILABLE:
        try:
            if ext == ".mp3":
                _read_mp3(path, need_bitrate, tags)
            elif ext == ".flac":
                audio = config.FLAC(path)
                _read_vorbis(audio, tags)
                if need_bitrate:
                    tags["bitrate"] = _info_bitrate(audio, path)
            else:
                # easy=True expose des champs nommés uniformes (MP4, WMA,
                # OGG...), ce qui évite un cas particulier par conteneur.
                audio = config.mutagen.File(path, easy=True)
                if audio is not None:
                    if audio.tags:
                        _read_vorbis(audio.tags, tags)
                    if need_bitrate:
                        tags["bitrate"] = _info_bitrate(audio, path)
        except Exception:
            pass

    tags["title_tagged"] = bool(tags["title"])
    if not tags["title"]:
        tags["title"] = os.path.splitext(os.path.basename(path))[0]

    if key is not None:
        if len(_TAG_CACHE) >= _TAG_CACHE_MAX:
            _TAG_CACHE.clear()
        _TAG_CACHE[path] = (key, bool(need_bitrate), tags)
    return tags


def read_audio_tags(path):
    """Retourne (titre, artiste, album, débit_kbps) lus depuis un fichier local.

    Conservée pour les appels sur un fichier isolé ; ``read_tags`` donne le
    jeu complet de métadonnées.
    """
    tags = read_tags(path, need_bitrate=True)
    return tags["title"], tags["artist"], tags["album"], tags["bitrate"]


def iter_audio_files(root):
    """Liste les fichiers audio sous ``root`` (récursif), triés par dossier.

    ``os.scandir`` renseigne le type de chaque entrée sans appel système
    supplémentaire : le parcours d'une arborescence de plusieurs milliers de
    fichiers prend quelques dizaines de millisecondes.
    """
    found = []
    stack = [root]
    while stack:
        current = stack.pop()
        dirs, files = [], []
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs.append(entry.path)
                        elif entry.name.lower().endswith(AUDIO_EXTENSIONS):
                            files.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
        found.extend(sorted(files, key=str.lower))
        stack.extend(sorted(dirs, key=str.lower, reverse=True))
    return found


class LocalScanThread(QThread):
    """Parcourt fichiers et dossiers (récursivement) et lit leurs métadonnées.

    ``paths`` peut mélanger des fichiers et des dossiers : chaque dossier est
    exploré récursivement (sous-dossiers inclus) à la recherche de fichiers
    audio. Le nombre total de fichiers est connu dès la fin du parcours
    (signal ``total``), les entrées analysées arrivent par lots (signal
    ``batch``) pour un affichage progressif, et la liste complète est émise
    à la fin via ``scanned``.

    ``need_bitrate=False`` accélère nettement l'analyse quand seul le
    rangement est visé (ni débit ni pochette à décoder).
    """

    total = pyqtSignal(int)             # nombre de fichiers à analyser
    progress = pyqtSignal(int)          # nombre de fichiers analysés
    batch = pyqtSignal(list)            # lot d'entrées prêtes
    scanned = pyqtSignal(list)          # liste finale de dicts

    BATCH_SIZE = 200

    def __init__(self, paths, need_bitrate=True):
        super().__init__()
        self.paths = paths
        self.need_bitrate = need_bitrate
        self._running = True

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    def _collect(self):
        """Chemins de tous les fichiers audio désignés par ``self.paths``."""
        files = []
        for entry in self.paths:
            if not self._running:
                break
            if os.path.isdir(entry):
                files.extend(iter_audio_files(entry))
            elif os.path.isfile(entry) and entry.lower().endswith(AUDIO_EXTENSIONS):
                files.append(entry)
        return files

    def _read_one(self, path):
        """Entrée complète d'un fichier (exécutée dans le pool de threads)."""
        if not self._running:
            return None
        tags = read_tags(path, need_bitrate=self.need_bitrate)
        entry = dict(tags)
        entry["path"] = path
        entry["format"] = os.path.splitext(path)[1].lower().lstrip(".").upper()
        return entry

    def run(self):
        files = self._collect()
        self.total.emit(len(files))

        results, pending = [], []
        executor = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
        try:
            # map conserve l'ordre des chemins : l'arbre se remplit dossier
            # par dossier, dans l'ordre alphabétique du parcours.
            for entry in executor.map(self._read_one, files):
                if not self._running:
                    break
                if entry is None:
                    continue
                results.append(entry)
                pending.append(entry)
                if len(pending) >= self.BATCH_SIZE:
                    self.batch.emit(pending)
                    self.progress.emit(len(results))
                    pending = []
        finally:
            # cancel_futures : à l'arrêt, les lectures non commencées sont
            # abandonnées au lieu d'être attendues une à une.
            executor.shutdown(wait=False, cancel_futures=True)

        if pending and self._running:
            self.batch.emit(pending)
        self.progress.emit(len(results))
        self.scanned.emit(results)
