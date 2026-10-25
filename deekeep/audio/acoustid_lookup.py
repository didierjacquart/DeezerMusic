"""
Identification d'un fichier par son empreinte acoustique (AcoustID).

Dernier recours de la recherche de tags : quand ni les tags, ni le nom du
fichier, ni celui du dossier ne disent quoi que ce soit, seule l'écoute du
signal peut identifier le morceau. C'est ce que fait Chromaprint, en
réduisant les premières ``FINGERPRINT_SECONDS`` secondes à une empreinte
que l'on confronte à la base AcoustID — laquelle renvoie l'enregistrement
MusicBrainz correspondant.

Deux prérequis, tous deux extérieurs à l'application :

    - un calculateur d'empreinte. « fpcalc » (l'outil de Chromaprint) est
      préféré : c'est la référence, et son format est celui qu'attend le
      service. À défaut, un ffmpeg compilé avec le muxer « chromaprint »
      fait l'affaire — celui livré avec l'application ne l'est pas
      forcément, ``fingerprint_tool`` le vérifie plutôt que de le supposer ;
    - une clé AcoustID. Elle est gratuite mais nominative
      (https://acoustid.org/new-application) : le service refuse les appels
      anonymes, il n'y a pas de contournement.

Sans l'un ou l'autre, ``availability`` explique lequel manque et la
recherche par empreinte est simplement passée — les autres services, eux,
n'ont besoin de rien.
"""

import json
import os
import sys

import requests

from deekeep.audio import audio_probe
from deekeep.core.utils import resource_path

LOOKUP_URL = "https://api.acoustid.org/v2/lookup"

# Durée analysée. C'est la valeur par défaut de fpcalc, et celle sur
# laquelle la base a été construite : la modifier dégraderait les
# correspondances plus qu'elle ne les améliorerait.
FINGERPRINT_SECONDS = 120

# Score rendu par AcoustID (0 à 1). Une vraie correspondance dépasse
# largement 0,9 ; en dessous de ``MIN_MATCH`` il ne s'agit au mieux que
# d'un fragment commun.
MIN_MATCH = 0.5
SURE_MATCH = 0.85

_tool = None            # résultat mémoïsé de la détection
_tool_searched = False

# Exécution de commandes et localisation de ffmpeg : mutualisées avec les
# autres mesures faites sur le signal.
_run = audio_probe.run


def _ffmpeg_has_chromaprint(exe):
    """Vrai si ce ffmpeg sait produire une empreinte Chromaprint.

    Le muxer est facultatif à la compilation : beaucoup de binaires publics
    en sont dépourvus, dont celui que l'application embarque.
    """
    code, out, _err = _run([exe, "-hide_banner", "-muxers"], timeout=30)
    return code == 0 and "chromaprint" in out


def _candidate_fpcalc():
    """Chemins où chercher fpcalc : dossier livré avec l'application, puis PATH."""
    exe = "fpcalc.exe" if sys.platform == "win32" else "fpcalc"
    yield os.path.join(resource_path("fpcalc"), exe)
    yield os.path.join(resource_path("chromaprint"), exe)
    yield exe        # laissé au PATH


def fingerprint_tool(refresh=False):
    """Outil d'empreinte retenu : {"kind", "path"} ou None s'il n'y en a pas.

    La détection lance réellement les binaires (une fois, mémoïsée) : la
    présence d'un ffmpeg ne dit rien de son muxer chromaprint.
    """
    global _tool, _tool_searched
    if _tool_searched and not refresh:
        return _tool

    _tool_searched = True
    _tool = None
    for path in _candidate_fpcalc():
        code, out, _err = _run([path, "-version"], timeout=20)
        if code == 0 or "fpcalc" in out.lower():
            _tool = {"kind": "fpcalc", "path": path}
            return _tool
    for path in audio_probe.ffmpeg_candidates():
        if _ffmpeg_has_chromaprint(path):
            _tool = {"kind": "ffmpeg", "path": path}
            return _tool
    return _tool


def availability(key):
    """(disponible, message) — ce qui manque, dit en clair, pour l'interface."""
    tool = fingerprint_tool()
    if tool is None:
        return False, (
            "aucun calculateur d'empreinte : installez fpcalc (Chromaprint) "
            "dans le dossier « fpcalc » de l'application, ou un ffmpeg "
            "compilé avec le muxer chromaprint"
        )
    if not str(key or "").strip():
        return False, (
            "clé AcoustID absente : elle est gratuite sur "
            "acoustid.org/new-application, à renseigner dans les Paramètres"
        )
    label = "fpcalc" if tool["kind"] == "fpcalc" else "ffmpeg (muxer chromaprint)"
    return True, f"empreinte calculée par {label}"


def compute_fingerprint(path, seconds=FINGERPRINT_SECONDS):
    """Empreinte Chromaprint en base64, ou chaîne vide en cas d'échec."""
    tool = fingerprint_tool()
    if tool is None or not os.path.exists(path):
        return ""

    if tool["kind"] == "fpcalc":
        code, out, _err = _run(
            [tool["path"], "-json", "-length", str(seconds), path], timeout=120)
        if code != 0:
            return ""
        try:
            return str(json.loads(out).get("fingerprint") or "")
        except Exception:
            # Sortie texte (« FINGERPRINT=… ») des versions anciennes.
            for line in out.splitlines():
                if line.upper().startswith("FINGERPRINT="):
                    return line.split("=", 1)[1].strip()
            return ""

    code, out, _err = _run([
        tool["path"], "-hide_banner", "-loglevel", "error", "-i", path,
        "-t", str(seconds), "-ac", "1", "-ar", "22050",
        "-f", "chromaprint", "-fp_format", "base64", "-",
    ], timeout=120)
    return out.strip() if code == 0 else ""


def parse_results(payload, min_match=MIN_MATCH):
    """Convertit la réponse AcoustID en pistes candidates.

    Même forme que les autres services (titre, artiste, album, durée), plus
    ``match`` : le score rendu par AcoustID, qui vaut identification et ne
    demande aucune corroboration textuelle.
    """
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return []

    tracks = []
    for result in payload.get("results") or []:
        try:
            match = float(result.get("score") or 0.0)
        except (TypeError, ValueError):
            match = 0.0
        if match < min_match:
            continue
        for recording in result.get("recordings") or []:
            artists = " ".join(
                str(a.get("name") or "") for a in recording.get("artists") or []
            ).strip()
            groups = [str(g.get("title") or "")
                      for g in recording.get("releasegroups") or []]
            tracks.append({
                "title": str(recording.get("title") or ""),
                "artist": artists,
                "album": groups[0] if groups else "",
                "albums": groups,
                "seconds": int(recording.get("duration") or 0),
                "cover_url": "",
                "match": match,
            })
    # Le meilleur score d'abord : plusieurs enregistrements peuvent partager
    # une même empreinte (rééditions, compilations).
    tracks.sort(key=lambda t: -t["match"])
    return tracks


def lookup(path, duration, key):
    """Pistes identifiées par empreinte pour ce fichier (liste, jamais None)."""
    ok, _message = availability(key)
    if not ok:
        return []
    fingerprint = compute_fingerprint(path)
    if not fingerprint:
        return []
    try:
        resp = requests.get(LOOKUP_URL, params={
            "client": str(key).strip(),
            "meta": "recordings+releasegroups",
            "duration": int(duration or 0),
            "fingerprint": fingerprint,
        }, timeout=20)
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    try:
        return parse_results(resp.json())
    except Exception:
        return []
