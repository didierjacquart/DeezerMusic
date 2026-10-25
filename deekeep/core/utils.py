"""
Fonctions utilitaires : nettoyage de noms de fichiers, formatage de durée,
génération vectorielle de l'icône applicative et marquage des jaquettes.
"""

import os
import re
import subprocess
import sys

from PyQt6.QtCore import Qt, QBuffer, QIODevice, QUrl
from PyQt6.QtGui import (QColor, QPixmap, QIcon, QImage, QImageReader,
                         QPainter, QBrush, QPen, QDesktopServices)


# Marquage des jaquettes : côté du badge rapporté au plus petit côté de la
# jaquette, et marge depuis les bords haut et droit (même référence). Le
# badge doit se lire sans jamais disputer la vedette à la pochette.
APP_BADGE_RATIO = 0.18
APP_BADGE_MARGIN = 0.04
# En deçà, la jaquette est une vignette : y poser un badge la masquerait
# plus qu'il ne la signerait.
APP_BADGE_MIN_COVER = 96


# Racine du projet : les ressources livrées avec l'application (icon.ico,
# ffmpeg, deno) sont posées à côté de main.py, soit deux dossiers au-dessus
# de ce module (deekeep/core/utils.py).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def resource_path(relative):
    """Chemin d'une ressource, valable en script comme en exécutable gelé.

    PyInstaller décompresse les ressources dans un dossier temporaire
    exposé via sys._MEIPASS ; en mode script on part de la racine du projet.
    """
    base = getattr(sys, "_MEIPASS", PROJECT_ROOT)
    return os.path.join(base, relative)


def ffmpeg_location():
    """Dossier du FFmpeg embarqué avec l'application, ou None s'il est absent.

    Quand le dossier ``ffmpeg`` (contenant ffmpeg.exe/ffprobe.exe) est livré
    avec l'application, yt-dlp doit être pointé dessus via son option
    ``ffmpeg_location`` : l'application fonctionne alors même sans FFmpeg
    installé sur le poste. En cas d'absence, retourne None et yt-dlp
    retombe sur le PATH système.
    """
    directory = resource_path("ffmpeg")
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    if os.path.exists(os.path.join(directory, exe)):
        return directory
    return None


def deno_location():
    """Dossier du runtime JavaScript Deno embarqué, ou None s'il est absent.

    Les versions récentes de YouTube exigent un moteur JavaScript côté
    yt-dlp (sans quoi certaines extractions échouent ou perdent des
    formats). Deno (deno.exe) est livré avec l'application dans le dossier
    ``deno`` ; yt-dlp le détecte automatiquement s'il figure dans le PATH.
    """
    directory = resource_path("deno")
    exe = "deno.exe" if sys.platform == "win32" else "deno"
    if os.path.exists(os.path.join(directory, exe)):
        return directory
    return None


def ensure_deno_on_path():
    """Ajoute le Deno embarqué au PATH du processus (détection par yt-dlp).

    yt-dlp recherche ``deno`` dans le PATH : préfixer notre dossier garantit
    qu'il utilise le runtime livré avec l'application même si aucun Deno
    n'est installé sur le poste. Sans effet si le binaire embarqué est absent.
    """
    directory = deno_location()
    if not directory:
        return
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if directory not in parts:
        os.environ["PATH"] = directory + os.pathsep + current


def _select_via_shell(path):
    """Sélectionne le fichier via l'API shell de Windows (SHOpenFolderAndSelectItems).

    On passe par l'API plutôt que par la ligne de commande : ``explorer
    /select,`` n'accepte pas que son argument soit mis entre guillemets d'un
    bloc, ce que fait toute construction de ligne de commande dès que le
    chemin contient une espace : l'explorateur ouvre alors sa page d'accueil
    au lieu du dossier demandé.
    """
    import ctypes

    shell32 = ctypes.windll.shell32
    # Le shell exige un apartment COM initialisé sur le thread appelant.
    ctypes.windll.ole32.CoInitialize(None)
    # restype explicite : sans lui ctypes tronque le pointeur à 32 bits et
    # la sélection échoue silencieusement sur un Windows 64 bits.
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]

    pidl = shell32.ILCreateFromPathW(path)
    if not pidl:
        return False
    try:
        return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
    finally:
        shell32.ILFree(pidl)


def reveal_in_explorer(path):
    """Ouvre l'explorateur sur le fichier, sélectionné dans son dossier.

    Ouvrir le dossier seul laisse l'utilisateur chercher son fichier au
    milieu des autres. En dernier recours (autre système, fichier disparu
    entre-temps, API indisponible), on retombe sur l'ouverture du dossier
    parent.
    """
    path = os.path.normpath(path) if path else ""
    folder = os.path.dirname(path) if path else ""
    if path and sys.platform == "win32" and os.path.exists(path):
        try:
            if _select_via_shell(path):
                return True
        except (OSError, AttributeError):
            pass
        # Repli : la ligne de commande, avec les guillemets autour du seul
        # chemin. La chaîne est passée telle quelle : une liste d'arguments
        # ferait guillemeter ``/select,`` avec le chemin.
        try:
            subprocess.Popen('explorer /select,"%s"' % path)
            return True
        except OSError:
            pass
    if folder and os.path.isdir(folder):
        return QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
    return False


def sanitize_filename(name):
    """Retire les caractères interdits dans un nom de fichier."""
    return re.sub(r'[\\/*?:"<>|]', "", name)


def format_duration(seconds):
    """Convertit une durée en secondes vers le format mm:ss."""
    try:
        secs = int(seconds)
        mins = secs // 60
        secs = secs % 60
        return f"{mins:02d}:{secs:02d}"
    except Exception:
        return "--:--"


def create_app_icon():
    """Icône de l'application : fichier icon.ico si présent, sinon vectoriel.

    Le .ico (multi-résolutions) donne un meilleur rendu dans la barre des
    tâches et la zone de notification ; le dessin vectoriel reste la
    référence et sert de repli quand le fichier n'existe pas.
    """
    ico = resource_path("icon.ico")
    if os.path.exists(ico):
        icon = QIcon(ico)
        if not icon.isNull():
            return icon
    return _draw_app_icon()


def app_icon_image(size):
    """Icône de l'application en QImage carrée de côté ``size``, fond transparent.

    QImage et non QPixmap : le marquage des jaquettes s'exécute dans les
    threads de téléchargement, où seul QImage peut être manipulé (Qt réserve
    QPixmap au thread graphique).
    """
    image = _icon_file_image(size)
    if image is not None and not image.isNull():
        return image
    return _draw_app_icon_image(size)


def _icon_file_image(size):
    """Meilleure image d'icon.ico ramenée à ``size``, ou None s'il est absent.

    Un .ico porte plusieurs résolutions : on retient la plus petite qui
    couvre déjà la taille demandée (réduire préserve les détails, agrandir
    les invente), à défaut la plus grande disponible.
    """
    path = resource_path("icon.ico")
    if not os.path.exists(path):
        return None
    try:
        reader = QImageReader(path)
        frames = []
        while True:
            frame = reader.read()
            if frame.isNull():
                break
            frames.append(frame)
            if not reader.jumpToNextImage():
                break
        if not frames:
            return None
        covering = [f for f in frames if min(f.width(), f.height()) >= size]
        best = (min(covering, key=lambda f: f.width()) if covering
                else max(frames, key=lambda f: f.width()))
        return best.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    except Exception:
        return None


def _draw_app_icon(size=128):
    """Génère l'icône vectorielle de l'application (note de musique)."""
    return QIcon(QPixmap.fromImage(_draw_app_icon_image(size)))


def _draw_app_icon_image(size=128):
    """Dessin vectoriel de l'icône, rendu dans une QImage."""
    from PyQt6.QtGui import QPainterPath

    pixmap = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # Le dessin est défini dans un repère 128×128 : on met à l'échelle.
    painter.scale(size / 128.0, size / 128.0)

    # Fond arrondi cyan néon (design Neon Nocturne)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#00e5ff")))
    painter.drawRoundedRect(0, 0, 128, 128, 30, 30)

    # Note de musique sombre (croche) posée sur le cyan
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#00363d")))

    stem_x, stem_w = 78, 9
    stem_top, stem_bottom = 32, 90

    # Hampe (verticale, côté droit de la tête)
    painter.drawRoundedRect(stem_x, stem_top, stem_w, stem_bottom - stem_top, 4, 4)

    # Crochet (drapeau) : forme pleine et lisse partant du haut de la hampe
    flag = QPainterPath()
    flag.moveTo(stem_x + stem_w, stem_top)
    flag.cubicTo(stem_x + 34, stem_top + 8, stem_x + 32, stem_top + 30, stem_x + 8, stem_top + 34)
    flag.cubicTo(stem_x + 26, stem_top + 24, stem_x + 26, stem_top + 12, stem_x + stem_w, stem_top + 14)
    flag.closeSubpath()
    painter.drawPath(flag)

    # Tête de note (ellipse inclinée, accolée au bas de la hampe)
    painter.save()
    painter.translate(62, 84)
    painter.rotate(-20)
    painter.drawEllipse(-20, -14, 40, 28)
    painter.restore()

    painter.end()
    return pixmap


def stamp_app_icon(cover_bytes, ratio=APP_BADGE_RATIO, margin=APP_BADGE_MARGIN):
    """Jaquette signée de l'icône de l'application, en haut à droite (JPEG).

    Le badge marque les fichiers passés par DeeKeep — ajoutés, téléchargés
    ou réencodés : la pochette elle-même dit d'où vient le titre, sans qu'il
    faille ouvrir ses tags. Il occupe moins d'un cinquième du plus petit
    côté, assez pour se voir, trop peu pour gêner la pochette.

    Retourne les octets d'origine inchangés si la jaquette est vide,
    illisible, trop petite pour être signée, ou si l'icône est introuvable :
    marquer n'est jamais une raison de perdre une pochette.
    """
    if not cover_bytes:
        return cover_bytes
    try:
        cover = QImage()
        if not cover.loadFromData(cover_bytes):
            return cover_bytes
        side = min(cover.width(), cover.height())
        if side < APP_BADGE_MIN_COVER:
            return cover_bytes

        badge = max(16, int(round(side * ratio)))
        gap = max(1, int(round(side * margin)))
        icon = app_icon_image(badge)
        if icon is None or icon.isNull():
            return cover_bytes

        # RGB32 : la pochette est réenregistrée en JPEG, qui ignore la
        # transparence — la convertir ici évite un fond noir sur les PNG.
        cover = cover.convertToFormat(QImage.Format.Format_RGB32)
        painter = QPainter(cover)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(cover.width() - icon.width() - gap, gap, icon)
        painter.end()

        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not cover.save(buffer, "JPEG", 92):
            return cover_bytes
        return bytes(buffer.data())
    except Exception:
        return cover_bytes
