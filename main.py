"""
Point d'entrée de l'application DeeKeep.

Lancement (depuis le dossier contenant ce fichier) :
    python main.py
"""

import sys

from PyQt6.QtWidgets import QApplication

from deekeep.ui.main_window import DeeKeepApp
from deekeep.core.utils import create_app_icon, ensure_deno_on_path


def main():
    # Rendre le runtime JavaScript Deno embarqué détectable par yt-dlp
    # (exigé par YouTube pour l'extraction). Sans effet si Deno est absent.
    ensure_deno_on_path()

    # Identifiant applicatif Windows : sans lui, la barre des tâches regroupe
    # la fenêtre sous python.exe et affiche l'icône de Python au lieu de la
    # nôtre. Doit être défini avant la création de toute fenêtre.
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "DeeKeep.MusicDownloader"
        )

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(create_app_icon())
    # La fenêtre peut se cacher dans la zone de notification : ne pas
    # quitter quand la dernière fenêtre visible se ferme (c'est le
    # closeEvent de la fenêtre principale qui décide de quitter).
    app.setQuitOnLastWindowClosed(False)
    window = DeeKeepApp()
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
