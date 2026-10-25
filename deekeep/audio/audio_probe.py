"""
Mesures faites sur le signal d'un fichier audio, via ffmpeg.

Deux besoins la réclament : la recherche de tags, qui s'appuie sur la durée
comme seule preuve objective d'identité, et le calcul d'empreinte acoustique
(voir ``acoustid_lookup``), qui partage ici la localisation de ffmpeg.

La durée déclarée par l'en-tête d'un fichier n'est pas toujours celle de la
musique : un mauvais découpage laisse parfois une longue plage muette à la
fin. Un titre de 4'22 devient un fichier de 6'54, et toute comparaison avec
un catalogue échoue. ``effective_duration`` retranche ce silence.
"""

import os
import re
import subprocess
import sys
import threading

from deekeep.core.utils import ffmpeg_location, resource_path

# Silence : niveau en dessous duquel le signal est tenu pour muet, et durée
# minimale d'une plage pour être signalée. Un seuil trop bas laisserait
# passer le souffle d'un enregistrement analogique.
SILENCE_NOISE = "-45dB"
SILENCE_MIN_SECONDS = 3

# Tolérance sur la coïncidence entre la fin d'un silence et la fin du
# fichier : ffmpeg clôt la dernière plage sur la dernière trame décodée.
SILENCE_END_MARGIN = 1.5

# Empêche l'ouverture d'une console à chaque appel sous Windows.
NO_WINDOW = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
             if sys.platform == "win32" else 0)

_ffmpeg = None
_ffmpeg_searched = False
# La détection est lancée depuis plusieurs threads d'analyse à la fois : sans
# verrou, le premier appelant publierait « recherche faite » avant d'avoir
# trouvé quoi que ce soit, et les autres conclueraient à l'absence de ffmpeg.
_ffmpeg_lock = threading.Lock()


def run(args, timeout=180):
    """Exécute une commande et rend (code, sortie standard, sortie d'erreur)."""
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=NO_WINDOW,
        )
        return done.returncode, done.stdout or "", done.stderr or ""
    except Exception as exc:
        return -1, "", str(exc)


def ffmpeg_candidates():
    """Chemins où chercher ffmpeg : celui livré avec l'application, puis le PATH."""
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    bundled = ffmpeg_location()
    if bundled:
        yield os.path.join(bundled, exe)
    yield exe


def ffmpeg_exe(refresh=False):
    """Premier ffmpeg utilisable, ou None. Détection faite une seule fois.

    Le résultat n'est publié qu'une fois la recherche terminée : un appel
    concurrent attend au verrou plutôt que de lire un état intermédiaire.
    """
    global _ffmpeg, _ffmpeg_searched
    if _ffmpeg_searched and not refresh:
        return _ffmpeg
    with _ffmpeg_lock:
        # Un autre thread a pu mener la recherche pendant l'attente.
        if _ffmpeg_searched and not refresh:
            return _ffmpeg
        found = None
        for path in ffmpeg_candidates():
            code, _out, _err = run([path, "-hide_banner", "-version"], timeout=30)
            if code == 0:
                found = path
                break
        _ffmpeg = found
        _ffmpeg_searched = True
    return _ffmpeg


def trailing_silence_start(path, declared):
    """Instant où débute le silence final, ou None s'il n'y en a pas.

    ``declared`` est la durée annoncée par l'en-tête : c'est elle qui permet
    de reconnaître, parmi les plages muettes, celle qui court jusqu'au bout
    du fichier — un silence au milieu d'un morceau ne compte pas.
    """
    exe = ffmpeg_exe()
    if exe is None or not declared or not os.path.exists(path):
        return None

    code, out, err = run([
        exe, "-hide_banner", "-nostats", "-i", path,
        "-af", "silencedetect=noise=%s:d=%s" % (SILENCE_NOISE, SILENCE_MIN_SECONDS),
        "-f", "null", "-",
    ])
    if code != 0:
        return None
    log = err + out
    starts = [float(v) for v in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(v) for v in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    if not starts:
        return None
    # Silence encore ouvert à la fin du décodage, ou clos sur la dernière
    # trame : dans les deux cas il s'agit du silence final.
    if len(starts) > len(ends) or abs(ends[-1] - declared) <= SILENCE_END_MARGIN:
        return starts[-1]
    return None


def effective_duration(path, declared):
    """Durée de la musique en secondes, silence final retranché.

    Rend None quand il n'y a rien à retrancher : l'appelant sait alors que la
    durée déclarée fait déjà foi et qu'il n'a pas à réessayer.
    """
    start = trailing_silence_start(path, declared)
    if start is None:
        return None
    useful = int(round(start))
    return useful if useful > 0 else None
