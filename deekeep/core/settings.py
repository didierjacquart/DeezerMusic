"""
Persistance des paramètres et de l'historique de DeeKeep.

Les données sont enregistrées dans un fichier JSON placé dans le dossier
de données de l'utilisateur (indépendant du dossier d'installation) :
    Windows : %APPDATA%\\DeeKeep\\deekeep_config.json
    Autres  : ~/.config/DeeKeep/deekeep_config.json

Ce choix garantit que l'historique et les paramètres sont conservés
lorsque l'application est réinstallée ou mise à jour : l'installateur
ne touche que le dossier du programme, jamais le dossier de données.

Les anciennes versions enregistraient le fichier à côté du programme ;
au premier lancement, ce fichier est automatiquement migré vers le
nouvel emplacement (voir _migrate_legacy_config).

Sont conservés :
    - download_dir     : dossier d'exportation de la musique
    - search_history   : liste des recherches effectuées
    - downloaded_history : liste des fichiers téléchargés avec succès
    - org_*            : options de rangement de la bibliothèque
    - qual_*           : seuils de détection des défauts sonores
                         (écran « Qualité » ; valeurs par défaut dans
                         quality_scan.DEFAULT_THRESHOLDS)

Deux caches vivent à côté, dans le même dossier de données, et se
reconstruisent seuls s'ils viennent à manquer :
    - quality_cache.json   : mesures de l'écran « Qualité », par fichier
    - reference_cache.json : durées trouvées chez Deezer / MusicBrainz
"""

import os
import sys
import json
import shutil


CONFIG_FILENAME = "deekeep_config.json"
APP_DIR_NAME = "DeeKeep"


def data_dir():
    """Dossier de données utilisateur de l'application (créé si absent)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path():
    """Chemin complet du fichier de configuration (dossier de données)."""
    return os.path.join(data_dir(), CONFIG_FILENAME)


def _legacy_config_paths():
    """
    Emplacements utilisés par les anciennes versions (fichier à côté du
    programme). Deux cas possibles :
      - exécution depuis les sources : racine du projet (celle de main.py,
        deux dossiers au-dessus de ce module)
      - application PyInstaller      : dossier _internal et dossier de l'exe
    """
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    paths = [os.path.join(project_root, CONFIG_FILENAME)]
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        paths.append(os.path.join(exe_dir, CONFIG_FILENAME))
    return paths


def _migrate_legacy_config():
    """
    Copie l'ancien fichier de configuration vers le nouvel emplacement
    s'il existe et que le nouveau n'existe pas encore. L'ancien fichier
    est laissé en place (l'installateur le supprimera avec le programme).
    """
    new_path = config_path()
    if os.path.exists(new_path):
        return

    for old_path in _legacy_config_paths():
        if os.path.exists(old_path):
            try:
                shutil.copy2(old_path, new_path)
                print(f"[settings] Configuration migrée : {old_path} -> {new_path}")
            except OSError as exc:
                print(f"[settings] Migration impossible depuis {old_path} : {exc}")
            return


def default_settings():
    """Valeurs par défaut utilisées à la première exécution."""
    return {
        "download_dir": os.path.join(os.path.expanduser("~"), "Music"),
        "file_format": "MP3",
        "quality": "320 kbps (HQ)",
        "org_artist": True,
        "org_album": True,
        # --- Rangement de la bibliothèque (écran « Organiser ») ---
        # Les artistes comptant moins de "org_divers_min" titres sont
        # rassemblés dans un dossier « Divers » plutôt que de recevoir
        # chacun un dossier d'un ou deux fichiers.
        "org_divers": False,
        "org_divers_min": 3,
        # Renommage : motif de nommage (balises %titre%, %artiste%, …)
        "org_rename": False,
        "org_pattern": "%titre% - %artiste%",
        "org_remove_empty": True,
        "org_report_untagged": True,
        # Ne pas défaire les dossiers de compilation / bande originale
        # (plusieurs artistes réunis autour d'un même album).
        "org_keep_albums": True,
        # Plusieurs copies d'un même titre : n'en conserver qu'une (la mieux
        # encodée) et supprimer les autres du disque.
        "org_dedup": True,
        # Volet des filtres déplié à l'ouverture de l'écran « Organiser ».
        "org_filters_open": False,
        # --- Miroir Opus de la bibliothèque (écran « Bibliothèque ») ---
        # Débit du réencodage envoyé au seau de stockage objet. 128 kb/s est
        # le point où Opus cesse d'être distinguable de la source ; les clés
        # du seau, elles, vivent dans « bucket.json » (voir blob_store).
        "mirror_bitrate": 128,
        # Clé d'application AcoustID, gratuite et nominative
        # (https://acoustid.org/new-application) : sans elle, la recherche
        # de tags par empreinte acoustique est indisponible, le service
        # refusant les appels anonymes.
        "acoustid_key": "",
        # Navigateur dont les cookies YouTube sont réutilisés par yt-dlp
        # ("Aucun (anonyme)" = pas de compte, extraction anonyme).
        # NB : sous Windows, Chrome/Edge chiffrent leurs cookies de façon
        # inaccessible aux programmes externes (app-bound) ; seul Firefox
        # fonctionne par cette méthode. Pour Chrome, utiliser cookies_file.
        "youtube_browser": "Aucun (anonyme)",
        # Fichier cookies.txt (format Netscape) exporté depuis le navigateur ;
        # prioritaire sur youtube_browser. "" = non utilisé.
        "cookies_file": "",
        "search_history": [],
        "downloaded_history": [],
    }


def load_settings():
    """
    Charge la configuration depuis le disque.

    Migre d'abord un éventuel fichier d'une ancienne version, puis
    fusionne le contenu lu avec les valeurs par défaut afin qu'une clé
    manquante (ancienne version du fichier) n'entraîne pas d'erreur.
    Retourne toujours un dictionnaire exploitable.
    """
    _migrate_legacy_config()

    settings = default_settings()
    path = config_path()

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                settings.update(data)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[settings] Impossible de lire {path} : {exc}")

    return settings


def save_settings(settings):
    """
    Écrit la configuration sur le disque (écriture atomique).

    Écrit d'abord dans un fichier temporaire puis le renomme afin d'éviter
    un fichier corrompu si l'application est interrompue en cours d'écriture.
    """
    path = config_path()
    tmp_path = path + ".tmp"

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except OSError as exc:
        print(f"[settings] Impossible d'écrire {path} : {exc}")
