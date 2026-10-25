"""
Miroir Opus de la bibliothèque : la même musique, dix fois plus légère.

Le problème posé était simple à énoncer et sans solution directe : une
bibliothèque de 14 Go, et aucune base gratuite qui accepte d'en héberger le
flux audio — Turso plafonne à 5 Go, le plus généreux des concurrents à 10 Gio,
et aucune ne sait servir un morceau par tranches. Le seau de stockage objet
(voir ``blob_store``) lève la seconde objection ; celui-ci lève la première.

Un fichier est **réencodé en Opus** par le ffmpeg livré avec l'application :
à 128 kb/s le codec est transparent ou presque sur de la musique — c'est le
débit auquel les services de diffusion servent leur qualité haute — et une
bibliothèque en MP3 320 ou en FLAC y perd les deux tiers de son poids.

Encore faut-il que la bibliothèque soit lourde. Mesure faite sur celle de
référence, disque entier : **3 576 de ses 3 581 fichiers sont déjà à 128 kb/s
ou moins**, et les réencoder en Opus 128 ne rend pas un octet — huit titres
tirés au hasard pèsent 30,4 Mo avant, 30,4 Mo après. Le réencodage ne gagne
quelque chose qu'en **descendant** sous le débit de la source. D'où deux
règles :

  * ``plan`` compare le débit du fichier à celui demandé, et **envoie le
    fichier tel quel** quand il est déjà le plus léger des deux. Le faire
    passer par une deuxième compression avec perte pour un poids identique
    serait le pire des deux mondes ;
  * l'écran Bibliothèque affiche, pour chaque débit proposé (64 à 192 kb/s),
    ce que la bibliothèque **pèsera** une fois envoyée, comparé aux 10 Go
    gratuits. Le débit ne se choisit pas dans l'abstrait.

Les originaux ne bougent pas. Le miroir ne remplace rien : il double la
bibliothèque en léger, pour l'écouter d'ailleurs et la retrouver après une
réinstallation. La jaquette n'y est pas recopiée — la table ``covers`` la
détient déjà, dédoublonnée, et une image de 100 Ko par titre pèserait plus
lourd que ce que le réencodage fait gagner sur trois minutes de musique.

Deux clés évitent le travail inutile :

  * l'**empreinte date+taille** du fichier source (``stamp``) dit si le
    fichier a changé depuis son dernier envoi — un titre retéléchargé par
    l'écran Qualité est ainsi repéré, et lui seul est réexpédié ;
  * l'**empreinte SHA-1** de son contenu donne la clé de l'objet. Deux copies
    du même fichier, dans deux dossiers, n'occupent qu'une place dans le
    seau ; et un objet déjà présent n'est ni réencodé ni renvoyé, ce qui rend
    un envoi interrompu reprenable à peu de frais.

Le chemin **inverse** existe aussi (``RestoreThread``), et c'est lui qui
donne au miroir sa raison d'être : redescendre du seau les titres que le
disque n'a plus. Ce qui était parti tel quel revient **octet pour octet**,
empreinte SHA-1 revérifiée après écriture — la bibliothèque se reconstitue à
l'identique, sans que rien n'ait été réencodé au passage. Ce qui avait été
réduit en Opus n'a, lui, pas d'original à rendre : il revient sous
l'extension « .opus », à côté du nom d'origine, et se donne pour ce qu'il
est. Rien n'est jamais écrasé.
"""

import hashlib
import os
import subprocess
import tempfile
import time

from PyQt6.QtCore import QThread, pyqtSignal

from deekeep.audio.audio_probe import NO_WINDOW, ffmpeg_exe
from deekeep.core import config
from deekeep.db import blob_store


# Débits proposés. Le choix se fait sur mesure, jauge en main (voir l'écran
# Bibliothèque) : le bon débit dépend entièrement de ce que contient la
# bibliothèque. Sur une collection déjà en MP3 128, viser 128 en Opus ne
# gagnerait pas un octet — il faut descendre pour libérer de la place. Sur du
# MP3 320 ou du FLAC, 128 divise déjà le poids par trois.
#
# 80 kb/s n'était pas dans la liste, et c'est pourtant le seul palier qui
# réponde au cas de la bibliothèque de référence : 239 h 44 de musique, que
# 96 kb/s porterait à 10,36 Go — soit 0,36 Go **au-dessus** de l'offre
# gratuite, donc de la qualité perdue sans que le problème soit résolu. À 80,
# le miroir pèse 8,63 Go et tient, avec de la marge. Entre deux paliers, le
# bon n'est pas le plus proche : c'est le premier qui passe.
BITRATES = (64, 80, 96, 128, 160, 192)
DEFAULT_BITRATE = 128

# Clé de configuration du débit, rangée avec les autres paramètres.
BITRATE_SETTING = "mirror_bitrate"

# Miroir « tout tel quel » : aucun réencodage, le seau reçoit les fichiers
# d'origine. Le miroir pèse alors exactement le poids du disque — et se
# restaure **intégralement** à l'identique, ce qu'un miroir Opus ne peut pas
# promettre. Encore faut-il que le disque tienne dans l'offre : la
# bibliothèque de référence pèse 14,08 Go pour 10 Go offerts, et ne peut donc
# pas y passer entière. Le réglage vaut pour une bibliothèque plus petite, ou
# pour la part qu'on choisit d'y mettre — la jauge tranche, pas le principe.
SOURCE_ONLY_SETTING = "mirror_source_only"

# Un réencodage dure quelques secondes ; au-delà de cette limite, ffmpeg est
# tenu pour bloqué (fichier illisible, disque en défaut).
TRANSCODE_TIMEOUT = 300

# Débit du MP3 rendu à la restauration d'un objet Opus. Il n'a pas à être
# économe : la contrainte de place était celle du seau, pas celle du disque.
# Il doit en revanche être assez large pour que le réencodage n'ajoute rien
# d'audible par-dessus l'Opus — dont le contenu est déjà borné dans les
# aigus. À 192, LAME le restitue sans y toucher ; viser 80 pour « rester au
# débit d'origine » empilerait au contraire la faiblesse du MP3 à bas débit
# sur celle de l'Opus.
RESTORE_MP3_BITRATE = 192

# Rayon des objets envoyés tels quels (voir ``object_key``). C'est cette
# marque qui distingue, à la restauration, une copie conforme d'une simple
# réduction Opus.
SOURCE_SHELF = "audio/source/"

# États d'un titre vis-à-vis du miroir.
ABSENT = "absent"          # jamais envoyé
FRESH = "fresh"            # envoyé, et le fichier n'a pas bougé depuis
STALE = "stale"            # envoyé, mais le fichier a changé depuis


def canonical_path(path):
    """Chemin normalisé, casse conservée : la clé de la table des titres.

    Reprise de ``db_init.canonical_path`` — ce module ne charge pas l'outil
    en ligne de commande pour une ligne.
    """
    return os.path.normpath(str(path or "").replace("/", os.sep))


def stamp(path):
    """Empreinte date+taille d'un fichier, ou "" s'il est absent.

    Même forme que celle de l'écran Qualité : le fichier remplacé se
    reconnaît sans avoir à relire son contenu.
    """
    try:
        info = os.stat(path)
    except OSError:
        return ""
    return "%d:%d" % (int(info.st_mtime), info.st_size)


def file_sha1(path, chunk=1 << 20):
    """Empreinte SHA-1 du contenu d'un fichier, lue par tranches de 1 Mo."""
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def probe_bitrate(path):
    """Débit réel d'un fichier, en kb/s, ou 0 s'il reste indéchiffrable.

    La table des titres ne le connaît pas toujours : sur la bibliothèque de
    référence, 198 lignes portent un débit nul — durée comprise — pour des
    titres entrés sans que leurs en-têtes aient jamais été lus. Les relire ne
    coûte qu'une ouverture mutagen, sans sous-processus, et c'est ce qui
    évite de réencoder à l'aveugle ce qui n'a rien à y gagner : 197 de ces
    198 fichiers sont des MP3 128, le dernier étant tronqué.

    Deux lectures, dans cet ordre : l'en-tête, puis — s'il ne déclare rien,
    ce qui arrive en débit variable — le rapport entre la taille du fichier
    et sa durée.
    """
    if config.mutagen is None:
        return 0
    try:
        info = getattr(config.mutagen.File(path), "info", None)
        if info is None:
            return 0
        bitrate = getattr(info, "bitrate", 0) or 0
        if bitrate > 0:
            return int(round(bitrate / 1000.0))
        length = getattr(info, "length", 0) or 0
        if length > 0:
            return int(round(os.path.getsize(path) * 8 / length / 1000.0))
    except Exception:
        # Fichier tronqué, en-tête introuvable : rendre 0 laisse ``plan``
        # l'envoyer tel quel, ce qui vaut mieux que de lancer ffmpeg sur un
        # fichier abîmé pour en tirer un Opus abîmé.
        pass
    return 0


# Type de contenu des fichiers envoyés tels quels, par extension.
CONTENT_TYPES = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/wav", ".wma": "audio/x-ms-wma", ".aiff": "audio/aiff",
    ".aif": "audio/aiff", ".alac": "audio/mp4",
}


def plan(source_bitrate, target, source_only=False):
    """Ce qu'il faut faire d'un fichier : (débit retenu, copie telle quelle ?).

    Réencoder un MP3 128 en Opus 128 ne gagnerait pas un octet et lui ferait
    subir une deuxième compression avec perte : le fichier est alors **envoyé
    tel quel**. Le réencodage n'a de sens qu'en descendant.

    ``source_only`` supprime le réencodage tout court — voir
    ``SOURCE_ONLY_SETTING``.

    Un **débit inconnu** vaut désormais « à laisser tel quel », et non « à
    réduire ». La règle inverse, qui valait jusqu'ici, réencodait à
    l'aveugle : mesure faite, les 332 fichiers dont la base ignorait le débit
    étaient 331 MP3 128 qu'une deuxième compression aurait abîmés sans rien
    faire gagner, et un fichier tronqué que ffmpeg n'aurait pas su lire. Ne
    rien savoir n'autorise pas à dégrader — d'autant que ``MirrorThread``
    relit le débit (``probe_bitrate``) avant d'en arriver là.

    Le débit rendu sert de repère de fraîcheur : c'est lui qu'on retrouve
    dans la ligne du miroir. Faute de mieux il vaut ``target``, jamais zéro —
    une ligne à zéro se comparerait mal et rendrait le titre éternellement
    « à renvoyer ».
    """
    rate = int(source_bitrate or 0)
    target = int(target or DEFAULT_BITRATE)
    if source_only or not rate or rate <= target:
        return rate or target, True
    return target, False


def object_key(prefix, sha1, bitrate, extension=".opus"):
    """Clé de l'objet dans le seau.

    Le débit fait partie du chemin : changer de débit ne doit pas écraser
    silencieusement des objets encodés autrement, ni faire passer pour à jour
    une copie qui ne l'est plus. Les fichiers envoyés tels quels ont leur
    propre rayon — leur poids ne dépend d'aucun réglage.
    """
    extension = (extension or ".opus").lower()
    if extension != ".opus":
        return "%s%s%s/%s%s" % (prefix, SOURCE_SHELF, sha1[:2], sha1, extension)
    return "%saudio/%d/%s/%s.opus" % (prefix, bitrate, sha1[:2], sha1)


def is_source_copy(key):
    """Vrai si l'objet est le fichier d'origine, et non une réduction Opus.

    C'est le **rayon** qui le dit, et non l'extension : un fichier déjà en
    Opus part lui aussi tel quel, et se reconnaître à son « .opus » le ferait
    passer pour un réencodage — donc pour une copie dégradée alors qu'il est
    l'original.
    """
    return SOURCE_SHELF in (key or "")


def restore_target(path, key):
    """Où écrire l'objet restauré : (destination, est-ce l'original ?).

    Un objet du rayon ``audio/source/`` est le fichier d'origine, octet pour
    octet : il retrouve son chemin exact, et la bibliothèque est reconstituée
    à l'identique.

    Un objet Opus, lui, n'est qu'une réduction du fichier disparu. Il est
    **reconverti en MP3** au passage (voir ``transcode_to_mp3``) et reprend le
    chemin d'origine : un autoradio ne lit pas le « .opus », et une copie de
    secours qui ne joue pas là où l'on écoute ne sert à rien. Le fichier rendu
    porte donc une extension qui ne ment pas sur son contenu — c'est bien un
    MP3 — mais il n'a plus la qualité de l'original, et l'écran le compte à
    part.

    Le chemin d'origine n'est repris tel quel que s'il finissait déjà en
    « .mp3 ». Un titre parti d'un « .flac » ou d'un « .m4a » revient sous
    « .mp3 » : l'extension suit le contenu, pas l'histoire.
    """
    if is_source_copy(key):
        return path, True
    base, extension = os.path.splitext(path)
    if extension.lower() == ".mp3":
        return path, False
    return base + ".mp3", False


def state_of(row, current_stamp):
    """État du miroir pour un titre, d'après sa ligne et son fichier."""
    if not row or not row.get("key"):
        return ABSENT
    if current_stamp and row.get("stamp") and row["stamp"] != current_stamp:
        return STALE
    return FRESH


def transcode(source, destination, bitrate=DEFAULT_BITRATE, registry=None):
    """Réencode ``source`` en Opus. Rend (succès, message d'erreur).

    ``registry`` reçoit le processus lancé, le temps qu'il tourne : c'est ce
    qui permet à un arrêt demandé de le tuer sans attendre la fin du
    fichier en cours.
    """
    exe = ffmpeg_exe()
    if exe is None:
        return False, ("FFmpeg est introuvable : le réencodage en Opus est "
                       "impossible sans lui.")
    args = [
        exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", source,
        # Pas de piste vidéo : la jaquette intégrée au fichier source n'a
        # rien à faire dans le miroir (voir l'en-tête du module).
        "-vn",
        "-map_metadata", "0",
        "-c:a", "libopus", "-b:a", "%dk" % bitrate,
        "-vbr", "on", "-application", "audio",
        destination,
    ]
    process = None
    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW)
        if registry is not None:
            registry.append(process)
        _out, err = process.communicate(timeout=TRANSCODE_TIMEOUT)
        code = process.returncode
    except subprocess.TimeoutExpired:
        if process is not None:
            process.kill()
            process.communicate()
        return False, "Réencodage interrompu : ffmpeg ne répond plus."
    except OSError as exc:
        return False, f"Réencodage impossible : {exc}"
    finally:
        if registry is not None and process is not None and process in registry:
            registry.remove(process)

    if code != 0:
        detail = (err or b"").decode("utf-8", "replace").strip()
        last = detail.splitlines()[-1] if detail else "cause inconnue"
        return False, f"Réencodage refusé : {last}"
    if not os.path.exists(destination) or os.path.getsize(destination) == 0:
        return False, "Réencodage sans résultat : fichier produit vide."
    return True, ""


def transcode_to_mp3(source, destination, bitrate=RESTORE_MP3_BITRATE,
                     registry=None):
    """Reconvertit un Opus restauré en MP3. Rend (succès, message d'erreur).

    ``-map_metadata 0`` reporte les tags que l'envoi avait conservés : le
    titre rendu ne revient pas anonyme. La jaquette, elle, n'est pas dans le
    miroir (voir l'en-tête du module) et ne peut donc pas revenir ici — c'est
    la table ``covers`` qui la détient.

    ``-id3v2_version 3`` plutôt que la version 4 par défaut : c'est celle que
    lisent les autoradios et les vieux baladeurs, et rendre un fichier que
    l'appareil visé ne sait pas étiqueter irait contre le but.
    """
    exe = ffmpeg_exe()
    if exe is None:
        return False, ("FFmpeg est introuvable : la conversion en MP3 est "
                       "impossible sans lui.")
    args = [
        exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", source,
        "-vn",
        "-map_metadata", "0",
        "-c:a", "libmp3lame", "-b:a", "%dk" % bitrate,
        "-id3v2_version", "3",
        destination,
    ]
    process = None
    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW)
        if registry is not None:
            registry.append(process)
        _out, err = process.communicate(timeout=TRANSCODE_TIMEOUT)
        code = process.returncode
    except subprocess.TimeoutExpired:
        if process is not None:
            process.kill()
            process.communicate()
        return False, "Conversion interrompue : ffmpeg ne répond plus."
    except OSError as exc:
        return False, f"Conversion impossible : {exc}"
    finally:
        if registry is not None and process is not None and process in registry:
            registry.remove(process)

    if code != 0:
        detail = (err or b"").decode("utf-8", "replace").strip()
        last = detail.splitlines()[-1] if detail else "cause inconnue"
        return False, f"Conversion refusée : {last}"
    if not os.path.exists(destination) or os.path.getsize(destination) == 0:
        return False, "Conversion sans résultat : fichier produit vide."
    return True, ""


def remove_from_mirror(store, row):
    """Efface du seau l'objet d'un titre. Rend (succès, message)."""
    key = (row or {}).get("key") or ""
    if not key:
        return True, ""
    try:
        store.delete(key)
    except blob_store.BlobError as exc:
        return False, str(exc)
    return True, ""


class MirrorThread(QThread):
    """Réencode et envoie une liste de fichiers vers le seau.

    Le travail est mené fichier par fichier — et non en parallèle : le
    réencodage sature déjà un cœur, et deux envois simultanés sur une
    liaison montante domestique ne vont pas plus vite que l'un après
    l'autre. L'intérêt d'un fil séparé est ailleurs : l'interface reste
    vivante, et la liste s'écoute pendant que le miroir se constitue.
    """

    # (chemin, état, ligne ou None, message) — une émission par fichier.
    done = pyqtSignal(str, str, object, str)
    # (traités, total)
    progress = pyqtSignal(int, int)
    # (envoyés, ignorés, échecs, octets envoyés)
    completed = pyqtSignal(int, int, int, int)

    # Suites possibles pour un fichier, telles qu'annoncées par ``done``.
    SENT = "sent"              # réencodé et envoyé
    REUSED = "reused"          # objet déjà présent dans le seau
    SKIPPED = "skipped"        # déjà à jour, rien à faire
    FAILED = "failed"          # échec, message à l'appui

    def __init__(self, items, known=None, bitrate=DEFAULT_BITRATE,
                 credentials=None, source_only=False, parent=None):
        super().__init__(parent)
        # ``items`` : des couples (chemin, débit du fichier source). Le débit
        # de la source décide si le fichier est réencodé ou envoyé tel quel —
        # la base le connaît déjà, inutile de relire les en-têtes.
        self._items = [(path, rate) for path, rate in items]
        # Lignes déjà en base, indexées par chemin : de quoi sauter ce qui
        # est à jour sans interroger le seau.
        self._known = dict(known or {})
        self._bitrate = int(bitrate or DEFAULT_BITRATE)
        # Miroir « tout tel quel » : rien ne passe par ffmpeg.
        self._source_only = bool(source_only)
        self._credentials = credentials
        self._stop = False
        self._processes = []

    def stop(self):
        """Demande l'arrêt et tue le réencodage en cours."""
        self._stop = True
        for process in list(self._processes):
            try:
                process.kill()
            except OSError:
                pass

    def run(self):
        total = len(self._items)
        try:
            store = blob_store.BlobStore(self._credentials)
        except blob_store.BlobError as exc:
            for path, _rate in self._items:
                self.done.emit(path, self.FAILED, None, str(exc))
            self.completed.emit(0, 0, total, 0)
            return

        sent = reused = skipped = failed = 0
        uploaded_bytes = 0

        for index, (path, source_rate) in enumerate(self._items, start=1):
            if self._stop:
                break
            state, row, message = self._mirror_one(store, path, source_rate)
            if state == self.SENT:
                sent += 1
                uploaded_bytes += int((row or {}).get("bytes") or 0)
            elif state == self.REUSED:
                reused += 1
            elif state == self.SKIPPED:
                skipped += 1
            else:
                failed += 1
            self.done.emit(path, state, row, message)
            self.progress.emit(index, total)

        self.completed.emit(sent, reused + skipped, failed, uploaded_bytes)

    # ------------------------------------------------------------------
    def _mirror_one(self, store, path, source_rate):
        """Traite un fichier. Rend (état, ligne ou None, message)."""
        if not os.path.exists(path):
            return self.FAILED, None, "Fichier absent du disque."

        # Débit ignoré de la base : le relire plutôt que de laisser ``plan``
        # trancher sans savoir. C'est cette lecture qui décide entre une
        # copie conforme et une deuxième compression avec perte.
        if not source_rate:
            source_rate = probe_bitrate(path)
        rate, as_is = plan(source_rate, self._bitrate, self._source_only)
        current = stamp(path)
        known = self._known.get(canonical_path(path))
        if state_of(known, current) == FRESH and known.get("bitrate") == rate:
            return self.SKIPPED, None, "Déjà en miroir."

        # L'empreinte est toujours relue : deux fichiers distincts peuvent
        # partager date et taille, et un cache indexé là-dessus enverrait un
        # titre sous la clé d'un autre.
        try:
            sha1 = file_sha1(path)
        except OSError as exc:
            return self.FAILED, None, f"Lecture impossible : {exc}"

        extension = os.path.splitext(path)[1].lower()
        key = object_key(store.prefix, sha1, rate,
                         extension if as_is else ".opus")
        row = {
            "path": canonical_path(path),
            "sha1": sha1,
            "stamp": current,
            "key": key,
            "bitrate": rate,
            "source_bytes": os.path.getsize(path),
            "uploaded_at": time.time(),
        }

        # Objet déjà dans le seau : ni réencodage ni envoi. C'est le cas
        # d'une deuxième copie du même fichier, et celui d'un envoi
        # interrompu qu'on relance.
        try:
            existing = store.head(key)
        except blob_store.BlobError as exc:
            return self.FAILED, None, str(exc)
        if existing:
            row["bytes"] = existing["bytes"]
            return self.REUSED, row, "Déjà dans le seau."

        if self._stop:
            return self.FAILED, None, "Envoi interrompu."

        # Fichier déjà plus léger que la cible : il part tel quel, sans
        # deuxième compression avec perte (voir ``plan``).
        if as_is:
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError as exc:
                return self.FAILED, None, f"Lecture impossible : {exc}"
            try:
                store.put(key, data, content_type=CONTENT_TYPES.get(
                    extension, "application/octet-stream"))
            except blob_store.BlobError as exc:
                return self.FAILED, None, str(exc)
            row["bytes"] = len(data)
            return self.SENT, row, ""

        handle, temporary = tempfile.mkstemp(suffix=".opus", prefix="deekeep-")
        os.close(handle)
        try:
            ok, message = transcode(path, temporary, rate,
                                    registry=self._processes)
            if not ok:
                return self.FAILED, None, ("Envoi interrompu." if self._stop
                                           else message)
            with open(temporary, "rb") as opus:
                data = opus.read()
        except OSError as exc:
            return self.FAILED, None, f"Réencodage illisible : {exc}"
        finally:
            try:
                os.remove(temporary)
            except OSError:
                pass

        if self._stop:
            return self.FAILED, None, "Envoi interrompu."

        try:
            store.put(key, data, content_type="audio/ogg")
        except blob_store.BlobError as exc:
            return self.FAILED, None, str(exc)

        row["bytes"] = len(data)
        return self.SENT, row, ""


class RestoreThread(QThread):
    """Redescend du seau les titres que le disque n'a plus.

    Le chemin inverse du miroir, et le seul qui justifie qu'il existe : une
    réinstallation, un disque changé, un dossier effacé par mégarde, et la
    bibliothèque se reconstitue depuis le seau sans que rien n'ait été
    réencodé au passage.

    Deux garanties, et une limite dite franchement :

      * un fichier **envoyé tel quel** revient octet pour octet, et son
        empreinte SHA-1 est **revérifiée après écriture** — c'est la ligne
        du miroir qui la détient. Une copie qui ne se vérifie pas est
        effacée plutôt que gardée : mieux vaut un titre manquant qu'un
        titre corrompu qui se donne pour restauré ;
      * un fichier **réencodé en Opus** n'a pas d'original à rendre. Il est
        **reconverti en MP3** au passage (``transcode_to_mp3``) et reprend
        son chemin : un autoradio ne lit pas le « .opus », et une copie de
        secours qui ne joue pas là où l'on écoute ne sert à rien. L'écran le
        compte à part — ce n'est pas une restauration, c'est un repêchage,
        et le fichier rendu a traversé deux compressions avec perte.

    Rien n'est jamais écrasé. Un titre dont le fichier est déjà là est
    passé, sans même une requête au seau.
    """

    # (chemin d'origine, état, ligne de miroir à réécrire ou None, message)
    done = pyqtSignal(str, str, object, str)
    # (traités, total)
    progress = pyqtSignal(int, int)
    # (restaurés, repêchés en Opus, ignorés, échecs, octets reçus)
    completed = pyqtSignal(int, int, int, int, int)

    # Suites possibles pour un titre, telles qu'annoncées par ``done``.
    RESTORED = "restored"      # fichier d'origine rendu, empreinte vérifiée
    DEGRADED = "degraded"      # seule la réduction Opus existait
    SKIPPED = "skipped"        # le fichier est déjà sur le disque
    FAILED = "failed"          # échec, message à l'appui

    def __init__(self, rows, credentials=None, parent=None):
        super().__init__(parent)
        # ``rows`` : les lignes de la table ``opus_mirror`` des titres à
        # rendre. Tout y est — le chemin visé, la clé de l'objet et
        # l'empreinte attendue — sans avoir à relire quoi que ce soit.
        self._rows = [dict(row) for row in rows]
        self._credentials = credentials
        self._stop = False
        self._processes = []

    def stop(self):
        """Demande l'arrêt et tue la conversion en cours."""
        self._stop = True
        for process in list(self._processes):
            try:
                process.kill()
            except OSError:
                pass

    def run(self):
        total = len(self._rows)
        try:
            store = blob_store.BlobStore(self._credentials)
        except blob_store.BlobError as exc:
            for row in self._rows:
                self.done.emit(row.get("path") or "", self.FAILED, None,
                               str(exc))
            self.completed.emit(0, 0, 0, total, 0)
            return

        restored = degraded = skipped = failed = 0
        received = 0

        for index, row in enumerate(self._rows, start=1):
            if self._stop:
                break
            state, updated, message, size = self._restore_one(store, row)
            if state == self.RESTORED:
                restored += 1
                received += size
            elif state == self.DEGRADED:
                degraded += 1
                received += size
            elif state == self.SKIPPED:
                skipped += 1
            else:
                failed += 1
            self.done.emit(row.get("path") or "", state, updated, message)
            self.progress.emit(index, total)

        self.completed.emit(restored, degraded, skipped, failed, received)

    # ------------------------------------------------------------------
    def _restore_one(self, store, row):
        """Rend un titre. Retourne (état, ligne à réécrire, message, octets)."""
        path = row.get("path") or ""
        key = row.get("key") or ""
        if not path or not key:
            return self.FAILED, None, "Ligne de miroir incomplète.", 0

        destination, identical = restore_target(path, key)
        # Rien n'est écrasé : la restauration comble des trous, elle ne
        # remplace pas ce qui est là. Un titre déjà présent ne coûte donc
        # même pas une requête.
        if os.path.exists(destination):
            return self.SKIPPED, None, "Déjà sur le disque.", 0

        folder = os.path.dirname(destination)
        if folder and not os.path.isdir(folder):
            # Reconstituer un disque, c'est aussi refaire l'arborescence :
            # les dossiers d'album ont disparu avec les fichiers.
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                return self.FAILED, None, f"Dossier impossible à créer : {exc}", 0

        if not identical:
            return self._restore_converted(store, row, key, destination, folder)

        try:
            size = store.download(key, destination)
        except blob_store.BlobError as exc:
            return self.FAILED, None, str(exc), 0
        except OSError as exc:
            return self.FAILED, None, f"Écriture impossible : {exc}", 0

        # Copie conforme annoncée : elle doit se prouver. L'empreinte du
        # fichier d'origine est en base depuis l'envoi ; celle du fichier
        # rendu doit lui être identique, sans quoi ce n'est pas le même
        # fichier — et un titre corrompu qui se donne pour restauré est pire
        # qu'un titre manquant, qu'on sait au moins aller rechercher.
        expected = (row.get("sha1") or "").lower()
        if identical and expected:
            trouble = ("L'empreinte du fichier rendu ne correspond pas à "
                       "celle attendue : la copie a été écartée.")
            try:
                actual = file_sha1(destination)
            except OSError as exc:
                actual, trouble = "", f"Relecture impossible : {exc}"
            if actual != expected:
                try:
                    os.remove(destination)
                except OSError:
                    pass
                return self.FAILED, None, trouble, 0

        # Le fichier est de retour, mais avec une date de modification
        # neuve : sans cette mise à jour de l'empreinte date+taille, tout ce
        # qu'on vient de restaurer se donnerait pour « à renvoyer » — et
        # une bibliothèque entière repartirait vers le seau pour rien.
        updated = dict(row)
        updated["stamp"] = stamp(destination)
        return self.RESTORED, updated, "", size

    # ------------------------------------------------------------------
    def _restore_converted(self, store, row, key, destination, folder):
        """Rend un objet Opus sous forme de MP3. Mêmes retours que ci-dessus.

        L'Opus descend d'abord dans un temporaire — il n'a pas vocation à
        rester sur le disque — puis ffmpeg en tire le MP3. Celui-ci est écrit
        lui aussi à côté avant d'être mis en place d'un coup : une conversion
        interrompue ne laisse ni l'un ni l'autre.
        """
        handle, temporaire_opus = tempfile.mkstemp(suffix=".opus",
                                                   prefix="deekeep-")
        os.close(handle)
        handle, temporaire_mp3 = tempfile.mkstemp(suffix=".mp3", prefix=".dk_",
                                                  dir=folder or ".")
        os.close(handle)
        try:
            try:
                size = store.download(key, temporaire_opus)
            except blob_store.BlobError as exc:
                return self.FAILED, None, str(exc), 0
            except OSError as exc:
                return self.FAILED, None, f"Écriture impossible : {exc}", 0

            if self._stop:
                return self.FAILED, None, "Restauration interrompue.", 0

            ok, message = transcode_to_mp3(temporaire_opus, temporaire_mp3,
                                           registry=self._processes)
            if not ok:
                return self.FAILED, None, ("Restauration interrompue."
                                           if self._stop else message), 0
            try:
                os.replace(temporaire_mp3, destination)
            except OSError as exc:
                return self.FAILED, None, f"Mise en place impossible : {exc}", 0
        finally:
            for chemin in (temporaire_opus, temporaire_mp3):
                try:
                    os.remove(chemin)
                except OSError:
                    pass

        # Le fichier rendu n'est plus celui dont la ligne porte l'empreinte
        # SHA-1 — il a traversé deux compressions. L'empreinte date+taille est
        # tout de même remise à jour, et pour une raison précise : sans elle,
        # le titre se donnerait pour « à renvoyer », et un envoi de plus le
        # ferait repartir vers le seau en **quatrième** génération. La copie
        # en ligne reste la référence de ce titre ; il n'y a rien à renvoyer.
        updated = dict(row)
        updated["stamp"] = stamp(destination)
        return self.DEGRADED, updated, "Converti en MP3.", size


class CheckThread(QThread):
    """Vérifie qu'un seau répond et accepte les identifiants proposés.

    Dans un fil séparé : un point d'entrée erroné met le délai d'attente
    complet à le dire, et la fenêtre ne doit pas rester figée pendant ce
    temps.
    """

    # (succès, message d'erreur)
    checked = pyqtSignal(bool, str)

    def __init__(self, credentials=None, parent=None):
        super().__init__(parent)
        self._credentials = credentials

    def run(self):
        try:
            blob_store.BlobStore(self._credentials).check()
        except blob_store.BlobError as exc:
            self.checked.emit(False, str(exc))
            return
        self.checked.emit(True, "")


class UsageThread(QThread):
    """Relève l'occupation réelle du seau, sans figer l'interface."""

    # (nombre d'objets, octets, message d'erreur)
    measured = pyqtSignal(int, int, str)

    def __init__(self, credentials=None, parent=None):
        super().__init__(parent)
        self._credentials = credentials

    def run(self):
        try:
            store = blob_store.BlobStore(self._credentials)
            count, total = store.usage()
        except blob_store.BlobError as exc:
            self.measured.emit(0, 0, str(exc))
            return
        self.measured.emit(count, total, "")
