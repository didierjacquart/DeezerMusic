"""
Écriture des tags dans un fichier audio.

Pendant de ``reencode_scan.read_tags`` : le vocabulaire est le même — les
clés de ``name_pattern.TOKENS`` — et c'est ici, en un seul endroit, que se
fait la traduction vers les trames du conteneur. Trois écrivains s'en
servent :

    - le téléchargement et le réencodage, qui posent les tags du fichier
      qu'ils produisent (``download_thread.apply_metadata``) ;
    - la recherche des tags manquants de l'écran « Organiser »
      (``tag_lookup.write_tags``) ;
    - toute reprise ultérieure : un champ ajouté ici est écrit par les deux.

Deux précautions gouvernent l'écriture :

    - seuls les champs fournis sont touchés. Le reste du fichier — les
      autres tags, la jaquette, le flux audio — n'est pas réécrit ;
    - les champs jumelés (« 3/12 ») sont fusionnés avec ce que le fichier
      porte déjà. Écrire le seul nombre total de pistes ne doit pas effacer
      le numéro de piste qui l'accompagnait, ni l'inverse.
"""

import os

from deekeep.core import config


# Champs écrivables, dans le vocabulaire de ``read_tags`` / ``name_pattern``.
FIELDS = ("title", "artist", "album", "albumartist", "genre", "date",
          "track", "tracktotal", "disc", "disctotal", "publisher")

# ID3 (MP3) : une trame par champ simple.
_ID3_FRAMES = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "albumartist": "TPE2",
    "genre": "TCON",
    "date": "TDRC",
    "publisher": "TPUB",
}

# Champs jumelés dans une seule trame ID3, sous la forme « numéro/total ».
_ID3_PAIRS = {
    "TRCK": ("track", "tracktotal"),
    "TPOS": ("disc", "disctotal"),
}

# Conteneurs à champs nommés (FLAC, OGG, Opus) : noms Vorbis usuels, ceux
# que ``reencode_scan._read_vorbis`` relit.
_VORBIS_KEYS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "albumartist": "albumartist",
    "genre": "genre",
    "date": "date",
    "track": "tracknumber",
    "tracktotal": "tracktotal",
    "disc": "discnumber",
    "disctotal": "disctotal",
    "publisher": "organization",
}

# Interface « easy » de mutagen (MP4, WMA, ASF…) : elle n'expose qu'un jeu
# de champs restreint, les autres y sont refusés.
_EASY_KEYS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "albumartist": "albumartist",
    "genre": "genre",
    "date": "date",
    "track": "tracknumber",
    "disc": "discnumber",
}


def clean_values(values):
    """Champs connus et non vides de ``values``, valeurs en texte."""
    cleaned = {}
    for field in FIELDS:
        value = values.get(field)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            cleaned[field] = text
    return cleaned


def _pair_text(frames, frame_name, values):
    """Valeur « numéro/total » d'une trame jumelée, ou None si rien à écrire.

    L'ancienne valeur sert de base : renseigner le seul total ne doit pas
    effacer le numéro déjà présent.
    """
    number_field, total_field = _ID3_PAIRS[frame_name]
    if number_field not in values and total_field not in values:
        return None

    current, current_total = "", ""
    try:
        existing = frames.get(frame_name)
    except Exception:
        existing = None
    if existing is not None and getattr(existing, "text", None):
        raw = str(existing.text[0])
        current, _, current_total = raw.partition("/")
        current, current_total = current.strip(), current_total.strip()

    number = values.get(number_field) or current
    total = values.get(total_field) or current_total
    if not number:
        # Un total sans numéro n'a pas de forme ID3 : « /12 » n'est lu par
        # personne. On s'abstient plutôt que d'écrire une valeur illisible.
        return None
    return f"{number}/{total}" if total else number


def _write_mp3(path, values, cover_bytes):
    audio = config.MP3(path, ID3=config.ID3)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    for field, frame_name in _ID3_FRAMES.items():
        if field not in values:
            continue
        frame = getattr(config, frame_name, None)
        if frame is not None:
            tags.add(frame(encoding=3, text=values[field]))

    for frame_name in _ID3_PAIRS:
        text = _pair_text(tags, frame_name, values)
        frame = getattr(config, frame_name, None)
        if text and frame is not None:
            tags.add(frame(encoding=3, text=text))

    if cover_bytes:
        tags.add(config.APIC(encoding=3, mime="image/jpeg", type=3,
                             desc="Cover", data=cover_bytes))
    audio.save()


def _write_flac(path, values, cover_bytes):
    audio = config.FLAC(path)
    for field, key in _VORBIS_KEYS.items():
        if field in values:
            audio[key] = values[field]
    if cover_bytes:
        picture = config.Picture()
        picture.data = cover_bytes
        picture.type = 3
        picture.mime = "image/jpeg"
        picture.desc = "Cover"
        audio.clear_pictures()
        audio.add_picture(picture)
    audio.save()


def _write_easy(path, values):
    """Formats restants (MP4, WMA, OGG…) via l'interface « easy »."""
    audio = config.mutagen.File(path, easy=True)
    if audio is None:
        return False, "format non pris en charge"
    if audio.tags is None:
        audio.add_tags()
    written = 0
    for field, key in _EASY_KEYS.items():
        if field not in values:
            continue
        try:
            audio[key] = values[field]
            written += 1
        except Exception:
            # Le conteneur ne connaît pas ce champ : les autres restent
            # écrits, et l'appelant n'a pas à s'en préoccuper.
            continue
    if written:
        audio.save()
    return True, ""


def write(path, values, cover_bytes=None):
    """Écrit ``values`` (et la jaquette) dans ``path``. Rend (succès, message).

    ``values`` emploie les clés de ``FIELDS`` ; les inconnues et les valeurs
    vides sont ignorées. Le fichier n'est ouvert que s'il y a quelque chose
    à écrire.
    """
    if not config.MUTAGEN_AVAILABLE:
        return False, "mutagen n'est pas disponible"
    values = clean_values(values)
    if not values and not cover_bytes:
        return True, ""
    if not os.path.exists(path):
        return False, "fichier introuvable"

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".mp3":
            _write_mp3(path, values, cover_bytes)
        elif ext == ".flac":
            _write_flac(path, values, cover_bytes)
        else:
            return _write_easy(path, values)
        return True, ""
    except Exception as exc:
        return False, str(exc)
