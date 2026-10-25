"""
Accès à la base DeeKeep en ligne (Turso / libSQL).

Le service retenu est Turso : offre gratuite sans carte bancaire
(5 Go de stockage, 500 millions de lignes lues et 10 millions de lignes
écrites par mois), et surtout une base au dialecte SQLite pilotable en
HTTP. Deux conséquences pratiques :

  * aucune dépendance nouvelle — un POST JSON avec ``requests``, déjà
    utilisé pour Deezer et MusicBrainz, suffit à tout faire, création des
    tables comprise (une base PostgreSQL hébergée aurait imposé de coller
    le schéma à la main dans une console web, ou d'embarquer un pilote) ;
  * le cache local est un simple fichier SQLite au même schéma, donc les
    mêmes requêtes des deux côtés (voir ``local_db``).

Protocole : point d'entrée ``POST /v2/pipeline``, décrit à l'adresse
https://docs.turso.tech/sdk/http/reference. Le corps envoyé énumère des
requêtes, chacune portant son SQL et ses paramètres typés ; la réponse
renvoie les colonnes et les lignes, elles aussi typées.

Les identifiants (URL de la base, jeton) ne vivent pas dans les
paramètres de l'application : ceux-ci sont désormais stockés dans la base
elle-même, il faut donc pouvoir s'y connecter avant de les lire. Ils sont
donc rangés à part, dans ``cloud.json`` du dossier de données, et peuvent
être surchargés par les variables d'environnement DEEKEEP_DB_URL et
DEEKEEP_DB_TOKEN.
"""

import base64
import json
import os
import time

import requests

from deekeep.core import settings as app_settings


CREDENTIALS_FILENAME = "cloud.json"

# Au-delà de cette taille, un lot d'instructions est coupé en deux. Les
# jaquettes pèsent de 10 à 200 Ko chacune : sans découpage, un envoi de
# mille images formerait une requête de plusieurs dizaines de Mo, que le
# serveur refuserait et qu'un réseau capricieux perdrait en route.
MAX_REQUEST_BYTES = 1_500_000

# Nombre maximal de paramètres liés dans une seule instruction SQLite.
MAX_BOUND_PARAMS = 900

DEFAULT_TIMEOUT = 30


class CloudError(RuntimeError):
    """Échec d'un échange avec la base en ligne."""


class NotConfigured(CloudError):
    """Aucun identifiant de base en ligne n'est enregistré."""


# ----------------------------------------------------------------------
# Identifiants
# ----------------------------------------------------------------------

def credentials_path():
    return os.path.join(app_settings.data_dir(), CREDENTIALS_FILENAME)


def load_credentials():
    """
    Identifiants de la base en ligne : {"url": ..., "token": ...}.

    Les variables d'environnement DEEKEEP_DB_URL / DEEKEEP_DB_TOKEN sont
    prioritaires : elles permettent d'essayer une autre base sans toucher
    au fichier, et de ne pas écrire de jeton sur le disque.
    """
    data = {"url": "", "token": ""}
    path = credentials_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                data["url"] = str(stored.get("url") or "")
                data["token"] = str(stored.get("token") or "")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[cloud_db] Lecture de {path} impossible : {exc}")

    data["url"] = os.environ.get("DEEKEEP_DB_URL") or data["url"]
    data["token"] = os.environ.get("DEEKEEP_DB_TOKEN") or data["token"]
    return data


def save_credentials(url, token):
    """Écrit les identifiants dans le dossier de données (écriture atomique)."""
    path = credentials_path()
    tmp_path = path + ".tmp"
    payload = {"url": (url or "").strip(), "token": (token or "").strip()}
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    # Le jeton donne un accès complet à la base : le fichier ne doit pas
    # être lisible par les autres comptes de la machine.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def is_configured():
    creds = load_credentials()
    return bool(creds["url"] and creds["token"])


def normalize_url(url):
    """
    Ramène une URL de base à sa forme HTTPS.

    Les consoles Turso donnent l'URL sous la forme
    ``libsql://ma-base-mon-org.turso.io`` ; le point d'entrée HTTP est le
    même hôte en https. Le suffixe ``/v2/pipeline`` est ajouté ici pour
    que l'appelant n'ait pas à s'en soucier.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    elif url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    elif not url.startswith("https://"):
        url = "https://" + url
    if url.endswith("/v2/pipeline"):
        return url
    return url + "/v2/pipeline"


# ----------------------------------------------------------------------
# Encodage des valeurs (protocole Hrana)
# ----------------------------------------------------------------------

def encode_value(value):
    """
    Convertit une valeur Python en argument typé du protocole.

    Entiers et flottants voyagent sous forme de chaîne : c'est ce que
    prévoit le protocole pour ne rien perdre des 64 bits d'un entier ni
    des décimales d'un flottant au passage par JSON. Les images sont
    transmises en base64.
    """
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "blob",
                "base64": base64.b64encode(bytes(value)).decode("ascii")}
    return {"type": "text", "value": str(value)}


def decode_value(cell):
    """Convertit une valeur reçue du serveur en valeur Python."""
    if cell is None:
        return None
    kind = cell.get("type")
    if kind in (None, "null"):
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    if kind == "blob":
        raw = cell.get("base64") or ""
        # Le serveur omet parfois le remplissage « = » de la base64.
        raw += "=" * (-len(raw) % 4)
        return base64.b64decode(raw)
    return cell.get("value")


class Result:
    """Résultat d'une instruction : colonnes, lignes, compteurs."""

    def __init__(self, columns, rows, affected=0, last_rowid=None):
        self.columns = columns
        self.rows = rows
        self.affected = affected
        self.last_rowid = last_rowid

    def dicts(self):
        return [dict(zip(self.columns, row)) for row in self.rows]

    def scalar(self, default=None):
        if self.rows and self.rows[0]:
            return self.rows[0][0]
        return default

    def __len__(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------

class TursoClient:
    """
    Client HTTP de la base en ligne.

    Utilisation :
        client = TursoClient.from_credentials()
        client.execute("SELECT COUNT(*) FROM tracks")
        client.batch([("INSERT ...", [1, "a"]), ...])
    """

    def __init__(self, url, token, timeout=DEFAULT_TIMEOUT, retries=3):
        self.endpoint = normalize_url(url)
        self.token = (token or "").strip()
        self.timeout = timeout
        self.retries = retries
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })
        # Compteurs d'usage, utiles pour rester dans l'offre gratuite.
        self.rows_read = 0
        self.rows_written = 0

    @classmethod
    def from_credentials(cls, **kwargs):
        creds = load_credentials()
        if not creds["url"] or not creds["token"]:
            raise NotConfigured(
                "Aucune base en ligne configurée : renseignez l'URL et le "
                "jeton dans Paramètres > Base de données."
            )
        return cls(creds["url"], creds["token"], **kwargs)

    # -- transport ------------------------------------------------------

    def _post(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error = None
        for attempt in range(self.retries):
            try:
                response = self._session.post(
                    self.endpoint, data=body, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = CloudError(f"Base en ligne injoignable : {exc}")
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        last_error = CloudError(f"Réponse illisible : {exc}")
                elif response.status_code in (401, 403):
                    # Un jeton refusé ne s'arrangera pas en réessayant.
                    raise CloudError(
                        "Jeton refusé par la base en ligne (401/403) : "
                        "vérifiez l'URL et le jeton d'accès."
                    )
                elif response.status_code == 404:
                    raise CloudError(
                        f"Base introuvable à l'adresse {self.endpoint} (404)."
                    )
                elif response.status_code in (413, 414):
                    raise _TooLarge(f"Requête trop volumineuse ({response.status_code}).")
                else:
                    last_error = CloudError(
                        f"Erreur {response.status_code} : "
                        f"{response.text[:300]}"
                    )
            # Attente croissante : 0,5 s puis 1,5 s.
            if attempt < self.retries - 1:
                time.sleep(0.5 * (attempt + 1) ** 2)
        raise last_error or CloudError("Échec inconnu de la requête.")

    def _run(self, statements):
        """
        Exécute une liste de (sql, params) en un seul aller-retour.

        Toutes les instructions du lot partagent la même connexion, la
        requête ``close`` finale la libérant côté serveur.
        """
        requests_payload = []
        for sql, params in statements:
            stmt = {"sql": sql}
            if params:
                stmt["args"] = [encode_value(value) for value in params]
            requests_payload.append({"type": "execute", "stmt": stmt})
        requests_payload.append({"type": "close"})

        data = self._post({"requests": requests_payload})
        results = []
        for index, entry in enumerate(data.get("results", [])):
            if entry.get("type") == "error":
                error = entry.get("error", {})
                message = error.get("message", "erreur inconnue")
                sql = statements[index][0] if index < len(statements) else ""
                raise CloudError(f"{message}\n  SQL : {sql[:200]}")
            response = entry.get("response") or {}
            if response.get("type") != "execute":
                continue
            result = response.get("result") or {}
            columns = [col.get("name") for col in result.get("cols", [])]
            rows = [[decode_value(cell) for cell in row]
                    for row in result.get("rows", [])]
            self.rows_read += int(result.get("rows_read") or 0)
            self.rows_written += int(result.get("rows_written") or 0)
            results.append(Result(
                columns, rows,
                affected=int(result.get("affected_row_count") or 0),
                last_rowid=result.get("last_insert_rowid"),
            ))
        return results

    # -- API ------------------------------------------------------------

    def execute(self, sql, params=None):
        """Exécute une instruction et renvoie son résultat."""
        results = self._run([(sql, params or [])])
        return results[0] if results else Result([], [])

    def batch(self, statements, progress=None):
        """
        Exécute une longue liste d'instructions, découpée en lots.

        ``statements`` est une liste de (sql, params). Le découpage suit le
        poids réel des paramètres — un lot de jaquettes atteint la limite
        en quelques dizaines de lignes là où un lot de titres en accepte
        des milliers. ``progress`` est appelé avec le nombre
        d'instructions exécutées.
        """
        done = 0
        chunk, chunk_bytes = [], 0
        for sql, params in statements:
            weight = len(sql) + _params_weight(params)
            if chunk and chunk_bytes + weight > MAX_REQUEST_BYTES:
                done += self._flush(chunk, progress, done)
                chunk, chunk_bytes = [], 0
            chunk.append((sql, params or []))
            chunk_bytes += weight
        if chunk:
            done += self._flush(chunk, progress, done)
        return done

    def _flush(self, chunk, progress, already_done):
        """Envoie un lot, en le coupant en deux si le serveur le refuse."""
        try:
            self._run(chunk)
        except _TooLarge:
            if len(chunk) == 1:
                raise CloudError(
                    "Une seule ligne dépasse la taille acceptée par le "
                    "serveur ; jaquette trop lourde ?"
                )
            middle = len(chunk) // 2
            first = self._flush(chunk[:middle], progress, already_done)
            second = self._flush(chunk[middle:], progress, already_done + first)
            return first + second
        if progress is not None:
            progress(already_done + len(chunk))
        return len(chunk)

    def executemany(self, sql, rows, progress=None):
        """Exécute la même instruction pour chaque jeu de paramètres."""
        return self.batch([(sql, params) for params in rows], progress=progress)

    def ping(self):
        """Vrai si la base répond. N'élève pas d'exception."""
        try:
            self.execute("SELECT 1")
            return True
        except CloudError:
            return False

    def table_names(self):
        result = self.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        return [row[0] for row in result.rows]

    def close(self):
        self._session.close()


class _TooLarge(CloudError):
    """Le serveur a refusé la requête pour sa taille (usage interne)."""


def _params_weight(params):
    """Poids approché des paramètres d'une instruction, en octets envoyés."""
    total = 0
    for value in params or []:
        if isinstance(value, (bytes, bytearray, memoryview)):
            # base64 : quatre caractères pour trois octets.
            total += len(value) * 4 // 3 + 32
        elif isinstance(value, str):
            total += len(value.encode("utf-8", "replace")) + 24
        else:
            total += 32
    return total
