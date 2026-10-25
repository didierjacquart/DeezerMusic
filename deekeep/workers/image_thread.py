"""
Thread dédié au téléchargement asynchrone des images (jaquettes / portraits).

La recherche renvoie désormais uniquement les métadonnées et les URL des
images ; le téléchargement effectif des vignettes est délégué à ce thread
afin que l'interface reste fluide et qu'un indicateur de chargement (loader)
puisse être affiché pendant la récupération.

Chaque image téléchargée est émise individuellement via ``cover_ready`` avec
l'index de la ligne concernée, ce qui permet de remplacer le loader dès que
la vignette est disponible. ``finished_signal`` signale la fin du lot.
"""

import requests
from PyQt6.QtCore import QThread, pyqtSignal


class CoverLookupThread(QThread):
    """
    Recherche et télécharge la jaquette d'un titre de l'historique.

    Utilisée par la fenêtre "loupe" : si aucune image n'est stockée pour
    l'entrée, on interroge l'API Deezer à partir de l'artiste et du titre
    pour récupérer la pochette de l'album correspondant.
    """
    result_signal = pyqtSignal(object)  # bytes de l'image ou None

    def __init__(self, artist, title, cover_url=None):
        super().__init__()
        self.artist = artist or ""
        self.title = title or ""
        self.cover_url = cover_url

    def run(self):
        data = None
        try:
            url = self.cover_url or self._lookup_cover_url()
            if url:
                resp = requests.get(url, timeout=8)
                if resp.status_code == 200:
                    data = resp.content
        except Exception:
            data = None
        self.result_signal.emit(data)

    def _lookup_cover_url(self):
        query = f'artist:"{self.artist}" track:"{self.title}"'
        resp = requests.get(
            "https://api.deezer.com/search",
            params={"q": query, "limit": 1}, timeout=8
        )
        data = resp.json().get("data", []) if resp.status_code == 200 else []
        if not data:
            # Repli : recherche libre "artiste titre"
            resp = requests.get(
                "https://api.deezer.com/search",
                params={"q": f"{self.artist} {self.title}", "limit": 1}, timeout=8
            )
            data = resp.json().get("data", []) if resp.status_code == 200 else []
        if data:
            album = data[0].get("album", {})
            return album.get("cover_big") or album.get("cover_medium") or album.get("cover_small")
        return None


class CoverDownloadThread(QThread):
    # (index de ligne, données binaires de l'image)
    cover_ready = pyqtSignal(int, bytes)
    finished_signal = pyqtSignal()

    def __init__(self, items, size="small"):
        """
        :param items: liste de tuples (index_ligne, url_image)
        :param size:  "small" (rapide, vignettes) ou "big" (en-tête d'album)
        """
        super().__init__()
        self.items = items
        self.size = size
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        for row, url in self.items:
            if not self._running:
                break
            data = self._download(url)
            if data and self._running:
                self.cover_ready.emit(row, data)
        self.finished_signal.emit()

    def _download(self, url):
        if not url:
            return None
        try:
            # Pour les vignettes de liste, on privilégie la petite taille
            # (plus rapide) ; l'en-tête d'album conserve la taille demandée.
            if self.size == "small":
                url = url.replace("cover_medium", "cover_small").replace(
                    "picture_medium", "picture_small"
                )
            resp = requests.get(url, timeout=6)
            if resp.status_code == 200:
                return resp.content
        except Exception:
            pass
        return None
