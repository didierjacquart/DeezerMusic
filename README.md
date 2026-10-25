# DeeKeep — Music Downloader

Application PyQt6 de téléchargement de musique (YouTube via yt-dlp). La recherche des pistes utilise l'API publique et gratuite de Deezer.

## Arborescence

Le point d'entrée et les ressources livrées avec l'application (`icon.ico`,
`ffmpeg/`, `deno/`) restent à la racine ; le code est réparti en paquets
`deekeep.*` selon ce dont il s'occupe :

```
main.py                     Point d'entrée
deekeep/
  core/                     Socle applicatif
    config.py               Détection des dépendances optionnelles
    settings.py             Réglages persistés (dossier de données utilisateur)
    utils.py                Fonctions utilitaires + icône
  db/                       Persistance et synchronisation
    db_schema.py            Schéma commun au cache local et à la base en ligne
    local_db.py             Cache local SQLite (`deekeep_cache.db`)
    cloud_db.py             Client HTTP de la base en ligne (Turso / libSQL)
    blob_store.py           Seau de stockage objet compatible S3 (R2, B2), signature SigV4
    sync_service.py         Rapprochement des deux bases, détection des conflits
    db_init.py              Collecte, initialisation et outil en ligne de commande
  library/                  Bibliothèque de fichiers
    library_rules.py        Règles de rangement (dossiers, nommage, « Divers »)
    library_index.py        Index du dossier de dépôt : « ce titre y est-il déjà ? »
    name_pattern.py         Balises et motifs de nommage (%titre%, %artiste%…)
  audio/                    Signal, identification, qualité
    audio_probe.py          Mesures faites sur le signal via ffmpeg (durée utile, silences)
    quality_scan.py         Détection des défauts sonores (souffle, craquements…)
    reencode_scan.py        Analyse locale des fichiers audio (QThread + pool de lecture)
    acoustid_lookup.py      Empreinte acoustique (AcoustID / Chromaprint)
    tag_lookup.py           Complétion des tags depuis les bases en ligne
    tag_write.py            Écriture des tags dans le fichier (ID3, Vorbis, MP4)
    reference_duration.py   Durée d'un titre d'après Deezer puis MusicBrainz (cache disque)
    opus_mirror.py          Miroir Opus de la bibliothèque : réencodage et envoi au seau
  workers/                  Fils de fond
    search_thread.py        Recherche via l'API publique Deezer (QThread)
    download_thread.py      Téléchargement YouTube + méta-tagging (QThread)
    image_thread.py         Recherche et téléchargement des jaquettes (QThread)
  ui/                       Interface PyQt
    main_window.py          Fenêtre principale (8 pages)
    library_page.py         Page « Bibliothèque » : lister, chercher, écouter, miroir
    organize_page.py        Page « Organiser » : rangement de la bibliothèque
    quality_page.py         Page « Qualité » : titres abîmés et retéléchargement
    player.py               Lecteur audio commun : égaliseur, position, file d'attente
    add_track_dialog.py     Recherche et sélection de pistes
    dedup_dialog.py         Recherche de doublons, écran de déduplication
                            (fichiers entre eux) et doublons de la recherche
    tag_lookup_dialog.py    Arbitrage des tags proposés
    sync_ui.py              Carte « Base de données », arbitrage, fils de fond
    styles.py               Feuille de style QSS
```

Les imports sont absolus depuis la racine du projet
(`from deekeep.library.library_rules import …`) : les modules se lisent sans
avoir à savoir où se trouve celui qui les importe.

## Ne pas retélécharger ce qu'on a déjà

La recherche affichait ce que Deezer répond, sans jamais regarder le disque :
rien n'empêchait de remettre en file un titre déjà rangé. Chaque piste porte
désormais une **pastille**, posée juste avant sa case de sélection, qui dit ce
que le dossier de dépôt contient :

| Pastille | État | Ce qui a été reconnu | Case de sélection |
|---|---|---|---|
| **✓ vert** | déjà présent | même titre, même artiste — ou un fichier portant exactement le nom qu'une extraction lui donnerait | **désactivée** |
| **≈ orange** | doublon possible | même titre et même artiste principal (l'invité diffère), autre version du morceau (remix, live, radio edit), même titre dans le même album, ou titre approchant chez un artiste compatible | libre, mais décochée |
| **○ gris** | absent | rien de tel sur le disque | libre |
| **⏳** | en cours | le dossier n'a pas fini d'être lu | libre |

Un titre vert n'est pas *déconseillé*, il est **impossible à sélectionner** :
il n'y a rien à extraire. Un titre orange, si : la ressemblance n'est pas une
preuve, et seul l'utilisateur sait si le remix qu'il a déjà remplace la version
d'album qu'il cherche. Les deux sont décochés, et « Tout sélectionner » les
laisse de côté — cocher les cinquante titres d'un album ne remet pas en file
ce qui est déjà rangé. Le compte s'affiche à droite du filtre, et l'infobulle
de chaque pastille nomme les fichiers en cause.

Le tableau de la page **Musique** porte la même pastille, mais à titre
d'information seulement : sa case à cocher sert à *retirer* des lignes, pas à
choisir ce qui s'extrait — la désactiver priverait l'utilisateur du moyen
d'écarter un doublon.

### Voir les doublons, et les défaire

Le bilan affiché à droite du filtre ne se contente plus de compter : deux
boutons le prolongent.

- **Voir les doublons (n)** est une bascule qui ne laisse afficher que les
  lignes vertes et orange. Elle se cumule au filtre textuel, elle ne le
  remplace pas — « Voir les doublons » puis « live » ne montre que les
  doublons dont le titre contient « live ». Le filtre se relâche de lui-même
  quand il ne reste rien à montrer, plutôt que de laisser un tableau vide sans
  raison visible.
- **Dédoublonner…** ouvre l'écran des doublons
  (`dedup_dialog.LibraryDuplicateDialog`) : sous chaque piste de la recherche
  viennent les fichiers du disque que l'index lui a rapprochés, avec le motif
  du rapprochement, la qualité (format, débit, taille) et l'emplacement. Les
  fichiers cochés sont supprimés, après confirmation — la piste redevient alors
  extractible sans quitter la recherche.

Rien n'est coché d'avance quand un seul fichier répond d'une piste : sa
suppression est un choix, pas une évidence, et le rapprochement peut désigner
une version que l'on garde volontiers. Quand **plusieurs** fichiers répondent
de la même piste, ils sont en double entre eux : tous sont cochés sauf le
meilleur exemplaire — meilleur débit, puis fichier le plus gros (à débit égal,
la différence tient à la pochette ou aux tags), puis le moins enfoui. Un
fichier réclamé par deux pistes voisines n'est montré que sous la première :
deux cases ne décident pas du sort d'un seul fichier.

La suppression ne relit pas le dossier de dépôt : l'index oublie les chemins
un à un (`LibraryIndex.remove`), et sa rediffusion remet à jour les pastilles
de la recherche **et** celles de la page Musique. La pastille repasse au gris,
la case se libère, et la même fenêtre sert à réextraire ce qu'on vient
d'effacer.

Cet écran est le pendant de la **recherche de doublons** de l'écran
« Organiser » (plus bas) : celui-là rapproche deux fichiers du disque entre
eux, celui-ci rapproche une piste de Deezer aux fichiers qu'elle a rencontrés.

### Ce qui est comparé, et à quel prix

L'index est construit dans un fil séparé (`library_index.py`) : le parcours
d'une bibliothèque de trois mille titres demande une centaine de
millisecondes, et chaque titre affiché est confronté à l'index en quelques
dizaines de microsecondes. Ni l'ouverture de la fenêtre ni celle de la
recherche ne l'attendent ; tant qu'il n'est pas prêt, les pastilles affichent
« ⏳ » et **rien n'est décoché**.

Aucun tag n'est relu au passage — lire les tags de milliers de fichiers
demande des dizaines de secondes, ce qui est le travail de l'écran
« Organiser », pas d'une recherche. Trois sources y suppléent :

- l'**historique de téléchargement**, qui porte le titre, l'artiste et l'album
  exacts — ceux de Deezer — de tout ce que DeeKeep a extrait ;
- le **cache local** (table `tracks`), pour les fichiers déjà relevés par la
  collecte de la base de données ;
- le **chemin du fichier** pour tous les autres. DeeKeep nomme lui-même ce
  qu'il extrait (« Titre - Artiste ») et le range par artiste : le chemin
  porte l'essentiel. Quand le nom du dossier ne dit pas de quel côté du tiret
  se trouve l'artiste, les deux lectures sont indexées — mieux vaut un doublon
  signalé en orange qu'un titre extrait deux fois.

Sur les 573 titres de l'historique d'une bibliothèque réelle, la
reconnaissance par nom de fichier seul retrouve les **573**. À l'inverse, ces
mêmes titres attribués à un artiste étranger — une reprise, donc — ne sont
signalés dans aucun cas : la comparaison porte toujours sur le couple
titre/artiste, jamais sur le titre seul (sauf album identique, où deux
écritures d'un même artiste sont en cause).

Le dossier est relu à chaque ouverture de la recherche — il a pu bouger depuis
(rangement, retéléchargement, ajout hors de DeeKeep) — et un titre fraîchement
extrait entre dans l'index sans relecture : les lignes encore en file le savent
aussitôt.

## Organiser la bibliothèque

La page **Outils → Organiser** range les fichiers déjà présents sur le disque
selon leurs tags, avec la même convention que les téléchargements :

- l'analyse tourne dans un thread dédié et remplit une **arborescence**
  (dossiers, sous-dossiers, fichiers) au fil de l'eau ;
- chaque ligne — ou chaque dossier — se coche indépendamment, rien n'est
  déplacé avant validation. **Tout fichier analysé porte une case**, même
  celui qui est déjà en place : elle ne commande pas que le déplacement, elle
  désigne la ligne, et les deux recherches de tags s'y tiennent aussi.
  Décocher un dossier suffit à l'écarter de tout ce que l'écran entreprend.
  Un changement d'option reconstruit le plan **sans toucher aux cases** : la
  sélection en cours est celle de l'utilisateur, pas celle du plan ;
- le **renommage** suit un motif composé de balises (`%titre%`, `%artiste%`,
  `%piste_00%`, `%annee%`…) choisies dans le menu « Insérer une balise » ;
  le suffixe `_00` force deux chiffres (`%piste_00%` → `07`) et `%total_pistes%`
  donne le nombre de pistes de l'album, à ne pas confondre avec `%piste%` ;
- les artistes comptant moins de *N* titres peuvent être regroupés dans un
  dossier **« Divers »** (option des Paramètres) au lieu de recevoir chacun
  un dossier d'un ou deux fichiers.

La règle « Divers » joue **dans les deux sens** : un titre qui s'y trouve
déjà en ressort dès que son artiste atteint le seuil. Le décompte additionne
les titres du lot analysé et ceux que contient déjà le dossier de l'artiste
sur le disque — deux titres égarés dans « Divers » rejoignent donc le dossier
existant même si l'analyse ne porte que sur « Divers ».

### Ces règles valent pour toutes les extractions

Sous-dossiers, motif de nommage et regroupement « Divers » ne décrivent pas
seulement le rangement des fichiers déjà présents : **tout titre extrait les
suit** (`library_rules.py`), qu'il vienne de la page Musique ou du
retéléchargement de la page Qualité. Un titre fraîchement téléchargé se pose
donc là où l'analyse l'aurait mis, et non ailleurs pour y être déplacé au
passage suivant. Une nuance : « Renommer les fichiers » décide du sort des
fichiers *déjà nommés* ; un fichier qui n'existe pas encore doit bien recevoir
un nom, et sans motif demandé c'est la convention historique
« Titre - Artiste » qui s'applique.

Le décompte de la règle « Divers », lui, voit la même chose des deux côtés.
Une extraction ne relit pas les tags de milliers de fichiers, mais elle
n'attribue pas non plus un titre à son seul dossier d'artiste : le décompte
(`library_rules.Tally`) réunit les titres déjà rangés sous l'artiste, ceux
qu'un nom de fichier lui attribue dans « Divers » ou à la racine, et **les
titres du lot en cours d'extraction**, réservés avant qu'aucun ne démarre.
Sans ce dernier point, la page Musique lançant dix extractions en parallèle,
les dix premiers titres d'un artiste nouveau se voyaient chacun seul et
partaient tous dans « Divers » — d'où l'analyse suivante les ressortait.
Comme « Organiser », le décompte réunit les écritures d'un même nom
(« Inna », « Inna Feat. Flo Rida »).

Le **réencodage** fait exception : il convertit sur place, sans rien déplacer.

### Trouver la bonne vidéo, ou ne rien faire

Une extraction commence par une recherche YouTube bâtie sur les tags du
fichier. Tant qu'ils sont propres, la première requête — « Artiste - Titre
(Official Audio) » — suffit. Les tags d'une bibliothèque glanée en ligne le
sont rarement : un tampon de site collé au titre
(« White Knuckle Ride Full Edit [Www Keepvid Com] »), une mention de version
que le catalogue nomme autrement (« Sexyfied (Radio Edit Vf 2) »), un invité
accolé à l'artiste (« Mad Ice Feat. Irina »). YouTube ne rend alors **aucun**
résultat, et relancer dix fois la même requête ne pouvait rien y changer : la
ligne finissait en « Erreur » après une minute d'essais identiques.

La recherche est donc reprise **de la plus fidèle à la plus large** : le titre
débarrassé de son tampon, puis ramené au morceau seul, sous le nom de
l'artiste principal. Le même nettoyage sert à la correspondance Deezer, ce qui
renseigne du même coup la colonne « Correspondance », la jaquette et les tags.

Chaque requête examine **les trois premières vidéos**, et non la seule
première : celle-là est parfois soumise à vérification d'âge, privée ou
retirée — un refus définitif, qu'aucune tentative ne lève — quand la deuxième
porte le même morceau.

Une vidéo n'est retenue que si **son titre reprend celui du morceau
cherché**. C'est la contrepartie indispensable de requêtes élargies : une
recherche large finit toujours par rendre *quelque chose*, et le réencodage
écrit par-dessus le fichier d'origine. Faute de candidat convaincant, la
ligne s'arrête sur une erreur qui dit pourquoi, plutôt que de remplacer un
titre par une vidéo sans rapport.

Les échecs sont enfin **distingués selon qu'ils se dissipent ou non**. Un
« HTTP 403 » signé par un nœud du CDN, une coupure réseau : la vidéo reste la
bonne, la tentative est refaite (jusqu'à dix fois, comme avant). Une vidéo
hors d'atteinte ou une recherche sans résultat : la ligne s'arrête aussitôt,
en quelques secondes au lieu d'une minute. Dans tous les cas, la cause exacte
s'affiche **au survol de la cellule « Statut »** — elle ne partait
jusqu'ici que sur la console.

### Compléter les tags

**Un seul bouton, un seul passage**, pour deux manques que rien ne gagnait à
séparer.

Le premier empêche de ranger : il manque au fichier le titre, l'artiste ou
l'album. Chacun est cherché sur Deezer d'après ses tags, son nom de fichier et
son dossier, puis sur MusicBrainz, et en dernier recours reconnu à son
**empreinte acoustique** — la seule voie pour un fichier nommé « 01.mp3 » sans
le moindre tag. La durée fait preuve : au-delà de trente secondes d'écart, la
piste est écartée quel que soit son titre.

Le second ne bloque rien : l'artiste de l'album, le genre, l'année et le numéro
de piste. Leur absence se voit partout ailleurs — un motif de nommage qui
réclame `%annee%` ne rend rien, et les colonnes correspondantes de la base
restent muettes. La piste identifiée, le catalogue est **interrogé une seconde
fois** : `/track/{id}` porte le rang dans l'album, le numéro de disque et la
date de l'enregistrement d'origine, `/album/{id}` le genre et l'artiste de la
parution. L'album n'est demandé **qu'une fois pour toutes ses pistes** — sur
une bibliothèque, la même parution revient dix à quinze fois.

Les deux se cherchent ensemble parce que **la même identification les sert** :
le catalogue ne rend le genre qu'une fois la piste reconnue. Et un fichier non
rangeable est presque toujours aussi dépourvu de genre et d'année — deux
boutons imposaient deux recherches, deux revues et deux analyses pour le
compléter, la première réécrivant sur le disque les tags que la seconde devait
relire. C'est `find_tags` qui décide, **fichier par fichier**, ce qu'il faut
demander et à qui : rien de plus n'est demandé pour un fichier qui n'en a pas
besoin, et la colonne « source » de la revue dit ligne par ligne ce qui a
réellement été interrogé.

Trois choix gouvernent les tags complémentaires :

- **Deezer seul.** Ni MusicBrainz ni AcoustID ne connaissent ces champs, et
  calculer une empreinte pour un genre manquant coûterait la lecture complète
  du fichier. Ils ne sont donc pas sollicités.
- **L'année seule**, non la date complète : c'est ce que porte la très grande
  majorité des fichiers déjà étiquetés, et mêler deux conventions rendrait la
  colonne illisible. La date de la *piste* est préférée à celle de l'album,
  qui peut n'être que celle d'une réédition — « The Bridge » annonce 1995 sur
  ses pistes et 2015 sur sa parution.
- **Le numéro de disque et le nombre de pistes ne sont jamais réclamés pour
  eux-mêmes.** Ils sont écrits quand la réponse les porte ; les exiger
  tiendrait pour éternellement incomplet un simple exemplaire qui n'a pas de
  numéro de disque à porter, et la bibliothèque entière serait reprise à
  chaque passage.

La recherche ne porte que sur les **fichiers cochés** dans l'arborescence :
cocher le seul dossier d'un artiste suffit à s'y limiter, et le bouton s'éteint
dès qu'il ne reste rien de coché à chercher.

La revue précède l'écriture : les correspondances sûres sont cochées d'office,
les probables laissées à l'examen, et **seuls les champs vides sont écrits** —
aucun tag existant n'est remplacé, sauf demande explicite. Une requête par
fichier : comptez une seconde par fichier concerné, et l'écran annonce l'ordre
de grandeur avant de commencer.

Cette attente n'est pas subie : **« Arrêter la recherche »** interrompt
l'interrogation du catalogue sans fermer la revue, et le bouton d'écriture
s'allume **dès la première correspondance** — de quoi lancer une
bibliothèque de deux mille fichiers, laisser tourner le temps d'un café et
écrire ce qui a été trouvé. Les fichiers non encore interrogés sont laissés
tels quels et l'écran le dit, avec le compte exact ; un passage suivant les
reprendra. Écrire met fin à la recherche : les tags changent sur le disque,
les propositions suivantes porteraient sur un état périmé.

Ce que cette recherche rattrape, les extractions ne le laissent plus
manquer : **tout fichier produit par l'application** — téléchargé, ajouté ou
réencodé — porte désormais l'ensemble de ces tags, demandés à Deezer au
moment de l'extraction (`download_thread.apply_metadata`, `tag_write.py`).
Les tags du fichier qu'un réencodage remplace gardent la main : l'édition
possédée n'est pas forcément celle du catalogue.

### Rechercher les doublons

Le bouton **Rechercher les doublons** rapproche les fichiers qui semblent
être le même titre et ouvre un écran où chaque groupe se décide à la main.
Il est plus large que l'option « Ne garder qu'un exemplaire », qui ne
supprime que les copies dont le plan est certain — celles qui visent le même
fichier de destination. Le rapprochement joue sur trois clés : le nom de
fichier normalisé, le couple titre/artiste, et le couple titre/**artiste
principal** — c'est cette dernière qui réunit « Right Round - Flo Rida » et
« Right Round - Flo Rida Feat. Ke$ha », que rien d'autre ne rapproche.
Sur une bibliothèque de 3 300 titres déjà rangée, la recherche relève
dix-sept paires de ce genre.

## Repérer les titres abîmés

La page **Outils → Qualité** écoute le *signal* de chaque fichier — et non
ses tags ni son débit — pour lister les titres dont l'extraction a mal
tourné, avec la possibilité de les **retélécharger** sur place.

Six défauts sont mesurés, chacun avec son seuil réglable :

| Défaut | Ce qui est mesuré | Titre sain | Défaut franc |
|---|---|---|---|
| **Souffle** | plancher de bruit au-dessus de 8 kHz | sous −80 dB | −60 dB et au-delà |
| **Craquements** | échantillons imprévisibles (`adeclick`) | moins de 0,001 % | 0,004 % et au-delà |
| **Saturation** | échantillons collés à la butée 16 bits | 0 % | 0,2 % et au-delà |
| **Coupures** | part de silence du passage de 10 s le plus troué | sous 5 % | 30 % et au-delà |
| **Durée** | écart à la durée du morceau (Deezer / MusicBrainz) | 0 à 3 s | 10 s et au-delà |
| **Blancs** | plage muette au début ou à la fin | 0 à 3 s | 5 s et au-delà |

- les **coupures** sont des échantillons *nuls* — des zéros exacts, non du
  signal faible — semés au milieu d'un morceau par une extraction qui a
  hoqueté. Les compter ne suffirait pas : « We Will Rock You » en montre
  soixante-neuf, les silences qui séparent ses frappes de pied, et se porte
  très bien. Ce qui trahit un bégaiement, c'est la **densité** — la part de
  silence du passage de dix secondes le plus troué. Elle sépare franchement
  les deux familles : sur quarante-cinq fichiers d'une bibliothèque réelle,
  un seul dépassait 30 %, celui dont l'introduction bégaie, à 51 % ; le pire
  fichier sain plafonnait à 19 %, et la plupart sous 5 %. Les blancs de début
  et de fin sont exclus, ils ont leur propre seuil ;
- le **souffle** demande deux conditions, faute de quoi tout morceau de
  danse que rien n'interrompt serait accusé : le plancher de bruit doit
  dépasser le seuil, *et* le bruit doit être large bande. Au moment le plus
  calme, un souffle laisse la bande haute à quelques décibels du signal
  entier ; la musique, pauvre en aigus dans ses passages calmes, l'en laisse
  à 20 ou 30 dB. Le survol de la colonne « Souffle » donne l'écart mesuré et
  le verdict qu'il entraîne ;
- les **craquements** sont les seuls à coûter cher (l'analyse est environ
  quatre fois plus lente) : ils se décochent pour une analyse rapide ;
- la **saturation** relève d'un choix de mastering plus que d'une mauvaise
  extraction — une variété récente en montre couramment 0,03 à 0,08 % —, d'où
  un seuil placé haut : retélécharger ne changerait rien en deçà ;
- la **durée** est le seul défaut que le signal ne trahit pas : un rip radio
  propre se mesure exactement comme un master propre. Comparer la durée du
  fichier à celle du morceau le révèle — plus court ou plus long, ce n'est
  pas la version attendue ;
- les **blancs** ne sont mis en cause que si le fichier est **plus long que
  le morceau** : voir la section suivante ;
- **modifier un seuil ne relit aucun fichier** : les mesures sont conservées,
  seul le verdict est recalculé. Elles sont aussi mises en cache sur le
  disque, indexées par date de modification : relancer une analyse sur un
  dossier déjà parcouru est immédiat.

### La durée du morceau, et ce qu'elle permet de juger

Deux défauts ne se lisent pas dans le signal seul. Ils demandent de savoir
combien de temps le morceau dure réellement, information cherchée chez
**Deezer** puis, à défaut, chez **MusicBrainz** (`reference_duration.py`).

**La durée elle-même.** Un fichier plus court ou plus long que le morceau
n'est pas la version attendue : montage écourté, édition radio, extraction
d'une mauvaise source. C'est souvent le seul indice disponible — un rip radio
propre se mesure comme un master propre, et aucun seuil sur le signal ne les
sépare.

**Les blancs.** Un silence n'est un défaut que s'il a été *ajouté*. Beaucoup
de morceaux s'achèvent sur un fondu ou des applaudissements que leur master
contient bel et bien : les accuser revenait à proposer de retélécharger une
version identique. Un blanc n'est donc signalé que si le fichier dépasse en
outre la durée du morceau.

Communs aux deux :

- parmi les durées trouvées (éditions radio, live, remix), c'est la **plus
  proche** de celle du fichier qui sert de référence : accuser un titre de ne
  pas durer autant qu'un remix de six minutes n'aurait aucun sens ;
- sans référence — titre inconnu des deux services, ou réseau indisponible —
  **ni la durée ni les blancs ne sont signalés**, et les colonnes le disent ;
- la recherche tourne dans son propre thread, à cinq titres par seconde tant
  que Deezer répond (un par seconde pour ceux qu'il ignore). Une bibliothèque
  de trois mille titres demande donc une quinzaine de minutes la première
  fois, et rien du tout ensuite : les réponses sont mises en cache sur le
  disque. Le tableau se remplit pendant ce temps et reste utilisable ;
- sur soixante-dix titres tirés au hasard, l'écart médian à la référence est
  de 1,6 s et les trois quarts tiennent sous 3,4 s ; au-delà de dix secondes
  commence une autre famille, qui compte pour 17 % de la bibliothèque.

Sur un dossier Queen de 41 titres, seize fichiers portent un blanc et huit
seulement sont retenus. « Half A World Away (Live) » finit sur quatorze
secondes d'applaudissements qui s'éteignent — le fichier dure 218,7 s, la
référence 221 : rien à corriger. « One Vision » dure 262,8 s pour une
référence de 243 : les trente et une secondes de vide finales sont bien de
trop.

L'analyse **s'exploite sans attendre sa fin** : elle tourne dans un thread
dédié, remplit le tableau au fil de l'eau, et cocher comme retélécharger
restent disponibles pendant qu'elle travaille — une bibliothèque entière
demande plusieurs dizaines de minutes en analyse approfondie, et les titres
signalés dans les premières secondes n'ont pas à les attendre. Le bouton
**Arrêter** l'interrompt sur-le-champ (les lectures FFmpeg en cours sont
tuées, non attendues) sans rien perdre de ce qui est déjà mesuré. Seuls
restent bloqués le temps d'une analyse : le relancement d'une analyse et la
case « Détecter les craquements », qui décide de ce qui est mesuré. Les
seuils, eux, ne relisent aucun fichier et restent réglables.

Le **retéléchargement** emprunte le chemin du réencodage : correspondance
Deezer résolue à partir des tags, extraction via yt-dlp au format des
Paramètres, et remplacement de la version abîmée. Le fichier produit est
rangé selon les **règles de l'écran « Organiser »** — un titre égaré dans
« Divers » rejoint donc le dossier de son artiste au passage. Les titres
dont les tags ne donnent ni artiste ni titre sont écartés — la recherche
n'aurait rien à chercher. Chaque titre remplacé est **réanalysé** aussitôt :
la ligne indique si le défaut a réellement disparu.

## Bibliothèque : lister, chercher, écouter

Les autres écrans regardent la bibliothèque par le petit bout de leur
lorgnette : ce qui se télécharge, ce qui est mal rangé, ce qui sonne mal.
Aucun ne répondait à la question la plus simple — « qu'est-ce que j'ai, et où
est ce morceau ? ». L'écran **Bibliothèque**, deuxième de la barre latérale
juste sous **Musique**, ne fait que cela.

Rien n'est relu du disque : les titres viennent de la table `tracks`, et
l'écran s'ouvre en **375 ms** sur une bibliothèque de 3 500 lignes, sans
réseau. Un seul appel système par titre vérifie sa date et sa taille — de quoi
distinguer un fichier encore présent d'un fichier disparu, et une copie en
ligne à jour d'une copie périmée.

### Une recherche sur tous les tags, et cinq propositions

La recherche ne demande pas dans quelle colonne chercher. Elle porte sur
**titre, artiste, album, artiste d'album, genre, année et format à la fois**,
plus le nom du fichier : on tape ce dont on se souvient. Les mots sont exigés
tous ensemble mais dans n'importe quel ordre — « daft one more » trouve le
titre — accents, casse et ponctuation ignorés (`library_index.canonical`).
Filtrer 3 500 titres prend de 5 à 60 ms, et la frappe n'est traitée qu'une
fois retombée (220 ms) pour que la saisie ne saccade jamais.

Dès la **troisième lettre**, **cinq propositions cliquables** s'affichent sous
le champ. Elles ne sortent pas d'un dictionnaire : ce sont les valeurs de tags
réellement présentes dans la bibliothèque, chacune annoncée par sa nature et
par le nombre de titres qu'elle rassemble.

```
mad │ Artiste · Madonna                    (61 titres)
    │ Album · Made in Heaven               (13 titres)
    │ Album · Made in Love                 (13 titres)
    │ Artiste · Mad Ice Feat. Irina         (1 titre)
    │ Album · Madman Across The Water       (1 titre)
```

Trois règles les gouvernent, et chacune répond à un travers constaté :

- une valeur qui **commence** par ce qui est tapé passe devant une valeur qui
  ne fait que le contenir, et à égalité c'est celle qui rassemble le plus de
  titres qui monte ;
- une valeur n'apparaît **qu'une fois**, quel que soit le nombre de tags qui
  la portent. « Daft Punk » est presque toujours à la fois l'artiste et
  l'artiste d'album : l'annoncer deux fois gâcherait deux des cinq places pour
  un filtre identique. La nature retenue est la plus parlante — artiste plutôt
  qu'artiste d'album ;
- seules les valeurs de titres **effectivement affichables** sont proposées :
  une proposition qui ne donnerait aucune ligne serait une fausse piste.

Le clic remplit le champ et filtre aussitôt ; les flèches ↑ ↓ parcourent la
liste, Entrée retient, Échap la referme. Le tri se fait par clic d'en-tête sur
n'importe quelle colonne, y compris **NUAGE**.

### Écouter, y compris ce qui n'est plus sur le disque

Le clic sur **▶** ouvre le lecteur, et **la liste affichée devient sa file
d'attente** : filtrer sur un album, c'est se faire une sélection. Le
double-clic et le menu contextuel font de même.

Une bibliothèque posée sur un disque externe disparaît quand il n'est pas
branché — sur la bibliothèque de référence, 1 337 titres sur 3 519 vivent sur
`D:\`. La case **Titres absents du disque** les fait apparaître, et ceux dont
le nuage détient une copie à jour restent **écoutables** : le lecteur joue
alors le flux servi par le seau, sous une URL signée valable une heure. C'est
là que le miroir cesse d'être une sauvegarde pour devenir une bibliothèque
d'appoint.

## Le flux audio ne tient pas dans la base

14 Go de musique, et 5 Go de base en ligne. Le calcul ne tombe pas juste, et
aucun hébergeur gratuit ne le fait tomber juste : le plus généreux plafonne à
10 Gio. Mais la taille n'est même pas le premier argument — **libSQL rend une
ligne entière par requête HTTP**. Jouer un titre imposerait de télécharger ses
huit mégaoctets avant la première note, sans avance rapide possible. Et un
envoi complet consommerait quatre fois le quota mensuel de synchronisation.

Le flux audio part donc ailleurs, et la base ne garde que de quoi le
retrouver.

| | Où | Quoi |
|---|---|---|
| **Métadonnées** | Turso | tags, jaquettes, mesures — et quelques dizaines d'octets par titre pour le miroir (table `opus_mirror`) |
| **Flux audio** | seau de stockage objet | un objet par titre, servi par tranches (requêtes `Range`) |

Deux offres gratuites conviennent, toutes deux compatibles S3 :
**Cloudflare R2** (10 Go, **sortie de données gratuite et illimitée**) et
**Backblaze B2** (10 Go, sortie gratuite jusqu'à trois fois le volume stocké).
Aucune dépendance nouvelle n'a été ajoutée pour autant : la signature AWS
SigV4 tient en quarante lignes de `hmac` et `hashlib`, et les échanges passent
par `requests` — embarquer `boto3` aurait coûté une quinzaine de mégaoctets à
l'exécutable pour signer trois sortes de requêtes.

### Le miroir Opus

Les fichiers sont **réencodés en Opus** par le ffmpeg livré avec
l'application. À 128 kb/s — le débit auquel les services de diffusion servent
leur qualité haute — le codec est transparent ou presque sur de la musique, et
une bibliothèque en MP3 320 ou en FLAC y perd les deux tiers de son poids. Les
originaux ne bougent pas : le miroir ne remplace rien, il double la
bibliothèque en léger.

Encore faut-il que la bibliothèque soit lourde, et **c'est là que la mesure a
démenti le raisonnement**. Sur la bibliothèque de référence, disque entier,
**3 576 des 3 581 fichiers sont déjà à 128 kb/s ou moins** : les réencoder en
Opus 128 ne rend pas un octet. Huit titres tirés au hasard pèsent 30,4 Mo avant, 30,4 Mo après —
pour une deuxième compression avec perte par-dessus la première. Le réencodage
ne gagne quelque chose qu'en **descendant** sous le débit de la source.

Quatre dispositions en découlent :

- un fichier **déjà plus léger** que le débit demandé est envoyé **tel quel**,
  octet pour octet — pas de réencodage inutile, pas de perte gratuite. Il vit
  dans son propre rayon du seau (`audio/source/…`), son poids ne dépendant
  d'aucun réglage ; la colonne NUAGE dit « copié tel quel » au survol ;
- un fichier dont la base **ignore le débit** voit son en-tête relu plutôt
  que d'être réencodé à l'aveugle. La mesure a tranché : sur 198 lignes à
  débit nul, 197 sont des MP3 128 qu'une deuxième compression aurait abîmés
  sans rien faire gagner, et une est un fichier tronqué que ffmpeg n'aurait
  pas su lire. La lecture coûte 0,4 ms par fichier (en-tête mutagen, sans
  sous-processus) ; à défaut de réponse, le fichier part tel quel — **ne rien
  savoir n'autorise pas à dégrader** ;
- la case **« Envoyer les fichiers tels quels »** supprime le réencodage tout
  court. Le miroir pèse alors exactement le poids du disque, et se restaure
  **intégralement** à l'identique — ce qu'un miroir Opus ne peut pas
  promettre. C'est le bon réglage dès que la bibliothèque tient dans l'offre
  gratuite, ce que la jauge dit à l'octet près ;
- le volet **Nuage** porte une **jauge** qui annonce, pour le débit choisi, ce
  que la bibliothèque pèsera en ligne — face aux 10 Go gratuits, et en
  signalant les titres qui partiraient sans un octet de gagné. L'estimation ne
  coûte aucune lecture de disque : la durée et le débit de chaque fichier sont
  en base. Sur la bibliothèque de référence :

| Débit | Poids en ligne | Restaurable à l'identique | Verdict |
|---|---|---|---|
| 64 kb/s | 3,8 Go | 0 % | tout est réencodé |
| **80 kb/s** | **4,8 Go** | **0 %** | **le palier retenu — voir ci-dessous** |
| 96 kb/s | 5,7 Go | 0 % | la place gagnée se paie en fidélité |
| 128 kb/s | 8,5 Go | 99,9 % | 2 198 titres copiés tels quels — aucun gain |
| 160 / 192 kb/s | 8,5 Go | 99,9 % | idem, le débit ne sert plus à rien |
| **sans réencodage** | **8,5 Go** | **100 %** | **le miroir est le disque** |

Le débit ne se choisit donc pas dans l'abstrait : la jauge le dit avant que
trois heures de réencodage ne le démontrent.

**Attention à ce que la jauge compte.** Elle ne connaît que les titres de la
table `tracks` : 2 200 fichiers, 8,51 Go. Le disque, lui, en porte **3 581
pour 14,08 Go** — 1 381 fichiers (5,57 Go) n'ont jamais été indexés par
l'analyse de la bibliothèque. Tant qu'ils ne le sont pas, la jauge annonce un
miroir qui tient dans les 10 Go gratuits ; une fois l'analyse passée, la
bibliothèque entière **dépasse l'offre de 4,08 Go**.

Ce qui met le doigt sur le vrai arbitrage : la bibliothèque étant déjà en MP3
128 de bout en bout, il n'existe pas de réglage qui la fasse à la fois tenir
dans les 10 Go gratuits **et** revenir à l'identique. Sur le disque entier —
3 581 fichiers, **239 h 44** de musique — un miroir Opus pèserait :

| Débit | Miroir complet | |
|---|---|---|
| 64 kb/s | 6,90 Go | tient, large |
| **80 kb/s** | **8,63 Go** | **tient** |
| 96 kb/s | 10,36 Go | dépasse de 0,36 Go |
| 128 kb/s | 13,81 Go | dépasse de 3,81 Go |

**96 kb/s est le piège** : il coûte de la qualité sans résoudre le problème,
puisqu'il dépasse encore. Entre deux paliers, le bon n'est pas le plus proche
du débit d'origine — c'est le premier qui passe. D'où le choix de **80 kb/s**,
seul palier gratuit qui couvre la bibliothèque entière.

Ce que ce choix coûte, et qu'il faut savoir : plus rien ne revient à
l'identique. La restauration, elle, **reconvertit l'Opus en MP3** (voir plus
bas) — un autoradio ne lit pas le `.opus`, et une copie de secours qui ne joue
pas là où l'on écoute ne servirait à rien. L'alternative était de tout envoyer
tel quel et de payer les 4 Go de dépassement, de l'ordre de quelques centimes
par mois.

La jaquette n'est pas recopiée dans le miroir. La table `covers` la détient
déjà, dédoublonnée ; une image de 100 Ko par titre pèserait plus lourd que ce
que le réencodage fait gagner sur trois minutes de musique.

Deux empreintes évitent le travail inutile :

- l'**empreinte date+taille** du fichier source dit s'il a changé depuis son
  dernier envoi. Un titre retéléchargé par l'écran Qualité est ainsi repéré, et
  lui seul est réexpédié — la colonne NUAGE l'annonce « à renvoyer » ;
- l'**empreinte SHA-1** de son contenu donne la clé de l'objet. Deux copies du
  même fichier, dans deux dossiers, n'occupent qu'une place ; et un objet déjà
  présent n'est ni réencodé ni renvoyé, ce qui rend un envoi interrompu
  reprenable à peu de frais. Le débit fait partie de la clé : le changer ne
  peut pas écraser silencieusement des objets encodés autrement.

Le réencodage se fait fichier par fichier, dans un fil séparé : il sature déjà
un cœur, et deux envois simultanés sur une liaison montante domestique ne vont
pas plus vite que l'un après l'autre. L'intérêt du fil est ailleurs — la liste
reste parcourable et écoutable pendant que le miroir se constitue, et
**Arrêter** abandonne le titre en cours sans perdre les précédents.

### Restaurer le disque depuis le seau

C'est le chemin inverse, et celui qui donne au miroir sa raison d'être :
**Restaurer** redescend du seau les titres que le disque n'a plus. Une
réinstallation, un disque changé, un dossier effacé par mégarde — la
bibliothèque se reconstitue sans que rien n'ait été réencodé au passage.

Trois règles, et une limite dite franchement :

- **rien n'est jamais écrasé.** Seuls les fichiers *absents* sont écrits ; un
  titre déjà en place ne coûte même pas une requête au seau. L'arborescence
  disparue avec les fichiers est refaite au passage ;
- un fichier **envoyé tel quel** revient **octet pour octet**, et son
  empreinte SHA-1 est **revérifiée après écriture** — c'est la ligne du miroir
  qui la détient depuis l'envoi. Une copie qui ne se vérifie pas est effacée
  plutôt que gardée : mieux vaut un titre manquant, qu'on sait aller
  rechercher, qu'un titre corrompu qui se donne pour restauré ;
- un fichier **réencodé en Opus** n'a pas d'original à rendre. Il est
  **reconverti en MP3** à la descente et reprend son chemin d'origine : le
  `.opus` n'existe que dans le seau, jamais sur le disque. Le MP3 est produit
  à **192 kb/s** — non pour « rester au débit d'origine », ce qui empilerait
  la faiblesse du MP3 à bas débit sur celle de l'Opus, mais pour que le
  réencodage n'ajoute rien d'audible par-dessus une source déjà bornée dans
  les aigus. Le bilan les compte à part : « rendus à l'identique » d'un côté,
  « convertis en MP3 » de l'autre — ces derniers ont traversé deux
  compressions avec perte.

Un titre parti d'un `.flac` ou d'un `.m4a` revient donc sous `.mp3` :
l'extension suit le contenu, pas l'histoire. Et l'empreinte date+taille de la
ligne de miroir est remise à jour même dans ce cas — sans quoi le titre se
donnerait pour « à renvoyer », et un envoi de plus le ferait repartir vers le
seau en **quatrième** génération.

Les tags suivent (`-map_metadata`), en ID3v2.3 — la version que lisent les
autoradios et les vieux baladeurs. La jaquette, elle, n'est pas dans le miroir
et ne revient donc pas : c'est la table `covers` qui la détient.

La case **« Envoyer les fichiers tels quels »** supprime ce cas de figure : un
miroir constitué sans réencodage se restaure entièrement à l'identique, sans
une seule conversion. Il coûte en revanche le poids du disque — 14,08 Go pour
cette bibliothèque-ci, soit 4,08 Go de plus que l'offre gratuite. La case ne
dispense pas de l'arbitrage, elle le rend lisible.

Le fichier revient avec une **date de modification neuve**. L'empreinte
date+taille de la ligne de miroir est donc remise à jour dans la foulée : sans
cela, tout ce qui vient d'être restauré se donnerait aussitôt pour « à
renvoyer », et une bibliothèque entière repartirait vers le seau où elle se
trouve déjà, à l'identique.

Le bouton agit sur les titres cochés, ou sur tous ceux affichés à défaut — et
la liste ne montre les titres disparus du disque que si **Titres absents du
disque** est coché. Quand aucun titre affiché n'est à restaurer alors que la
bibliothèque en compte ailleurs, l'écran le dit plutôt que de laisser croire
qu'il n'y a rien. Le menu contextuel porte la même action pour un seul titre.

L'écriture se fait dans un temporaire du dossier de destination, remplacé d'un
coup une fois le fichier complet : une restauration interrompue — **Arrêter**
vaut pour les deux sens — ne laisse pas de moignon qui passerait pour un
titre.

### Mise en service du seau

1. Créer un seau chez [Cloudflare R2](https://developers.cloudflare.com/r2/)
   (ou chez Backblaze B2) et un jeu de clés d'accès S3.
2. **Bibliothèque → volet « Nuage »** : coller le point d'entrée
   (`https://<compte>.r2.cloudflarestorage.com`), le nom du seau, la région
   (`auto` chez R2) et les deux clés. **Tester**, puis **Enregistrer**.
3. Choisir le débit **ou** cocher **« Envoyer les fichiers tels quels »** —
   la jauge dit ce que chaque option pèserait face aux 10 Go gratuits. Puis
   cocher des titres et **Envoyer au nuage**. **Mesurer l'occupation** compte
   ensuite ce que le seau détient réellement.
4. Le retour se fait par **Restaurer**, sur les titres que le disque n'a plus
   (cocher **Titres absents du disque** pour les voir).

Les clés vivent dans `%APPDATA%\DeeKeep\bucket.json`, **hors de la base** —
celle-ci n'a pas à porter de quoi vider un seau. Les variables
d'environnement `DEEKEEP_S3_ENDPOINT`, `_BUCKET`, `_REGION`, `_KEY`, `_SECRET`
et `_PREFIX` sont prioritaires. Tout ce que l'application écrit vit sous le
préfixe `deekeep/` : le seau reste utilisable pour autre chose.

## Barres d'actions

Tous les écrans qui affichent une liste — **Musique**, **Bibliothèque**,
**Réencoder**, **Organiser**, **Qualité**, l'historique des téléchargements
— portent la même barre, **au-dessus de la liste**, et lue de gauche à droite dans le même
ordre :

| Place | Contenu | Style |
|---|---|---|
| à gauche | manipulation de ce qui est affiché : **Tout cocher**, **Tout décocher**, **Tout déplier**, **Retirer la sélection**, **Vider la liste** | pilule « verre » (`control-btn`) |
| à droite | actions secondaires : **Purger les téléchargés**, **Réessayer**, **Compléter les tags**, **Rechercher les doublons**, **Restaurer**, **Arrêter** | pilule « verre » |
| avant-dernière | l'étape qui **alimente ou analyse** la liste : **Rechercher**, **Ajouter des fichiers**, **Analyser**, **Actualiser** | pilule cyan ajourée (`accent-btn`) |
| dernière | l'action qui **écrit** — sur le disque ou dans le seau : **Extraire**, **Convertir**, **Organiser**, **Retélécharger la sélection**, **Envoyer au nuage** | pilule cyan pleine (`primary-btn`) |

Un seul bouton plein par écran, donc, et toujours au même endroit : la place
occupée dit ce que le bouton engage, avant même qu'on lise son libellé. Les
suppressions de fichiers sur le disque — l'écran de déduplication — sont
seules à porter la pilule rouge (`danger-btn`). Les filtres d'affichage ne
sont pas des actions : ils vivent dans leur propre ligne, sous la barre
(« Masquer ce qui est déjà en place », « Masquer ceux déjà au format cible »,
les cases de défauts de l'écran Qualité).

Les libellés suivent la même règle : **Retirer** ne touche qu'à la liste,
**Supprimer** touche au disque, **Purger** retire une catégorie entière
(« les téléchargés », « les convertis », « les fichiers introuvables ») et
**Vider** retire tout. Les quatre classes de boutons sont décrites une seule
fois, dans `styles.py` ; aucun écran ne repeint un bouton à la main.

## Écouter les titres

Deux écoutes cohabitent, et le geste les distingue partout de la même façon.

Tous les écrans qui affichent des titres portent une colonne **ÉCOUTE** : la
page Musique, **Bibliothèque**, l'historique des téléchargements,
**Réencoder** et **Qualité**. Le **clic** y écoute tout de suite : le fichier local dans le
**lecteur** (`player.py`), ou — page Musique, fenêtre de recherche — l'**extrait
Deezer de 30 s** d'un titre pas encore extrait, sans qu'aucune fenêtre
s'ouvre. Le **double-clic sur la ligne** et le **menu contextuel** (« Écouter
dans le lecteur ») ouvrent, eux, le lecteur complet dans tous les écrans,
y compris **Organiser**, dépourvu de colonne libre.

Le lecteur est une fenêtre indépendante — la bibliothèque reste parcourable
pendant l'écoute — qui reprend la liste affichée en **file d'attente**, dans
l'ordre du tableau. Un seul lecteur existe à la fois : demander un autre
titre change de titre, il n'ouvre pas une seconde fenêtre.

L'extrait de 30 s, lui, ne s'encombre de rien : il vérifie qu'on a coché le
bon titre avant de lancer l'extraction. Un deuxième clic l'arrête, et un seul
joue à la fois. Dès que l'extraction aboutit, la même cellule ouvre le lecteur
complet sur le fichier obtenu ; le bouton ne reste éteint que pour les titres
dont Deezer ne donne aucun extrait. Le double-clic d'un titre en attente ouvre
malgré tout le lecteur, sur l'extrait : c'est là qu'on trouve la position, le
volume et la jaquette.

Les deux lectures partagent une règle, et une seule : **un seul son à la
fois**. Lancer un extrait met la fenêtre du lecteur en pause — elle le dit, et
reprend le titre au même endroit ; relancer la lecture, ou ouvrir le lecteur
sur un autre titre, coupe l'extrait en cours.

C'est l'écran **Qualité** qui l'a réclamé. Un souffle à −38 dB ou deux
secondes de blanc final ne se jugent pas sur un chiffre : il faut entendre le
fichier, et pouvoir aller directement à l'endroit en cause. Le lecteur offre
donc, outre lecture/pause et arrêt :

- un **égaliseur d'analyse** à dix bandes d'octave (31 Hz → 16 kHz), avec
  maintien de crête. Un souffle se voit à la bande des 16 kHz qui ne retombe
  jamais, une coupure aux dix colonnes qui s'éteignent d'un coup ;
- une **barre de position** cliquable — un clic dans la rainure amène la
  lecture au point visé, sans avoir à faire glisser le curseur ;
- le **temps écoulé et la durée** du titre, ainsi que le rang dans la file ;
- le contexte du titre écouté : sur l'écran Qualité, son format, son débit,
  sa note et les défauts relevés — de quoi savoir ce qu'on cherche à
  entendre ;
- au clavier : **Espace** (lecture/pause), **←** / **→** (± 5 s),
  **↑** / **↓** (volume), **P** / **N** (titre précédent / suivant).

L'égaliseur mesure le signal, il ne l'invente pas : Qt ne donne aucun accès
aux échantillons qu'il joue, le fichier est donc décodé **en parallèle** par
le ffmpeg livré avec l'application, et chaque trame de dix niveaux est rangée
à son instant — la position du lecteur suffit alors à retrouver celle à
afficher, y compris après un déplacement dans le titre. L'analyse prend de
l'avance sur la lecture puis s'endort : la transformée de Fourier est écrite
en Python, et la calculer à plein régime ferait saccader l'interface pour un
résultat qui ne s'affichera que trois minutes plus tard. Sans ffmpeg, la
lecture fonctionne et seul l'égaliseur reste éteint.

## Base de données

Toutes les informations de l'application vivent dans une base de données —
plus seulement dans des fichiers JSON : paramètres, dossier de dépôt, filtres
et motif de renommage, seuils de l'écran Qualité, historique des recherches,
historique des téléchargements, tags, jaquettes, caractéristiques d'encodage
de chaque fichier, mesures de qualité et durées de référence.

### Deux copies, une seule vérité

| | Où | Rôle |
|---|---|---|
| **Cache local** | `%APPDATA%\DeeKeep\deekeep_cache.db` | SQLite. Tout ce que l'application lit vient d'ici : trois mille titres et leurs jaquettes s'affichent en quelques millisecondes, et l'application reste entièrement utilisable sans réseau. |
| **Base en ligne** | Turso (offre gratuite : 5 Go, sans carte bancaire) | La référence, partageable entre plusieurs machines. Dialecte SQLite, pilotée en HTTP : aucune dépendance supplémentaire. |

Les deux portent **le même schéma**, décrit une seule fois dans
`db_schema.py`. Le cache est reconstructible ; la base en ligne, elle, garde
la mémoire longue.

### Tables

| Table | Contenu |
|---|---|
| `app_settings` | un paramètre par ligne, valeur en JSON (le type est préservé) |
| `searches` | historique des recherches, ordre compris |
| `covers` | jaquettes, **dédoublonnées par SHA-1** — 3 329 fichiers ne portent que 1 492 images distinctes, 105 Mo au lieu de 198 |
| `tracks` | un titre par fichier : tags, durée, débit, taille, date, renvoi vers sa jaquette, rang dans l'historique |
| `quality` | mesures de l'écran Qualité et défauts constatés |
| `opus_mirror` | pour chaque titre envoyé au seau : la clé de l'objet, son poids, son débit — **pas** le flux audio (voir « Le flux audio ne tient pas dans la base ») |
| `reference_durations` | durées relevées chez Deezer et MusicBrainz |

### Synchronisation

Chaque ligne porte une empreinte de son contenu ; chaque table, la somme de
celles de ses lignes. Comparer trois nombres — empreinte locale, empreinte
distante, empreinte du dernier échange réussi — suffit à savoir quoi faire,
**sans transférer une seule donnée** :

- un seul côté a changé → l'échange se fait tout seul, dans le bon sens ;
- **les deux ont changé** → l'application le signale et demande qui fait foi.

Aucune fusion automatique n'est tentée dans ce dernier cas : décider seul
laquelle de deux versions de trois mille titres est la bonne, sur la foi
d'une horloge, ne se rattrape pas. La fenêtre d'arbitrage affiche le détail
table par table et propose les deux sens.

Les transferts n'échangent d'abord que l'index des lignes (clé et
empreinte), puis les seules lignes qui diffèrent — décisif sur la table des
jaquettes. Les suppressions voyagent en pierres tombales, sans quoi une
ligne effacée d'un côté reviendrait au rapprochement suivant. Le
rapprochement a lieu au démarrage, quatre secondes après une modification,
puis toutes les cinq minutes ; toujours dans un fil séparé — une base qui
ne répond pas ne fige jamais la fenêtre.

Hors ligne, les écritures s'empilent dans une file d'attente qui survit à la
fermeture de l'application et repart au rétablissement. L'état est visible en
pied de barre latérale, depuis n'importe quel écran.

### Mise en service

1. Créer un compte sur [turso.tech](https://turso.tech) (connexion GitHub,
   sans carte bancaire), puis une base — bouton **Create Database**.
2. Relever l'URL (`libsql://…`) et créer un jeton d'accès.
3. **Paramètres → Base de données** : coller les deux, **Tester**, puis
   **Mettre en service**.

En ligne de commande :

```powershell
python -m deekeep.db.db_init --root "D:/" --url "libsql://ma-base-mon-org.turso.io" --token "…" --push
python -m deekeep.db.db_init --status                 # état de la synchronisation
python -m deekeep.db.db_init --resolve local          # trancher : le cache fait foi
python -m deekeep.db.db_init --resolve remote         # trancher : la base fait foi
python -m deekeep.db.db_init --root "D:/" --reset     # reconstruire le cache local
```

L'URL et le jeton sont conservés à part, dans `%APPDATA%\DeeKeep\cloud.json`
(et non dans la base : il faut bien pouvoir s'y connecter avant de la lire).
Les variables d'environnement `DEEKEEP_DB_URL` et `DEEKEEP_DB_TOKEN` sont
prioritaires. La clé AcoustID et le jeton Deezer, eux, **restent sur la
machine** : ils ne partent en ligne que si le paramètre
`cloud_sync_secrets` est mis à vrai.

## Installation

Depuis le dossier du projet :

```powershell
python -m pip install PyQt6 requests
```

Optionnels (l'appli démarre sans eux, la page Diagnostic indique ce qui manque) :

```powershell
python -m pip install yt-dlp mutagen
```

`yt-dlp` nécessite en plus **FFmpeg** pour la conversion audio. Si le dossier `ffmpeg\` (contenant `ffmpeg.exe` et `ffprobe.exe`) est présent à côté des sources, il est utilisé automatiquement ; sinon l'application se rabat sur le FFmpeg du PATH système. La page Diagnostic indique lequel est utilisé.

## Lancement

Depuis la racine du projet (le dossier de `main.py`) :

```powershell
python main.py
```

## Zone de notification

- L'icône DeeKeep apparaît dans la barre des tâches (identifiant applicatif Windows dédié).
- **Réduire** la fenêtre l'envoie dans les icônes cachées de la zone de notification ; un clic sur l'icône la restaure.
- Le menu contextuel de l'icône propose **Afficher DeeKeep** et **Quitter**. Le bouton Fermer de la fenêtre quitte réellement l'application.

## Compilation (exécutable Windows)

```powershell
python -m pip install pyinstaller pillow
python -m PyInstaller --noconfirm --clean --windowed --name DeeKeep --icon icon.ico --add-data "icon.ico;." --add-data "ffmpeg;ffmpeg" --collect-submodules yt_dlp main.py
```

L'exécutable est produit dans `dist\DeeKeep\DeeKeep.exe`. Il est **autosuffisant** : Python y est embarqué par PyInstaller et FFmpeg (dossier `ffmpeg\` du projet, build officiel [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)) est inclus via `--add-data` — rien à installer sur le poste cible.

## Installeur Windows

Le script [installer.iss](installer.iss) (Inno Setup 6) crée un installeur qui référence l'application dans les « Applications installées » de Windows, ajoute le raccourci du menu Démarrer et propose (optionnel) l'icône sur le bureau :

```powershell
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" installer.iss
```

L'installeur est produit dans `installer\DeeKeep-Setup-<version>.exe`.
