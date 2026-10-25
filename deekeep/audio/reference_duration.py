"""
Durée de référence d'un titre, cherchée en ligne.

Un long silence à la fin d'un fichier n'est un défaut que s'il n'appartient
pas au morceau. « Half A World Away (Live) » de R.E.M. se termine sur
quatorze secondes d'applaudissements qui s'éteignent : le fichier dure
218,7 s, la référence 221 s — le silence est dans le master, il n'y a rien à
corriger. « One Vision » de Queen dure 262,8 s là où la référence en donne
243 : les vingt secondes de plus sont bien du vide ajouté par l'extraction.

D'où ce module : il rend les durées connues d'un titre, et l'écran « Qualité »
n'accuse un blanc que si le fichier dépasse la plus proche d'entre elles.

Deux sources, interrogées dans cet ordre :

- **Deezer**, déjà utilisée pour les recherches et les jaquettes, sans clé ni
  quota déclaré ;
- **MusicBrainz** en second recours, pour les titres que Deezer ne distribue
  pas. Son usage impose un en-tête d'identification et une requête par
  seconde au plus, que ``_throttle`` fait respecter.

Les réponses sont mises en cache sur le disque : une durée ne change pas, et
une bibliothèque de plusieurs milliers de titres ne doit interroger chaque
service qu'une fois. Les recherches infructueuses sont conservées aussi —
sans quoi les titres introuvables seraient redemandés à chaque analyse — mais
elles expirent au bout d'un mois, le temps qu'un catalogue s'enrichisse.
"""

import json
import os
import re
import threading
import time

import requests

from deekeep.core import settings as app_settings


CACHE_FILENAME = "reference_cache.json"
CACHE_VERSION = 1

# Durée de validité d'une recherche restée sans réponse.
NEGATIVE_TTL = 30 * 24 * 3600

# MusicBrainz demande un agent identifiable et au plus une requête par
# seconde ; Deezer ne publie pas de limite, on reste néanmoins mesuré.
USER_AGENT = "DeeKeep/1.0 (bibliotheque musicale personnelle)"
DEEZER_INTERVAL = 0.2
MUSICBRAINZ_INTERVAL = 1.1
MUSICBRAINZ_RETRIES = 2

TIMEOUT = 8

_last_call = {}
_throttle_lock = threading.Lock()


def _throttle(service, interval):
    """Espace les appels d'un même service d'au moins ``interval`` secondes."""
    with _throttle_lock:
        previous = _last_call.get(service, 0.0)
        wait = previous + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[service] = time.monotonic()


def _canonical(text):
    """Titre réduit à ses lettres et chiffres, pour comparer deux écritures."""
    text = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _related(candidate, wanted):
    """Vrai si deux titres désignent plausiblement le même enregistrement.

    La comparaison joue dans les deux sens : « Blame » retrouve « Blame
    (feat. John Newman) », et l'inverse. Une correspondance trop stricte
    écarterait la bonne version — pour « Alabina », le seul titre exactement
    homonyme chez Deezer est un remix de six minutes.
    """
    if not candidate or not wanted:
        return False
    return candidate.startswith(wanted) or wanted.startswith(candidate)


# ----------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------
def _from_deezer(artist, title):
    """Durées des enregistrements Deezer correspondant à (artiste, titre)."""
    _throttle("deezer", DEEZER_INTERVAL)
    try:
        response = requests.get(
            "https://api.deezer.com/search",
            params={"q": 'artist:"%s" track:"%s"' % (artist, title), "limit": 25},
            timeout=TIMEOUT,
        )
        data = response.json().get("data", []) if response.status_code == 200 else []
    except Exception:
        return None      # panne réseau : ne rien conclure, ne rien mettre en cache

    wanted = _canonical(title)
    close, all_found = [], []
    for track in data:
        seconds = track.get("duration") or 0
        if seconds <= 0:
            continue
        all_found.append(seconds)
        if _related(_canonical(track.get("title", "")), wanted):
            close.append(seconds)
    return close or all_found


def _musicbrainz_recordings(artist, title):
    """Réponse brute de MusicBrainz, avec une seconde chance en cas de 503.

    Le service répond 503 quand il est saturé, y compris à qui respecte sa
    cadence d'une requête par seconde. Prendre ce refus pour une absence de
    résultat mettrait en cache un « titre inconnu » qui n'en est pas un ;
    l'interpréter comme une panne réseau ferait renoncer à la référence. Une
    seconde tentative, un peu plus tard, suffit presque toujours.
    """
    for attempt in range(MUSICBRAINZ_RETRIES):
        _throttle("musicbrainz", MUSICBRAINZ_INTERVAL)
        try:
            response = requests.get(
                "https://musicbrainz.org/ws/2/recording",
                params={
                    "query": 'artist:"%s" AND recording:"%s"' % (artist, title),
                    "fmt": "json", "limit": 25,
                },
                headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
            )
        except Exception:
            return None
        if response.status_code == 503 and attempt + 1 < MUSICBRAINZ_RETRIES:
            time.sleep(MUSICBRAINZ_INTERVAL * (attempt + 2))
            continue
        if response.status_code != 200:
            return None
        try:
            return response.json().get("recordings", [])
        except ValueError:
            return None
    return None


def _from_musicbrainz(artist, title):
    """Durées des enregistrements MusicBrainz correspondant à (artiste, titre)."""
    data = _musicbrainz_recordings(artist, title)
    if data is None:
        return None

    wanted = _canonical(title)
    found = []
    for recording in data:
        # Le score dit la confiance du moteur de recherche : sous 90, la
        # réponse n'a plus grand-chose à voir avec la demande.
        if int(recording.get("score") or 0) < 90:
            continue
        milliseconds = recording.get("length") or 0
        if milliseconds <= 0:
            continue
        if _related(_canonical(recording.get("title", "")), wanted):
            found.append(int(round(milliseconds / 1000.0)))
    return found


# ----------------------------------------------------------------------
# Cache disque
# ----------------------------------------------------------------------
class ReferenceCache:
    """Durées déjà cherchées, indexées par (artiste, titre) normalisés."""

    def __init__(self, path=None):
        self.path = path or os.path.join(app_settings.data_dir(), CACHE_FILENAME)
        self._entries = {}
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._dirty = False
        self.load()

    @staticmethod
    def key(artist, title):
        return _canonical(artist) + "|" + _canonical(title)

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("version") == CACHE_VERSION:
            entries = data.get("entries")
            if isinstance(entries, dict):
                self._entries = entries

    def save(self):
        with self._lock:
            if not self._dirty:
                return
            payload = {"version": CACHE_VERSION, "entries": dict(self._entries)}
            self._dirty = False
        tmp = self.path + ".tmp"
        try:
            with self._io_lock:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                os.replace(tmp, self.path)
        except OSError as exc:
            with self._lock:
                self._dirty = True
            print(f"[reference] Cache non enregistré : {exc}")

    def get(self, artist, title):
        """Durées connues, ou None si le couple n'a jamais été cherché."""
        with self._lock:
            entry = self._entries.get(self.key(artist, title))
        if not entry:
            return None
        durations = entry.get("durations")
        if not isinstance(durations, list):
            return None
        # Une recherche restée vide finit par être retentée : un titre absent
        # d'un catalogue aujourd'hui peut y figurer dans six mois.
        if not durations and time.time() - (entry.get("time") or 0) > NEGATIVE_TTL:
            return None
        return durations

    def put(self, artist, title, durations):
        with self._lock:
            self._entries[self.key(artist, title)] = {
                "durations": list(durations), "time": time.time(),
            }
            self._dirty = True


# ----------------------------------------------------------------------
# Recherche
# ----------------------------------------------------------------------
def lookup(artist, title, cache=None):
    """Durées connues du titre, en secondes (liste éventuellement vide).

    Retourne None quand la recherche n'a pas pu aboutir (réseau injoignable) :
    l'appelant sait ainsi qu'il ne s'agit pas d'un titre introuvable, et
    s'abstient de conclure quoi que ce soit sur ses blancs.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist or not title:
        return []

    if cache is not None:
        known = cache.get(artist, title)
        if known is not None:
            return known

    durations = _from_deezer(artist, title)
    if durations is None:
        return None
    if not durations:
        # Deezer ne connaît pas ce titre : MusicBrainz, lui, recense aussi ce
        # qu'aucune plateforme ne distribue.
        from_mb = _from_musicbrainz(artist, title)
        if from_mb is None:
            return None
        durations = from_mb

    if cache is not None:
        cache.put(artist, title, durations)
    return durations


def closest(durations, measured):
    """Durée de référence la plus proche de celle mesurée, ou None.

    Retenir la plus proche est le choix le plus indulgent, et c'est celui
    qu'il faut : la liste mêle éditions radio, versions live et remix, et
    accuser un fichier parce qu'il ne dure pas autant qu'un remix de six
    minutes n'aurait aucun sens.
    """
    if not durations or not measured:
        return None
    return min(durations, key=lambda value: abs(value - measured))
