"""
Thread de téléchargement d'une piste.

Le téléchargement se fait via YouTube (yt-dlp). Le thread gère la
progression, les tentatives multiples en cas d'échec (``max_retries``),
le rangement du fichier produit et l'application des métadonnées
(tags ID3 / FLAC + jaquette).

Le rangement n'est pas décidé ici : il suit les règles de l'écran
« Organiser » (``library_rules``), passées par ``layout``. Un titre extrait
atterrit donc exactement là où l'analyse de la bibliothèque l'aurait mis,
sous le nom qu'elle lui aurait donné. ``layout=None`` conserve le
comportement d'un remplacement sur place : le fichier reste dans le dossier
qu'on lui indique et prend le nom de la convention historique — c'est ce
qu'attend le réencodage, dont le but est justement de ne rien déplacer.
"""

import os
import re
import time
import base64
import shutil
import tempfile

import requests

from PyQt6.QtCore import QThread, pyqtSignal

from deekeep.audio import tag_lookup
from deekeep.audio import tag_write
from deekeep.library import library_rules
from deekeep.core.utils import sanitize_filename, ffmpeg_location, stamp_app_icon
from deekeep.core import config


# Clients de lecture YouTube interrogés par yt-dlp, par ordre de préférence.
# Le choix par défaut de yt-dlp ne suffit plus : YouTube refuse ses propres
# flux d'un « HTTP Error 403: Forbidden », ou n'en publie plus aucun sans
# jeton PO. La boucle de tentatives de ``run`` s'y épuisait en vain — dix
# essais, dix refus, la piste abandonnée — car un refus par le client de
# lecture ne se dissipe pas, lui, à l'essai suivant.
#
# « web_embedded » vient en tête : c'est le seul des trois à publier des
# formats audio seuls (opus ~131 kb/s, m4a ~129 kb/s), donc à ne pas faire
# passer la vidéo entière par le réseau pour n'en garder que le son. Les deux
# suivants ne servent que de repli — une vidéo dont l'intégration est refusée
# n'expose aucun format côté « web_embedded » — et livrent le flux muxé, dont
# FFmpeg extrait ensuite l'audio.
YOUTUBE_PLAYER_CLIENTS = ['web_embedded', 'mweb', 'tv_simply']

# Nombre de vidéos examinées par requête. La première n'est pas toujours
# exploitable — vidéo soumise à vérification d'âge, privée, retirée — et s'y
# arrêter condamnait le titre alors que la deuxième réponse le porte tout
# aussi bien.
SEARCH_CANDIDATES = 3

# Part des mots du titre qu'une vidéo doit reprendre pour être tenue pour la
# bonne. Le réencodage *remplace* le fichier d'origine : accepter la première
# réponse de YouTube sans la vérifier revient à écraser un titre par ce que
# la recherche a bien voulu rendre — une vidéo sans rapport quand les tags du
# fichier ne mènent à rien.
MIN_TITLE_MATCH = 0.7

# Échecs qu'une nouvelle tentative ne dissipera pas : la vidéo est hors
# d'atteinte, pas momentanément indisponible. Les distinguer évite d'épuiser
# dix tentatives — et une minute — sur un refus définitif, et permet de dire
# à l'utilisateur ce qui bloque au lieu d'un « Erreur » muet.
_PERMANENT_MARKERS = (
    "confirm your age", "age-restricted", "private video",
    "removed by the uploader", "members-only", "this video is unavailable",
    "video unavailable", "not available in your country", "who has blocked it",
)


def _is_permanent(message):
    """Vrai si l'échec ne se dissipera pas à l'essai suivant."""
    text = (message or "").lower()
    return any(marker in text for marker in _PERMANENT_MARKERS)


def search_queries(artist, title):
    """Requêtes YouTube à essayer, de la plus fidèle à la plus large.

    La première est celle qu'a toujours employée l'application : artiste,
    titre et « (Official Audio) ». Les suivantes n'existent que pour les
    fichiers dont les tags portent du bruit — un tampon de site, une mention
    de version que le catalogue nomme autrement, un invité accolé à
    l'artiste. YouTube ne rend alors *aucun* résultat, et la relancer dix
    fois telle quelle ne pouvait rien y changer.
    """
    queries = []
    performer = tag_lookup.main_artist(artist)
    for candidate in (
        f"{artist} - {title} (Official Audio)",
        f"{performer} - {tag_lookup.clean_stem(title)}",
        f"{performer} - {tag_lookup.core_title(title)}",
    ):
        query = re.sub(r"\s+", " ", candidate).strip(" -")
        if query and query not in queries:
            queries.append(query)
    return queries


class _YtdlpLogger:
    """Journal yt-dlp muet : ses messages ne vont plus sur la console.

    Sans ``logger``, yt-dlp écrit ses propres « ERROR: ... » sur la sortie
    d'erreur du processus, et ``quiet`` n'y change rien. Or un échec n'est pas
    une fin : YouTube refuse une bonne part des premières tentatives par un
    « HTTP Error 403: Forbidden » transitoire (l'URL de flux est signée par un
    nœud du CDN qui la rejette ensuite ; une nouvelle extraction en donne une
    autre, qui passe). La boucle de ``run`` réessaie précisément pour cela et
    aboutit presque toujours en deux ou trois essais — mais la console se
    remplissait d'erreurs pour des extractions réussies, donnant à croire à
    une panne.

    Rien n'est retenu ici : détourner l'affichage n'empêche pas yt-dlp de
    lever son ``DownloadError``, dont le message porte déjà la cause exacte.
    C'est lui que ``download_via_youtube`` rapporte, et c'est par
    ``finished_signal`` que le sort de la piste arrive à l'interface.
    """

    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


class TrackDownloaderThread(QThread):
    progress_signal = pyqtSignal(int, int, str)      # row_index, pourcentage, statut
    finished_signal = pyqtSignal(int, bool, str, dict)  # row_index, success, msg, track_info

    def __init__(self, row_index, track_id, title, artist, album, save_dir,
                 file_format, quality, cover_bytes, layout=None,
                 max_retries=10, cookies_browser=None, cookies_file=None,
                 original_path=None, resolve_metadata=False, extra_tags=None,
                 tally=None):
        super().__init__()
        self.row_index = row_index
        self.track_id = track_id
        self.title = title
        self.artist = artist
        self.album = album
        self.save_dir = save_dir
        self.file_format = file_format.lower()
        self.quality = quality
        # Règles de rangement (voir library_rules.rules_from_settings) ;
        # None = ne rien déplacer, écrire dans ``save_dir``.
        self.layout = layout
        # Décompte des titres par artiste du lot en cours
        # (``library_rules.Tally``), partagé par les fils d'un même lot : sans
        # lui, chaque extraction se croit seule de son artiste et la règle
        # « Divers » l'y envoie, dix titres en parallèle comme un seul.
        self.tally = tally
        # Métadonnées complémentaires (artiste d'album, genre, date, rang
        # dans l'album) : un motif de nommage peut les réclamer, et elles
        # doivent se retrouver dans le fichier produit. Celles que l'appelant
        # fournit — les tags du fichier remplacé, lors d'un réencodage — font
        # foi ; les autres sont demandées à Deezer (voir _resolve_extra_tags).
        self.extra_tags = dict(extra_tags or {})
        self._extras_resolved = False
        self.cover_bytes = cover_bytes
        self.max_retries = max_retries
        # Réencodage : chemin du fichier source d'origine à remplacer.
        # Après un réencodage réussi, ce fichier est supprimé s'il diffère
        # du fichier produit (par ex. changement d'extension MP3 → FLAC),
        # de sorte que le dossier ne conserve que la version réencodée.
        self.original_path = original_path
        # Nom du navigateur (clé yt-dlp : "chrome", "firefox", "edge"...)
        # dont les cookies YouTube sont réutilisés ; None = anonyme.
        self.cookies_browser = cookies_browser
        # Fichier cookies.txt (format Netscape) ; prioritaire sur le
        # navigateur car c'est la seule méthode fiable avec Chrome/Edge.
        self.cookies_file = cookies_file
        # Réencodage sans correspondance Deezer préalable : interroger Deezer
        # au moment de la conversion pour récupérer jaquette + métadonnées.
        self.resolve_metadata = resolve_metadata
        self._resolved = False
        self.attempts = 0  # nombre de tentatives réellement effectuées
        # Vidéos retenues par la recherche, gardées d'une tentative à
        # l'autre : la recherche a déjà été payée, et la relancer donnerait
        # les mêmes réponses.
        self._candidates = None
        # Échec définitif rencontré : la boucle de ``run`` s'arrête au lieu
        # d'épuiser ses dix tentatives (voir _is_permanent).
        self._permanent_failure = ""

    def _emit_progress(self, percent, text):
        """Émet la progression en rappelant la tentative en cours (si > 1)."""
        if self.attempts > 1:
            text = f"{text} (essai {self.attempts}/{self.max_retries})"
        self.progress_signal.emit(self.row_index, percent, text)

    def _cover_b64(self):
        """Jaquette encodée en base64 (JSON-compatible) pour l'historique."""
        if self.cover_bytes:
            try:
                return base64.b64encode(self.cover_bytes).decode("ascii")
            except Exception:
                return None
        return None

    def _tags(self):
        """Métadonnées de la piste, sous les clés attendues par ``name_pattern``."""
        tags = dict(self.extra_tags)
        tags.update({
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "format": self.file_format.upper(),
        })
        return tags

    def _destination(self):
        """(dossier, nom sans extension) visés pour le fichier produit.

        Appelé une fois les métadonnées Deezer résolues : l'album et l'artiste
        qu'elle rapporte décident du dossier, et les tenir pour acquis avant
        la résolution rangerait le titre d'après les tags du fichier abîmé.
        """
        extension = "." + self.file_format
        if not self.layout:
            # Remplacement sur place : le dossier est imposé, le nom suit la
            # convention historique de DeeKeep.
            return self.save_dir, library_rules.target_stem(self._tags(), None)
        path = library_rules.destination(
            self.save_dir, self._tags(), self.layout, extension,
            exclude=self.original_path, tally=self.tally,
        )
        return os.path.dirname(path), os.path.splitext(os.path.basename(path))[0]

    def run(self):
        # Tenter le téléchargement jusqu'à ``max_retries`` fois en cas d'échec,
        # avec une attente de plus en plus longue entre les tentatives
        # (jusqu'à 5 secondes avant la dernière).
        last_msg = ""
        for attempt in range(1, self.max_retries + 1):
            self.attempts = attempt
            try:
                success, msg, info = self.download_via_youtube()
            except Exception as e:
                success, msg, info = False, f"Erreur: {str(e)}", {}

            if success:
                self.finished_signal.emit(self.row_index, True, msg, info)
                return
            last_msg = msg

            # Refus définitif (aucune vidéo trouvée, toutes hors d'atteinte) :
            # les neuf tentatives suivantes rendraient le même verdict une
            # minute plus tard. On s'arrête en disant ce qui bloque.
            if self._permanent_failure:
                self.finished_signal.emit(
                    self.row_index, False, self._permanent_failure, {})
                return

            # Échec de lecture des cookies du navigateur (base verrouillée
            # tant que Chrome/Edge est ouvert) : réessayer ne sert à rien,
            # on s'arrête tout de suite avec une explication.
            if self.cookies_browser and "cookie" in msg.lower():
                self.finished_signal.emit(
                    self.row_index, False,
                    f"Cookies {self.cookies_browser} illisibles : fermez "
                    f"complètement le navigateur, ou choisissez Firefox / "
                    f"« Aucun (anonyme) » dans les Paramètres. ({msg})", {},
                )
                return

            if attempt < self.max_retries:
                delay = min(5.0, attempt * 5.0 / max(1, self.max_retries - 1))
                self.progress_signal.emit(
                    self.row_index, 5,
                    f"Échec, nouvelle tentative dans {delay:.1f} s "
                    f"(essai {attempt + 1}/{self.max_retries})...",
                )
                time.sleep(delay)

        self.finished_signal.emit(
            self.row_index, False,
            f"Abandonné après {self.max_retries} tentatives : {last_msg}", {},
        )

    def _resolve_deezer_metadata(self):
        """Interroge Deezer (titre/artiste locaux) pour récupérer jaquette et
        métadonnées propres. Exécuté une seule fois, dans le thread de travail.
        """
        if not self.resolve_metadata or self._resolved:
            return
        self._resolved = True
        try:
            from deekeep.workers.search_thread import deezer_best_match
            self._emit_progress(3, "Recherche Deezer...")
            match = deezer_best_match(self.artist, self.title)
            if not match:
                return
            # L'identifiant de la piste reconnue ouvre l'accès aux tags que
            # la recherche ne rapporte pas (voir _resolve_extra_tags).
            if not self.track_id and match.get("id"):
                self.track_id = match["id"]
            if match.get("title"):
                self.title = match["title"]
            if match.get("artist"):
                self.artist = match["artist"]
            if match.get("album"):
                self.album = match["album"]
            url = match.get("cover_big") or match.get("cover_url")
            if url:
                resp = requests.get(url, timeout=8)
                if resp.status_code == 200:
                    self.cover_bytes = resp.content
        except Exception:
            pass  # échec de résolution : on convertit avec les tags locaux

    def _resolve_extra_tags(self):
        """Complète ``extra_tags`` des tags que seul le catalogue connaît.

        La recherche Deezer ne rapporte que titre, artiste et album ; le
        genre, l'artiste d'album, la date de parution et le rang dans
        l'album se demandent à part, à partir de l'identifiant de la piste.
        Sans eux, le fichier produit ne porterait que trois tags — et un
        motif de nommage réclamant l'année ne rendrait rien.

        Les valeurs déjà fournies par l'appelant ne sont pas touchées : les
        tags du fichier qu'un réencodage remplace sont ceux de
        l'utilisateur, et l'édition qu'il possède n'est pas forcément celle
        du catalogue.
        """
        if self._extras_resolved or not self.track_id:
            return
        self._extras_resolved = True
        try:
            from deekeep.audio import tag_lookup
            found = tag_lookup.deezer_details(self.track_id)
        except Exception:
            return      # tags complémentaires indisponibles : sans gravité
        for field, value in found.items():
            if not str(self.extra_tags.get(field) or "").strip():
                self.extra_tags[field] = value

    def _search(self, query):
        """(identifiant, titre) des premières vidéos répondant à ``query``.

        Recherche « à plat » : yt-dlp se contente de lire la page de
        résultats, sans extraire chaque vidéo — ce qui coûterait presque
        autant que le téléchargement lui-même. Une liste vide signifie que
        YouTube n'a rien rendu, ce qui arrive dès qu'une requête est trop
        précise (voir ``search_queries``).
        """
        opts = {
            'quiet': True,
            'logger': _YtdlpLogger(),
            'skip_download': True,
            'extract_flat': True,
            'noplaylist': True,
        }
        try:
            with config.yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    f"ytsearch{SEARCH_CANDIDATES}:{query}", download=False)
        except Exception:
            return []
        return [
            (entry.get("id"), entry.get("title") or "")
            for entry in (info.get("entries") or [])
            if entry and entry.get("id")
        ]

    def _youtube_candidates(self):
        """Vidéos à essayer pour ce titre, la plus probable en tête.

        Les requêtes sont essayées de la plus fidèle à la plus large
        (``search_queries``) et l'on s'arrête dès que l'une rend une vidéo
        convaincante : le titre du fichier doit se retrouver dans celui de la
        vidéo (``MIN_TITLE_MATCH``). Cette vérification est la contrepartie
        indispensable de l'élargissement des requêtes — une recherche large
        finit toujours par rendre *quelque chose*, et le réencodage écrase le
        fichier d'origine avec ce qu'elle a rapporté.

        Le résultat est gardé : les tentatives suivantes de ``run`` reprennent
        la même liste au lieu de repayer la recherche.
        """
        if self._candidates is not None:
            return self._candidates

        wanted = tag_lookup.core_title(self.title)
        artist = tag_lookup.main_artist(self.artist)
        kept, seen = [], set()
        for query in search_queries(self.artist, self.title):
            self._emit_progress(4, "Recherche YouTube...")
            for video_id, name in self._search(query):
                if video_id in seen:
                    continue
                seen.add(video_id)
                score = tag_lookup.contained(wanted, name)
                if score < MIN_TITLE_MATCH:
                    continue
                # Le nom de l'artiste dans le titre de la vidéo lève le
                # dernier doute : entre deux morceaux homonymes, celui qui
                # l'annonce est le bon.
                if tag_lookup.contained(artist, name) >= MIN_TITLE_MATCH:
                    score += 1
                kept.append((score, video_id, name))
            if kept:
                break

        # Tri stable : à score égal, l'ordre de pertinence de YouTube est
        # conservé.
        kept.sort(key=lambda entry: -entry[0])
        self._candidates = [(video_id, name) for _, video_id, name in kept]
        return self._candidates

    def _extract_candidate(self, video_id, final_dir, filename_clean,
                           output_filepath):
        """Extrait une vidéo précise et installe le fichier produit.

        Retourne (succès, message). Le message d'échec est rendu tel que
        yt-dlp le formule : c'est lui qui dit si la vidéo est soumise à
        vérification d'âge, privée, ou simplement refusée le temps d'une
        tentative (voir ``_is_permanent``).
        """
        ytdl_quality = '320'
        if "128" in self.quality:
            ytdl_quality = '128'
        elif "flac" in self.file_format or "wav" in self.file_format:
            ytdl_quality = '0'

        def ytdl_hook(d):
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 1
                downloaded = d.get('downloaded_bytes', 0)
                percent = int((downloaded / total) * 90)
                self._emit_progress(max(5, percent), "Extraction...")
            elif d['status'] == 'finished':
                self._emit_progress(95, "Finalisation...")

        # Chaque téléchargement travaille dans son propre dossier temporaire
        # (unique) : les fichiers intermédiaires (.webm/.part) de plusieurs
        # extractions simultanées ne peuvent plus entrer en collision ni
        # rester verrouillés d'une extraction à l'autre. C'est ce qui évite
        # l'erreur Windows « fichier utilisé par un autre processus ».
        work_dir = tempfile.mkdtemp(prefix=".dk_", dir=final_dir)
        try:
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(work_dir, f"{filename_clean}.%(ext)s"),
                'noplaylist': True,
                'quiet': True,
                # Détourner les messages de yt-dlp : sans cela, chaque
                # tentative refusée par YouTube s'affiche sur la console
                # alors que la suivante aboutit (voir _YtdlpLogger).
                'logger': _YtdlpLogger(),
                # Écraser un fichier existant portant le même nom.
                'overwrites': True,
                # Autoriser yt-dlp à récupérer les composants distants (EJS)
                # qui résolvent les défis JavaScript de YouTube. Sans eux,
                # l'extraction échoue désormais en HTTP 403 Forbidden malgré
                # la présence du runtime Deno. Équivaut à l'option CLI
                # « --remote-components ejs:github » (méthode recommandée).
                'remote_components': ['ejs:github'],
                # Client de lecture imposé (voir YOUTUBE_PLAYER_CLIENTS) :
                # celui que yt-dlp choisit seul se fait désormais refuser
                # ses flux par YouTube.
                'extractor_args': {
                    'youtube': {'player_client': YOUTUBE_PLAYER_CLIENTS},
                },
                'progress_hooks': [ytdl_hook],
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': self.file_format,
                    'preferredquality': ytdl_quality,
                }],
            }

            # S'identifier auprès de YouTube : soit via un fichier cookies.txt
            # exporté (seule méthode fiable avec Chrome/Edge sous Windows),
            # soit en lisant directement les cookies du navigateur choisi.
            if self.cookies_file and os.path.exists(self.cookies_file):
                ydl_opts['cookiefile'] = self.cookies_file
            elif self.cookies_browser:
                ydl_opts['cookiesfrombrowser'] = (self.cookies_browser,)

            # FFmpeg embarqué avec l'application : le poste n'a pas besoin
            # d'avoir FFmpeg installé ni dans son PATH.
            bundled_ffmpeg = ffmpeg_location()
            if bundled_ffmpeg:
                ydl_opts['ffmpeg_location'] = bundled_ffmpeg

            # La vidéo est désignée par son adresse, non par une recherche :
            # c'est ``_youtube_candidates`` qui a choisi, et lui seul sait à
            # quelle vidéo passer si celle-ci se refuse.
            try:
                with config.yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            except Exception as e:
                return False, f"Erreur: {str(e)}"

            # Fichier audio produit dans le dossier temporaire.
            tmp_output = os.path.join(work_dir, f"{filename_clean}.{self.file_format}")
            if not os.path.exists(tmp_output):
                # Repli : retrouver le fichier extrait quelle que soit son
                # extension exacte (certains formats diffèrent légèrement).
                produced = [
                    os.path.join(work_dir, f) for f in os.listdir(work_dir)
                ]
                audio_files = [p for p in produced if not p.endswith((".part", ".ytdl"))]
                if not audio_files:
                    return False, "Fichier extrait introuvable."
                tmp_output = max(audio_files, key=os.path.getsize)

            self.apply_metadata(tmp_output)

            # Supprimer d'éventuels doublons dans le dossier de destination
            # sans toucher au fichier source d'origine d'un réencodage :
            # le nom visé, la convention historique « Titre - Artiste » et
            # l'ancienne « Artiste - Titre ». Un motif personnalisé ne dit
            # rien des noms qu'ont pris les extractions précédentes.
            legacy_names = (
                sanitize_filename(f"{self.title} - {self.artist}"),
                sanitize_filename(f"{self.artist} - {self.title}"),
            )
            for candidate in (filename_clean,) + legacy_names:
                path = os.path.join(final_dir, f"{candidate}.{self.file_format}")
                if self.original_path and os.path.abspath(path) == os.path.abspath(self.original_path):
                    continue
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

            # Déplacer le fichier finalisé vers sa destination (même volume
            # que le dossier temporaire → remplacement atomique).
            os.replace(tmp_output, output_filepath)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        return True, ""

    def download_via_youtube(self):
        """Retourne (success, message, track_info)."""
        if not config.YTDLP_AVAILABLE:
            return False, "yt-dlp est manquant.", {}

        try:
            # Récupérer jaquette et métadonnées propres depuis Deezer avant de
            # construire la requête YouTube (réencodage de fichiers non appariés).
            self._resolve_deezer_metadata()
            # Avant _destination : le motif de nommage peut réclamer l'année
            # ou le numéro de piste, qui ne seront connus qu'ici.
            self._resolve_extra_tags()

            self._emit_progress(5, "Initialisation...")
            # Dossier et nom du fichier produit : ce sont les règles de
            # l'écran « Organiser » qui les fixent (voir _destination).
            final_dir, filename_clean = self._destination()
            os.makedirs(final_dir, exist_ok=True)
            output_filepath = os.path.join(
                final_dir, f"{filename_clean}.{self.file_format}")

            candidates = self._youtube_candidates()
            if not candidates:
                # Rien de convaincant : le dire, plutôt que de télécharger la
                # première vidéo venue par-dessus le fichier de l'utilisateur.
                # Des tags approximatifs — un tampon de site collé au titre, un
                # artiste inexact — en sont la cause la plus fréquente ; la
                # colonne « Correspondance » les corrige quand Deezer reconnaît
                # le titre.
                self._permanent_failure = (
                    f"Aucune vidéo ne correspond à « {tag_lookup.main_artist(self.artist)}"
                    f" - {tag_lookup.core_title(self.title)} » sur YouTube : les "
                    f"tags du "
                    f"fichier ne mènent à rien de reconnaissable."
                )
                return False, self._permanent_failure, {}

            last_msg = ""
            for position, (video_id, name) in enumerate(candidates, 1):
                if position > 1:
                    self._emit_progress(
                        5, f"Autre version ({position}/{len(candidates)})...")
                ok, last_msg = self._extract_candidate(
                    video_id, final_dir, filename_clean, output_filepath)
                if ok:
                    break
                # Échec passager (403 signé par un nœud du CDN, coupure
                # réseau) : la vidéo reste la bonne, c'est la tentative qui est
                # à refaire — la boucle de ``run`` s'en charge. Passer à la
                # suivante dégraderait le choix pour rien.
                if not _is_permanent(last_msg):
                    return False, last_msg, {}
            else:
                refused = (
                    "La seule vidéo trouvée n'est pas accessible"
                    if len(candidates) == 1 else
                    f"Aucune des {len(candidates)} vidéos trouvées n'est accessible"
                )
                self._permanent_failure = f"{refused} : {last_msg}"
                return False, self._permanent_failure, {}

            # Réencodage réussi : remplacer le fichier d'origine en le
            # supprimant s'il diffère du fichier produit (extension changée).
            if self.original_path and os.path.exists(self.original_path):
                if os.path.abspath(self.original_path) != os.path.abspath(output_filepath):
                    try:
                        os.remove(self.original_path)
                    except OSError:
                        pass

            self._emit_progress(100, "Terminé ✓")
            info = {"title": self.title, "artist": self.artist, "album": self.album,
                    "path": output_filepath, "cover_b64": self._cover_b64(),
                    # Tags complémentaires écrits dans le fichier : notés ici
                    # pour que l'historique reste renseigné même si le fichier
                    # disparaît du disque (voir db_init.merge_history).
                    "tags": tag_write.clean_values(self.extra_tags)}
            return True, "Sauvegardé", info
        except Exception as e:
            return False, f"Erreur: {str(e)}", {}

    def apply_metadata(self, filepath):
        """Pose les tags et la jaquette sur le fichier produit.

        Tous les tags connus y passent, non les trois de base seuls : un
        fichier qui n'annonce ni son genre, ni sa date, ni son rang dans
        l'album est incomplet partout où on le relit — écran « Organiser »,
        motifs de nommage, colonnes de la base. La traduction vers les
        trames du conteneur est celle de ``tag_write``, commune à tous les
        écrivains de l'application.
        """
        if not config.MUTAGEN_AVAILABLE or not os.path.exists(filepath):
            return
        # Jaquette signée de l'icône de l'application : tout fichier écrit ici
        # — ajouté, téléchargé ou réencodé — porte la marque de DeeKeep. Seule
        # la copie intégrée au fichier est marquée ; ``self.cover_bytes`` reste
        # la pochette d'origine, celle qu'affiche l'interface et que garde
        # l'historique.
        cover_bytes = stamp_app_icon(self.cover_bytes)
        ok, message = tag_write.write(filepath, self._tags(), cover_bytes)
        if not ok:
            print(f"[Tags] Erreur lors de l'application : {message}")
