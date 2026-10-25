"""
Thread de recherche Deezer.

Deux comportements selon le type de recherche :

* "Artiste" / "Album" (recherche par mot-clé) : renvoie une liste
  d'ENTITÉS cliquables (artistes ou albums) via ``entities_signal``.
  L'utilisateur clique ensuite sur une entité pour afficher ses pistes.

* Titre, playlist, liens directs et exploration d'une entité :
  renvoie une liste de PISTES via ``results_signal``.

Les jaquettes ne sont plus téléchargées ici : chaque résultat contient
une clé ``cover_url`` et ``cover_bytes`` est initialisé à ``None``. Le
téléchargement des images est confié à ``image_thread.CoverDownloadThread``.
"""

import requests
from PyQt6.QtCore import QThread, pyqtSignal

from deekeep.audio import tag_lookup
from deekeep.core.utils import format_duration

# Part des mots du morceau qu'un titre du catalogue doit reprendre pour que
# la correspondance élargie soit retenue (voir ``deezer_best_match``).
MIN_TITLE_MATCH = 0.7


class DeezerSearchThread(QThread):
    results_signal = pyqtSignal(list)          # liste de pistes
    entities_signal = pyqtSignal(list, str)    # (liste d'entités, "artist"|"album")
    error_signal = pyqtSignal(str)

    def __init__(self, query, search_type, album_title=None, album_cover_url=None):
        super().__init__()
        self.query = query
        self.search_type = search_type
        # Contexte optionnel lorsqu'on explore un album précis
        self.album_title = album_title
        self.album_cover_url = album_cover_url

    def run(self):
        try:
            # --- Liens directs Deezer ------------------------------------
            if "track/" in self.query:
                track_id = self.query.split("track/")[-1].split("?")[0].strip()
                self.fetch_and_emit_single_track(f"https://api.deezer.com/track/{track_id}")
                return

            if "playlist/" in self.query:
                playlist_id = self.query.split("playlist/")[-1].split("?")[0].strip()
                self.fetch_and_emit_tracks_list(
                    f"https://api.deezer.com/playlist/{playlist_id}/tracks"
                )
                return

            if "album/" in self.query:
                album_id = self.query.split("album/")[-1].split("?")[0].strip()
                title = self.album_title
                cover = self.album_cover_url
                # Compléter le titre / la pochette si non fournis
                if not title or not cover:
                    meta = requests.get(f"https://api.deezer.com/album/{album_id}", timeout=10)
                    if meta.status_code == 200:
                        album_data = meta.json()
                        title = title or album_data.get("title")
                        cover = cover or album_data.get("cover_medium") or album_data.get("cover_small")
                self.fetch_and_emit_tracks_list(
                    f"https://api.deezer.com/album/{album_id}/tracks?limit=100",
                    album_title=title, album_cover_url=cover
                )
                return

            if "artist/" in self.query:
                artist_id = self.query.split("artist/")[-1].split("?")[0].strip()
                self.fetch_and_emit_tracks_list(
                    f"https://api.deezer.com/artist/{artist_id}/top?limit=50"
                )
                return

            # --- Recherche par mot-clé -----------------------------------
            if self.search_type == "Artiste":
                self.fetch_and_emit_entities(
                    f"https://api.deezer.com/search/artist?q={self.query}&limit=25", "artist"
                )

            elif self.search_type == "Album":
                self.fetch_and_emit_entities(
                    f"https://api.deezer.com/search/album?q={self.query}&limit=25", "album"
                )

            elif self.search_type == "Playlist":
                if self.query.isdigit():
                    playlist_id = self.query
                else:
                    resp = requests.get(
                        f"https://api.deezer.com/search/playlist?q={self.query}", timeout=10
                    )
                    data = resp.json().get("data", []) if resp.status_code == 200 else []
                    if not data:
                        self.results_signal.emit([])
                        return
                    playlist_id = data[0].get("id")
                self.fetch_and_emit_tracks_list(
                    f"https://api.deezer.com/playlist/{playlist_id}/tracks?limit=100"
                )

            else:  # "Tout / Titre"
                self.fetch_and_emit_tracks_list(
                    f"https://api.deezer.com/search?q={self.query}"
                )

        except Exception as e:
            self.error_signal.emit(str(e))

    # ------------------------------------------------------------------
    # Entités (artistes / albums) cliquables
    # ------------------------------------------------------------------
    def fetch_and_emit_entities(self, url, kind):
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            self.error_signal.emit("Impossible de récupérer les résultats.")
            return

        data = response.json().get("data", [])
        entities = []
        for item in data:
            if kind == "artist":
                entities.append({
                    "kind": "artist",
                    "id": str(item.get("id")),
                    "name": item.get("name", "Artiste inconnu"),
                    "subtitle": f"{item.get('nb_album', 0)} albums · {item.get('nb_fan', 0):,} fans".replace(",", " "),
                    "cover_url": item.get("picture_medium") or item.get("picture_small"),
                    "cover_bytes": None,
                })
            else:  # album
                entities.append({
                    "kind": "album",
                    "id": str(item.get("id")),
                    "name": item.get("title", "Album inconnu"),
                    "subtitle": item.get("artist", {}).get("name", "Artiste inconnu"),
                    "cover_url": item.get("cover_medium") or item.get("cover_small"),
                    "cover_bytes": None,
                })
        self.entities_signal.emit(entities, kind)

    # ------------------------------------------------------------------
    # Pistes
    # ------------------------------------------------------------------
    def fetch_and_emit_single_track(self, url):
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "error" not in data:
                cover_url = data.get("album", {}).get("cover_medium") or data.get("album", {}).get("cover_small")
                self.results_signal.emit([{
                    "id": str(data.get("id")),
                    "title": data.get("title"),
                    "artist": data.get("artist", {}).get("name", "Artiste Inconnu"),
                    "artist_id": str(data.get("artist", {}).get("id") or ""),
                    "album": data.get("album", {}).get("title", "Album Inconnu"),
                    "album_id": str(data.get("album", {}).get("id") or ""),
                    "duration": format_duration(data.get("duration", 0)),
                    "rank": data.get("rank", 0),
                    "preview": data.get("preview") or "",
                    "cover_url": cover_url,
                    "cover_bytes": None,
                }])
            else:
                self.results_signal.emit([])

    def fetch_and_emit_tracks_list(self, url, album_title=None, album_cover_url=None):
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            results = response.json().get("data", [])
            formatted = []
            for track in results:
                cover_url = (
                    album_cover_url
                    or track.get("album", {}).get("cover_medium")
                    or track.get("album", {}).get("cover_small")
                )
                formatted.append({
                    "id": str(track.get("id")),
                    "title": track.get("title"),
                    "artist": track.get("artist", {}).get("name", "Artiste Inconnu"),
                    "artist_id": str(track.get("artist", {}).get("id") or ""),
                    "album": album_title if album_title else track.get("album", {}).get("title", "Album Inconnu"),
                    "album_id": str(track.get("album", {}).get("id") or ""),
                    "duration": format_duration(track.get("duration", 0)),
                    "rank": track.get("rank", 0),
                    "preview": track.get("preview") or "",
                    "cover_url": cover_url,
                    "cover_bytes": None,
                })
            self.results_signal.emit(formatted)
        else:
            self.error_signal.emit("Impossible de récupérer les pistes.")


def _deezer_first(query):
    """Première piste que Deezer rend pour ``query`` (None si aucune)."""
    try:
        resp = requests.get(
            "https://api.deezer.com/search",
            params={"q": query, "limit": 1}, timeout=8,
        )
    except Exception:
        return None
    data = resp.json().get("data", []) if resp.status_code == 200 else []
    return data[0] if data else None


def _match_from(track):
    """Correspondance formatée à partir d'une piste de l'API Deezer."""
    album = track.get("album", {})
    return {
        "id": str(track.get("id")),
        "title": track.get("title", ""),
        "artist": track.get("artist", {}).get("name", ""),
        "album": album.get("title", ""),
        "duration": format_duration(track.get("duration", 0)),
        "cover_url": album.get("cover_medium") or album.get("cover_small"),
        # Grande jaquette pour l'intégration dans le fichier.
        "cover_big": (
            album.get("cover_xl") or album.get("cover_big")
            or album.get("cover_medium")
        ),
    }


def deezer_best_match(artist, title):
    """Meilleure correspondance Deezer pour (artiste, titre), ou None.

    Fonction synchrone réutilisable (hors thread Qt) : utilisée par le thread
    de correspondance de l'écran « Réencoder » comme par le thread de
    téléchargement lorsqu'il doit récupérer jaquette et métadonnées propres
    au moment de la conversion.

    Les deux premières requêtes reprennent les tags tels quels. Quand elles
    ne rendent rien — un tampon de site collé au titre, une mention de
    version que le catalogue nomme autrement, un invité accolé à l'artiste
    suffisent à les faire échouer toutes les deux — la recherche est reprise
    sur le morceau seul. Ce repli-là *vérifie* ce qu'il rapporte : une
    requête élargie finit toujours par rendre quelque chose, et une
    correspondance erronée retaguerait le fichier au nom d'un autre morceau.
    """
    queries = []
    if artist and title:
        queries.append(f'artist:"{artist}" track:"{title}"')
    free = f"{artist} {title}".strip()
    if free:
        queries.append(free)

    for query in queries:
        track = _deezer_first(query)
        if track:
            return _match_from(track)

    # --- Repli sur le morceau débarrassé de son bruit -------------------
    wanted = tag_lookup.core_title(title)
    performer = tag_lookup.main_artist(artist)
    # L'artiste reste de la requête : cherché sur le seul titre, le catalogue
    # rend le premier morceau homonyme venu — une autre signature, un autre
    # morceau — et le fichier se retrouverait retagué au nom de quelqu'un
    # d'autre. Un titre reconnu sous un artiste faux vaut moins que pas de
    # correspondance du tout.
    for query in (f'artist:"{performer}" track:"{wanted}"',
                  f"{performer} {wanted}".strip()):
        if not query or query in queries:
            continue
        queries.append(query)
        track = _deezer_first(query)
        if not track:
            continue
        # Le titre rapporté doit reprendre les mots du morceau cherché,
        # sans quoi c'est un autre morceau que Deezer a trouvé.
        if tag_lookup.contained(wanted, track.get("title", "")) >= MIN_TITLE_MATCH:
            return _match_from(track)
    return None


class DeezerMatchThread(QThread):
    """Recherche automatique de correspondances Deezer pour des fichiers locaux.

    Pour chaque fichier (identifié par un jeton stable ``uid``), interroge
    l'API de recherche Deezer à partir de son artiste et de son titre, et
    émet la meilleure correspondance (identifiant, métadonnées propres et
    URL de jaquette) via ``match_ready``. ``None`` est émis lorsqu'aucune
    correspondance n'est trouvée.
    """

    match_ready = pyqtSignal(int, object)  # uid, dict de correspondance ou None
    finished_signal = pyqtSignal()

    def __init__(self, items):
        """:param items: liste de tuples (uid, artiste, titre)."""
        super().__init__()
        self.items = items
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        for uid, artist, title in self.items:
            if not self._running:
                break
            match = None
            try:
                match = self._search(artist, title)
            except Exception:
                match = None
            if self._running:
                self.match_ready.emit(uid, match)
        self.finished_signal.emit()

    def _search(self, artist, title):
        return deezer_best_match(artist, title)
