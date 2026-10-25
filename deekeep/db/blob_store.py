"""
Accès au seau de stockage objet où vit le miroir Opus de la bibliothèque.

Pourquoi un seau et non la base — 14 Go de musique ne tiennent pas dans les
5 Go de l'offre Turso, et le quota de synchronisation (3 Go par mois) serait
consommé près de cinq fois par un seul envoi. Mais la taille n'est même pas le
premier argument : libSQL rend une ligne **entière** par requête HTTP. Jouer
un titre imposerait de télécharger ses huit mégaoctets avant la première
note, sans avance rapide possible. Un seau, lui, sert les requêtes
``Range`` : le lecteur y prend le morceau où il veut, quand il veut.

Deux offres gratuites conviennent, toutes deux compatibles S3 :

  * **Cloudflare R2** — 10 Go, **sortie de données gratuite et illimitée** ;
    point d'entrée ``https://<compte>.r2.cloudflarestorage.com``,
    région ``auto`` ;
  * **Backblaze B2** — 10 Go, sortie gratuite jusqu'à trois fois le volume
    stocké par mois ; point d'entrée ``https://s3.<région>.backblazeb2.com``.

Aucune dépendance nouvelle, dans l'esprit de ``cloud_db`` : la signature
AWS SigV4 tient en quarante lignes de ``hmac`` et ``hashlib``, tous deux de
la bibliothèque standard, et les échanges passent par ``requests``, déjà
utilisé partout ailleurs. Embarquer ``boto3`` aurait ajouté une quinzaine de
mégaoctets à l'exécutable pour signer trois sortes de requêtes.

Les requêtes sont adressées en **chemin** (``/seau/clé``) et non en
sous-domaine : R2 comme B2 l'acceptent, et cela évite d'avoir à deviner la
forme du nom d'hôte à partir du point d'entrée.

Les identifiants ne vivent pas dans la base — celle-ci n'a pas à porter de
quoi vider un seau — mais dans ``bucket.json`` du dossier de données, à côté
de ``cloud.json``. Les variables d'environnement ``DEEKEEP_S3_*`` sont
prioritaires.
"""

import datetime
import hashlib
import hmac
import json
import os
import tempfile
import xml.etree.ElementTree as ElementTree
from urllib.parse import quote

import requests

from deekeep.core import settings as app_settings


CREDENTIALS_FILENAME = "bucket.json"

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"

# Un envoi de quelques mégaoctets sur une liaison domestique montante peut
# demander une bonne minute : le délai d'attente est nettement plus large que
# celui des appels à la base (30 s).
DEFAULT_TIMEOUT = 180

# Durée de validité d'une URL signée. Le lecteur ouvre le flux une fois et le
# garde ; une heure couvre largement l'écoute d'un titre, y compris reprise.
PRESIGNED_SECONDS = 3600

# Préfixe des objets dans le seau. Tout ce que l'application y écrit vit
# dessous : le seau reste utilisable pour autre chose.
DEFAULT_PREFIX = "deekeep/"

# Offre gratuite des deux hébergeurs retenus, pour la jauge d'occupation.
# Indicatif : c'est le seau qui fait foi, pas cette constante.
FREE_TIER_BYTES = 10 * 1000 ** 3


class BlobError(RuntimeError):
    """Échec d'un échange avec le seau de stockage."""


class NotConfigured(BlobError):
    """Aucun seau n'est enregistré."""


# ----------------------------------------------------------------------
# Identifiants
# ----------------------------------------------------------------------

def credentials_path():
    return os.path.join(app_settings.data_dir(), CREDENTIALS_FILENAME)


def load_credentials():
    """Identifiants du seau : point d'entrée, région, nom, clés, préfixe."""
    data = {"endpoint": "", "region": "auto", "bucket": "",
            "access_key": "", "secret_key": "", "prefix": DEFAULT_PREFIX}
    path = credentials_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                for key in data:
                    if stored.get(key):
                        data[key] = str(stored[key])
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[blob_store] Lecture de {path} impossible : {exc}")

    for key, variable in (("endpoint", "DEEKEEP_S3_ENDPOINT"),
                          ("region", "DEEKEEP_S3_REGION"),
                          ("bucket", "DEEKEEP_S3_BUCKET"),
                          ("access_key", "DEEKEEP_S3_KEY"),
                          ("secret_key", "DEEKEEP_S3_SECRET"),
                          ("prefix", "DEEKEEP_S3_PREFIX")):
        data[key] = os.environ.get(variable) or data[key]
    return data


def save_credentials(endpoint, region, bucket, access_key, secret_key,
                     prefix=DEFAULT_PREFIX):
    """Écrit les identifiants dans le dossier de données (écriture atomique)."""
    path = credentials_path()
    tmp_path = path + ".tmp"
    payload = {
        "endpoint": normalize_endpoint(endpoint),
        "region": (region or "auto").strip(),
        "bucket": (bucket or "").strip(),
        "access_key": (access_key or "").strip(),
        "secret_key": (secret_key or "").strip(),
        "prefix": normalize_prefix(prefix),
    }
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    # La clé secrète donne un accès complet au seau : le fichier ne doit pas
    # être lisible par les autres comptes de la machine.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def is_configured():
    creds = load_credentials()
    return bool(creds["endpoint"] and creds["bucket"]
                and creds["access_key"] and creds["secret_key"])


def normalize_endpoint(endpoint):
    """Ramène un point d'entrée à la forme ``https://hôte``, sans chemin."""
    value = (endpoint or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    scheme, _, rest = value.partition("://")
    host = rest.split("/", 1)[0]
    return f"{'https' if scheme != 'http' else 'http'}://{host}"


def normalize_prefix(prefix):
    """Préfixe d'objet : sans « / » de tête, avec « / » de queue."""
    value = (prefix or "").strip().strip("/")
    return (value + "/") if value else ""


# ----------------------------------------------------------------------
# Signature AWS SigV4
# ----------------------------------------------------------------------

def _quote_key(key):
    """Encodage d'un chemin d'objet : les « / » restent des séparateurs."""
    return quote(key, safe="/~")


def _quote_value(value):
    """Encodage d'une valeur de paramètre (les « / » y sont encodés)."""
    return quote(str(value), safe="~")


def _sign(key, message):
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret, datestamp, region):
    key = _sign(("AWS4" + secret).encode("utf-8"), datestamp)
    key = _sign(key, region)
    key = _sign(key, SERVICE)
    return _sign(key, "aws4_request")


def _now():
    """Horodatage de la signature : (AAAAMMJJTHHMMSSZ, AAAAMMJJ), en UTC."""
    stamp = datetime.datetime.now(datetime.timezone.utc)
    return stamp.strftime("%Y%m%dT%H%M%SZ"), stamp.strftime("%Y%m%d")


class BlobStore:
    """Client d'un seau compatible S3 : envoi, lecture, listage, effacement."""

    def __init__(self, credentials=None):
        creds = credentials or load_credentials()
        self.endpoint = normalize_endpoint(creds.get("endpoint"))
        self.region = (creds.get("region") or "auto").strip()
        self.bucket = (creds.get("bucket") or "").strip()
        self.access_key = (creds.get("access_key") or "").strip()
        self.secret_key = (creds.get("secret_key") or "").strip()
        self.prefix = normalize_prefix(creds.get("prefix"))
        if not (self.endpoint and self.bucket
                and self.access_key and self.secret_key):
            raise NotConfigured(
                "Aucun seau de stockage n'est configuré : renseignez le point "
                "d'entrée, le nom du seau et les deux clés d'accès."
            )
        self.host = self.endpoint.split("://", 1)[1]
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Signature et envoi
    # ------------------------------------------------------------------
    def _canonical_query(self, params):
        """Paramètres triés et encodés, forme exigée par la signature."""
        if not params:
            return ""
        items = sorted((_quote_value(name), _quote_value(value))
                       for name, value in params.items())
        return "&".join(f"{name}={value}" for name, value in items)

    def _request(self, method, key="", params=None, body=b"", headers=None,
                 timeout=DEFAULT_TIMEOUT, stream=False):
        """Requête signée. Rend la réponse ``requests`` telle quelle."""
        amz_date, datestamp = _now()
        payload_hash = hashlib.sha256(body or b"").hexdigest()
        path = "/" + _quote_key(self.bucket)
        if key:
            path += "/" + _quote_key(key)

        signed = {
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        for name, value in (headers or {}).items():
            signed[name.lower()] = str(value)

        canonical_headers = "".join(
            f"{name}:{signed[name].strip()}\n" for name in sorted(signed))
        signed_headers = ";".join(sorted(signed))
        canonical_request = "\n".join([
            method, path, self._canonical_query(params),
            canonical_headers, signed_headers, payload_hash,
        ])

        scope = f"{datestamp}/{self.region}/{SERVICE}/aws4_request"
        to_sign = "\n".join([
            ALGORITHM, amz_date, scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signature = hmac.new(
            _signing_key(self.secret_key, datestamp, self.region),
            to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        sent = dict(signed)
        sent.pop("host", None)          # posé par requests lui-même
        sent["Authorization"] = (
            f"{ALGORITHM} Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        url = self.endpoint + path
        try:
            return self._session.request(
                method, url, params=params, data=body, headers=sent,
                timeout=timeout, stream=stream)
        except requests.RequestException as exc:
            raise BlobError(f"Seau injoignable : {exc}") from exc

    @staticmethod
    def _fail(response, action):
        """Transforme une réponse d'erreur en ``BlobError`` lisible."""
        detail = ""
        try:
            root = ElementTree.fromstring(response.content or b"")
            parts = []
            for tag in ("Code", "Message"):
                found = root.find(".//" + tag)
                if found is not None and found.text:
                    parts.append(found.text)
            detail = " — ".join(parts)
        except ElementTree.ParseError:
            detail = (response.text or "")[:200]
        raise BlobError(f"{action} : HTTP {response.status_code}"
                        + (f" ({detail})" if detail else ""))

    # ------------------------------------------------------------------
    # Opérations
    # ------------------------------------------------------------------
    def put(self, key, data, content_type="application/octet-stream"):
        """Dépose un objet. Rend son ETag."""
        response = self._request(
            "PUT", key, body=data,
            headers={"content-type": content_type,
                     "content-length": str(len(data))})
        if response.status_code not in (200, 201):
            self._fail(response, f"Envoi de {key} refusé")
        return (response.headers.get("ETag") or "").strip('"')

    def head(self, key):
        """Taille et ETag d'un objet, ou None s'il est absent."""
        response = self._request("HEAD", key, timeout=30)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            self._fail(response, f"Lecture de {key} refusée")
        return {
            "bytes": int(response.headers.get("Content-Length") or 0),
            "etag": (response.headers.get("ETag") or "").strip('"'),
        }

    def get(self, key):
        """Contenu d'un objet (octets). Lève ``BlobError`` s'il est absent."""
        response = self._request("GET", key)
        if response.status_code != 200:
            self._fail(response, f"Lecture de {key} refusée")
        return response.content

    def download(self, key, destination, chunk=1 << 20):
        """Écrit un objet dans un fichier. Rend le nombre d'octets écrits.

        Le contenu arrive par tranches, et non d'un bloc comme dans ``get``
        : une restauration enchaîne des milliers de titres de plusieurs
        mégaoctets, et les tenir en mémoire l'un après l'autre n'apporte
        rien.

        Le fichier n'apparaît à sa place qu'une fois complet — l'écriture se
        fait dans un temporaire du **même dossier**, remplacé d'un coup à la
        fin. Une restauration interrompue, un réseau qui lâche : le disque
        ne garde pas de moignon qui passerait pour un titre.
        """
        response = self._request("GET", key, stream=True)
        if response.status_code != 200:
            self._fail(response, f"Lecture de {key} refusée")

        folder = os.path.dirname(destination) or "."
        handle, temporary = tempfile.mkstemp(prefix=".dk_", dir=folder)
        written = 0
        try:
            with os.fdopen(handle, "wb") as output:
                for block in response.iter_content(chunk_size=chunk):
                    if block:
                        output.write(block)
                        written += len(block)
            os.replace(temporary, destination)
        except requests.RequestException as exc:
            _discard(temporary)
            raise BlobError(f"Téléchargement de {key} interrompu : {exc}") from exc
        except BaseException:
            # Toute autre sortie — disque plein, arrêt demandé : le
            # temporaire ne doit pas survivre à l'échec.
            _discard(temporary)
            raise
        finally:
            response.close()
        return written

    def delete(self, key):
        """Efface un objet. Un objet déjà absent n'est pas une erreur."""
        response = self._request("DELETE", key, timeout=30)
        if response.status_code not in (200, 204, 404):
            self._fail(response, f"Effacement de {key} refusé")
        return True

    def iter_objects(self, prefix=None, max_keys=1000):
        """Parcourt les objets du seau : (clé, taille), page après page."""
        token = None
        while True:
            params = {"list-type": "2", "max-keys": str(max_keys)}
            wanted = self.prefix if prefix is None else prefix
            if wanted:
                params["prefix"] = wanted
            if token:
                params["continuation-token"] = token
            response = self._request("GET", params=params, timeout=60)
            if response.status_code != 200:
                self._fail(response, "Listage du seau refusé")
            try:
                root = ElementTree.fromstring(response.content or b"")
            except ElementTree.ParseError as exc:
                raise BlobError(f"Réponse illisible au listage : {exc}") from exc

            for node in root:
                if _tag(node) != "Contents":
                    continue
                key, size = "", 0
                for field in node:
                    if _tag(field) == "Key":
                        key = field.text or ""
                    elif _tag(field) == "Size":
                        size = int(field.text or 0)
                if key:
                    yield key, size

            token = ""
            for node in root:
                if _tag(node) == "NextContinuationToken":
                    token = node.text or ""
            if not token:
                return

    def usage(self, prefix=None):
        """(nombre d'objets, octets occupés) sous le préfixe de DeeKeep."""
        count = total = 0
        for _key, size in self.iter_objects(prefix):
            count += 1
            total += size
        return count, total

    def check(self):
        """Vérifie identifiants et existence du seau, sans rien écrire."""
        response = self._request(
            "GET", params={"list-type": "2", "max-keys": "1"}, timeout=30)
        if response.status_code != 200:
            self._fail(response, "Connexion au seau refusée")
        return True

    def presigned_get(self, key, expires=PRESIGNED_SECONDS):
        """URL temporaire de lecture directe — c'est elle que joue le lecteur.

        Le flux est servi par l'hébergeur, avec les requêtes ``Range`` : le
        titre démarre sans attendre son téléchargement complet, et le
        curseur de position reste utilisable.
        """
        amz_date, datestamp = _now()
        scope = f"{datestamp}/{self.region}/{SERVICE}/aws4_request"
        path = "/" + _quote_key(self.bucket) + "/" + _quote_key(key)
        params = {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(int(expires)),
            "X-Amz-SignedHeaders": "host",
        }
        query = self._canonical_query(params)
        canonical_request = "\n".join([
            "GET", path, query, f"host:{self.host}\n", "host",
            "UNSIGNED-PAYLOAD",
        ])
        to_sign = "\n".join([
            ALGORITHM, amz_date, scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signature = hmac.new(
            _signing_key(self.secret_key, datestamp, self.region),
            to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{self.endpoint}{path}?{query}&X-Amz-Signature={signature}"


def _discard(path):
    """Efface un fichier temporaire, sans se plaindre s'il a déjà disparu."""
    try:
        os.remove(path)
    except OSError:
        pass


def _tag(node):
    """Nom d'une balise XML, espace de noms retiré."""
    return node.tag.rsplit("}", 1)[-1]


def open_store():
    """Client du seau enregistré, ou ``NotConfigured`` s'il n'y en a pas."""
    return BlobStore()
