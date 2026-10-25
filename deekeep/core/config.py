"""
Détection centralisée des dépendances optionnelles.

Ce module tente d'importer les bibliothèques externes utilisées par
l'application (yt-dlp, mutagen) et expose des drapeaux booléens
de disponibilité ainsi que les objets importés. Les autres modules
importent depuis ici plutôt que de refaire les try/except à chaque endroit.
"""

# --- yt-dlp : extraction gratuite via YouTube ---
try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    yt_dlp = None
    YTDLP_AVAILABLE = False

# --- mutagen : gestion des métadonnées (ID3, FLAC, APIC, etc.) ---
#
# Les trames complémentaires (TPE2, TCON, TDRC, TRCK, TPOS) sont importées
# au même titre que les trois premières : un fichier n'est complet que
# lorsqu'il porte aussi son artiste d'album, son genre, sa date et son rang
# dans l'album (voir ``deekeep.audio.tag_write``).
try:
    import mutagen
    from mutagen.mp3 import MP3
    from mutagen.id3 import (ID3, APIC, TIT2, TPE1, TALB, TPE2, TCON, TDRC,
                             TRCK, TPOS, TPUB)
    from mutagen.flac import FLAC, Picture
    MUTAGEN_AVAILABLE = True
except ImportError:
    mutagen = None
    MP3 = ID3 = APIC = TIT2 = TPE1 = TALB = None
    TPE2 = TCON = TDRC = TRCK = TPOS = TPUB = None
    FLAC = Picture = None
    MUTAGEN_AVAILABLE = False
