"""
Recherche en ligne des tags manquants (écran « Organiser »).

Deux recherches, une même mécanique. La première comble les tags sans
lesquels un fichier ne peut pas être rangé — titre, artiste, album. La
seconde, ``extras``, comble ceux qui n'empêchent rien mais manquent partout
ailleurs : artiste de l'album, genre, année, rang dans l'album. Leur absence
ne se voit pas dans le plan de rangement, elle se voit dans un motif de
nommage qui réclame l'année et ne rend rien, et dans les colonnes muettes de
la base. Seul Deezer les rapporte, et seulement une fois la piste identifiée
(voir ``deezer_details``) : ni MusicBrainz ni l'empreinte acoustique n'en
savent quoi que ce soit, et ils ne sont donc pas sollicités quand il n'y a
plus qu'eux à combler.

Un fichier dont l'artiste, le titre ou l'album manquent ne peut pas être
rangé : le plan le signale sans y toucher. Ce module tente de l'identifier
à partir des seuls indices disponibles — les tags présents, le nom du
fichier et celui de son dossier — auprès de deux services interrogés dans
cet ordre :

    - Deezer, celui qu'utilise déjà la recherche de l'application. Son
      catalogue commercial couvre bien la variété récente ;
    - MusicBrainz en repli, encyclopédie communautaire dont la couverture
      est meilleure sur les anciens fonds, les éditions locales et les
      compilations. Elle donne aussi la durée de chaque enregistrement d'un
      album, ce qui permet d'identifier un titre dont on ne connaît que
      l'artiste et l'album (voir ``musicbrainz_queries``) ;
    - la durée réelle du fichier, silence final retranché, quand aucun des
      services ne reconnaît la durée annoncée (voir ``audio_probe``) ;
    - AcoustID en dernier recours, qui n'interroge plus le texte mais le
      signal : l'empreinte acoustique du fichier identifie le morceau même
      lorsque tags, nom de fichier et dossier sont muets. C'est la seule
      voie pour un fichier nommé « 01.mp3 » sans le moindre tag, mais elle
      exige un outil et une clé extérieurs (voir ``acoustid_lookup``).

MusicBrainz limite les clients anonymes à une requête par seconde et exige
un User-Agent identifiant l'application : les deux contraintes sont tenues
ici (``MUSICBRAINZ_AGENT``, ``_throttle``). Le service n'est sollicité que
pour les fichiers que Deezer n'a pas su identifier, ce qui borne le nombre
de requêtes à ce que la première passe a laissé de côté.

Trois principes gouvernent l'identification, car écrire un tag faux est
pire que n'en écrire aucun :

    - la durée fait preuve. C'est le seul élément objectif attestant qu'il
      s'agit du même enregistrement : au-delà de ``DURATION_MAX`` secondes
      d'écart, la piste est écartée quel que soit son titre ;
    - le texte doit se retrouver dans ce que l'on sait déjà du fichier. Une
      piste dont le titre et l'artiste n'apparaissent ni dans les tags, ni
      dans le nom du fichier, ni dans celui du dossier n'est pas retenue ;
    - rien n'est écrit sans validation. ``find_tags`` propose, avec son
      niveau de confiance et la raison qui la motive ; l'écran de revue
      dispose ; ``write_tags`` n'écrit que les champs restés vides.
"""

import os
import re
import threading
import time
import unicodedata

import requests

from PyQt6.QtCore import QThread, pyqtSignal

from deekeep.audio import acoustid_lookup
from deekeep.audio import audio_probe
from deekeep.audio import tag_write
from deekeep.core import config

# Seuils de décision.
MIN_SCORE = 0.55        # en dessous, aucune proposition n'est faite
SURE_SCORE = 0.80       # au-dessus, la proposition est cochée d'office
DURATION_EXACT = 3      # s : deux durées à ce point proches valent preuve
DURATION_MAX = 30       # s : au-delà, ce n'est pas le même enregistrement

# Écart en deçà duquel une autre piste reste une rivale crédible. Une
# identification fondée sur la seule durée n'est retenue que si aucune autre
# piste ne s'approche autant du fichier : sur un album dont tous les titres
# durent entre trois et quatre minutes, la durée ne désigne personne.
DURATION_AMBIGUOUS = 8

# Note d'une identification reposant sur la seule durée : volontairement
# sous ``SURE_SCORE``. Un nombre de secondes ne prouve pas un titre — deux
# morceaux d'un même album peuvent durer autant — donc la proposition est
# soumise à l'utilisateur, jamais cochée d'office.
DURATION_ONLY_SCORE = 0.78

# MusicBrainz : une requête par seconde pour les clients anonymes, et un
# User-Agent identifiant l'application, sans quoi les requêtes sont
# refusées. Y ajouter une adresse de contact est recommandé par leur
# politique — voir https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting
MUSICBRAINZ_AGENT = "DeeKeep/1.0 ( organiseur de bibliothèque musicale )"
MUSICBRAINZ_DELAY = 1.05
MUSICBRAINZ_MAX_QUERIES = 3   # par fichier : le repli ne doit pas s'éterniser

# Dossiers dont le nom ne renseigne sur rien : les joindre à la requête
# n'ajouterait que du bruit.
GENERIC_FOLDERS = {
    "divers", "various", "va", "musique", "music", "mp3", "cd", "cd1", "cd2",
    "disc", "disc1", "disc2", "disk", "disk1", "disk2", "itunes", "downloads",
    "telechargements", "compil", "compils", "compilation", "compilations",
    "electro", "dance", "pop", "rock", "pop rock", "pop dance", "ambiance",
    "disco", "funk", "rap", "jazz", "classique", "varietes", "no title",
}

# Bruit courant des noms de fichiers glanés en ligne : mentions de clip, et
# marquage du débit qu'ajoutent certains téléchargeurs (« - 128 », « 320kbps »).
_NOISE_RE = re.compile(
    r"(?i)\((?:official[^)]*|lyrics?[^)]*|audio|video|clip|hd|hq|full)\)"
    r"|\[[^\]]*\]"
    r"|\b(?:official (?:video|audio|lyric video)|lyrics?)\b"
    r"|[\s\-_]+\d{2,3}\s*k?b?(?:ps)?\s*$"
)
_LEADING_TRACK_RE = re.compile(r"^\s*\d{1,3}\s*[.)\-_]\s*|^\s*\d{2}\s+")

# Mention de version accolée à un titre de catalogue (« (Radio Edit) »).
_QUALIFIER_RE = re.compile(r"\s*[(\[][^)\]]*[)\]]\s*")

# Même mention, mais sans parenthèses pour la contenir (« ... Full Edit ») :
# les fichiers glanés en ligne la collent volontiers en fin de titre.
_VERSION_TAIL_RE = re.compile(
    r"(?i)[\s\-–_]+(?:full|radio|extended|club|maxi|original|album|single"
    r"|vf|vo)?\s*(?:edit|mix|version|remix|remaster(?:ed)?)\s*\d*$"
)

# Marqueur d'invité dans un nom d'artiste : ce qui suit n'appartient pas à
# l'artiste principal. Même liste que les écrans « Organiser » et « Doublons ».
_GUEST_RE = re.compile(
    r"(?i)\s*[(\[]?\b(?:feat\.?|featuring|ft\.?|avec|with|vs\.?|versus)\b.*$"
)

# Note maximale d'un titre reconnu à sa seule partie nue : légèrement en
# deçà d'une correspondance complète, qui doit rester préférée.
CORE_TITLE_WEIGHT = 0.9


def words(text):
    """Mots significatifs d'un texte : minuscules, sans accent ni ponctuation."""
    folded = unicodedata.normalize("NFKD", (text or "").casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return [w for w in re.findall(r"[a-z0-9]+", folded) if w]


# Mots trop communs pour prouver quoi que ce soit : « La brioche » et « La
# Rasbaïa » partagent « la » sans avoir de rapport. Les compter donnerait la
# moitié des points à un titre entièrement étranger.
_STOPWORDS = {
    "la", "le", "les", "l", "un", "une", "des", "de", "du", "d", "et", "ou",
    "au", "aux", "en", "the", "an", "of", "to", "in", "on", "and", "or",
}


def _significant(items):
    """Mots porteurs de sens ; tous si le texte n'en contient aucun (« The The »)."""
    kept = [word for word in items if word not in _STOPWORDS]
    return kept or items


def contained(needle, haystack):
    """Part des mots significatifs de ``needle`` retrouvés dans ``haystack``.

    Volontairement asymétrique : « Losing My Religion » est pleinement
    contenu dans « Losing My Religion - R.E.M. », ce qui est bien le sens de
    la question posée — la piste trouvée correspond-elle à ce que l'on sait
    du fichier ?
    """
    target = _significant(words(needle))
    if not target:
        return 0.0
    source = set(words(haystack))
    return sum(1 for word in target if word in source) / len(target)


def clean_stem(name):
    """Nom de fichier ramené à ce qui peut servir de requête.

    Retire l'extension, le numéro de piste de tête et les mentions
    parasites (« (Official Video) », « [320kbps] ») que Deezer ne
    reconnaîtrait pas.
    """
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    stem = _LEADING_TRACK_RE.sub("", stem)
    stem = _NOISE_RE.sub(" ", stem)
    return re.sub(r"\s+", " ", stem).strip(" -_.")


def core_title(title):
    """Titre ramené au morceau : sans tampon de site ni mention de version.

    « White Knuckle Ride Full Edit [Www Keepvid Com] » devient « White
    Knuckle Ride ». Ni Deezer ni YouTube ne connaissent la version que le
    fichier annonce — quand ils la nomment, ils la nomment autrement — et
    une recherche qui la garde ne rend *rien du tout*.
    """
    text = _NOISE_RE.sub(" ", str(title or ""))
    text = _QUALIFIER_RE.sub(" ", text)
    text = _VERSION_TAIL_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" -_.") or str(title or "").strip()


def main_artist(artist):
    """Nom de l'artiste principal : ce qui précède le premier « feat. ».

    « Mad Ice Feat. Irina » devient « Mad Ice » : c'est sous cette signature
    que les catalogues rangent le morceau, l'invité n'apparaissant qu'au
    titre, ou pas du tout.
    """
    text = _GUEST_RE.sub("", str(artist or "")).strip(" -_,&")
    return re.sub(r"\s+", " ", text) or str(artist or "").strip()


def local_duration(path):
    """Durée du fichier en secondes, 0 si elle ne peut pas être lue."""
    if not config.MUTAGEN_AVAILABLE:
        return 0
    try:
        audio = config.mutagen.File(path)
        return int(round(getattr(audio.info, "length", 0) or 0))
    except Exception:
        return 0


def missing_fields(entry):
    """Champs title/artist/album que le fichier ne renseigne pas.

    Le titre est jugé absent lorsqu'il ne vient pas des tags : ``read_tags``
    y recopie le nom du fichier faute de mieux (voir ``title_tagged``).
    """
    absent = []
    if not entry.get("title_tagged"):
        absent.append("title")
    for field in ("artist", "album"):
        if not str(entry.get(field) or "").strip():
            absent.append(field)
    return absent


# Tags complémentaires : ils n'empêchent pas de ranger un fichier, mais leur
# absence se voit partout ailleurs — un motif de nommage qui réclame l'année
# rend une chaîne vide, et les colonnes correspondantes de la base restent
# muettes. Seul Deezer les rapporte (voir ``deezer_details``) ; MusicBrainz et
# l'empreinte acoustique n'en savent rien, et ne sont donc pas sollicités
# lorsqu'il n'y a plus qu'eux à combler.
EXTRA_FIELDS = ("albumartist", "genre", "date", "track", "tracktotal", "disc")

# Ceux dont l'absence justifie à elle seule une recherche. Le nombre de
# pistes de l'album et le numéro de disque n'en font pas partie : le
# catalogue ne les rapporte pas toujours, et un fichier qui n'obtiendrait
# jamais son numéro de disque serait éternellement tenu pour incomplet — la
# bibliothèque entière serait reprise à chaque passage. Ils sont écrits quand
# la réponse les porte, jamais réclamés pour eux-mêmes.
EXTRA_TRIGGER_FIELDS = ("albumartist", "genre", "date", "track")


def missing_extras(entry):
    """Tags complémentaires que le fichier ne renseigne pas."""
    return [field for field in EXTRA_FIELDS
            if not str(entry.get(field) or "").strip()]


def needs_extras(entry):
    """Vrai si une recherche des tags complémentaires a lieu d'être."""
    return any(not str(entry.get(field) or "").strip()
               for field in EXTRA_TRIGGER_FIELDS)


def _unique(queries):
    """Requêtes sans doublon, ordre conservé."""
    seen, kept = set(), []
    for query in queries:
        key = query.casefold()
        if query and key not in seen:
            seen.add(key)
            kept.append(query)
    return kept


def _clues(entry):
    """(nom de fichier nettoyé, artiste, album, dossier parent utile)."""
    path = entry.get("path") or ""
    parent = os.path.basename(os.path.dirname(path))
    if " ".join(words(parent)) in GENERIC_FOLDERS:
        parent = ""
    return (clean_stem(path),
            str(entry.get("artist") or "").strip(),
            str(entry.get("album") or "").strip(),
            parent)


def deezer_queries(entry):
    """Requêtes Deezer, de la plus étayée à la plus spéculative."""
    stem, artist, album, parent = _clues(entry)
    queries = []
    # Artiste et album connus : c'est le titre qui manque, et la piste se
    # trouve dans l'album — la durée dira laquelle.
    if artist and album:
        queries.append(f'artist:"{artist}" album:"{album}"')
    if artist and stem and words(stem) != words(artist):
        queries.append(f"{artist} {stem}")
    elif artist:
        # Le nom du fichier n'est que celui de l'artiste : rien n'indique le
        # titre, et l'album (« Piste ») peut n'être qu'un tag de remplissage.
        # Reste son catalogue, où la durée cherchera la piste.
        queries.append(f'artist:"{artist}"')
    if stem:
        queries.append(stem)
    if parent:
        if stem and words(stem) != words(parent):
            queries.append(f"{parent} {stem}")
        elif artist:
            queries.append(f"{parent} {artist}")
    return _unique(queries)


def _lucene(value):
    """Valeur échappée pour une requête MusicBrainz (syntaxe Lucene)."""
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", value or "")


def _without_artist(stem, artist):
    """Nom de fichier privé des mots du nom d'artiste.

    « Armin van Buuren - Blah Blah Blah » interrogé tel quel en tant que
    titre ne donne rien : MusicBrainz veut le titre seul dans son champ
    ``recording``, l'artiste allant dans le sien.
    """
    if not artist:
        return stem
    drop = set(words(artist))
    kept = [part for part in re.split(r"\s+", stem) if part
            and not set(words(part)) <= drop]
    return " ".join(kept).strip(" -_.")


def musicbrainz_queries(entry):
    """Requêtes MusicBrainz : champs nommés d'abord, texte libre en dernier.

    Quand l'artiste et l'album sont connus mais pas le titre, interroger
    l'album entier est la voie la plus sûre : MusicBrainz rend la durée de
    chaque enregistrement, et c'est elle qui désigne la piste.
    """
    stem, artist, album, parent = _clues(entry)
    queries = []
    if artist and album:
        queries.append(f'artist:"{_lucene(artist)}" AND release:"{_lucene(album)}"')
    if artist:
        title = _without_artist(stem, artist)
        if title:
            queries.append(
                f'recording:"{_lucene(title)}" AND artist:"{_lucene(artist)}"')
        else:
            # Nom de fichier réduit au nom de l'artiste : son catalogue est
            # tout ce qui reste à confronter à la durée.
            queries.append(f'artist:"{_lucene(artist)}"')
    elif stem:
        # Sans artiste connu, chercher dans les champs nommés reste bien plus
        # sélectif que le texte libre, qui rapporte tous les enregistrements
        # citant ces mots où que ce soit. Un nom de fichier suit le plus
        # souvent la forme « Titre - Artiste » — ou l'inverse : les deux
        # lectures sont tentées.
        left, _, right = stem.partition(" - ")
        left, right = left.strip(), right.strip()
        if right:
            queries.append(
                f'recording:"{_lucene(left)}" AND artist:"{_lucene(right)}"')
            queries.append(
                f'recording:"{_lucene(right)}" AND artist:"{_lucene(left)}"')
        else:
            queries.append(f'recording:"{_lucene(stem)}"')
    if stem:
        queries.append(_lucene(stem))
    if parent and stem and words(stem) != words(parent):
        queries.append(f'recording:"{_lucene(stem)}" AND artist:"{_lucene(parent)}"')
    return _unique(queries)[:MUSICBRAINZ_MAX_QUERIES]


def search_deezer(query, limit=8):
    """Pistes proposées par Deezer pour ``query`` (liste éventuellement vide)."""
    try:
        resp = requests.get(
            "https://api.deezer.com/search",
            params={"q": query, "limit": limit}, timeout=10,
        )
        data = resp.json().get("data", []) if resp.status_code == 200 else []
    except Exception:
        return []
    tracks = []
    for track in data:
        album = track.get("album") or {}
        tracks.append({
            "title": track.get("title") or "",
            "artist": (track.get("artist") or {}).get("name") or "",
            "album": album.get("title") or "",
            "seconds": int(track.get("duration") or 0),
            "cover_url": album.get("cover_medium") or album.get("cover_small") or "",
            # Identifiants de la piste retenue : la recherche ne rapporte ni
            # genre, ni rang dans l'album, ni date. Ce sont eux qui permettent
            # de les demander ensuite (voir ``deezer_details``).
            "id": str(track.get("id") or ""),
            "album_id": str(album.get("id") or ""),
        })
    return tracks


# ----------------------------------------------------------------------
# Tags complémentaires (Deezer)
# ----------------------------------------------------------------------

# Un album est demandé une fois pour toutes ses pistes : sur une
# bibliothèque, la même parution revient dix à quinze fois.
_album_cache = {}
_album_lock = threading.Lock()


def _deezer_get(url):
    """Document JSON d'une adresse Deezer, ou {} en cas d'échec.

    Le service signale ses erreurs dans le corps de la réponse (clé
    « error ») autant que par le code HTTP : les deux sont traitées de la
    même façon, comme une absence de réponse.
    """
    try:
        resp = requests.get(url, timeout=10)
    except Exception:
        return {}
    if resp.status_code != 200:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {}
    if not isinstance(data, dict) or "error" in data:
        return {}
    return data


def _deezer_album(album_id):
    """Genre, artiste et nombre de pistes d'un album Deezer (mis en cache)."""
    with _album_lock:
        cached = _album_cache.get(album_id)
    if cached is not None:
        return cached

    data = _deezer_get(f"https://api.deezer.com/album/{album_id}")
    genres = (data.get("genres") or {}).get("data") or []
    total = data.get("nb_tracks")
    details = {
        "genre": (genres[0].get("name") or "") if genres else "",
        "albumartist": (data.get("artist") or {}).get("name") or "",
        "tracktotal": str(total) if total else "",
        "date": str(data.get("release_date") or ""),
    }
    # Un échec n'est pas mis en cache : la panne peut n'être que passagère,
    # et l'album resservira à la piste suivante.
    if data:
        with _album_lock:
            _album_cache[album_id] = details
    return details


def _year(value):
    """Année d'une date Deezer (« 1995-11-07 » -> « 1995 »).

    La bibliothèque note l'année seule dans la très grande majorité des
    fichiers déjà étiquetés, et c'est elle que porte la colonne
    correspondante de la base : écrire la date complète pour les seuls
    fichiers complétés ici mêlerait deux conventions.
    """
    text = str(value or "").strip()
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else ""


def deezer_details(track_id, album_id=""):
    """Tags complémentaires d'une piste Deezer, dans le vocabulaire des tags.

    Deux appels au plus : la piste porte son rang dans l'album, son numéro
    de disque et la date de l'enregistrement d'origine ; l'album porte le
    genre, l'artiste d'album et son nombre de pistes. Les champs restés
    inconnus sont absents du résultat, jamais vides.
    """
    details = {}
    track = _deezer_get(f"https://api.deezer.com/track/{track_id}") if track_id else {}
    if track:
        if track.get("track_position"):
            details["track"] = str(track["track_position"])
        if track.get("disk_number"):
            details["disc"] = str(track["disk_number"])
        # La date de la piste est celle de la parution d'origine ; celle de
        # l'album peut n'être que celle d'une réédition (« The Bridge » :
        # 1995 sur la piste, 2015 sur la parution).
        details["date"] = _year(track.get("release_date"))
        album_id = album_id or str((track.get("album") or {}).get("id") or "")

    if album_id:
        album = _deezer_album(album_id)
        for field in ("genre", "albumartist", "tracktotal"):
            if album.get(field):
                details[field] = album[field]
        if not details.get("date"):
            details["date"] = _year(album.get("date"))
    return {field: value for field, value in details.items() if value}


_mb_lock = threading.Lock()
_mb_last = 0.0


def _throttle():
    """Espace les appels à MusicBrainz d'au moins ``MUSICBRAINZ_DELAY``.

    Leur service refuse les clients anonymes qui dépassent une requête par
    seconde ; l'attente est prise ici plutôt que subie sous forme d'erreurs.
    """
    global _mb_last
    with _mb_lock:
        wait = MUSICBRAINZ_DELAY - (time.monotonic() - _mb_last)
        if wait > 0:
            time.sleep(wait)
        _mb_last = time.monotonic()


def search_musicbrainz(query, limit=8, retry=True):
    """Enregistrements proposés par MusicBrainz pour ``query``.

    Le champ ``album`` retenu est celui de la parution dont le titre
    ressemble le plus à ce que l'on cherche, à défaut la première : un même
    enregistrement figure souvent sur des dizaines de compilations.
    """
    _throttle()
    try:
        resp = requests.get(
            "https://musicbrainz.org/ws/2/recording",
            params={"query": query, "fmt": "json", "limit": limit},
            headers={"User-Agent": MUSICBRAINZ_AGENT}, timeout=15,
        )
    except Exception:
        return []
    if resp.status_code == 503 and retry:
        # Quota dépassé : le service invite explicitement à réessayer.
        time.sleep(MUSICBRAINZ_DELAY)
        return search_musicbrainz(query, limit, retry=False)
    if resp.status_code != 200:
        return []
    try:
        recordings = resp.json().get("recordings", [])
    except Exception:
        return []

    tracks = []
    for record in recordings:
        credit = " ".join(
            part.get("name") or "" for part in record.get("artist-credit") or []
        ).strip()
        releases = [str(rel.get("title") or "")
                    for rel in record.get("releases") or []]
        length = record.get("length")
        tracks.append({
            "title": record.get("title") or "",
            "artist": credit,
            "album": releases[0] if releases else "",
            "albums": releases,
            "seconds": int(round(length / 1000)) if length else 0,
            "cover_url": "",
        })
    return tracks


# Services textuels, interrogés dans l'ordre : le second ne l'est que si le
# premier n'a rien su identifier. L'empreinte acoustique, de nature
# différente, vient après eux (voir ``find_tags``).
PROVIDERS = (
    ("Deezer", deezer_queries, search_deezer),
    ("MusicBrainz", musicbrainz_queries, search_musicbrainz),
)


def _duration_agreement(local, remote):
    """Concordance des durées : 1 pour identiques, 0 à ``DURATION_MAX``.

    Retourne None si l'une des deux est inconnue — la durée ne peut alors ni
    confirmer ni infirmer, et le texte devra suffire.
    """
    if not local or not remote:
        return None
    delta = abs(local - remote)
    if delta > DURATION_MAX:
        return -1.0
    if delta <= DURATION_EXACT:
        return 1.0
    return 1.0 - (delta - DURATION_EXACT) / (DURATION_MAX - DURATION_EXACT)


def title_source(entry):
    """Textes où le titre d'une piste candidate peut légitimement se lire.

    Le nom du fichier, celui du dossier, et le titre s'il vient des tags.
    Le titre de l'album en est volontairement exclu : « Live Europe '83:
    Children of the Eighties » contient « Children of the Eighties », et
    l'y chercher ferait passer une piste de l'album pour le fichier lui-même
    — au risque d'écrire le titre d'un morceau voisin.
    """
    path = entry.get("path") or ""
    parts = [clean_stem(path), os.path.basename(os.path.dirname(path))]
    if entry.get("title_tagged"):
        parts.append(str(entry.get("title") or ""))
    return " ".join(parts)


def evaluate(candidate, entry, duration, source):
    """Note une piste candidate. Retourne (score, raison) — score 0 = écartée.

    ``source`` est le texte où le titre doit se retrouver (voir
    ``title_source``). L'artiste, lui, est confronté au tag artiste quand
    il existe : une piste d'un autre interprète n'est pas ce fichier.
    """
    agreement = _duration_agreement(duration, candidate["seconds"])
    delta = (abs(duration - candidate["seconds"])
             if duration and candidate["seconds"] else 0)

    title_score = contained(candidate["title"], source)
    core = _QUALIFIER_RE.sub(" ", candidate["title"]).strip()
    if core and core != candidate["title"]:
        # Le catalogue précise la version (« Blink (Original Maxi Version) »)
        # là où le nom de fichier s'en tient au titre : celui-ci doit pouvoir
        # correspondre, à note un peu moindre.
        title_score = max(title_score,
                          CORE_TITLE_WEIGHT * contained(core, source))
    artist = str(entry.get("artist") or "").strip()
    album = str(entry.get("album") or "").strip()
    # Le tag artiste n'est pas parole d'évangile : il arrive qu'il désigne
    # quelqu'un d'autre (« All Mine » de Portishead étiqueté « Joan Baez »).
    # Le nom du fichier est un témoin de même rang, et il suffit que l'un des
    # deux corrobore l'interprète trouvé — la correction du tag fautif étant
    # ensuite soumise à l'utilisateur.
    artist_score = max(contained(candidate["artist"], artist) if artist else 0.0,
                       contained(candidate["artist"], source))
    album_score = contained(candidate["album"], album) if album else 0.0

    # Titre et artiste littéralement présents dans ce que l'on sait du
    # fichier : l'identification ne tient plus à la durée. Un montage ou un
    # maxi n'est pas la version du catalogue, mais il en garde l'artiste et
    # le titre — c'est-à-dire exactement ce que l'on s'apprête à écrire.
    conclusive = title_score >= CORE_TITLE_WEIGHT and artist_score >= 0.99
    if agreement is not None and agreement < 0 and not conclusive:
        return 0.0, ""

    if agreement is None:
        duration_score = 0.5
    elif agreement < 0:
        # Durées trop éloignées : la piste ne vaut plus que par son texte.
        # Entre deux versions, la plus proche reste néanmoins la plus
        # vraisemblable — d'où cette part résiduelle, décroissante.
        duration_score = 0.05 * DURATION_MAX / max(delta, DURATION_MAX)
    else:
        duration_score = agreement
    score = 0.45 * title_score + 0.30 * artist_score + 0.25 * duration_score

    # Le titre manquant se déduit de l'album : une durée identique dans le
    # bon album vaut mieux qu'un titre absent du nom de fichier. La note
    # reste sous le seuil de certitude — c'est un indice, pas une preuve.
    #
    # Le raccourci ne vaut que pour un titre que rien ne corrobore. Un titre
    # reconnu, lui, doit suivre la voie ordinaire : le donner pour identifié
    # par sa seule durée le soumettrait au verdict des rivales
    # (``_best_from_text``), et une piste voisine du même album — il en dure
    # toujours une à quelques secondes près — suffirait à écarter une
    # correspondance pourtant parfaite.
    if (title_score < 0.5 and album_score >= 0.8
            and agreement is not None and agreement >= 1.0):
        return max(score, DURATION_ONLY_SCORE), (
            "durée identique dans l'album déjà connu")

    if title_score < 0.5:
        return 0.0, ""

    if artist_score < 0.5:
        # L'interprète trouvé n'apparaît nulle part : ni dans les tags, ni
        # dans le nom du fichier ou du dossier.
        if artist:
            # Un tag artiste existe et le contredit : c'est un autre morceau.
            return 0.0, ""
        # Aucun artiste où que ce soit : exiger qu'il se corrobore rendrait
        # ces fichiers définitivement introuvables (« 15 French Love.mp3 »).
        # Deux garanties le remplacent : un titre exact, et non seulement
        # reconnu, et une durée au plus près.
        exact = words(candidate["title"]) == words(clean_stem(entry.get("path") or ""))
        if not exact or agreement is None or agreement < 1.0:
            return 0.0, ""
        return DURATION_ONLY_SCORE, (
            "titre exact et durée identique — artiste non vérifiable "
            "dans le nom du fichier")

    if agreement is not None and agreement < 0:
        reason = f"titre et artiste reconnus, mais durée très différente ({delta} s d'écart)"
    elif agreement is not None and agreement >= 1.0:
        reason = "titre reconnu, durée identique"
    elif conclusive:
        reason = "titre et artiste reconnus dans le nom du fichier"
    elif agreement is None:
        reason = "titre reconnu, durée inconnue"
    else:
        reason = f"titre reconnu, durée proche ({delta} s d'écart)"
    return score, reason


def _rivals(best, seen, duration):
    """Autres pistes dont la durée approche autant celle du fichier.

    Une même piste rééditée sur plusieurs compilations ne compte pas pour
    une rivale : seuls un autre titre ou un autre interprète en sont.
    """
    return [
        candidate for candidate in seen
        if candidate is not best and candidate["seconds"]
        and abs(candidate["seconds"] - duration) <= DURATION_AMBIGUOUS
        and (words(candidate["title"]) != words(best["title"])
             or words(candidate["artist"]) != words(best["artist"]))
    ]


def _best_from_text(entry, duration, source, build_queries, search):
    """Meilleure piste d'un service interrogé par requêtes : (piste, note, raison, requête)."""
    best, best_score, best_reason, best_query = None, 0.0, "", ""
    seen = []
    for query in build_queries(entry):
        limit = 25 if any(f in query for f in ("artist:", "release:", "recording:")) else 8
        for candidate in search(query, limit):
            seen.append(candidate)
            score, reason = evaluate(candidate, entry, duration, source)
            if score > best_score:
                best, best_score, best_reason, best_query = (
                    candidate, score, reason, query)
        # Une correspondance franche rend les requêtes suivantes inutiles.
        if best_score >= 0.95:
            break

    # Identification reposant sur la seule durée : elle ne vaut que si aucune
    # autre piste ne s'en approche autant. Le titre, lui, se corrobore par le
    # texte et n'a pas besoin de cette précaution.
    if best is not None and best_reason.startswith("durée"):
        if _rivals(best, seen, duration):
            return None, 0.0, "", ""
    return best, best_score, best_reason, best_query


def _best_from_fingerprint(entry, duration, acoustid_key):
    """Meilleure piste identifiée par l'empreinte acoustique du fichier.

    Aucune corroboration textuelle n'est exigée : c'est précisément l'intérêt
    de l'empreinte, qui reconnaît un fichier muet. La durée, en revanche,
    reste vérifiée quand la base la connaît — une empreinte peut désigner un
    enregistrement voisin (édition allongée, remix).
    """
    candidates = acoustid_lookup.lookup(
        entry.get("path") or "", duration, acoustid_key)
    for candidate in candidates:
        if not candidate["title"]:
            continue
        agreement = _duration_agreement(duration, candidate["seconds"])
        if agreement is not None and agreement < 0:
            continue
        match = candidate["match"]
        reason = f"empreinte acoustique (concordance {match:.0%})"
        if agreement is not None and agreement >= 1.0:
            reason += ", durée identique"
        # La note reflète la concordance rendue par AcoustID, tempérée quand
        # la durée ne la confirme pas.
        score = match if agreement is None or agreement >= 1.0 else 0.75 * match
        return candidate, score, reason, "empreinte"
    return None, 0.0, "", ""


def _propose(entry, absent, extras, duration, source, album, acoustid_key,
             note=""):
    """Interroge les services pour une durée donnée. Rend une proposition ou None.

    ``absent`` liste les champs qui empêchent de ranger le fichier,
    ``extras`` les tags complémentaires à combler. Les seconds ne justifient
    ni MusicBrainz ni l'empreinte acoustique : ni l'un ni l'autre ne les
    connaît, et calculer une empreinte pour un genre manquant coûterait une
    lecture complète du fichier.
    """
    attempts = [
        (name, lambda e, b=build, s=search: _best_from_text(e, duration, source, b, s))
        for name, build, search in PROVIDERS
        if absent or name == "Deezer"
    ]
    if absent:
        attempts.append(
            ("AcoustID", lambda e: _best_from_fingerprint(e, duration, acoustid_key)))

    for name, attempt in attempts:
        best, best_score, best_reason, best_query = attempt(entry)
        if best is None or best_score < MIN_SCORE:
            continue    # au service suivant, s'il en reste un

        # Un enregistrement figure souvent sur des dizaines de parutions :
        # retenir celle qui ressemble à l'album cherché, à défaut la première.
        if album:
            for title in best.get("albums") or []:
                if contained(title, album) >= 0.8 or contained(album, title) >= 0.8:
                    best = dict(best, album=title)
                    break

        values = {field: best[field] for field in absent if best.get(field)}
        if absent and not values:
            continue    # ce service ne comble rien de bloquant

        # Les tags complémentaires sont demandés une fois la piste identifiée,
        # et à Deezer seul : c'est lui qui les porte.
        if extras and name == "Deezer":
            found = deezer_details(best.get("id", ""), best.get("album_id", ""))
            values.update({field: value for field, value in found.items()
                           if field in extras})
        if not values:
            continue

        # Tags présents que la correspondance contredit : ils ne sont pas
        # écrits d'office (voir ``write_tags``), mais l'écran de revue peut
        # proposer de les corriger. Une différence de casse ou de ponctuation
        # n'est pas une contradiction.
        replaces = {}
        for field in ("title", "artist", "album"):
            current = str(entry.get(field) or "").strip()
            found = best.get(field) or ""
            if (field not in values and current and found
                    and words(current) != words(found)):
                replaces[field] = found

        return {
            "title": best["title"], "artist": best["artist"],
            "album": best["album"], "seconds": best["seconds"],
            "cover_url": best.get("cover_url", ""),
            "values": values, "fills": list(values),
            "replaces": replaces, "corrects": list(replaces),
            "score": round(best_score, 2), "sure": best_score >= SURE_SCORE,
            "reason": best_reason + note, "query": best_query,
            "duration": duration, "source": name,
        }
    return None


def find_tags(entry, acoustid_key="", extras=False):
    """Meilleure proposition de tags pour un fichier, ou None.

    ``extras`` étend la recherche aux tags complémentaires — artiste d'album,
    genre, date, rang dans l'album : ceux qui n'empêchent pas de ranger le
    fichier mais manquent partout ailleurs. Un fichier dont les trois tags de
    base sont déjà là n'est alors plus écarté d'emblée.

    Les services sont interrogés dans l'ordre : le suivant n'est sollicité
    que si le précédent n'a rien su identifier, l'empreinte acoustique
    venant en dernier — elle coûte une lecture du fichier.

    Faute de correspondance, une seconde passe reprend la recherche avec la
    durée mesurée hors silence final : un fichier mal découpé annonce une
    durée que nul catalogue ne peut confirmer, et c'est précisément cette
    durée qui départage les candidats.

    Le dictionnaire retourné porte les tags trouvés, les champs qu'il
    comblerait (``fills``), ceux qu'il corrigerait (``corrects``), la
    confiance (``sure``), la raison retenue et le service d'origine, de quoi
    présenter la proposition sans la refaire.
    """
    absent = missing_fields(entry)
    # Un fichier auquel il ne manque qu'un numéro de disque ne vaut pas une
    # requête : c'est ``needs_extras`` qui en décide, pas la seule liste des
    # champs vides.
    extras = missing_extras(entry) if extras and needs_extras(entry) else []
    if not absent and not extras:
        return None

    path = entry.get("path") or ""
    duration = local_duration(path)
    source = title_source(entry)
    album = str(entry.get("album") or "").strip()

    proposal = _propose(entry, absent, extras, duration, source, album,
                        acoustid_key)
    if proposal is not None and proposal["sure"]:
        return proposal
    if not absent:
        # Seuls des tags complémentaires manquaient : ni la remesure de la
        # durée ni les services suivants n'y changeraient quoi que ce soit,
        # et l'une comme les autres coûtent cher (voir ``_propose``).
        return proposal

    # Rien trouvé, ou rien de sûr : la durée annoncée est peut-être en cause.
    # La remesurer hors silence final peut aussi bien révéler une
    # correspondance que transformer un « probable » en certitude.
    useful = audio_probe.effective_duration(path, duration)
    if useful and abs(useful - duration) > DURATION_EXACT:
        retried = _propose(entry, absent, extras, useful, source, album,
                           acoustid_key,
                           note=" (durée mesurée hors silence final)")
        if retried is not None and (proposal is None
                                    or retried["score"] > proposal["score"]):
            return retried
    return proposal


def write_tags(path, values):
    """Écrit les tags ``values`` dans un fichier. Retourne (succès, message).

    Seuls les champs fournis sont écrits : le reste du fichier, pochette
    comprise, n'est pas touché. La traduction vers les trames du conteneur
    est celle de ``tag_write``, partagée avec le téléchargement — un champ
    que l'un sait écrire, l'autre le sait aussi.
    """
    return tag_write.write(path, values)


class TagLookupThread(QThread):
    """Interroge le service en ligne pour une liste de fichiers.

    Une requête réseau par fichier, séquentielle : l'API est publique et
    sans clé, la marteler en parallèle exposerait à un refus de service.
    Les propositions sont émises au fil de l'eau afin que la revue se
    remplisse pendant la recherche.
    """

    found = pyqtSignal(int, object)   # rang dans la liste, proposition ou None
    progress = pyqtSignal(int)        # nombre de fichiers traités
    done = pyqtSignal()

    def __init__(self, entries, acoustid_key="", extras=False):
        super().__init__()
        self.entries = entries
        self.acoustid_key = acoustid_key
        # Étendre la recherche aux tags complémentaires (voir ``find_tags``).
        self.extras = extras
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        for index, entry in enumerate(self.entries):
            if not self._running:
                break
            try:
                proposal = find_tags(entry, self.acoustid_key, self.extras)
            except Exception:
                proposal = None
            if not self._running:
                break
            self.found.emit(index, proposal)
            self.progress.emit(index + 1)
        self.done.emit()
