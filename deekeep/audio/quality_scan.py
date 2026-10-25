"""
Détection des défauts sonores des fichiers de la bibliothèque.

Un titre mal extrait s'entend avant de se voir : souffle continu, craquements,
saturation, longue plage muette collée à la fin. Ce module mesure ces quatre
défauts sur le signal lui-même — les tags et le débit n'en disent rien — afin
que l'écran « Qualité » puisse proposer de retélécharger les titres abîmés.

Tout repose sur une seule invocation de ffmpeg par fichier, dont le graphe de
filtres dérive le signal vers plusieurs mesures menées de front :

    ┌── silencedetect@long ─ aformat=s16 ─ astats@full  blancs, écrêtage
    │
 ───┼── highpass=8 kHz ────────────────── astats@hf     souffle
    │
    ├── silencedetect@gap ───────────────               coupures
    │
    └── adeclick ────────────────────────               craquements (option)

Pourquoi ces mesures-là :

``astats@full`` travaille sur le signal ramené en 16 bits, donc *écrêté* aux
bornes du format : ``Peak count`` y compte exactement les échantillons collés
à la butée, c'est-à-dire les échantillons saturés. Sur le signal flottant
d'origine cette même mesure ne compterait qu'un ou deux maxima et ne dirait
rien de la saturation.

``astats@hf`` mesure le plancher de bruit **au-dessus de 8 kHz**, là où la
musique se tait dans les passages calmes mais où le souffle, lui, ne s'arrête
jamais. Sur un titre sain ce plancher tombe sous −80 dB (souvent −∞, le
silence numérique étant exact) ; un souffle audible le remonte à son propre
niveau, à un décibel près.

Ce plancher ne suffit pourtant pas à lui seul. Un morceau de danse qui ne se
tait à aucun moment n'a pas de plancher à mesurer : ce qu'``astats`` rapporte
alors n'est que le bas de la distribution de la musique, autour de −50 dB, et
la moitié d'un dossier se retrouverait accusée de souffler. D'où la seconde
condition, qui tranche : comparer le **creux** de la bande haute à celui du
signal entier. Un souffle est large bande — au moment le plus calme, les deux
creux se tiennent à quelques décibels (2 à 4 dB mesurés) puisque c'est le même
bruit qui les fixe. La musique, elle, est pauvre en aigus dans ses passages
calmes : son creux haute bande plonge 20 à 30 dB sous le creux large bande.
Le seuil est posé entre les deux (voir ``HISS_TROUGH_GAP_DB``).

``silencedetect@gap`` cherche autre chose que les blancs de début et de fin :
les **coupures**, ces fractions de seconde de silence *numérique* — des zéros
exacts, non du signal faible — semées au milieu d'un titre par une extraction
qui a hoqueté. Le seuil est placé très bas (−80 dB) et la durée minimale très
courte (20 ms), ce qu'aucune fin de note ne franchit.

Les compter ne suffit pourtant pas. « We Will Rock You » en montre
soixante-neuf : le morceau n'est fait que de frappes séparées de silences, et
son master les a rendus numériquement exacts. Ce qui distingue une extraction
qui bégaie, c'est qu'elle blanchit une large part d'un passage entier. D'où
la mesure retenue, la **densité** : la part de silence du passage de dix
secondes le plus troué. Elle sépare franchement les deux familles — 51 % pour
un titre dont l'introduction bégaie, 19 % au pire pour un fichier sain, le
plus souvent moins de 5 %.

``adeclick`` est le seul détecteur d'impulsions fiable : il prédit chaque
échantillon à partir des précédents et signale ceux qui démentent la
prédiction. C'est aussi le plus lent (il multiplie le temps d'analyse par
quatre), d'où son caractère facultatif. Son seuil est volontairement élevé :
au réglage par défaut du filtre, un titre parfaitement sain affiche déjà 3 %
d'échantillons « suspects », ce qui ne distingue plus rien.

Les **blancs** de début et de fin, eux, ne se jugent pas sur le seul signal :
un morceau qui s'achève sur un fondu ou des applaudissements contient ce
silence dans son master, et le lui reprocher revenait à proposer de
retélécharger une version identique. Ils ne sont donc mis en cause que si le
fichier est plus long que le morceau ne dure réellement, d'après Deezer ou
MusicBrainz (voir ``reference_duration`` et ``ReferenceLookupThread``).

Les mesures sont conservées dans un cache disque indexé par (chemin, date de
modification, taille) : relancer l'analyse d'une bibliothèque déjà parcourue
est immédiat, et changer un seuil ne relance aucune mesure puisque le verdict
est recalculé à part (voir ``evaluate``).
"""

import json
import os
import queue
import re
import subprocess
import threading

from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QThread, pyqtSignal

from deekeep.audio import reencode_scan
from deekeep.audio import reference_duration
from deekeep.core import settings as app_settings

from deekeep.audio.audio_probe import NO_WINDOW, ffmpeg_exe

# ----------------------------------------------------------------------
# Défauts reconnus
# ----------------------------------------------------------------------
HISS = "hiss"
CRACKLE = "crackle"
CLIPPING = "clipping"
GAPS = "gaps"
DURATION = "duration"
TAIL = "tail"
HEAD = "head"

# Ordre d'affichage et libellés (l'ordre sert aussi aux cases de filtrage).
DEFECT_ORDER = (HISS, CRACKLE, CLIPPING, GAPS, DURATION, TAIL, HEAD)

DEFECT_LABELS = {
    HISS: "Souffle",
    CRACKLE: "Craquements",
    CLIPPING: "Saturation",
    GAPS: "Coupures",
    DURATION: "Durée",
    TAIL: "Blanc de fin",
    HEAD: "Blanc de début",
}

# Défauts qui ne se voient pas dans le signal seul : ils demandent la durée
# du morceau, cherchée en ligne (voir ``reference_duration``).
REFERENCE_DEFECTS = (DURATION, TAIL, HEAD)

# Défauts que seule l'analyse approfondie sait voir.
DEEP_DEFECTS = (CRACKLE,)

# ----------------------------------------------------------------------
# Seuils de déclenchement
# ----------------------------------------------------------------------
# Calibrés sur une bibliothèque réelle et sur des dégradations fabriquées
# (bruit blanc injecté à niveau connu, impulsions, gain avant écrêtage).
DEFAULT_THRESHOLDS = {
    # Plancher de bruit au-dessus de 8 kHz, en dB. Titres sains mesurés :
    # −78 dB dans le pire cas, le plus souvent sous −95 dB ou −∞. Un souffle
    # injecté à −60 dB se lit −62 dB : le seuil laisse donc passer le bruit
    # de codage sans rien manquer d'audible.
    "hiss_db": -65.0,
    # Part d'échantillons collés à la butée 16 bits, en %. Le seuil est haut
    # à dessein : un master de variété récente en montre couramment 0,03 à
    # 0,08 % — c'est un choix de mastering, que retélécharger ne changerait
    # pas puisque la source est la même. Au-delà de 0,2 %, en revanche, la
    # distorsion s'entend : +3 dB de gain avant écrêtage en produisent 0,25 %.
    "clipping_pct": 0.20,
    # Part d'échantillons signalés par adeclick, en %. Titres sains :
    # 0,0000 à 0,0007 %. Deux craquements par seconde : 0,0038 %.
    "crackle_pct": 0.0025,
    # Part de silence numérique, en %, du passage de dix secondes le plus
    # troué. Fichiers sains mesurés : 19 % au pire (« We Will Rock You », qui
    # n'est que frappes et silences), le plus souvent sous 5 %. Une extraction
    # qui bégaie en montre 51 %.
    "gap_density_pct": 30.0,
    # Durée du silence final, en secondes (une fin en fondu en occupe deux
    # ou trois : seul un blanc franc constitue un défaut).
    "tail_seconds": 5.0,
    # Durée du silence initial, en secondes. Une à deux secondes d'attaque
    # calme sont si répandues qu'en faire un défaut noierait les vrais.
    "head_seconds": 3.0,
    # Secondes dont le fichier doit dépasser sa durée de référence pour que
    # ses blancs soient tenus pour ajoutés (voir ``evaluate``). Les durées
    # relevées sur des fichiers sains s'écartent de la référence de 3 s au
    # plus, le temps de codage et d'arrondi.
    "duration_excess": 3.0,
    # Écart, dans un sens ou dans l'autre, à partir duquel le fichier n'est
    # plus tenu pour la même version que la référence. Sur soixante-dix titres
    # tirés au hasard, l'écart médian est de 1,6 s et les trois quarts tiennent
    # sous 3,4 s ; au-delà commence une autre famille — éditions radio, montages
    # écourtés, rips de mauvaise source — dont l'écart se compte en dizaines de
    # secondes. Le seuil est posé entre les deux.
    "duration_gap": 10.0,
}

# ----------------------------------------------------------------------
# Analyse ffmpeg
# ----------------------------------------------------------------------
# Fréquence de coupure du filtre passe-haut servant à isoler le souffle.
HISS_HIGHPASS_HZ = 8000

# Écart maximal, en dB, entre le creux du signal entier et celui de sa bande
# haute pour que le bruit mesuré passe pour un souffle. Un souffle injecté
# donne 2 à 4 dB ; un morceau que rien n'interrompt, 22 à 31 dB. Le seuil est
# posé au large des deux familles.
HISS_TROUGH_GAP_DB = 12.0

# Seuil de détection d'adeclick (1 = très sensible, 100 = très permissif).
DECLICK_THRESHOLD = 12

# Silence : niveau et durée minimale retenus par silencedetect. Deux secondes
# suffisent à repérer les plages ; la comparaison au seuil de l'utilisateur
# vient après, sur la durée mesurée.
SILENCE_NOISE_DB = -50
SILENCE_MIN_SECONDS = 2

# Coupures : silence *numérique* (des zéros, pas du signal faible) d'une
# fraction de seconde au milieu du morceau. Le niveau est volontairement
# inatteignable par de la musique enregistrée, et la durée assez courte pour
# qu'un simple hoquet d'extraction soit vu.
GAP_NOISE_DB = -80
GAP_MIN_SECONDS = 0.02

# Marge, en secondes, écartant du décompte les coupures collées au début ou à
# la fin : ce sont les blancs de début et de fin, qui ont leur propre mesure.
GAP_EDGE_MARGIN = 1.0

# Fenêtre sur laquelle se mesure la densité de coupures (voir ``_gap_stats``).
# Dix secondes : assez long pour qu'un morceau à trous rythmiques y noie ses
# silences, assez court pour qu'un bégaiement de quelques secondes y ressorte.
GAP_WINDOW_SECONDS = 10.0

# Le silence final se reconnaît à ce qu'il court jusqu'au bout du fichier ;
# ffmpeg clôt la dernière plage sur la dernière trame décodée, d'où la marge.
SILENCE_END_MARGIN = 1.5

# Temps maximal accordé à l'analyse d'un fichier. Une heure de musique passe
# en moins de deux minutes, même avec la détection des craquements.
PROBE_TIMEOUT = 900

# Nombre d'analyses menées de front. Contrairement à la lecture des tags, la
# tâche est ici gourmande en calcul : on laisse deux cœurs à l'interface.
SCAN_WORKERS = max(2, min(12, (os.cpu_count() or 4) - 2))

_ASTATS_RE = re.compile(r"^\[astats@(\w+) @ [^\]]*\] ([A-Za-z][^:]*): (.+)$")
# Les deux silencedetect sont nommés (« @long », « @gap ») : sans cela leurs
# lignes seraient indiscernables dans le journal, et les coupures viendraient
# se mêler aux blancs de début et de fin.
_SILENCE_RE = re.compile(
    r"^\[silencedetect@(\w+) @ [^\]]*\] silence_(start|end):\s*(-?[\d.]+)")
_DECLICK_RE = re.compile(r"Detected clicks in (\d+) of (\d+) samples")
_DURATION_RE = re.compile(r"Duration: (\d+):(\d+):([\d.]+)")
_RATE_RE = re.compile(r"Audio: .*?, (\d+) Hz")


def _filtergraph(deep):
    """Graphe de filtres de l'analyse (une seule lecture du fichier)."""
    branches = 4 if deep else 3
    labels = "[a][b][c]" + ("[d]" if deep else "")
    graph = [
        "[0:a:0]asplit=%d%s;" % (branches, labels),
        "[a]silencedetect@long=noise=%ddB:d=%d,aformat=sample_fmts=s16,"
        "astats@full=metadata=0:measure_perchannel=none[s1];"
        % (SILENCE_NOISE_DB, SILENCE_MIN_SECONDS),
        "[b]highpass=f=%d,astats@hf=metadata=0:measure_perchannel=none[s2];"
        % HISS_HIGHPASS_HZ,
        "[c]silencedetect@gap=noise=%ddB:d=%s[s3];"
        % (GAP_NOISE_DB, GAP_MIN_SECONDS),
    ]
    if deep:
        graph.append("[d]adeclick=t=%d[s4];" % DECLICK_THRESHOLD)
        graph.append("[s1][s2][s3][s4]amix=inputs=4[out]")
    else:
        graph.append("[s1][s2][s3]amix=inputs=3[out]")
    return "".join(graph)


def _to_float(text):
    """Nombre lu dans une ligne d'astats ; ``-inf`` et ``nan`` compris."""
    try:
        return float(text)
    except ValueError:
        stripped = text.strip().lower()
        if stripped.endswith("inf"):
            return float("-inf") if stripped.startswith("-") else float("inf")
        return float("nan")


def _run_ffmpeg(args, registry=None):
    """Exécute ffmpeg et rend (code, sortie fusionnée).

    Le processus est enregistré dans ``registry`` le temps de son exécution :
    l'arrêt d'une analyse peut ainsi tuer les lectures en cours au lieu
    d'attendre la fin de chacune (plusieurs secondes par fichier).
    """
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", creationflags=NO_WINDOW,
        )
    except OSError as exc:
        return -1, str(exc)

    if registry is not None:
        registry.add(proc)
    try:
        out, err = proc.communicate(timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return -1, (out or "") + (err or "") + "\nAnalyse interrompue (délai dépassé)."
    finally:
        if registry is not None:
            registry.discard(proc)
    return proc.returncode, (out or "") + (err or "")


def _parse(log):
    """Extrait les mesures utiles du journal de ffmpeg."""
    stats = {}
    silence_starts, silence_ends = [], []
    gap_starts, gap_ends = [], []
    clicks = None
    duration = 0.0
    rate = 0

    for line in log.splitlines():
        match = _ASTATS_RE.match(line)
        if match:
            stats["%s|%s" % (match.group(1), match.group(2))] = _to_float(match.group(3))
            continue
        match = _SILENCE_RE.match(line)
        if match:
            name, kind, value = match.group(1), match.group(2), float(match.group(3))
            if name == "gap":
                (gap_starts if kind == "start" else gap_ends).append(value)
            else:
                (silence_starts if kind == "start" else silence_ends).append(value)
            continue
        found = _DECLICK_RE.search(line)
        if found:
            total = int(found.group(2)) or 1
            clicks = 100.0 * int(found.group(1)) / total
            continue
        if not duration:
            found = _DURATION_RE.search(line)
            if found:
                duration = (int(found.group(1)) * 3600 + int(found.group(2)) * 60
                            + float(found.group(3)))
        if not rate:
            found = _RATE_RE.search(line)
            if found:
                rate = int(found.group(1))

    # Silence initial : la première plage muette doit commencer au tout début.
    head = 0.0
    if silence_starts and silence_starts[0] <= 0.2 and silence_ends:
        head = max(0.0, silence_ends[0])

    # Silence final : plage encore ouverte à la fin du décodage, ou close sur
    # la dernière trame. Sans durée connue, rien n'est affirmé.
    tail = 0.0
    if silence_starts and duration:
        closed_on_end = bool(silence_ends) and abs(silence_ends[-1] - duration) <= SILENCE_END_MARGIN
        if len(silence_starts) > len(silence_ends) or closed_on_end:
            tail = max(0.0, duration - silence_starts[-1])
        # Un fichier entièrement muet n'a pas de « blanc de fin » : il n'est
        # que silence, ce que la durée du silence initial dit déjà.
        if head and tail >= duration - 0.5:
            tail = 0.0

    samples = stats.get("full|Number of samples", 0) or 0
    peak_count = stats.get("full|Peak count", 0) or 0
    peak_db = stats.get("full|Peak level dB", float("nan"))

    # ``Peak count`` ne compte les échantillons saturés que si le signal
    # touche effectivement la butée : au-dessous, il ne compte que le ou les
    # deux échantillons les plus forts, qui ne saturent rien.
    clipping = 0.0
    if samples and peak_db == peak_db and peak_db >= -0.5:
        clipping = 100.0 * peak_count / samples

    gap_count, gap_seconds, gap_density = _gap_stats(gap_starts, gap_ends, duration)

    return {
        "duration": duration,
        "sample_rate": rate,
        "peak_db": peak_db,
        "clipping_pct": clipping,
        "flat_factor": stats.get("full|Flat factor", 0.0),
        "hiss_db": stats.get("hf|Noise floor dB", float("-inf")),
        "hiss_trough_db": stats.get("hf|RMS through dB", float("-inf")),
        "full_trough_db": stats.get("full|RMS through dB", float("-inf")),
        "crackle_pct": clicks,
        "gap_count": gap_count,
        "gap_seconds": gap_seconds,
        "gap_density": gap_density,
        "head_seconds": head,
        "tail_seconds": tail,
    }


def _gap_stats(starts, ends, duration):
    """Coupures internes : (nombre, durée totale, densité maximale).

    Une plage encore ouverte à la fin du décodage est le blanc de fin, non
    une coupure : sans borne de fin, elle est ignorée.

    La **densité** est la part de silence numérique du passage de dix
    secondes le plus troué. C'est elle qui fait le verdict, et non le nombre
    de coupures : « We Will Rock You » en compte soixante-neuf — les silences
    entre les frappes de pied d'un morceau qui n'est fait que de cela — et
    n'en reste pas moins parfaitement sain. Ce qui trahit une extraction qui
    hoquette, c'est qu'elle blanchit une large part d'un passage entier :
    51 % pour « The Second Twilight », contre 19 % au pire pour les fichiers
    sains mesurés.
    """
    if not duration:
        return 0, 0.0, 0.0
    segments = [
        (start, end) for start, end in zip(starts, ends)
        if start >= GAP_EDGE_MARGIN and end <= duration - GAP_EDGE_MARGIN
    ]
    if not segments:
        return 0, 0.0, 0.0

    total = sum(end - start for start, end in segments)
    # Fenêtre glissante ancrée sur chaque coupure : la plus trouée l'emporte.
    # Les ancrer sur les coupures suffit — la fenêtre la plus dense commence
    # forcément sur l'une d'elles.
    density = 0.0
    for origin, _end in segments:
        covered = sum(
            max(0.0, min(end, origin + GAP_WINDOW_SECONDS) - max(start, origin))
            for start, end in segments
        )
        density = max(density, covered / GAP_WINDOW_SECONDS)
    return len(segments), total, min(1.0, density)


def measure(path, deep=True, registry=None):
    """Mesures brutes d'un fichier audio (dict, jamais None).

    La clé ``error`` porte l'explication en cas d'échec ; les mesures valent
    alors leur valeur neutre, de sorte qu'aucun défaut ne soit annoncé à tort.
    """
    empty = {
        "duration": 0.0, "sample_rate": 0,
        "peak_db": float("nan"), "clipping_pct": 0.0,
        "flat_factor": 0.0, "hiss_db": float("-inf"),
        "hiss_trough_db": float("-inf"), "full_trough_db": float("-inf"),
        "crackle_pct": None, "gap_count": 0, "gap_seconds": 0.0,
        "gap_density": 0.0,
        "head_seconds": 0.0, "tail_seconds": 0.0, "deep": bool(deep), "error": "",
    }

    exe = ffmpeg_exe()
    if exe is None:
        empty["error"] = "FFmpeg introuvable"
        return empty
    if not os.path.exists(path):
        empty["error"] = "Fichier introuvable"
        return empty

    code, log = _run_ffmpeg([
        exe, "-hide_banner", "-nostats", "-i", path,
        "-filter_complex", _filtergraph(deep), "-map", "[out]", "-f", "null", "-",
    ], registry=registry)

    result = dict(empty)
    result.update(_parse(log))
    result["deep"] = bool(deep)
    if code != 0:
        # Un code non nul sans aucune mesure signale un fichier illisible ;
        # avec des mesures, ffmpeg a travaillé et seule la fin a échoué.
        if not result["duration"]:
            result = dict(empty)
            result["error"] = _error_line(log) or "Analyse impossible"
    return result


def _error_line(log):
    """Dernière ligne d'erreur exploitable du journal ffmpeg."""
    for line in reversed(log.splitlines()):
        text = line.strip()
        if text and not text.startswith(("frame=", "size=", "  ")):
            return text[:160]
    return ""


# ----------------------------------------------------------------------
# Verdict
# ----------------------------------------------------------------------
def _severity(value, threshold, span):
    """Gravité d'un dépassement : 0 au seuil, 1 à ``seuil + span``."""
    if span <= 0:
        return 1.0
    return max(0.0, min(1.0, (value - threshold) / float(span)))


def trough_gap(measures):
    """Écart en dB entre le creux du signal entier et celui de sa bande haute.

    None quand l'un des deux creux vaut −∞ : le fichier comporte alors du
    silence numérique franc, sur lequel la comparaison ne dit rien.
    """
    full = measures.get("full_trough_db")
    high = measures.get("hiss_trough_db")
    for value in (full, high):
        if value is None or value != value or value in (float("-inf"), float("inf")):
            return None
    return full - high


def hiss_measurable(measures):
    """Vrai si la bande au-dessus de 8 kHz existe dans ce fichier.

    Sous 20 kHz d'échantillonnage, Nyquist passe sous la coupure du filtre :
    celui-ci n'a plus de bande à laisser passer et ne rend que du bruit de
    calcul, qu'il serait absurde de prendre pour un souffle.
    """
    rate = measures.get("sample_rate") or 0
    return rate == 0 or rate >= 2.5 * HISS_HIGHPASS_HZ


def _is_broadband(measures):
    """Vrai si le bruit mesuré est large bande, donc un vrai souffle.

    Voir l'en-tête du module : c'est ce test qui sépare le souffle du simple
    bas de distribution d'un morceau que rien n'interrompt.
    """
    gap = trough_gap(measures)
    # Creux indisponible (silence numérique) : le plancher de bruit fait foi
    # à lui seul — il vaut alors −∞ dans les faits, et rien n'est signalé.
    return gap is None or gap <= HISS_TROUGH_GAP_DB


def duration_excess(measures, reference):
    """Secondes dont le fichier dépasse sa durée de référence (None si inconnue)."""
    if reference is None:
        return None
    duration = measures.get("duration") or 0.0
    if not duration:
        return None
    return duration - reference


def evaluate(measures, thresholds=None, reference=None):
    """Rend (liste des défauts, gravités, note sur 100) pour des mesures.

    Séparer le verdict de la mesure permet de rejouer l'évaluation après un
    changement de seuil sans relire le moindre fichier — et, pour les blancs,
    d'attendre la durée de référence sans relire non plus.

    ``reference`` est la durée du morceau d'après Deezer ou MusicBrainz (voir
    ``reference_duration``), dont dépendent les défauts de ``REFERENCE_DEFECTS``.
    Sans elle, ni la durée ni les blancs ne sont mis en cause :

    - un silence de fin n'est un défaut que s'il allonge le fichier au-delà de
      ce que dure réellement le morceau. Beaucoup de titres finissent sur un
      fondu ou des applaudissements que le master contient bel et bien ; les
      accuser revenait à proposer de retélécharger une version identique ;
    - une **durée** qui s'écarte de la référence dans un sens ou dans l'autre
      dit que le fichier n'est pas la version attendue. C'est le seul indice
      exploitable pour un titre que rien ne trahit dans son signal — un rip
      radio propre mesure comme un master propre.
    """
    limits = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        limits.update(thresholds)

    defects, severities = [], {}

    hiss = measures.get("hiss_db", float("-inf"))
    if (hiss == hiss and hiss > limits["hiss_db"]
            and hiss_measurable(measures) and _is_broadband(measures)):
        defects.append(HISS)
        severities[HISS] = _severity(hiss, limits["hiss_db"], 15.0)

    crackle = measures.get("crackle_pct")
    if crackle is not None and crackle > limits["crackle_pct"]:
        defects.append(CRACKLE)
        severities[CRACKLE] = _severity(crackle, limits["crackle_pct"],
                                        limits["crackle_pct"] * 8)

    clipping = measures.get("clipping_pct", 0.0)
    if clipping > limits["clipping_pct"]:
        defects.append(CLIPPING)
        severities[CLIPPING] = _severity(clipping, limits["clipping_pct"],
                                         limits["clipping_pct"] * 8)

    density = 100.0 * (measures.get("gap_density", 0.0) or 0.0)
    if density > limits["gap_density_pct"]:
        defects.append(GAPS)
        # Le silence occupant la moitié d'un passage : bégaiement franc.
        severities[GAPS] = _severity(density, limits["gap_density_pct"], 25.0)

    excess = duration_excess(measures, reference)

    # Une durée qui s'écarte de la référence, dans un sens comme dans l'autre,
    # dit que le fichier n'est pas la version attendue : montage écourté,
    # édition radio, rip d'une mauvaise source. Rien dans le signal ne le
    # trahit — c'est la comparaison seule qui le révèle.
    if excess is not None and abs(excess) > limits["duration_gap"]:
        defects.append(DURATION)
        severities[DURATION] = _severity(abs(excess), limits["duration_gap"], 30.0)

    # Les blancs ne sont mis en cause que si le fichier est plus long que le
    # morceau : c'est la seule preuve que le silence a été ajouté.
    if excess is not None and excess > limits["duration_excess"]:
        tail = measures.get("tail_seconds", 0.0)
        if tail > limits["tail_seconds"]:
            defects.append(TAIL)
            severities[TAIL] = _severity(tail, limits["tail_seconds"], 20.0)

        head = measures.get("head_seconds", 0.0)
        if head > limits["head_seconds"]:
            defects.append(HEAD)
            severities[HEAD] = _severity(head, limits["head_seconds"], 8.0)

    # Note indicative servant surtout au tri « du pire au meilleur » : chaque
    # défaut coûte un forfait, aggravé par son ampleur.
    penalty = sum(12 + 20 * severities.get(key, 0.0) for key in defects)
    score = max(0, int(round(100 - penalty)))
    return defects, severities, score


# En deçà, le plancher de bruit n'a plus de sens physique : l'estimateur
# d'astats rend des valeurs fantaisistes (−897 dB) sur les plages de silence
# numérique. Autant l'afficher comme ce qu'il est — rien de mesurable.
HISS_DISPLAY_FLOOR_DB = -120.0


def describe(measures, defect):
    """Texte court chiffrant un défaut (colonne du tableau)."""
    if defect == HISS:
        value = measures.get("hiss_db", float("-inf"))
        if value != value or value <= HISS_DISPLAY_FLOOR_DB:
            return "—"
        return "%.0f dB" % value
    if defect == CRACKLE:
        value = measures.get("crackle_pct")
        return "%.4f %%" % value if value is not None else "—"
    if defect == CLIPPING:
        return "%.3f %%" % measures.get("clipping_pct", 0.0)
    if defect == GAPS:
        count = measures.get("gap_count", 0) or 0
        if not count:
            return "—"
        return "%d · %.0f %%" % (count, 100.0 * (measures.get("gap_density", 0.0) or 0.0))
    if defect == TAIL:
        return "%.0f s" % measures.get("tail_seconds", 0.0)
    if defect == HEAD:
        return "%.0f s" % measures.get("head_seconds", 0.0)
    return ""


# ----------------------------------------------------------------------
# Cache disque des mesures
# ----------------------------------------------------------------------
# Numéro à incrémenter dès qu'une mesure change de définition : les entrées
# d'une version antérieure sont alors ignorées plutôt que mal interprétées.
CACHE_VERSION = 5
CACHE_FILENAME = "quality_cache.json"


class MeasureCache:
    """Mesures déjà calculées, indexées par (chemin, date, taille).

    Une analyse approfondie coûte plusieurs secondes par fichier : sans
    cache, revenir sur l'écran après un retéléchargement imposerait de tout
    relire. Les entrées d'une analyse rapide sont conservées à part de celles
    d'une analyse approfondie (elles ne portent pas les craquements).
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(app_settings.data_dir(), CACHE_FILENAME)
        self._entries = {}
        self._lock = threading.Lock()
        # Verrou distinct pour l'écriture : plusieurs réanalyses peuvent
        # demander un enregistrement en même temps, et elles se disputeraient
        # le même fichier temporaire. Le séparer du verrou des données évite
        # au passage de bloquer les analyses en cours le temps du disque.
        self._io_lock = threading.Lock()
        self._dirty = False
        self.load()

    @staticmethod
    def _stamp(path):
        try:
            st = os.stat(path)
            return "%d:%d" % (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

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
            # Rien n'est perdu : les mesures restent en mémoire et le
            # prochain enregistrement les reprendra.
            with self._lock:
                self._dirty = True
            print(f"[quality] Cache non enregistré : {exc}")

    def get(self, path, deep):
        stamp = self._stamp(path)
        if stamp is None:
            return None
        with self._lock:
            entry = self._entries.get(os.path.normcase(os.path.abspath(path)))
        if not entry or entry.get("stamp") != stamp:
            return None
        measures = entry.get("measures")
        if not isinstance(measures, dict):
            return None
        # Une analyse rapide ne répond pas à une demande approfondie ;
        # l'inverse, si : les mesures approfondies contiennent les autres.
        if deep and not measures.get("deep"):
            return None
        return _decode_measures(measures)

    def put(self, path, measures):
        stamp = self._stamp(path)
        if stamp is None or measures.get("error"):
            return
        with self._lock:
            self._entries[os.path.normcase(os.path.abspath(path))] = {
                "stamp": stamp, "measures": _encode_measures(measures),
            }
            self._dirty = True

    def forget(self, path):
        with self._lock:
            if self._entries.pop(os.path.normcase(os.path.abspath(path)), None):
                self._dirty = True

    def clear(self):
        with self._lock:
            self._entries = {}
            self._dirty = True
        self.save()


# JSON ne connaît ni ``-inf`` ni ``nan`` au sens strict : les valeurs
# infinies sont converties en repères textuels à l'écriture.
_INF_TOKEN = "-inf"


def _encode_measures(measures):
    out = {}
    for key, value in measures.items():
        if isinstance(value, float) and value in (float("-inf"), float("inf")):
            out[key] = _INF_TOKEN if value < 0 else "inf"
        elif isinstance(value, float) and value != value:
            out[key] = None
        else:
            out[key] = value
    return out


def _decode_measures(stored):
    out = dict(stored)
    for key, value in stored.items():
        if value == _INF_TOKEN:
            out[key] = float("-inf")
        elif value == "inf":
            out[key] = float("inf")
    if out.get("peak_db") is None:
        out["peak_db"] = float("nan")
    return out


# ----------------------------------------------------------------------
# Thread d'analyse
# ----------------------------------------------------------------------
class QualityScanThread(QThread):
    """Parcourt une bibliothèque et mesure la qualité de chaque fichier.

    Le parcours et la lecture des tags reprennent ``reencode_scan`` ; s'y
    ajoute une analyse ffmpeg par fichier, répartie sur un pool de threads.
    Les résultats remontent par lots pour que le tableau se remplisse au fil
    de l'eau, ce qui compte quand l'analyse d'une bibliothèque entière prend
    plusieurs minutes.
    """

    total = pyqtSignal(int)        # nombre de fichiers à analyser
    progress = pyqtSignal(int)     # nombre de fichiers analysés
    batch = pyqtSignal(list)       # lot d'entrées prêtes
    finished_scan = pyqtSignal(list, bool)   # entrées complètes, analyse entière

    BATCH_SIZE = 8

    def __init__(self, paths, deep=True, cache=None, parent=None):
        super().__init__(parent)
        self.paths = list(paths)
        self.deep = bool(deep)
        self.cache = cache
        self._running = True
        self._procs = set()

    def stop(self):
        """Demande l'arrêt et tue les lectures ffmpeg déjà lancées."""
        self._running = False
        for proc in list(self._procs):
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _collect(self):
        files = []
        for entry in self.paths:
            if not self._running:
                break
            if os.path.isdir(entry):
                files.extend(reencode_scan.iter_audio_files(entry))
            elif os.path.isfile(entry) and entry.lower().endswith(
                    reencode_scan.AUDIO_EXTENSIONS):
                files.append(entry)
        return files

    def _analyse_one(self, path):
        if not self._running:
            return None
        measures = None
        if self.cache is not None:
            measures = self.cache.get(path, self.deep)
        if measures is None:
            measures = measure(path, deep=self.deep, registry=self._procs)
            if not self._running:
                return None
            if self.cache is not None:
                self.cache.put(path, measures)
        return build_entry(path, measures)

    def run(self):
        files = self._collect()
        self.total.emit(len(files))

        results, pending = [], []
        executor = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
        try:
            for entry in executor.map(self._analyse_one, files):
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
            executor.shutdown(wait=False, cancel_futures=True)

        if pending and self._running:
            self.batch.emit(pending)
        self.progress.emit(len(results))
        if self.cache is not None:
            self.cache.save()
        self.finished_scan.emit(results, self._running)


def build_entry(path, measures):
    """Assemble tags et mesures en une ligne exploitable par l'écran."""
    tags = reencode_scan.read_tags(path, need_bitrate=True)
    entry = {
        "path": path,
        # Jeu complet de tags : un retéléchargement range le fichier produit
        # selon les règles d'« Organiser », dont le motif de nommage peut
        # réclamer l'année, le numéro de piste ou le genre.
        "tags": dict(tags),
        "title": tags.get("title", ""),
        "artist": tags.get("artist", ""),
        "album": tags.get("album", ""),
        "bitrate": tags.get("bitrate", 0),
        "format": os.path.splitext(path)[1].lower().lstrip(".").upper(),
        "measures": measures,
        "status": "",
    }
    entry["defects"], entry["severities"], entry["score"] = evaluate(measures)
    return entry


class ReferenceLookupThread(QThread):
    """Cherche en ligne la durée de référence des titres qu'on lui soumet.

    Le thread vit le temps d'une analyse et consomme une file alimentée au
    fil de l'eau : l'analyse rend ses lignes par lots de huit, et créer un
    thread par lot en ferait des centaines sur une bibliothèque entière.

    Il est séparé de l'analyse pour deux raisons : le réseau ne doit pas
    ralentir les lectures ffmpeg, et les recherches doivent survivre à l'arrêt
    de l'analyse — les lignes déjà obtenues attendent encore leur verdict.

    La cadence est fixée par ``reference_duration`` : environ cinq titres par
    seconde tant que Deezer répond, un par seconde pour ceux qu'il ignore et
    qu'il faut demander à MusicBrainz. Une bibliothèque de trois mille titres
    demande donc une quinzaine de minutes la première fois, et rien du tout
    ensuite — les réponses sont sur le disque.
    """

    found = pyqtSignal(list)     # [(identifiant, durée de référence ou None), …]

    BATCH_SIZE = 10
    # Au-delà, une recherche interrompue perdrait trop de réponses déjà
    # payées : le cache est écrit sans attendre la fin.
    SAVE_EVERY = 50

    def __init__(self, cache=None, parent=None):
        super().__init__(parent)
        self._jobs = queue.Queue()
        self.cache = cache
        self._running = True

    def submit(self, jobs):
        """Ajoute des titres à chercher : (identifiant, artiste, titre, durée)."""
        for job in jobs:
            self._jobs.put(job)

    def stop(self):
        self._running = False

    def _emit(self, pending):
        if pending:
            self.found.emit(list(pending))
        del pending[:]

    def run(self):
        pending, since_save = [], 0
        while self._running:
            try:
                uid, artist, title, measured = self._jobs.get(timeout=0.3)
            except queue.Empty:
                # File vide : rendre ce qui est trouvé plutôt que le garder
                # jusqu'au prochain lot d'analyse.
                self._emit(pending)
                continue
            try:
                durations = reference_duration.lookup(artist, title, cache=self.cache)
            except Exception as exc:      # une recherche ratée ne casse rien
                print(f"[quality] Durée de référence introuvable "
                      f"({artist} - {title}) : {exc}")
                durations = None
            pending.append((uid, reference_duration.closest(durations, measured)))
            since_save += 1
            if len(pending) >= self.BATCH_SIZE:
                self._emit(pending)
            if self.cache is not None and since_save >= self.SAVE_EVERY:
                self.cache.save()
                since_save = 0
        self._emit(pending)
        if self.cache is not None:
            self.cache.save()


class SingleAnalysisThread(QThread):
    """Réanalyse d'un fichier isolé, après son retéléchargement.

    Le remplacement d'un fichier n'a d'intérêt que s'il corrige le défaut :
    mesurer à nouveau donne la réponse sans relancer l'analyse complète.
    """

    done = pyqtSignal(int, object)   # identifiant de la ligne, entrée ou None

    def __init__(self, uid, path, deep=True, cache=None, parent=None):
        super().__init__(parent)
        self.uid = uid
        self.path = path
        self.deep = bool(deep)
        self.cache = cache

    def run(self):
        try:
            measures = measure(self.path, deep=self.deep)
            if self.cache is not None:
                self.cache.put(self.path, measures)
                self.cache.save()
            self.done.emit(self.uid, build_entry(self.path, measures))
        except Exception as exc:      # une réanalyse ratée ne doit rien casser
            print(f"[quality] Réanalyse impossible ({self.path}) : {exc}")
            self.done.emit(self.uid, None)
