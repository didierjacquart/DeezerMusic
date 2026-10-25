"""
Constitution de la base DeeKeep : collecte, remplissage du cache local,
envoi vers la base en ligne.

Ce module rassemble en un seul jeu de tables tout ce que l'application
connaissait jusqu'ici sous forme de fichiers JSON épars, et l'enrichit de
ce que seul le disque sait :

    deekeep_config.json   paramètres, dossier de dépôt, filtres et motif
                          de renommage, historique des recherches,
                          historique des téléchargements (avec jaquettes) ;
    quality_cache.json    mesures de l'écran « Qualité » ;
    reference_cache.json  durées de référence Deezer / MusicBrainz ;
    le disque              tags, jaquettes intégrées, débit, durée, taille
                          et date de chaque fichier audio trouvé.

Utilisation en ligne de commande :

    python -m deekeep.db.db_init                     # collecte + cache local
    python -m deekeep.db.db_init --push              # ... puis envoi en ligne
    python -m deekeep.db.db_init --root D:/ --push
    python -m deekeep.db.db_init --status            # état de la synchronisation
    python -m deekeep.db.db_init --no-scan --push    # sans reparcourir le disque

Rien n'est détruit : le cache est rempli par ``upsert``, les fichiers
JSON d'origine restent en place, et la base en ligne reçoit les lignes
sans être vidée au préalable.
"""

import argparse
import hashlib
import json
import os
import sys
import time

from deekeep.db import cloud_db
from deekeep.db import db_schema
from deekeep.db import local_db
from deekeep.core import settings as app_settings
from deekeep.db import sync_service


# Extensions parcourues. Reprise de ``reencode_scan.AUDIO_EXTENSIONS`` mais
# redéfinie ici pour que le script tourne sans charger l'interface Qt.
AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
                    ".wav", ".wma", ".alac", ".aiff", ".aif")

# Clés de configuration qui ne sont pas des paramètres : elles ont leur
# propre table et n'ont donc rien à faire dans « app_settings ».
NON_SETTING_KEYS = {"search_history", "downloaded_history"}

# Nombre de doublons rencontrés dans l'historique, pour information.
# L'historique JSON peut contenir deux fois le même fichier — un titre
# retéléchargé y était simplement ajouté une seconde fois. La table des
# titres est indexée par chemin : ces répétitions se fondent en une seule
# ligne, celle de la première occurrence, dont le rang est conservé.


# Secrets. Ils restent dans le fichier local par défaut : un jeton Deezer
# ou une clé AcoustID n'ont pas à voyager vers un service tiers sans que
# l'utilisateur l'ait demandé (paramètre « cloud_sync_secrets »).
SECRET_KEYS = {"arl", "acoustid_key"}


# ----------------------------------------------------------------------
# Outils
# ----------------------------------------------------------------------

def canonical_path(path):
    """Chemin normalisé conservant la casse d'origine (clé de la table)."""
    return os.path.normpath(str(path or "").replace("/", os.sep))


def match_key(path):
    """Clé de rapprochement insensible à la casse (Windows oblige)."""
    return os.path.normcase(canonical_path(path))


def image_info(data):
    """
    (type MIME, largeur, hauteur) d'une image, lus dans son en-tête.

    Écrit à la main pour ne pas ajouter de dépendance : Pillow n'est pas
    requis par l'application, et charger Qt dans un script en ligne de
    commande serait disproportionné. Une image d'un format inconnu n'est
    pas rejetée — elle est simplement stockée sans dimensions.
    """
    if not data:
        return ("", 0, 0)
    head = bytes(data[:32])

    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return ("image/png", width, height)

    if head.startswith(b"GIF8") and len(data) >= 10:
        return ("image/gif",
                int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"))

    if head.startswith(b"BM") and len(data) >= 26:
        return ("image/bmp",
                int.from_bytes(data[18:22], "little"),
                int.from_bytes(data[22:26], "little"))

    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ("image/webp", 0, 0)

    if head.startswith(b"\xff\xd8"):
        # JPEG : on saute de marqueur en marqueur jusqu'à un en-tête de
        # trame (SOF0..SOF15, hors marqueurs de restart et DHT/DAC), qui
        # porte les dimensions réelles.
        index, end = 2, len(data)
        while index + 9 < end:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = int.from_bytes(data[index + 2:index + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[index + 5:index + 7], "big")
                width = int.from_bytes(data[index + 7:index + 9], "big")
                return ("image/jpeg", width, height)
            if length <= 0:
                break
            index += 2 + length
        return ("image/jpeg", 0, 0)

    return ("application/octet-stream", 0, 0)


class CoverStore:
    """
    Jaquettes rencontrées, dédoublonnées par empreinte SHA-1.

    Les titres d'un même album partagent la même image : sur la
    bibliothèque de référence, 3 329 fichiers ne portent que 1 461 images
    distinctes, soit 105 Mo au lieu de 198.
    """

    def __init__(self):
        self.rows = {}

    def add(self, data):
        """Enregistre une image et rend son empreinte (None si vide)."""
        if not data:
            return None
        data = bytes(data)
        sha1 = hashlib.sha1(data).hexdigest()
        if sha1 not in self.rows:
            mime, width, height = image_info(data)
            self.rows[sha1] = {
                "sha1": sha1, "mime": mime, "bytes": len(data),
                "width": width, "height": height, "data": data,
            }
        return sha1

    def add_base64(self, text):
        """Variante pour les jaquettes de l'historique, stockées en base64."""
        if not text:
            return None
        import base64
        try:
            return self.add(base64.b64decode(text))
        except (ValueError, TypeError):
            return None

    def total_bytes(self):
        return sum(row["bytes"] for row in self.rows.values())


# ----------------------------------------------------------------------
# Lecture des tags et des jaquettes
# ----------------------------------------------------------------------

def _mutagen():
    try:
        import mutagen
        return mutagen
    except ImportError:
        return None


def _first_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip()


def read_file_metadata(path):
    """
    Tags, caractéristiques d'encodage et jaquette d'un fichier audio.

    Une seule ouverture du fichier sert aux trois : rouvrir trois fois
    3 300 fichiers coûterait plusieurs minutes de plus.
    """
    info = {
        "title": "", "artist": "", "album": "", "album_artist": "",
        "genre": "", "year": "", "track_no": "",
        "duration": None, "bitrate": None, "sample_rate": None,
        "channels": None, "codec": "",
    }
    cover = None

    mutagen = _mutagen()
    if mutagen is None:
        return info, cover

    try:
        audio = mutagen.File(path)
    except Exception:
        audio = None

    if audio is not None:
        stream = getattr(audio, "info", None)
        if stream is not None:
            info["duration"] = _round(getattr(stream, "length", None), 3)
            bitrate = getattr(stream, "bitrate", None)
            info["bitrate"] = int(bitrate // 1000) if bitrate else None
            info["sample_rate"] = getattr(stream, "sample_rate", None)
            info["channels"] = getattr(stream, "channels", None)
            info["codec"] = type(stream).__module__.rsplit(".", 1)[-1]

        tags = getattr(audio, "tags", None)
        if tags is not None:
            _read_tags(tags, info)
            cover = _read_cover(audio, tags)

    if not info["title"]:
        info["title"] = os.path.splitext(os.path.basename(path))[0]

    return info, cover


# Correspondance des noms de champs selon le conteneur. ID3 (MP3) utilise
# des trames à quatre lettres, MP4 des atomes, les formats Vorbis des noms
# en clair : les trois listes sont essayées dans l'ordre.
_TAG_KEYS = {
    "title": ("TIT2", "\xa9nam", "title"),
    "artist": ("TPE1", "\xa9ART", "artist"),
    "album": ("TALB", "\xa9alb", "album"),
    "album_artist": ("TPE2", "aART", "albumartist", "album artist"),
    "genre": ("TCON", "\xa9gen", "genre"),
    "year": ("TDRC", "TYER", "\xa9day", "date", "year", "originaldate"),
    "track_no": ("TRCK", "trkn", "tracknumber", "track"),
}


def _read_tags(tags, info):
    for field, keys in _TAG_KEYS.items():
        for key in keys:
            try:
                if key not in tags:
                    continue
                value = tags[key]
            except Exception:
                continue
            # Les atomes MP4 « trkn » sont des couples (piste, total).
            if isinstance(value, list) and value and isinstance(value[0], tuple):
                value = value[0][0]
            text = _first_text(getattr(value, "text", value))
            if text:
                info[field] = text
                break


def _read_cover(audio, tags):
    """Jaquette intégrée : première image trouvée, la face avant d'abord."""
    # FLAC et quelques autres exposent directement une liste d'images.
    pictures = getattr(audio, "pictures", None)
    if pictures:
        front = next((pic for pic in pictures if getattr(pic, "type", 0) == 3),
                     pictures[0])
        return bytes(front.data)

    # ID3 : trames APIC, type 3 = face avant.
    getall = getattr(tags, "getall", None)
    if getall is not None:
        try:
            frames = getall("APIC")
        except Exception:
            frames = []
        if frames:
            front = next((f for f in frames if getattr(f, "type", 0) == 3),
                         frames[0])
            return bytes(front.data)

    # MP4 / M4A : atome « covr ».
    try:
        if "covr" in tags:
            return bytes(tags["covr"][0])
    except Exception:
        pass

    # Vorbis : image encodée en base64 dans un champ texte.
    try:
        if "metadata_block_picture" in tags:
            import base64
            from mutagen.flac import Picture
            raw = tags["metadata_block_picture"]
            raw = raw[0] if isinstance(raw, list) else raw
            return bytes(Picture(base64.b64decode(raw)).data)
    except Exception:
        pass

    return None


def _round(value, digits):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Parcours du disque
# ----------------------------------------------------------------------

def iter_audio_files(root):
    """Fichiers audio sous ``root``, récursivement, dossiers système exclus."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # « $RECYCLE.BIN », « System Volume Information » : rien à y lire,
        # et le second refuse l'accès.
        dirnames[:] = [name for name in dirnames
                       if not name.startswith("$")
                       and name.lower() != "system volume information"]
        for name in filenames:
            if name.lower().endswith(AUDIO_EXTENSIONS):
                found.append(os.path.join(dirpath, name))
    return found


def scan_library(roots, covers, progress=None):
    """
    Relève les fichiers audio des dossiers donnés.

    Renvoie un dictionnaire clé_de_rapprochement -> ligne de la table
    ``tracks``, jaquettes rangées au passage dans ``covers``.
    """
    tracks = {}
    files = []
    for root in roots:
        if root and os.path.isdir(root):
            files.extend(iter_audio_files(root))

    total = len(files)
    for index, path in enumerate(files, 1):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        info, cover = read_file_metadata(path)
        canonical = canonical_path(path)
        tracks[match_key(path)] = {
            "path": canonical,
            "title": info["title"],
            "artist": info["artist"],
            "album": info["album"],
            "album_artist": info["album_artist"],
            "genre": info["genre"],
            "year": info["year"],
            "track_no": info["track_no"],
            "duration": info["duration"],
            "bitrate": info["bitrate"],
            "sample_rate": info["sample_rate"],
            "channels": info["channels"],
            "codec": info["codec"],
            "ext": os.path.splitext(path)[1].lower().lstrip("."),
            "file_size": stat.st_size,
            "file_mtime": round(stat.st_mtime, 3),
            "cover_sha1": covers.add(cover),
            "source": "library",
            "in_history": 0,
            "history_pos": None,
            "history_cover_sha1": None,
        }
        if progress is not None and (index % 100 == 0 or index == total):
            progress(index, total)
    return tracks


# ----------------------------------------------------------------------
# Lecture des fichiers JSON existants
# ----------------------------------------------------------------------

def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"[db_init] {path} illisible : {exc}")
        return default


def collect_settings(config, include_secrets=False):
    """Paramètres à enregistrer, historiques et secrets écartés."""
    result = {}
    for key, value in config.items():
        if key in NON_SETTING_KEYS:
            continue
        if key in SECRET_KEYS and not include_secrets:
            continue
        result[key] = value
    # Les valeurs par défaut absentes du fichier sont ajoutées : la base
    # doit décrire l'application entière, pas seulement ce que
    # l'utilisateur a modifié.
    for key, value in app_settings.default_settings().items():
        if key in NON_SETTING_KEYS or key in SECRET_KEYS:
            continue
        result.setdefault(key, value)
    # Seuils de l'écran « Qualité » : ils vivent dans la configuration
    # sous le préfixe « qual_ », avec les valeurs par défaut du module.
    try:
        from deekeep.audio import quality_scan
        for key, value in quality_scan.DEFAULT_THRESHOLDS.items():
            result.setdefault("qual_" + key, value)
        result.setdefault("qual_deep", True)
    except Exception:
        pass
    return result


def iter_history(config):
    """
    Entrées de l'historique, dédoublonnées par chemin, rang conservé.

    Rend des couples (rang d'origine, entrée). Le rang n'est pas
    renuméroté : il sert à restituer l'ordre d'affichage, et un trou de
    six positions sur cinq cent quatre-vingts est sans conséquence.
    """
    seen = set()
    for position, entry in enumerate(config.get("downloaded_history") or []):
        if not isinstance(entry, dict):
            continue
        key = match_key(entry.get("path") or "")
        if key in seen:
            continue
        seen.add(key)
        yield position, entry


# Tags complémentaires notés dans l'historique par le téléchargement, sous
# le vocabulaire des tags (voir ``tag_write.FIELDS``), et la colonne de la
# table « tracks » qui leur correspond.
_HISTORY_TAG_COLUMNS = {
    "albumartist": "album_artist",
    "genre": "genre",
    "date": "year",
}


def history_tags(entry):
    """Tags complémentaires d'une entrée d'historique, en colonnes de table.

    Les téléchargements récents y notent ce que le catalogue a rapporté : de
    quoi renseigner une ligne dont le fichier a disparu du disque, qu'aucune
    lecture de tags ne peut plus atteindre.
    """
    tags = entry.get("tags")
    if not isinstance(tags, dict):
        return {}
    values = {}
    for field, column in _HISTORY_TAG_COLUMNS.items():
        text = str(tags.get(field) or "").strip()
        if text:
            values[column] = text
    # Le numéro de piste se note « 5/15 » dans les fichiers, comme dans les
    # tags relus : le rang seul si le total est inconnu.
    number = str(tags.get("track") or "").strip()
    total = str(tags.get("tracktotal") or "").strip()
    if number:
        values["track_no"] = f"{number}/{total}" if total else number
    return values


def merge_history(config, tracks, covers):
    """
    Fond l'historique des téléchargements dans la table des titres.

    Un fichier retrouvé sur le disque garde ses tags lus sur place — plus
    fiables que ceux notés au téléchargement — et reçoit en plus son rang
    dans l'historique et la jaquette qui y était stockée. Une entrée dont
    le fichier a disparu forme une ligne à part (source = « history ») :
    l'historique doit rester consultable même après un ménage sur le
    disque — mais on tente d'abord de retrouver le fichier sous son nom,
    la bibliothèque ayant pu être déplacée depuis le téléchargement.
    """
    # Rattrapage des chemins périmés. Un déplacement de la bibliothèque
    # — de « D:\Musique\… » vers « D:\… », par exemple — laisse tout
    # l'historique pointant dans le vide. Le nom de fichier, lui, n'a pas
    # bougé : quand il ne désigne qu'un seul fichier relevé sur le disque,
    # l'entrée y est rattachée plutôt que de former une ligne orpheline.
    by_name = {}
    for row in tracks.values():
        by_name.setdefault(os.path.basename(row["path"]).lower(), []).append(row)

    # Chemins déjà attribués au cours de ce passage. C'est bien ce
    # décompte-là, et non le drapeau ``in_history`` de la ligne, qui garde
    # deux entrées de même nom de fichier de se disputer une ligne : sur un
    # cache déjà rempli, toutes les lignes rattachées portent ce drapeau, et
    # s'y fier recréerait les orphelines à chaque passage.
    claimed = set()

    added = 0
    for position, entry in iter_history(config):
        path = entry.get("path") or ""
        key = match_key(path)
        cover_sha1 = covers.add_base64(entry.get("cover_b64"))
        row = tracks.get(key)
        if row is None:
            free = [item for item
                    in by_name.get(
                        os.path.basename(canonical_path(path)).lower(), [])
                    if match_key(item["path"]) not in claimed]
            if len(free) == 1:
                row = free[0]
        if row is not None:
            claimed.add(match_key(row["path"]))
        if row is None:
            tracks[key] = {
                "path": canonical_path(path),
                "title": entry.get("title") or "",
                "artist": entry.get("artist") or "",
                "album": entry.get("album") or "",
                # Complétés juste après par ce que l'historique en sait : ces
                # lignes n'ont plus de fichier où lire des tags.
                "album_artist": "", "genre": "", "year": "", "track_no": "",
                "duration": None, "bitrate": None, "sample_rate": None,
                "channels": None, "codec": "",
                "ext": os.path.splitext(path)[1].lower().lstrip("."),
                "file_size": None, "file_mtime": None,
                "cover_sha1": cover_sha1,
                "source": "history",
                "in_history": 1,
                "history_pos": position,
                "history_cover_sha1": cover_sha1,
            }
            tracks[key].update(history_tags(entry))
            added += 1
        else:
            row["source"] = "both"
            row["in_history"] = 1
            row["history_pos"] = position
            row["history_cover_sha1"] = cover_sha1
            # Un fichier sans jaquette intégrée hérite de celle de
            # l'historique : c'est la même image, elle a juste été perdue
            # au réencodage.
            if not row.get("cover_sha1"):
                row["cover_sha1"] = cover_sha1
            for field, source in (("title", "title"), ("artist", "artist"),
                                  ("album", "album")):
                if not row.get(field) and entry.get(source):
                    row[field] = entry[source]
            # Même règle pour les tags complémentaires : ceux lus sur le
            # fichier font foi, l'historique ne comble que ce qui manque.
            for column, value in history_tags(entry).items():
                if not row.get(column):
                    row[column] = value
    return added


def collect_searches(config):
    """Historique des recherches, ordre conservé."""
    rows, seen = [], set()
    for position, query in enumerate(config.get("search_history") or []):
        text = str(query or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        rows.append({"query": text, "position": position})
    return rows


def collect_quality(tracks, settings_map, references):
    """
    Mesures de l'écran « Qualité », avec leur diagnostic.

    Le cache indexe ses entrées par chemin en minuscules : on les
    rapproche des titres relevés pour retrouver le chemin réel, faute de
    quoi la table ``quality`` ne se joindrait pas à ``tracks``.
    """
    path = os.path.join(app_settings.data_dir(), "quality_cache.json")
    data = read_json(path, {}) or {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return []

    thresholds = {}
    try:
        from deekeep.audio import quality_scan
        for key in quality_scan.DEFAULT_THRESHOLDS:
            value = settings_map.get("qual_" + key)
            if value is not None:
                thresholds[key] = value
    except Exception:
        quality_scan = None

    rows = []
    for cache_key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        measures = entry.get("measures")
        if not isinstance(measures, dict):
            continue
        track = tracks.get(match_key(cache_key))
        real_path = track["path"] if track else canonical_path(cache_key)

        defects = []
        if quality_scan is not None:
            try:
                decoded = quality_scan._decode_measures(measures)
                reference = _reference_for(track, references)
                verdict = quality_scan.evaluate(decoded, thresholds, reference)
                defects = list(verdict[0]) if isinstance(verdict, tuple) else list(verdict)
            except Exception:
                defects = []

        rows.append({
            "path": real_path,
            "stamp": entry.get("stamp") or "",
            "measures": db_schema.json_dump(measures),
            "defects": db_schema.json_dump(defects),
        })
    return rows


def _reference_for(track, references):
    """Durée de référence d'un titre, si elle est connue."""
    if not track or not references:
        return None
    key = "%s|%s" % (str(track.get("artist") or "").strip().lower(),
                     str(track.get("title") or "").strip().lower())
    durations = references.get(key)
    if not durations:
        return None
    ordered = sorted(durations)
    return ordered[len(ordered) // 2]


def collect_references():
    """Durées de référence Deezer / MusicBrainz déjà relevées."""
    path = os.path.join(app_settings.data_dir(), "reference_cache.json")
    data = read_json(path, {}) or {}
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return [], {}

    rows, by_key = [], {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        durations = entry.get("durations") or []
        by_key[key] = durations
        rows.append({
            "key": key,
            "durations": db_schema.json_dump(durations),
            "fetched_at": _round(entry.get("time"), 3),
        })
    return rows, by_key


# ----------------------------------------------------------------------
# Remplissage du cache local
# ----------------------------------------------------------------------

def build_local(roots=None, scan=True, include_secrets=False, verbose=True,
                db=None):
    """
    Collecte tout et remplit le cache local. Renvoie un bilan chiffré.

    Les lignes sont mises en file d'attente d'envoi : le premier
    rapprochement avec la base en ligne les y déposera.
    """
    def say(message):
        if verbose:
            print(message, flush=True)

    started = time.time()
    db = db or local_db.shared()
    config = read_json(app_settings.config_path(), {}) or {}

    settings_map = collect_settings(config, include_secrets=include_secrets)
    if roots is None:
        roots = [settings_map.get("download_dir") or ""]
    roots = [root for root in roots if root]

    covers = CoverStore()

    tracks = {}
    if scan:
        say(f"Parcours de {', '.join(roots)} …")

        def progress(index, total):
            say(f"  {index}/{total} fichiers lus")

        tracks = scan_library(roots, covers, progress=progress)
        say(f"  {len(tracks)} fichiers audio relevés")
    else:
        # Sans parcours, on repart des titres déjà en cache pour ne pas
        # perdre ce qui y est (les jaquettes restent en base, inutile de
        # les relire).
        for row in db.rows("tracks"):
            tracks[match_key(row["path"])] = {
                name: row.get(name)
                for name in db_schema.TABLES_BY_NAME["tracks"].content_columns
            }
        say(f"  {len(tracks)} titres repris du cache")

    raw_history = len(config.get("downloaded_history") or [])
    added = merge_history(config, tracks, covers)
    kept = sum(1 for _ in iter_history(config))
    say(f"Historique : {raw_history} entrées ({kept} chemins distincts), "
        f"dont {added} sans fichier sur le disque")

    reference_rows, references = collect_references()
    quality_rows = collect_quality(tracks, settings_map, references)
    search_rows = collect_searches(config)

    say(f"Jaquettes : {len(covers.rows)} images distinctes, "
        f"{covers.total_bytes() / 1e6:.1f} Mo")

    # Écriture. L'ordre suit celui du schéma : les jaquettes avant les
    # titres qui les désignent.
    db.set_settings(settings_map)
    if search_rows:
        db.upsert_many("searches", search_rows)
    if covers.rows:
        _write_in_batches(db, "covers", list(covers.rows.values()), say)
    if tracks:
        _write_in_batches(db, "tracks", list(tracks.values()), say)
    if quality_rows:
        _write_in_batches(db, "quality", quality_rows, say)
    if reference_rows:
        db.upsert_many("reference_durations", reference_rows)

    # Le bilan compte les lignes réellement en base, et non celles que ce
    # passage a écrites : sans parcours du disque, les jaquettes déjà
    # stockées ne repassent pas par ``covers`` et le compte paraîtrait
    # s'être effondré.
    summary = {name: db.count(name) for name in
               ("app_settings", "searches", "covers", "tracks", "quality",
                "archive", "reference_durations")}
    summary.update({
        "written_covers": len(covers.rows),
        "cover_bytes": db.query(
            "SELECT COALESCE(SUM(bytes), 0) AS s FROM covers "
            "WHERE deleted = 0")[0]["s"],
        "history": db.query(
            "SELECT COUNT(*) AS n FROM tracks WHERE in_history = 1 "
            "AND deleted = 0")[0]["n"],
        "pending": db.pending_count(),
        "cache_bytes": db.size_bytes(),
        "seconds": round(time.time() - started, 1),
    })
    say("Cache local : "
        + ", ".join(f"{summary[key]} {key}" for key in
                    ("app_settings", "searches", "covers", "tracks", "quality",
                     "archive", "reference_durations")))
    say(f"Fichier {db.path} — {summary['cache_bytes'] / 1e6:.1f} Mo, "
        f"en {summary['seconds']} s")
    return summary


def _write_in_batches(db, table, rows, say, size=500):
    """Écrit par tranches, pour ne pas tenir une transaction géante."""
    for start in range(0, len(rows), size):
        db.upsert_many(table, rows[start:start + size])
    say(f"  {table} : {len(rows)} lignes écrites")


# ----------------------------------------------------------------------
# Report de l'état courant de l'application
# ----------------------------------------------------------------------

def mirror_state(db, config, include_secrets=False):
    """
    Reporte dans le cache local l'état de l'application en mémoire.

    Appelée à chaque enregistrement de la configuration : c'est elle qui
    fait de la base la copie fidèle de ce que l'utilisateur voit, sans
    attendre une réinitialisation complète. Le parcours du disque, lui,
    n'est pas refait ici — il est trop long pour un enregistrement de
    paramètre et se déclenche à la demande (``build_local``).

    Renvoie le nombre de lignes touchées, pour que l'appelant sache s'il
    y a lieu de déclencher une synchronisation.
    """
    touched = 0
    touched += db.set_settings(collect_settings(config, include_secrets))
    touched += _mirror_searches(db, config)
    touched += _mirror_history(db, config)
    return touched


def _mirror_searches(db, config):
    """Aligne la table des recherches sur la liste affichée."""
    wanted = {row["query"]: row for row in collect_searches(config)}
    existing = {row["query"]: row for row in db.rows("searches")}

    changed = [row for query, row in wanted.items()
               if query not in existing
               or existing[query].get("position") != row["position"]]
    gone = [query for query in existing if query not in wanted]

    if changed:
        db.upsert_many("searches", changed)
    if gone:
        db.mark_deleted("searches", gone)
    return len(changed) + len(gone)


def _mirror_history(db, config):
    """
    Aligne la table des titres sur l'historique des téléchargements.

    Seules les entrées nouvelles ou déplacées sont traitées : décoder et
    hacher la jaquette de six cents entrées à chaque enregistrement de
    paramètre serait du travail perdu.
    """
    covers = CoverStore()
    rows, seen = [], set()

    # Index des noms de fichiers déjà en base, pour rattacher une entrée
    # dont le chemin a vieilli (bibliothèque déplacée). Même règle que dans
    # ``merge_history`` : sans elle, chaque démarrage recréerait une ligne
    # orpheline par entrée périmée.
    by_name = {}
    for row in db.query("SELECT path FROM tracks WHERE deleted = 0"):
        by_name.setdefault(os.path.basename(row["path"]).lower(), []).append(
            row["path"])

    # Chemins déjà attribués au cours de ce passage : deux entrées de
    # l'historique portant le même nom de fichier dans deux dossiers
    # différents ne doivent pas se disputer la même ligne.
    claimed = set()

    for position, entry in iter_history(config):
        raw_path = entry.get("path") or ""
        seen.add(match_key(raw_path))
        current = db.get_row("tracks", canonical_path(raw_path))
        if current is None:
            free = [path for path
                    in by_name.get(
                        os.path.basename(canonical_path(raw_path)).lower(), [])
                    if match_key(path) not in claimed]
            if len(free) == 1:
                current = db.get_row("tracks", free[0])

        if current is not None:
            claimed.add(match_key(current["path"]))
            seen.add(match_key(current["path"]))

        # Une ligne déjà en place et au bon rang est laissée telle quelle.
        # On ne compare pas les tags : ceux lus dans le fichier sont plus
        # sûrs que ceux notés au téléchargement, et les réécrire à chaque
        # passage relancerait une synchronisation sans fin.
        if (current and not current.get("deleted")
                and current.get("in_history") == 1
                and current.get("history_pos") == position
                and current.get("history_cover_sha1")):
            continue

        cover_sha1 = covers.add_base64(entry.get("cover_b64"))
        row = dict(current) if current else {}
        # Tags complémentaires notés au téléchargement : même règle que les
        # trois de base, ils ne comblent que ce qui manque (voir
        # ``history_tags``).
        for column, value in history_tags(entry).items():
            if not row.get(column):
                row[column] = value
        row.update({
            # Le chemin retenu est celui de la ligne rattachée, jamais le
            # chemin périmé de l'historique.
            "path": current["path"] if current else canonical_path(raw_path),
            # Les tags du fichier gardent la main ; ceux de l'historique ne
            # servent qu'à combler un vide.
            "title": row.get("title") or entry.get("title") or "",
            "artist": row.get("artist") or entry.get("artist") or "",
            "album": row.get("album") or entry.get("album") or "",
            "in_history": 1,
            "history_pos": position,
            "history_cover_sha1": cover_sha1 or row.get("history_cover_sha1"),
            "deleted": 0,
        })
        row.setdefault("source", "history")
        if row.get("source") == "history" and os.path.exists(row["path"]):
            row["source"] = "both"
        if not row.get("cover_sha1"):
            row["cover_sha1"] = cover_sha1
        # Colonnes techniques : recalculées par ``upsert``.
        for name in ("updated_at", "h"):
            row.pop(name, None)
        rows.append(row)

    # Entrées retirées de l'historique (purge, vidage). Le fichier peut
    # très bien rester sur le disque : on lève seulement le drapeau, et on
    # ne pose une pierre tombale que si le titre n'était connu que par
    # l'historique.
    dropped = []
    for row in db.query("SELECT * FROM tracks WHERE in_history = 1 "
                        "AND deleted = 0"):
        row = dict(row)
        if match_key(row["path"]) in seen:
            continue
        if row.get("source") == "history" and not os.path.exists(row["path"]):
            dropped.append(row["path"])
            continue
        updated = {name: row.get(name) for name in
                   db_schema.TABLES_BY_NAME["tracks"].content_columns}
        updated.update({"in_history": 0, "history_pos": None,
                        "source": "library"})
        rows.append(updated)

    if covers.rows:
        db.upsert_many("covers", list(covers.rows.values()))
    if rows:
        db.upsert_many("tracks", rows)
    if dropped:
        db.mark_deleted("tracks", dropped)
    return len(rows) + len(dropped) + len(covers.rows)


# ----------------------------------------------------------------------
# Ligne de commande
# ----------------------------------------------------------------------

def print_status(manager):
    report = manager.analyze()
    print(f"État : {report.message}")
    for name, table in report.tables.items():
        print(f"  {name:20s} local={table.local} distant={table.remote} "
              f"référence={table.synced} -> {table.verdict}")
    if report.status == sync_service.CONFLICT:
        print("\nDésynchronisation. Choisir un sens :")
        print("  python -m deekeep.db.db_init --resolve local"
              "    (le cache local fait foi)")
        print("  python -m deekeep.db.db_init --resolve remote"
              "   (la base en ligne fait foi)")
    return report


def main(argv=None):
    # La console Windows travaille en cp1252 : un caractère hors de cette
    # page — puce, flèche — y ferait échouer le print, et donc le script,
    # après un envoi pourtant réussi.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    parser = argparse.ArgumentParser(
        description="Initialise et synchronise la base DeeKeep."
    )
    parser.add_argument("--root", action="append", default=None,
                        help="dossier à parcourir (répétable ; par défaut, "
                             "le dossier de dépôt des paramètres)")
    parser.add_argument("--no-scan", action="store_true",
                        help="ne pas reparcourir le disque")
    parser.add_argument("--push", action="store_true",
                        help="envoyer le cache vers la base en ligne")
    parser.add_argument("--status", action="store_true",
                        help="afficher l'état de synchronisation et sortir")
    parser.add_argument("--resolve", choices=("local", "remote"),
                        help="trancher une désynchronisation")
    parser.add_argument("--table", action="append",
                        choices=tuple(db_schema.TABLE_ORDER),
                        help="restreindre --resolve à cette table (répétable ; "
                             "évite de retransférer les jaquettes quand seule "
                             "une autre table est à réaligner)")
    parser.add_argument("--url", help="URL de la base en ligne (enregistrée)")
    parser.add_argument("--token", help="jeton d'accès (enregistré)")
    parser.add_argument("--secrets", action="store_true",
                        help="inclure la clé AcoustID et le jeton Deezer")
    parser.add_argument("--reset", action="store_true",
                        help="vider le cache local avant de le reconstruire "
                             "(les données restent dans les fichiers JSON et "
                             "sur le disque)")
    args = parser.parse_args(argv)

    if args.url or args.token:
        current = cloud_db.load_credentials()
        cloud_db.save_credentials(args.url or current["url"],
                                  args.token or current["token"])
        print(f"Identifiants enregistrés dans {cloud_db.credentials_path()}")

    db = local_db.shared()
    manager = sync_service.SyncManager(local=db)

    if args.status:
        print_status(manager)
        return 0

    if args.resolve:
        if not manager.is_configured():
            print("Aucune base en ligne configurée.")
            return 2
        report = manager.resolve(args.resolve, tables=args.table)
        target = ", ".join(args.table) if args.table else "toutes les tables"
        print(f"Résolution « {args.resolve} » sur {target} : {report.summary()}")
        print_status(manager)
        return 0

    if args.reset:
        print("Cache local vidé.")
        db.reset_data()

    build_local(roots=args.root, scan=not args.no_scan,
                include_secrets=args.secrets, db=db)

    if not args.push:
        print("\nCache local prêt. Ajoutez --push (avec --url et --token la "
              "première fois) pour l'envoyer vers la base en ligne.")
        return 0

    if not manager.is_configured():
        print("\nAucune base en ligne configurée : passez --url et --token.")
        return 2

    print("\nCréation du schéma distant …")
    manager.ensure_remote_schema()

    print("Envoi …")
    total = db.pending_count()
    last = [0]

    def progress(done):
        # Une ligne toutes les 200 pour ne pas noyer la console.
        if done - last[0] >= 200 or done >= total:
            last[0] = done
            print(f"  {done}/{total} lignes", flush=True)

    report = manager.sync(progress=progress)
    print(f"\n{report.message} — {report.summary()}")
    print_status(manager)
    return 0 if report.status in (sync_service.IN_SYNC,) else 1


if __name__ == "__main__":
    sys.exit(main())
