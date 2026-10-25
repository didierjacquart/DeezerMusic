"""
Motifs de nommage des fichiers audio.

Un motif est une chaîne libre dans laquelle des balises « %tag% » sont
remplacées par les métadonnées du fichier, à la manière de Mp3tag :

    %piste_00% - %titre%          ->  03 - Africa
    %artiste% - %album% - %titre% ->  Toto - Toto IV - Africa

``TAG_CATEGORIES`` décrit les balises proposées à l'utilisateur, groupées
par catégorie : c'est cette liste qui alimente le menu « Insérer une
balise » de l'écran Organiser. ``PRESETS`` propose les combinaisons les
plus courantes.

Une balise sans valeur donne une chaîne vide ; ``format_name`` recolle
ensuite le résultat proprement (séparateurs orphelins supprimés) afin
qu'un album sans numéro de piste ne produise pas « - Africa ».
"""

import os
import re


# ----------------------------------------------------------------------
# Accès aux valeurs
# ----------------------------------------------------------------------
def _pad(value, width):
    """« 3 » -> « 03 ». Laisse la valeur telle quelle si ce n'est pas un nombre."""
    text = (value or "").strip()
    if not text:
        return ""
    return text.zfill(width) if text.isdigit() else text


def _year(entry):
    """Année sur 4 chiffres extraite de la date de sortie."""
    match = re.search(r"\d{4}", str(entry.get("date") or ""))
    return match.group(0) if match else ""


def _decade(entry):
    """Décennie sur 4 chiffres (1994 -> 1990)."""
    year = _year(entry)
    return f"{year[:3]}0" if year else ""


def _stem(entry):
    """Nom de fichier actuel, sans son extension."""
    return os.path.splitext(os.path.basename(entry.get("path") or ""))[0]


def _parent(entry):
    """Nom du dossier qui contient actuellement le fichier."""
    return os.path.basename(os.path.dirname(entry.get("path") or ""))


def _tag(key):
    return lambda entry: str(entry.get(key) or "").strip()


# Balise -> fonction qui en calcule la valeur pour une entrée analysée.
#
# Les balises de numérotation sont nommées d'après ce qu'elles produisent :
# « %piste_00% » rend « 07 » (deux chiffres), « %total_pistes% » rend le
# nombre de pistes de l'album. Écrire « %piste2% » laissait croire à la
# piste numéro 2, et « %nb_pistes% » se confondait avec le numéro de piste.
TOKENS = {
    "%titre%": _tag("title"),
    "%artiste%": _tag("artist"),
    "%artiste_album%": _tag("albumartist"),
    "%artiste_album_ou_artiste%": lambda e: (
        str(e.get("albumartist") or "").strip() or str(e.get("artist") or "").strip()
    ),
    "%album%": _tag("album"),
    "%piste%": _tag("track"),
    "%piste_00%": lambda e: _pad(e.get("track"), 2),
    "%total_pistes%": _tag("tracktotal"),
    "%disque%": _tag("disc"),
    "%disque_00%": lambda e: _pad(e.get("disc"), 2),
    "%total_disques%": _tag("disctotal"),
    "%date%": _tag("date"),
    "%annee%": _year,
    "%decennie%": _decade,
    "%genre%": _tag("genre"),
    "%compositeur%": _tag("composer"),
    "%editeur%": _tag("publisher"),
    "%commentaire%": _tag("comment"),
    "%compilation%": _tag("compilation"),
    "%nom_fichier%": _stem,
    "%dossier_parent%": _parent,
    "%format%": _tag("format"),
    "%debit%": lambda e: str(e.get("bitrate") or "") if e.get("bitrate") else "",
}

# Anciens noms de balises, conservés pour que les motifs déjà enregistrés
# continuent de fonctionner. Ils ne sont plus proposés dans le menu et sont
# réécrits vers leur équivalent actuel à l'ouverture de l'écran.
ALIASES = {
    "%piste2%": "%piste_00%",
    "%disque2%": "%disque_00%",
    "%nb_pistes%": "%total_pistes%",
    "%nb_disques%": "%total_disques%",
}

# Menu « Insérer une balise » : (catégorie, [(libellé, balise), ...]).
# Chaque libellé de balise numérique montre la valeur qu'elle produit, la
# seule façon de lever l'ambiguïté d'un coup d'œil.
TAG_CATEGORIES = [
    ("Tags de base", [
        ("Titre", "%titre%"),
        ("Artiste", "%artiste%"),
        ("Artiste de l'album", "%artiste_album%"),
        ("Artiste de l'album ou artiste", "%artiste_album_ou_artiste%"),
        ("Album", "%album%"),
        ("Genre", "%genre%"),
    ]),
    ("Numéros de piste et de disque", [
        ("Numéro de la piste   →  7", "%piste%"),
        ("Numéro de la piste sur 2 chiffres   →  07", "%piste_00%"),
        ("Nombre total de pistes de l'album   →  12", "%total_pistes%"),
        ("Numéro du disque   →  1", "%disque%"),
        ("Numéro du disque sur 2 chiffres   →  01", "%disque_00%"),
        ("Nombre total de disques   →  2", "%total_disques%"),
    ]),
    ("Date de sortie", [
        ("Date complète   →  1982-04-08", "%date%"),
        ("Année seule   →  1982", "%annee%"),
        ("Décennie   →  1980", "%decennie%"),
    ]),
    ("Tags supplémentaires", [
        ("Compositeur", "%compositeur%"),
        ("Éditeur / label", "%editeur%"),
        ("Commentaire", "%commentaire%"),
        ("Partie d'une compilation", "%compilation%"),
    ]),
    ("Fichier", [
        ("Nom de fichier actuel", "%nom_fichier%"),
        ("Dossier parent", "%dossier_parent%"),
        ("Format   →  MP3", "%format%"),
        ("Débit   →  320", "%debit%"),
    ]),
]

# Motifs proposés dans la liste déroulante de l'écran Organiser.
#
# Ce sont des combinaisons des balises descriptives (titre, artiste, album,
# année) : ce que l'on veut lire dans un nom de fichier. La numérotation de
# piste reste disponible dans le menu « Insérer une balise » pour qui range
# des albums complets, mais elle n'encombre pas la liste.
PRESETS = [
    "%titre% - %artiste%",
    "%artiste% - %titre%",
    "%artiste% - %album% - %titre%",
    "%album% - %titre%",
    "%artiste% - %titre% (%annee%)",
    "%artiste% - %album% (%annee%) - %titre%",
    "%artiste_album_ou_artiste% - %titre%",
    "%titre% (%artiste%)",
    "%titre%",
    "%nom_fichier%",
]

# Convention historique de DeeKeep, partagée avec le téléchargement.
DEFAULT_PATTERN = "%titre% - %artiste%"

_TOKEN_RE = re.compile(r"%[a-z0-9_]+%")


def modernize(pattern):
    """Réécrit les anciens noms de balises d'un motif enregistré.

    Purement cosmétique — les alias restent interprétés — mais évite
    d'afficher « %piste2% » dans un champ dont le menu propose désormais
    « %piste_00% ».
    """
    for old, new in ALIASES.items():
        pattern = (pattern or "").replace(old, new)
    return pattern


def unknown_tokens(pattern):
    """Balises du motif qui ne correspondent à rien (pour prévenir l'utilisateur)."""
    return [
        tok for tok in _TOKEN_RE.findall(pattern or "")
        if tok not in TOKENS and tok not in ALIASES
    ]


# Balises dont la valeur, faute de tag, serait reprise du nom de fichier
# actuel : (balise, clé attestant du tag, nom du tag pour l'utilisateur).
_FILENAME_BACKED = (
    ("%titre%", "title_tagged", "titre"),
)


def missing_required_tags(pattern, entry):
    """Tags que le motif réclame, qu'aucune métadonnée ne renseigne, et que le
    nom de fichier actuel comblerait à tort.

    ``read_tags`` retient le nom du fichier à défaut de titre, afin que les
    écrans aient toujours quelque chose à afficher. Reprendre cette valeur
    dans un motif la réinjecterait dans le nom à chaque rangement :
    « Joan Baez.mp3 » deviendrait « Joan Baez - Joan Baez.mp3 », puis
    « Joan Baez - Joan Baez - Joan Baez.mp3 », et ainsi de suite.

    Mieux vaut alors ne pas renommer et le signaler : le nom serait bâti sur
    lui-même, non sur les tags. ``%nom_fichier%``, qui demande explicitement
    le nom actuel, n'est pas concerné — il le reproduit à l'identique.
    """
    tokens = {ALIASES.get(tok, tok) for tok in _TOKEN_RE.findall(pattern or "")}
    return [
        label for token, flag, label in _FILENAME_BACKED
        if token in tokens and not entry.get(flag)
    ]


def _tidy(name):
    """Recolle un nom dont certaines balises se sont révélées vides.

    Supprime les séparateurs devenus orphelins (« - - », « () », espaces
    multiples) puis les séparateurs de début et de fin.
    """
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", name)
    # Séparateurs consécutifs : « A -  - B » -> « A - B ».
    previous = None
    while previous != name:
        previous = name
        name = re.sub(r"\s*([-–_,])\s*\1*\s*(?=[-–_,])", " ", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip(" -–_,.")


def format_name(pattern, entry):
    """Nom de fichier (sans extension) produit par ``pattern`` pour ``entry``.

    Retourne une chaîne vide si le motif ne produit rien d'exploitable :
    l'appelant décide alors de signaler le fichier plutôt que de le
    renommer en « .mp3 ».
    """
    def replace(match):
        token = match.group(0)
        getter = TOKENS.get(ALIASES.get(token, token))
        return getter(entry) if getter else token

    return _tidy(_TOKEN_RE.sub(replace, pattern or ""))
