"""
Module de lecture : un lecteur audio complet, partagé par tous les écrans.

Un titre mal extrait ne se reconnaît pas à ses chiffres. La page « Qualité »
peut bien annoncer un souffle à 38 dB et deux secondes de blanc final : seule
l'écoute tranche. Ce module donne donc à chaque titre affiché — page Musique,
historique des téléchargements, Réencoder, Organiser, Qualité — le même
bouton « ▶ » et la même fenêtre de lecture, appelée avec la liste visible :
le titre suivant s'enchaîne sans repasser par le tableau.

Deux lectures cohabitent, et ce module les tient toutes les deux : le
**lecteur complet** (fenêtre, égaliseur, file d'attente) pour un fichier du
disque, et l'**extrait de 30 s** servi par Deezer pour un titre pas encore
extrait — celui-là sans fenêtre, un simple flux qu'un second clic arrête
(``play_preview`` / ``stop_preview``, appelés par la page Musique et par la
fenêtre de recherche). Elles partagent une règle et une seule : un seul son à
la fois. Lancer un extrait met la fenêtre en pause (``pause_for_preview``),
et ouvrir ou relancer la fenêtre coupe l'extrait.

Trois pièces :

* ``SpectrumThread`` décode le fichier avec le ffmpeg livré avec
  l'application et en tire, vingt fois par seconde de musique, le niveau de
  dix bandes d'octave (31 Hz → 16 kHz).
* ``SpectrumView`` affiche ces niveaux façon égaliseur à diodes, avec
  maintien de crête.
* ``PlayerDialog`` assemble le tout : jaquette, égaliseur, curseur de
  position, temps écoulé / durée, volume et enchaînement de la liste.

Pourquoi analyser le fichier plutôt qu'écouter la sortie audio : Qt ne donne
aucun accès aux échantillons joués par ``QMediaPlayer``. L'analyse est donc
menée en parallèle sur le fichier lui-même, et chaque trame est rangée à son
instant — la position du lecteur suffit alors à savoir laquelle afficher, y
compris après un déplacement dans le titre.

L'analyse est volontairement bridée : elle prend de l'avance sur la lecture
(``SpectrumThread.LEAD_SECONDS``) puis s'endort. Sans ce frein, une
transformée de Fourier écrite en Python pur dévore un cœur entier pendant
quelques secondes et l'interface saccade — pour un résultat qui ne servira
que trois minutes plus tard.
"""

import math
import os
import subprocess
import sys
from array import array

from PyQt6.QtCore import Qt, QPointF, QRectF, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QPainter,
                         QPainterPath, QPixmap)
from PyQt6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QSlider, QStyle,
                             QMessageBox, QSizePolicy, QTableWidgetItem)

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    MULTIMEDIA_AVAILABLE = True
except ImportError:      # PyQt6 installé sans le module Multimedia
    QMediaPlayer = QAudioOutput = None
    MULTIMEDIA_AVAILABLE = False

from deekeep.audio import audio_probe
from deekeep.core import config
from deekeep.core.utils import reveal_in_explorer
from deekeep.ui.styles import ON_PRIMARY, PRIMARY, STYLE_SHEET, TEXT_FAINT

# --- Analyse spectrale ---------------------------------------------------
# 32 kHz : la dernière bande de l'égaliseur est centrée sur 16 kHz, soit la
# fréquence de Nyquist de ce taux — rien au-dessus n'est affiché, rien
# au-dessus n'est donc décodé.
RATE = 32000
# Fenêtre d'analyse : 1024 points à 32 kHz donnent une résolution de 31 Hz,
# tout juste ce qu'il faut pour isoler la bande des 31 Hz du continu.
WINDOW = 1024
# Trames d'analyse par seconde de musique. L'affichage est rafraîchi plus
# souvent (voir ``SpectrumView``) : la descente des barres est interpolée.
FPS = 20
HOP = RATE // FPS
# Lecture du tube ffmpeg par paquets d'une demi-seconde de musique.
READ_FRAMES = 10

BAND_LABELS = ("31", "62", "125", "250", "500", "1K", "2K", "4K", "8K", "16K")
BAND_CENTERS = (31.25, 62.5, 125.0, 250.0, 500.0, 1000.0,
                2000.0, 4000.0, 8000.0, 16000.0)

# Plage affichée, en dB sous la pleine échelle numérique : au-dessus de
# DB_CEIL toutes les diodes d'une bande sont allumées, en dessous de DB_FLOOR
# aucune. La fenêtre est calée sur ce que mesure réellement une bande d'un
# morceau masterisé récent (de l'ordre de -25 dB dans les passages forts,
# -50 dB dans les passages calmes) : plus bas, tout resterait au plafond.
DB_FLOOR = -66.0
DB_CEIL = -6.0
# Compensation de pente : l'énergie d'un enregistrement musical décroît avec
# la fréquence, et sans correction les quatre dernières bandes resteraient
# éteintes sur tous les titres. Gain ajouté par rang de bande.
BAND_TILT = 2.0


def _fft_tables(size):
    """Tables de permutation binaire et de rotation d'une FFT de ``size`` points."""
    levels = size.bit_length() - 1
    reverse = array("i", [0]) * size
    for i in range(size):
        value, rev = i, 0
        for _ in range(levels):
            rev = (rev << 1) | (value & 1)
            value >>= 1
        reverse[i] = rev
    twiddles = []
    span = 2
    while span <= size:
        half = span // 2
        twiddles.append([
            complex(math.cos(-2.0 * math.pi * k / span),
                    math.sin(-2.0 * math.pi * k / span))
            for k in range(half)
        ])
        span *= 2
    return reverse, twiddles


_REVERSE, _TWIDDLES = _fft_tables(WINDOW)
# Fenêtre de Hann : sans elle, la coupure aux bords de chaque tranche produit
# un étalement spectral qui allume toutes les bandes à la fois.
_HANN = [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (WINDOW - 1))
         for i in range(WINDOW)]
# Amplitude, sur sa raie, d'une sinusoïde pleine échelle fenêtrée :
# 32768 × N × gain de la fenêtre (0,5) / 2. C'est le 0 dB de référence.
_FULL_SCALE = 32768.0 * WINDOW / 4.0


def _band_bins():
    """Raies de la FFT retenues par bande (bornes à une demi-octave du centre)."""
    limit = WINDOW // 2
    spread = math.sqrt(2.0)
    bins = []
    for center in BAND_CENTERS:
        low = int(math.floor(center / spread * WINDOW / RATE))
        high = int(math.ceil(center * spread * WINDOW / RATE))
        low = max(1, min(low, limit - 1))
        high = max(low + 1, min(high, limit))
        bins.append((low, high))
    return tuple(bins)


_BAND_BINS = _band_bins()


def _fft(values):
    """FFT radix-2 d'une liste de ``WINDOW`` complexes, déjà permutée en entrée."""
    span, level = 2, 0
    while span <= WINDOW:
        half = span // 2
        table = _TWIDDLES[level]
        for start in range(0, WINDOW, span):
            for k in range(half):
                i = start + k
                j = i + half
                product = table[k] * values[j]
                base = values[i]
                values[i] = base + product
                values[j] = base - product
        span *= 2
        level += 1
    return values


def band_levels(samples):
    """Niveaux 0..1 des dix bandes pour une fenêtre d'échantillons entiers."""
    spectrum = _fft([complex(samples[_REVERSE[i]] * _HANN[_REVERSE[i]], 0.0)
                     for i in range(WINDOW)])
    levels = []
    for rank, (low, high) in enumerate(_BAND_BINS):
        energy = 0.0
        for k in range(low, high):
            value = spectrum[k]
            energy += value.real * value.real + value.imag * value.imag
        # Moyenne quadratique et non somme : les bandes hautes couvrent des
        # dizaines de raies, les basses une seule.
        magnitude = math.sqrt(energy / (high - low))
        decibels = (20.0 * math.log10(magnitude / _FULL_SCALE + 1e-12)
                    + rank * BAND_TILT)
        ratio = (decibels - DB_FLOOR) / (DB_CEIL - DB_FLOOR)
        levels.append(0.0 if ratio < 0.0 else (1.0 if ratio > 1.0 else ratio))
    return levels


class SpectrumThread(QThread):
    """Décode un fichier via ffmpeg et publie les niveaux de bandes.

    Les trames sont émises par paquets, dans l'ordre, avec l'indice de la
    première : le dialogue les empile et retrouve celle d'un instant donné
    par une simple division (voir ``PlayerDialog._tick``).
    """

    frames_ready = pyqtSignal(int, list)
    failed = pyqtSignal(str)

    # Avance maximale de l'analyse sur la lecture, en secondes de musique.
    # Au-delà, le thread dort : rien n'oblige à calculer en trois secondes ce
    # qui s'affichera dans trois minutes.
    LEAD_SECONDS = 25

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source
        self._stopped = False
        self._proc = None
        # Trame en cours de lecture, renseignée par le dialogue. Un entier
        # nu suffit : sa lecture et son écriture sont atomiques, et une
        # valeur en retard d'une trame ne change rien au bridage.
        self._played = 0

    def set_played_frame(self, index):
        self._played = max(0, int(index))

    def stop(self):
        """Interrompt l'analyse et le décodage (appelable depuis l'interface)."""
        self._stopped = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def run(self):
        exe = audio_probe.ffmpeg_exe()
        if exe is None:
            self.failed.emit("FFmpeg introuvable : égaliseur indisponible.")
            return

        args = [
            exe, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", self.source, "-vn", "-ac", "1", "-ar", str(RATE),
            "-f", "s16le", "-",
        ]
        try:
            self._proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=audio_probe.NO_WINDOW,
            )
        except OSError as exc:
            self.failed.emit("Décodage impossible : %s" % exc)
            return

        lead = self.LEAD_SECONDS * FPS
        buffer = bytearray()
        index = 0
        try:
            while not self._stopped:
                while not self._stopped and index - self._played > lead:
                    self.msleep(100)
                if self._stopped:
                    break
                chunk = self._proc.stdout.read(HOP * 2 * READ_FRAMES)
                if not chunk:
                    break
                buffer += chunk
                batch = []
                # HOP (1600) dépasse WINDOW (1024) : dès qu'un saut complet
                # est disponible, la fenêtre qui le précède l'est aussi.
                while len(buffer) >= HOP * 2:
                    samples = array("h")
                    samples.frombytes(bytes(buffer[:WINDOW * 2]))
                    if sys.byteorder != "little":
                        samples.byteswap()      # le flux ffmpeg est en s16le
                    batch.append(band_levels(samples))
                    del buffer[:HOP * 2]
                if batch:
                    self.frames_ready.emit(index, batch)
                    index += len(batch)
        except (OSError, ValueError):
            pass                                # tube fermé : fin d'analyse
        finally:
            proc, self._proc = self._proc, None
            if proc is not None:
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass


class SpectrumView(QWidget):
    """Égaliseur à diodes : dix colonnes de segments, avec maintien de crête.

    Le dialogue pousse ici les niveaux de la trame courante (``push``). La
    montée est immédiate — c'est l'attaque du son —, la descente amortie :
    des barres qui suivraient l'analyse à la lettre clignoteraient sans rien
    laisser voir.
    """

    COLUMNS = 10
    SEGMENTS = 18
    # Descente, en fraction de hauteur par rafraîchissement (40 ms).
    DECAY = 0.055
    # Crête : nombre de rafraîchissements de maintien, puis chute.
    PEAK_HOLD = 12
    PEAK_FALL = 0.02

    # Marges : place pour l'échelle latérale et les fréquences du bas.
    PAD_SIDE = 30
    PAD_TOP = 10
    PAD_BOTTOM = 20

    BACKGROUND = QColor("#080a0c")
    BORDER = QColor(255, 255, 255, 18)
    LOW = QColor("#22d366")          # bas de colonne : vert franc
    MID = QColor("#7dff9b")          # milieu : vert clair
    HIGH = QColor("#ffcf7a")         # haut : ambre
    TOP = QColor("#ff8a80")          # sommet : rouge doux
    PEAK = QColor("#00e5ff")         # trait de crête : cyan de l'application
    SCALE = QColor(132, 147, 150, 150)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._levels = [0.0] * self.COLUMNS
        self._peaks = [0.0] * self.COLUMNS
        self._holds = [0] * self.COLUMNS
        self._segment_colors = [self._segment_color(index)
                                for index in range(self.SEGMENTS)]

    def _segment_color(self, index):
        """Couleur d'un segment selon sa hauteur dans la colonne."""
        ratio = index / float(self.SEGMENTS - 1)
        if ratio < 0.55:
            return self._blend(self.LOW, self.MID, ratio / 0.55)
        if ratio < 0.82:
            return self._blend(self.MID, self.HIGH, (ratio - 0.55) / 0.27)
        return self._blend(self.HIGH, self.TOP, (ratio - 0.82) / 0.18)

    @staticmethod
    def _blend(first, second, ratio):
        ratio = min(1.0, max(0.0, ratio))
        return QColor(
            round(first.red() + (second.red() - first.red()) * ratio),
            round(first.green() + (second.green() - first.green()) * ratio),
            round(first.blue() + (second.blue() - first.blue()) * ratio),
        )

    def push(self, levels):
        """Nouvelle trame (ou ``None`` pour laisser retomber les barres)."""
        if not levels and not any(self._levels) and not any(self._peaks):
            return          # déjà éteint : rien à redessiner
        for index in range(self.COLUMNS):
            target = levels[index] if levels else 0.0
            current = self._levels[index]
            if target >= current:
                current = target
            else:
                current = max(target, current - self.DECAY)
            self._levels[index] = current

            if current >= self._peaks[index]:
                self._peaks[index] = current
                self._holds[index] = self.PEAK_HOLD
            elif self._holds[index] > 0:
                self._holds[index] -= 1
            else:
                self._peaks[index] = max(current,
                                         self._peaks[index] - self.PEAK_FALL)
        self.update()

    def reset(self):
        """Éteint l'égaliseur (changement de titre, arrêt)."""
        self._levels = [0.0] * self.COLUMNS
        self._peaks = [0.0] * self.COLUMNS
        self._holds = [0] * self.COLUMNS
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        width, height = self.width(), self.height()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.BACKGROUND)
        painter.drawRoundedRect(0, 0, width - 1, height - 1, 8, 8)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(self.BORDER)
        painter.drawRoundedRect(0, 0, width - 1, height - 1, 8, 8)

        area_x = self.PAD_SIDE
        area_w = width - 2 * self.PAD_SIDE
        area_y = self.PAD_TOP
        area_h = height - self.PAD_TOP - self.PAD_BOTTOM
        if area_w <= 0 or area_h <= 0:
            painter.end()
            return

        seg_h = area_h / float(self.SEGMENTS)
        gap = max(1.0, seg_h * 0.30)
        col_w = area_w / float(self.COLUMNS)
        bar_w = col_w * 0.66

        painter.setPen(Qt.PenStyle.NoPen)
        for column in range(self.COLUMNS):
            level = self._levels[column]
            lit = int(round(level * self.SEGMENTS))
            peak = int(round(self._peaks[column] * self.SEGMENTS))
            x = area_x + column * col_w + (col_w - bar_w) / 2.0

            for segment in range(self.SEGMENTS):
                y = area_y + area_h - (segment + 1) * seg_h
                color = self._segment_colors[segment]
                if segment < lit:
                    painter.setBrush(color)
                elif segment + 1 == peak:
                    # Crête : la diode reste visible, en retrait.
                    painter.setBrush(QColor(self.PEAK.red(), self.PEAK.green(),
                                            self.PEAK.blue(), 200))
                else:
                    # Diode éteinte : la teinte de la colonne, presque noire.
                    painter.setBrush(QColor(color.red(), color.green(),
                                            color.blue(), 26))
                painter.drawRect(int(x), int(y + gap / 2.0),
                                 int(bar_w), max(1, int(seg_h - gap)))

        self._paint_scale(painter, width, area_x, area_y, area_h, seg_h)
        self._paint_bands(painter, area_x, col_w, height)
        painter.end()

    def _paint_scale(self, painter, width, area_x, area_y, area_h, seg_h):
        """Échelle relative de part et d'autre : points par diode, 10/5/0."""
        font = QFont(painter.font())
        font.setPointSize(7)
        painter.setFont(font)
        painter.setPen(self.SCALE)
        for segment in range(0, self.SEGMENTS + 1, 2):
            y = int(area_y + area_h - segment * seg_h)
            painter.drawPoint(area_x - 8, y)
            painter.drawPoint(width - area_x + 8, y)
        for value, segment in ((10, self.SEGMENTS), (5, self.SEGMENTS // 2), (0, 0)):
            y = int(area_y + area_h - segment * seg_h)
            painter.drawText(2, y - 6, self.PAD_SIDE - 12, 12,
                             int(Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignVCenter), str(value))
            painter.drawText(width - self.PAD_SIDE + 12, y - 6,
                             self.PAD_SIDE - 12, 12,
                             int(Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter), str(value))

    def _paint_bands(self, painter, area_x, col_w, height):
        """Fréquence centrale sous chaque colonne."""
        font = QFont(painter.font())
        font.setPointSize(7)
        painter.setFont(font)
        painter.setPen(self.SCALE)
        for column, label in enumerate(BAND_LABELS):
            painter.drawText(int(area_x + column * col_w),
                             height - self.PAD_BOTTOM + 2,
                             int(col_w), self.PAD_BOTTOM - 4,
                             int(Qt.AlignmentFlag.AlignHCenter
                                 | Qt.AlignmentFlag.AlignVCenter), label)
        painter.drawText(int(area_x + self.COLUMNS * col_w) + 2,
                         height - self.PAD_BOTTOM + 2, self.PAD_SIDE - 4,
                         self.PAD_BOTTOM - 4,
                         int(Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignVCenter), "Hz")


class SeekSlider(QSlider):
    """Curseur de position : un clic dans la rainure y amène la lecture.

    Le comportement par défaut de Qt avance d'une page à chaque clic, ce qui
    n'a pas de sens pour une barre de progression : on veut le point cliqué.
    """

    def _value_at(self, x):
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(x), self.width()
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.setValue(self._value_at(event.position().x()))
            self.setSliderDown(True)     # émet sliderPressed
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            value = self._value_at(event.position().x())
            self.setValue(value)
            self.sliderMoved.emit(value)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.isSliderDown() and event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(False)    # émet sliderReleased
            event.accept()
            return
        super().mouseReleaseEvent(event)


# Feuille de style locale du lecteur : l'application n'utilise pas de curseur
# ailleurs, la règle n'a donc pas sa place dans la feuille globale.
_SLIDER_STYLE = """
    QSlider::groove:horizontal {
        background: rgba(255, 255, 255, 0.08);
        height: 6px;
        border-radius: 3px;
    }
    QSlider::sub-page:horizontal {
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                    stop:0 #343d96, stop:1 #00e5ff);
        height: 6px;
        border-radius: 3px;
    }
    QSlider::handle:horizontal {
        background: #e5e2e1;
        width: 12px;
        height: 12px;
        margin: -4px 0;
        border-radius: 6px;
    }
    QSlider::handle:horizontal:hover {
        background: #00e5ff;
    }
    QSlider::groove:horizontal:disabled {
        background: rgba(255, 255, 255, 0.04);
    }
    QSlider::handle:horizontal:disabled {
        background: #4a5c60;
    }
"""

_TRANSPORT_STYLE = """
    QPushButton {
        background-color: rgba(0, 229, 255, 0.12);
        border: none;
        border-radius: %(radius)dpx;
    }
    QPushButton:hover {
        background-color: #00e5ff;
    }
    QPushButton:disabled {
        background-color: rgba(255, 255, 255, 0.04);
    }
"""

# Symbole de la colonne « ÉCOUTE » des tableaux : là, un caractère suffit —
# il s'aligne sur le texte des autres cellules et c'est la vue qui le rend.
GLYPH_PLAY = "▶"

# Même colonne, pendant l'extrait de 30 s : un deuxième clic l'arrête. Les
# pictogrammes dédiés (U+23F8…) sont rendus en couleur par la police emoji de
# Windows ; un carré géométrique, lui, reste du texte — comme le triangle.
GLYPH_STOP = "■"

# Symbole d'un bouton éteint, accordé au curseur désactivé du lecteur.
GLYPH_DISABLED = "#4a5c60"


class TransportButton(QPushButton):
    """Bouton de transport dont le symbole est dessiné, non écrit.

    Aucun caractère ne donne un résultat propre à cette taille : les
    pictogrammes dédiés (U+23F8, U+23EE…) sont rendus en couleur par la
    police emoji de Windows, et les formes de repli arrivent avec les
    proportions de la police — la pause en particulier, deux pavés massifs au
    milieu du rond cyan. Le symbole est donc tracé au QPainter, dans un carré
    proportionnel au bouton, et suit son état : cyan au repos, sombre au
    survol (le fond, lui, devient cyan), gris s'il est éteint.
    """

    # Côté du symbole, en fraction du côté du bouton. Une forme large et
    # ajourée (les doubles triangles) demande plus de place qu'un carré plein
    # pour peser autant à l'œil.
    RATIOS = {"play": 0.40, "pause": 0.38, "stop": 0.36,
              "prev": 0.52, "next": 0.52}

    def __init__(self, kind, size, tooltip, parent=None):
        super().__init__(parent)
        self._kind = kind
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(tooltip)
        self.setStyleSheet(_TRANSPORT_STYLE % {"radius": size // 2})

    def set_kind(self, kind):
        """Change le symbole (lecture <-> pause) sans refaire le bouton."""
        if kind != self._kind:
            self._kind = kind
            self.update()

    def paintEvent(self, event):
        # Le fond (rond, survol, état éteint) reste décrit par la feuille de
        # style ; seul le symbole est de notre ressort.
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if not self.isEnabled():
            painter.setBrush(QColor(GLYPH_DISABLED))
        elif self.underMouse():
            painter.setBrush(QColor(ON_PRIMARY))
        else:
            painter.setBrush(QColor(PRIMARY))

        box = min(self.width(), self.height()) * self.RATIOS[self._kind]
        left = (self.width() - box) / 2.0
        top = (self.height() - box) / 2.0

        def point(ux, uy):
            """Point du carré unité ramené dans le bouton."""
            return QPointF(left + ux * box, top + uy * box)

        def triangle(x0, x1, pointing_right):
            path = QPainterPath()
            if pointing_right:
                path.moveTo(point(x0, 0.0))
                path.lineTo(point(x0, 1.0))
                path.lineTo(point(x1, 0.5))
            else:
                path.moveTo(point(x1, 0.0))
                path.lineTo(point(x1, 1.0))
                path.lineTo(point(x0, 0.5))
            path.closeSubpath()
            painter.drawPath(path)

        if self._kind == "play":
            triangle(0.08, 0.96, True)
        elif self._kind == "pause":
            radius = box * 0.10
            for x in (0.10, 0.62):
                painter.drawRoundedRect(
                    QRectF(point(x, 0.0), point(x + 0.28, 1.0)),
                    radius, radius)
        elif self._kind == "stop":
            radius = box * 0.16
            painter.drawRoundedRect(QRectF(point(0.0, 0.0), point(1.0, 1.0)),
                                    radius, radius)
        elif self._kind == "prev":
            triangle(0.00, 0.46, False)
            triangle(0.52, 0.98, False)
        else:                                       # next
            triangle(0.02, 0.48, True)
            triangle(0.54, 1.00, True)
        painter.end()


def embedded_cover(path):
    """Pochette intégrée à un fichier audio local (octets), ou None."""
    if not config.MUTAGEN_AVAILABLE or not path or not os.path.exists(path):
        return None
    try:
        extension = os.path.splitext(path)[1].lower()
        if extension == ".mp3":
            tags = config.ID3(path)
            for key in tags.keys():
                if key.startswith("APIC"):
                    return tags[key].data
        elif extension == ".flac":
            flac = config.FLAC(path)
            if flac.pictures:
                return flac.pictures[0].data
    except Exception:
        return None
    return None


def format_clock(milliseconds):
    """Durée en mm:ss (hh:mm:ss au-delà de l'heure), ou --:-- si inconnue."""
    if milliseconds is None or milliseconds < 0:
        return "--:--"
    total = int(milliseconds // 1000)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%02d:%02d" % (minutes, seconds)


def make_track(path=None, title="", artist="", album="", cover=None,
               subtitle="", url=None):
    """Fabrique l'entrée attendue par le lecteur (fichier local ou flux)."""
    return {
        "path": path or "",
        "url": url or "",
        "title": title or (os.path.basename(path) if path else ""),
        "artist": artist or "",
        "album": album or "",
        "cover": cover,
        "subtitle": subtitle or "",
    }


class PlayerDialog(QDialog):
    """Fenêtre de lecture : égaliseur, position, durée, volume, liste."""

    # Rafraîchissement de l'affichage (position, égaliseur).
    TICK_MS = 40
    # Déplacement au clavier (flèches gauche/droite).
    STEP_MS = 5000

    def __init__(self, tracks, index=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lecteur DeeKeep")
        self.setStyleSheet(STYLE_SHEET)
        self.setMinimumWidth(560)
        self.setModal(False)
        # Fenêtre à part entière : on veut pouvoir continuer à parcourir la
        # bibliothèque pendant l'écoute.
        self.setWindowFlag(Qt.WindowType.Window, True)

        self._tracks = list(tracks or [])
        self._index = 0
        self._frames = []           # niveaux par trame d'analyse
        self._spectrum = None       # thread d'analyse du titre courant
        self._dragging = False
        self._duration = 0
        # Vrai quand la pause vient d'un extrait de 30 s (voir
        # ``pause_for_preview``) : la reprise a alors un message à effacer.
        self._preview_notice = False

        self._audio = QAudioOutput()
        self._audio.setVolume(_volume_gain(_LAST_VOLUME))
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio)
        self._player.durationChanged.connect(self._on_duration)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.errorOccurred.connect(self._on_error)

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self.load(index)

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)

        # --- En-tête : jaquette et identité du titre ---
        header = QHBoxLayout()
        header.setSpacing(14)

        self.lbl_cover = QLabel()
        self.lbl_cover.setFixedSize(76, 76)
        self.lbl_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_cover.setStyleSheet(
            "background-color: #1c1e26; border-radius: 8px; "
            "color: #849396; font-size: 26px;"
        )
        header.addWidget(self.lbl_cover)

        identity = QVBoxLayout()
        identity.setSpacing(3)
        self.lbl_title = QLabel()
        self.lbl_title.setStyleSheet(
            "font-family: 'Montserrat', 'Segoe UI'; font-size: 17px; "
            "font-weight: bold; color: #00e5ff;"
        )
        self.lbl_title.setWordWrap(False)
        self.lbl_artist = QLabel()
        self.lbl_artist.setStyleSheet("font-size: 13px; color: #bac9cc;")
        self.lbl_extra = QLabel()
        self.lbl_extra.setStyleSheet("font-size: 11px; color: #5f7d84;")
        identity.addWidget(self.lbl_title)
        identity.addWidget(self.lbl_artist)
        identity.addWidget(self.lbl_extra)
        identity.addStretch()
        header.addLayout(identity, 1)

        self.lbl_queue = QLabel()
        self.lbl_queue.setStyleSheet("font-size: 11px; color: #5f7d84;")
        self.lbl_queue.setAlignment(Qt.AlignmentFlag.AlignTop
                                    | Qt.AlignmentFlag.AlignRight)
        header.addWidget(self.lbl_queue)
        layout.addLayout(header)

        # --- Égaliseur ---
        self.spectrum = SpectrumView()
        layout.addWidget(self.spectrum, 1)

        # --- Position dans le titre ---
        seek = QHBoxLayout()
        seek.setSpacing(10)
        self.lbl_elapsed = QLabel("00:00")
        self.lbl_total = QLabel("--:--")
        for label in (self.lbl_elapsed, self.lbl_total):
            label.setStyleSheet(
                "font-family: 'Consolas', 'Courier New', monospace; "
                "font-size: 12px; color: #bac9cc;"
            )
            label.setFixedWidth(52)
        self.lbl_elapsed.setAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)

        self.slider = SeekSlider(Qt.Orientation.Horizontal)
        self.slider.setStyleSheet(_SLIDER_STYLE)
        self.slider.setRange(0, 0)
        self.slider.setPageStep(5000)
        self.slider.sliderPressed.connect(self._on_seek_pressed)
        self.slider.sliderMoved.connect(self._on_seek_moved)
        self.slider.sliderReleased.connect(self._on_seek_released)

        seek.addWidget(self.lbl_elapsed)
        seek.addWidget(self.slider, 1)
        seek.addWidget(self.lbl_total)
        layout.addLayout(seek)

        # --- Transport et volume ---
        controls = QHBoxLayout()
        controls.setSpacing(10)

        self.btn_prev = TransportButton("prev", 34, "Titre précédent")
        self.btn_prev.clicked.connect(self.previous_track)
        self.btn_play = TransportButton("play", 46, "Lecture / pause (Espace)")
        self.btn_play.clicked.connect(self.toggle)
        self.btn_stop = TransportButton("stop", 34, "Arrêter")
        self.btn_stop.clicked.connect(self.stop)
        self.btn_next = TransportButton("next", 34, "Titre suivant")
        self.btn_next.clicked.connect(self.next_track)

        controls.addWidget(self.btn_prev)
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_stop)
        controls.addWidget(self.btn_next)
        controls.addSpacing(18)

        self.lbl_volume = QLabel("🔊")
        self.lbl_volume.setStyleSheet("font-size: 14px; color: #bac9cc;")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setStyleSheet(_SLIDER_STYLE)
        self.volume.setRange(0, 100)
        self.volume.setValue(_LAST_VOLUME)
        self.volume.setFixedWidth(110)
        self.volume.setToolTip("Volume")
        self.volume.valueChanged.connect(self._on_volume)
        controls.addWidget(self.lbl_volume)
        controls.addWidget(self.volume)
        controls.addStretch()

        self.btn_folder = QPushButton("Ouvrir le dossier")
        self.btn_folder.setProperty("class", "control-btn")
        self.btn_folder.clicked.connect(self._open_folder)
        controls.addWidget(self.btn_folder)
        layout.addLayout(controls)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("font-size: 11px; color: #5f7d84;")
        layout.addWidget(self.lbl_status)

    # ------------------------------------------------------------------
    # Chargement d'un titre
    # ------------------------------------------------------------------
    def set_playlist(self, tracks, index=0):
        """Remplace la liste en cours (réutilisation de la fenêtre ouverte)."""
        self._tracks = list(tracks or [])
        self.load(index)

    def load(self, index):
        """Charge et lance le titre de rang ``index``."""
        if not self._tracks:
            return
        self._index = max(0, min(int(index), len(self._tracks) - 1))
        track = self._tracks[self._index]

        # La fermeture de la fenêtre arrête le rafraîchissement : la même
        # fenêtre étant réutilisée à la lecture suivante (voir
        # ``play_tracks``), il faut le relancer ici, sinon ni l'égaliseur ni
        # la position ne bougeraient plus.
        self._timer.start()

        self._stop_spectrum()
        self._player.stop()
        self._frames = []
        self.spectrum.reset()
        self._duration = 0
        self.slider.setRange(0, 0)
        self.slider.setValue(0)
        self.lbl_elapsed.setText("00:00")
        self.lbl_total.setText("--:--")
        self.lbl_status.setText("")

        self._refresh_header(track)
        self._refresh_queue()

        path = track.get("path") or ""
        stream = track.get("url") or ""
        if path:
            if not os.path.exists(path):
                self._fail("Fichier introuvable : %s" % path)
                return
            self._player.setSource(QUrl.fromLocalFile(path))
            source = path
        elif stream:
            self._player.setSource(QUrl(stream))
            source = stream
        else:
            self._fail("Aucun fichier associé à ce titre.")
            return

        self.btn_folder.setEnabled(bool(path))
        self._start_spectrum(source)
        self._player.play()

    def _fail(self, message):
        """Titre illisible : on le dit, sans fermer la fenêtre."""
        self.lbl_status.setText(message)
        self.btn_play.setEnabled(False)
        self.slider.setEnabled(False)
        self.btn_folder.setEnabled(False)

    def _refresh_header(self, track):
        title = track.get("title") or "Sans titre"
        self.lbl_title.setText(title)
        self.lbl_title.setToolTip(title)
        parts = [part for part in (track.get("artist"), track.get("album"))
                 if part]
        self.lbl_artist.setText(" — ".join(parts) if parts else "")
        self.lbl_extra.setText(track.get("subtitle")
                               or track.get("path") or track.get("url") or "")
        self.lbl_extra.setToolTip(track.get("path") or "")
        self.btn_play.setEnabled(True)
        self.slider.setEnabled(True)

        data = track.get("cover") or embedded_cover(track.get("path") or "")
        pixmap = QPixmap()
        if data and pixmap.loadFromData(data):
            self.lbl_cover.setPixmap(pixmap.scaled(
                76, 76, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self.lbl_cover.setPixmap(QPixmap())
            self.lbl_cover.setText("🎵")

    def _refresh_queue(self):
        total = len(self._tracks)
        self.lbl_queue.setText("%d / %d" % (self._index + 1, total)
                               if total > 1 else "")
        self.btn_prev.setEnabled(self._index > 0)
        self.btn_next.setEnabled(self._index + 1 < total)

    # ------------------------------------------------------------------
    # Analyse spectrale du titre courant
    # ------------------------------------------------------------------
    def _start_spectrum(self, source):
        thread = SpectrumThread(source, self)
        thread.frames_ready.connect(self._on_frames)
        thread.failed.connect(self._on_spectrum_failed)
        self._spectrum = thread
        thread.start()

    def _stop_spectrum(self):
        thread, self._spectrum = self._spectrum, None
        if thread is None:
            return
        try:
            thread.frames_ready.disconnect(self._on_frames)
            thread.failed.disconnect(self._on_spectrum_failed)
        except TypeError:
            pass
        thread.stop()
        if thread.isRunning():
            thread.wait(3000)
        thread.deleteLater()

    def _on_frames(self, start, batch):
        # Un paquet posté par le thread du titre précédent peut arriver après
        # le changement de titre : il est écarté sur son émetteur, puis sur
        # son indice (les paquets d'un même thread arrivent dans l'ordre).
        if self.sender() is not self._spectrum or start != len(self._frames):
            return
        self._frames.extend(batch)

    def _on_spectrum_failed(self, message):
        self.lbl_status.setText(message)

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def toggle(self):
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            return
        # Reprise : c'est la fenêtre qui reprend la parole, l'extrait la rend.
        stop_preview()
        if self._preview_notice:
            self._preview_notice = False
            self.lbl_status.setText("")
        self._player.play()

    def stop(self):
        self._player.stop()
        self.spectrum.reset()

    def pause_for_preview(self):
        """Laisse la parole à un extrait de 30 s : la lecture se met en pause.

        Les deux lecteurs cohabitent, mais pas dans les haut-parleurs : un
        extrait lancé depuis un tableau mettrait sa voix sur celle du titre
        en cours. La fenêtre reste ouverte, à sa position — un clic sur
        « lecture » reprend le titre là où il en était.
        """
        if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return
        self._player.pause()
        self._preview_notice = True
        self.lbl_status.setText(
            "En pause : un extrait de 30 secondes est en cours d'écoute.")

    def next_track(self):
        if self._index + 1 < len(self._tracks):
            self.load(self._index + 1)

    def previous_track(self):
        if self._index > 0:
            self.load(self._index - 1)

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.btn_play.set_kind("pause" if playing else "play")

    def _on_duration(self, duration):
        self._duration = max(0, int(duration))
        self.slider.setRange(0, self._duration)
        self.lbl_total.setText(format_clock(self._duration)
                               if self._duration else "--:--")

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.spectrum.reset()
            if self._index + 1 < len(self._tracks):
                self.load(self._index + 1)

    def _on_error(self, error, message=""):
        if error == QMediaPlayer.Error.NoError:
            return
        self.lbl_status.setText("Lecture impossible : %s"
                                % (message or "format non pris en charge"))

    def _on_volume(self, value):
        global _LAST_VOLUME
        _LAST_VOLUME = int(value)
        self._audio.setVolume(_volume_gain(value))
        self.lbl_volume.setText("🔇" if value == 0 else "🔊")

    def _open_folder(self):
        path = (self._tracks[self._index].get("path")
                if self._tracks else "")
        if path:
            reveal_in_explorer(path)

    # ------------------------------------------------------------------
    # Curseur de position
    # ------------------------------------------------------------------
    def _on_seek_pressed(self):
        self._dragging = True

    def _on_seek_moved(self, value):
        self.lbl_elapsed.setText(format_clock(value))

    def _on_seek_released(self):
        self._dragging = False
        self._seek(self.slider.value())

    def _seek(self, position):
        position = max(0, min(int(position), self._duration or int(position)))
        self._player.setPosition(position)
        # L'égaliseur repart de l'instant visé : les barres en cours n'ont
        # plus rien à voir avec ce qu'on entendra.
        self.spectrum.reset()

    # ------------------------------------------------------------------
    # Rafraîchissement périodique
    # ------------------------------------------------------------------
    def _tick(self):
        position = self._player.position()
        if not self._dragging:
            self.lbl_elapsed.setText(format_clock(position))
            if self._duration:
                self.slider.setValue(int(position))

        playing = (self._player.playbackState()
                   == QMediaPlayer.PlaybackState.PlayingState)
        frame = int(position * FPS / 1000)
        thread = self._spectrum
        if thread is not None:
            thread.set_played_frame(frame)
        if playing and 0 <= frame < len(self._frames):
            self.spectrum.push(self._frames[frame])
        else:
            # En pause, arrêté, ou analyse pas encore parvenue jusque-là :
            # les barres retombent au lieu de rester figées.
            self.spectrum.push(None)

    # ------------------------------------------------------------------
    # Clavier et fermeture
    # ------------------------------------------------------------------
    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle()
        elif key == Qt.Key.Key_Right:
            self._seek(self._player.position() + self.STEP_MS)
        elif key == Qt.Key.Key_Left:
            self._seek(self._player.position() - self.STEP_MS)
        elif key == Qt.Key.Key_Up:
            self.volume.setValue(min(100, self.volume.value() + 5))
        elif key == Qt.Key.Key_Down:
            self.volume.setValue(max(0, self.volume.value() - 5))
        elif key in (Qt.Key.Key_MediaNext, Qt.Key.Key_N):
            self.next_track()
        elif key in (Qt.Key.Key_MediaPrevious, Qt.Key.Key_P):
            self.previous_track()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def closeEvent(self, event):
        self._timer.stop()
        self._player.stop()
        self._stop_spectrum()
        super().closeEvent(event)

    def done(self, result):
        self._timer.stop()
        self._player.stop()
        self._stop_spectrum()
        super().done(result)


# --- Point d'entrée du module -------------------------------------------
# Un seul lecteur à la fois : deux fenêtres joueraient deux titres en même
# temps, et la deuxième relancerait une analyse pendant la première.
_PLAYER = None
# Volume conservé d'une ouverture à l'autre (0-100).
_LAST_VOLUME = 85


def _volume_gain(value):
    """Volume perçu : QAudioOutput attend une amplitude linéaire."""
    return min(1.0, max(0.0, (value / 100.0) ** 2))


def available():
    """Vrai si Qt Multimedia est présent (sans lui, aucune lecture)."""
    return MULTIMEDIA_AVAILABLE


def unavailable_reason():
    return ("Lecture indisponible : le module Qt Multimedia "
            "(PyQt6-Multimedia) est absent de cette installation.")


def play_tracks(parent, tracks, index=0):
    """Ouvre le lecteur sur ``tracks``, à partir du rang ``index``.

    La fenêtre déjà ouverte est réutilisée : elle change de liste et de
    titre plutôt que de se dédoubler.
    """
    global _PLAYER
    if not tracks:
        return None
    if not MULTIMEDIA_AVAILABLE:
        QMessageBox.warning(parent, "Lecteur", unavailable_reason())
        return None

    # Le lecteur complet prend la main : un extrait encore en cours se
    # superposerait au titre demandé.
    stop_preview()

    window = parent.window() if parent is not None else None
    player = _PLAYER
    if player is not None:
        try:
            player.set_playlist(tracks, index)
        except RuntimeError:        # fenêtre détruite côté C++
            player = None
    if player is None:
        player = PlayerDialog(tracks, index, window)
        _PLAYER = player
    player.show()
    player.raise_()
    player.activateWindow()
    return player


def play_track(parent, track):
    """Ouvre le lecteur sur un titre seul."""
    return play_tracks(parent, [track], 0)


def close_player():
    """Ferme le lecteur s'il est ouvert (fermeture de l'application)."""
    global _PLAYER
    stop_preview()
    player, _PLAYER = _PLAYER, None
    if player is not None:
        try:
            player.close()
        except RuntimeError:
            pass


# --- Extrait de 30 secondes ---------------------------------------------
# La page Musique affiche des titres qui ne sont pas encore extraits : aucun
# fichier à jouer, mais Deezer donne de chaque titre un extrait de 30 s — de
# quoi vérifier qu'on a coché le bon avant de lancer l'extraction.
#
# L'extrait est joué par un QMediaPlayer nu : ni égaliseur (il faudrait
# décoder le flux distant en parallèle) ni file d'attente. Le lecteur
# complet, lui, reste réservé aux fichiers du disque.
_PREVIEW = None          # QMediaPlayer partagé des extraits
_PREVIEW_OUTPUT = None   # sortie audio : à garder référencée, Qt ne la possède pas
_PREVIEW_URL = ""        # extrait en cours, "" si rien ne joue
_PREVIEW_ON_STOP = None  # rappel de fin (l'appelant y remet son glyphe)


def preview_playing():
    """URL de l'extrait en cours de lecture, "" si rien ne joue."""
    return _PREVIEW_URL


def play_preview(url, on_stop=None):
    """Joue l'extrait ``url``, et retourne faux si la lecture est impossible.

    Un seul extrait à la fois : le précédent est arrêté et son ``on_stop``
    appelé. ``on_stop`` l'est aussi à la fin naturelle de l'extrait et sur
    erreur de lecture (extrait périmé, réseau coupé) — c'est là que
    l'appelant remet son bouton au repos.
    """
    global _PREVIEW, _PREVIEW_OUTPUT, _PREVIEW_URL, _PREVIEW_ON_STOP
    if not MULTIMEDIA_AVAILABLE or not url:
        return False
    # Un seul son à la fois : la fenêtre de lecture se met en pause le temps
    # de l'extrait, plutôt que de jouer par-dessus.
    pause_player()
    stop_preview()
    if _PREVIEW is None:
        _PREVIEW_OUTPUT = QAudioOutput()
        _PREVIEW_OUTPUT.setVolume(_volume_gain(_LAST_VOLUME))
        _PREVIEW = QMediaPlayer()
        _PREVIEW.setAudioOutput(_PREVIEW_OUTPUT)
        _PREVIEW.playbackStateChanged.connect(_on_preview_state)
        _PREVIEW.errorOccurred.connect(lambda *_: stop_preview())
    _PREVIEW_URL, _PREVIEW_ON_STOP = url, on_stop
    _PREVIEW.setSource(QUrl(url))
    _PREVIEW.play()
    return True


def pause_player():
    """Met la fenêtre de lecture en pause, si elle est ouverte et joue."""
    global _PLAYER
    if _PLAYER is None:
        return
    try:
        _PLAYER.pause_for_preview()
    except RuntimeError:        # fenêtre détruite côté C++
        _PLAYER = None


def stop_preview():
    """Arrête l'extrait en cours, et prévient celui qui l'avait lancé."""
    global _PREVIEW_URL, _PREVIEW_ON_STOP
    # L'état est remis à zéro avant l'arrêt : ``stop()`` émet
    # ``playbackStateChanged`` sur place, et le rappel ne doit pas partir
    # deux fois.
    on_stop, _PREVIEW_ON_STOP, _PREVIEW_URL = _PREVIEW_ON_STOP, None, ""
    if _PREVIEW is not None:
        _PREVIEW.stop()
    if on_stop is not None:
        on_stop()


def _on_preview_state(state):
    """Fin naturelle de l'extrait : mêmes suites qu'un arrêt demandé."""
    if state == QMediaPlayer.PlaybackState.StoppedState:
        stop_preview()


def set_item_playing(item, playing):
    """Bascule le glyphe d'une cellule « ÉCOUTE » entre lecture et arrêt.

    Retourne faux si la cellule a disparu entre-temps : pendant l'extrait, la
    ligne peut être supprimée ou le tableau reconstruit par un tri.
    """
    try:
        item.setText(GLYPH_STOP if playing else GLYPH_PLAY)
    except RuntimeError:        # item détruit côté C++
        return False
    return True


def play_item(tooltip="Écouter ce titre", enabled=True):
    """Cellule « ÉCOUTE » d'un tableau : le glyphe de lecture, posé en item.

    Un bouton par ligne coûterait un widget par titre — plusieurs milliers
    sur une bibliothèque entière —, et Qt ne redimensionne pas toujours les
    widgets de cellule posés sur des lignes déjà rendues : le bouton s'y
    retrouve rogné. L'item, lui, est dessiné par la vue : rien à créer, rien
    à repositionner.

    Le clic est capté par le tableau lui-même (signal ``cellClicked``) : voir
    les appelants dans ``quality_page``, ``main_window`` et ``organize_page``.
    """
    if not MULTIMEDIA_AVAILABLE:
        enabled = False
        tooltip = unavailable_reason()
    item = QTableWidgetItem(GLYPH_PLAY)
    item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
    # Ni sélectionnable ni cochable : la cellule n'est qu'un déclencheur.
    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
    item.setForeground(QColor(PRIMARY if enabled else TEXT_FAINT))
    # Le glyphe est agrandi : à la taille du texte des tableaux, un triangle
    # ne se remarque pas. La teinte de fond, elle, ne sert à rien — la
    # feuille de style de l'application peint déjà le fond des cellules.
    font = QFont(item.font())
    font.setPointSize(13)
    font.setBold(True)
    item.setFont(font)
    item.setToolTip(tooltip)
    return item
