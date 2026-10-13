"""
Couche Supabase pour AquaClient.

- Crée les tables (schema.sql) si absentes, via Postgres (mot de passe DB).
- Lit / écrit les mêmes tables que les CSV (clients, notes, fournitures, analyses, visites).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("aquaclient.supabase")

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

TABLE_BY_CSV = {
    "clients.csv": "clients",
    "notes.csv": "notes",
    "fournitures.csv": "fournitures",
    "analyses.csv": "analyses",
    "visites.csv": "visites",
}

INT_FIELDS = {"id", "client_id"}
FLOAT_FIELDS = {"latitude", "longitude"}

_client = None
_enabled = False
_ready = False


def configured() -> bool:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (
        os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_SECRET_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or ""
    ).strip()
    return bool(url and key)


def enabled() -> bool:
    return _enabled and _ready


def table_for_path(path: Path) -> str | None:
    return TABLE_BY_CSV.get(path.name)


def _api_key() -> str:
    return (
        os.getenv("SUPABASE_SECRET_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or ""
    ).strip()


def _project_ref() -> str:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    m = re.match(r"https?://([a-z0-9]+)\.supabase\.co", url, re.I)
    return m.group(1) if m else ""


def _db_urls() -> list[str]:
    """Chaînes Postgres possibles (direct + pooler multi-shards)."""
    explicit = (os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or "").strip()
    urls: list[str] = []
    if explicit:
        urls.append(explicit)

    password = (os.getenv("SUPABASE_DB_PASSWORD") or os.getenv("SUPABASE_DB_PASS") or "").strip()
    if not password:
        return urls

    ref = _project_ref()
    if not ref:
        return urls

    from urllib.parse import quote_plus

    pw = quote_plus(password)
    # Direct (souvent IPv6 only)
    urls.append(f"postgresql://postgres:{pw}@db.{ref}.supabase.co:5432/postgres")
    # Pooler shared (IPv4) — shards aws-0/1/2 × régions courantes
    shards = ["aws-1", "aws-0", "aws-2"]
    regions = [
        "eu-west-1", "eu-west-3", "eu-central-1", "eu-west-2", "eu-north-1",
        "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    ]
    for shard in shards:
        for region in regions:
            host = f"{shard}-{region}.pooler.supabase.com"
            urls.append(f"postgresql://postgres.{ref}:{pw}@{host}:6543/postgres")
            urls.append(f"postgresql://postgres.{ref}:{pw}@{host}:5432/postgres")
    return urls


def get_client():
    global _client
    if _client is not None:
        return _client
    from supabase import create_client

    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = _api_key()
    _client = create_client(url, key)
    return _client


def _secret_key() -> str:
    return (
        os.getenv("SUPABASE_SECRET_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or ""
    ).strip()


def has_service_role() -> bool:
    return bool(_secret_key())


_admin_client = None


def get_admin_client():
    """Client avec clé secrète (service_role) pour Auth Admin."""
    global _admin_client
    secret = _secret_key()
    if not secret:
        return None
    if _admin_client is not None:
        return _admin_client
    from supabase import create_client

    url = (os.getenv("SUPABASE_URL") or "").strip()
    _admin_client = create_client(url, secret)
    return _admin_client


def phone_to_e164(telephone: str) -> str:
    """Normalise un numéro FR vers +33…"""
    raw = re.sub(r"[^\d+]", "", (telephone or "").strip())
    if not raw:
        return ""
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if raw.startswith("+"):
        return raw
    if raw.startswith("0") and len(raw) >= 10:
        return "+33" + raw[1:]
    if raw.startswith("33"):
        return "+" + raw
    return "+" + raw if raw else ""


def create_auth_user(
    *,
    email: str = "",
    telephone: str = "",
    password: str,
    identifiant: str = "",
    prenom: str = "",
    nom: str = "",
    client_id: int | None = None,
) -> str:
    """Crée un user dans Supabase Auth (Authentication → Users). Retourne l'UID."""
    admin = get_admin_client()
    if admin is None:
        raise RuntimeError(
            "SUPABASE_SECRET_KEY manquante. "
            "Supabase → Project Settings → API → secret / service_role, "
            "puis SUPABASE_SECRET_KEY=… dans .env"
        )

    meta: dict[str, Any] = {
        "identifiant": identifiant,
        "prenom": prenom,
        "nom": nom,
        "role": "client",
    }
    if client_id is not None:
        meta["client_id"] = client_id

    payload: dict[str, Any] = {
        "password": password,
        "email_confirm": True,
        "phone_confirm": True,
        "user_metadata": meta,
    }
    if email:
        payload["email"] = email
    phone = phone_to_e164(telephone)
    if phone:
        payload["phone"] = phone
    if not email and not phone:
        raise RuntimeError("E-mail ou téléphone requis pour Auth Supabase.")

    try:
        resp = admin.auth.admin.create_user(payload)
    except Exception as e:
        raise RuntimeError(f"Création Auth Supabase impossible : {e}") from e

    user = getattr(resp, "user", None)
    if user is None and isinstance(resp, dict):
        user = resp.get("user")
    uid = getattr(user, "id", None) if user is not None else None
    if uid is None and isinstance(user, dict):
        uid = user.get("id")
    if not uid:
        raise RuntimeError("Auth Supabase : UID manquant après création.")
    return str(uid)


def update_auth_user(
    auth_uid: str,
    *,
    email: str = "",
    telephone: str = "",
    password: str | None = None,
    identifiant: str = "",
    prenom: str = "",
    nom: str = "",
    client_id: int | None = None,
    disable: bool = False,
) -> None:
    admin = get_admin_client()
    if admin is None:
        raise RuntimeError("SUPABASE_SECRET_KEY manquante pour mettre à jour Auth.")

    attrs: dict[str, Any] = {
        "email_confirm": True,
        "phone_confirm": True,
        "user_metadata": {
            "identifiant": identifiant,
            "prenom": prenom,
            "nom": nom,
            "role": "client",
            **({"client_id": client_id} if client_id is not None else {}),
        },
    }
    if email:
        attrs["email"] = email
    phone = phone_to_e164(telephone)
    if phone:
        attrs["phone"] = phone
    if password:
        attrs["password"] = password
    if disable:
        attrs["ban_duration"] = "876000h"
    else:
        attrs["ban_duration"] = "none"
    try:
        admin.auth.admin.update_user_by_id(auth_uid, attrs)
    except Exception as e:
        raise RuntimeError(f"Mise à jour Auth impossible : {e}") from e


def delete_auth_user(auth_uid: str) -> None:
    if not auth_uid:
        return
    admin = get_admin_client()
    if admin is None:
        logger.warning("Impossible de supprimer Auth %s : pas de clé secrète", auth_uid)
        return
    try:
        admin.auth.admin.delete_user(auth_uid)
    except Exception as e:
        logger.warning("Suppression Auth %s : %s", auth_uid, e)


def _row_out(row: dict[str, Any], fields: list[str] | None = None) -> dict[str, str]:
    keys = fields or list(row.keys())
    out: dict[str, str] = {}
    for k in keys:
        v = row.get(k, "")
        if v is None:
            out[k] = ""
        else:
            out[k] = str(v)
    return out


def _row_in(row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in fields:
        v = row.get(k, "")
        if v is None or v == "":
            if k in INT_FIELDS or k in FLOAT_FIELDS:
                out[k] = None
            else:
                out[k] = ""
            continue
        if k in INT_FIELDS:
            try:
                out[k] = int(float(str(v)))
            except ValueError:
                out[k] = None
        elif k in FLOAT_FIELDS:
            try:
                out[k] = float(str(v))
            except ValueError:
                out[k] = None
        else:
            out[k] = str(v)
    return out


def apply_schema() -> bool:
    """Exécute schema.sql via Postgres. Retourne True si OK."""
    urls = _db_urls()
    if not urls:
        logger.warning(
            "SUPABASE_DB_PASSWORD (ou DATABASE_URL) manquant : "
            "impossible de créer les tables automatiquement. "
            "Ajoute le mot de passe DB (Settings → Database) dans .env"
        )
        return False

    raw = SCHEMA_PATH.read_text(encoding="utf-8")
    # Retirer commentaires ligne, découper en statements
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    script = "\n".join(lines)
    statements = [s.strip() for s in script.split(";") if s.strip()]

    last_err: Exception | None = None

    try:
        import psycopg
    except ImportError as e:
        logger.error("psycopg non installé : %s", e)
        return False

    for url in urls:
        try:
            with psycopg.connect(url, connect_timeout=12) as conn:
                with conn.cursor() as cur:
                    for stmt in statements:
                        cur.execute(stmt)
                conn.commit()
            logger.info("Schéma Supabase appliqué (%s)", url.split("@")[-1])
            return True
        except Exception as e:
            last_err = e
            logger.debug("Connexion Postgres échouée (%s) : %s", url.split("@")[-1], e)

    logger.error("Échec création schéma Supabase : %s", last_err)
    return False


def tables_exist() -> bool:
    try:
        sb = get_client()
        sb.table("clients").select("id").limit(1).execute()
        return True
    except Exception as e:
        msg = str(e).lower()
        if "could not find the table" in msg or "pgrst205" in msg or "42p01" in msg:
            return False
        # Autre erreur (réseau, clé…) : on considère non prêt
        logger.warning("Vérification tables Supabase : %s", e)
        return False


def read_table(table: str, fields: list[str] | None = None) -> list[dict[str, str]]:
    sb = get_client()
    resp = sb.table(table).select("*").order("id").execute()
    rows = resp.data or []
    return [_row_out(r, fields) for r in rows]


def write_table(table: str, fields: list[str], rows: list[dict[str, Any]]) -> None:
    """Upsert les lignes + suppression des ids absents (sans truncate destructif)."""
    sb = get_client()
    existing = read_table(table, ["id"])
    old_ids = set()
    for r in existing:
        try:
            old_ids.add(int(r["id"]))
        except (TypeError, ValueError):
            pass

    payload = [_row_in(r, fields) for r in rows]
    payload = [r for r in payload if r.get("id") is not None]
    new_ids = {int(r["id"]) for r in payload}

    to_delete = sorted(old_ids - new_ids)
    for rid in to_delete:
        sb.table(table).delete().eq("id", rid).execute()

    if not payload:
        return
    batch = 200
    for i in range(0, len(payload), batch):
        chunk = payload[i : i + batch]
        sb.table(table).upsert(chunk).execute()


def seed_from_csv_if_empty(
    csv_readers: dict[str, tuple[Path, list[str], Any]],
) -> None:
    """
    csv_readers: table -> (path, fields, read_csv_fn)
    Importe les CSV locaux si la table Supabase est vide.
    """
    for table, (path, fields, read_fn) in csv_readers.items():
        try:
            existing = read_table(table, fields)
        except Exception as e:
            logger.warning("Lecture %s pour seed : %s", table, e)
            continue
        if existing:
            continue
        local = read_fn(path) if path.exists() else []
        if not local:
            continue
        logger.info("Import CSV → Supabase : %s (%s lignes)", table, len(local))
        write_table(table, fields, local)


def init_supabase(
    csv_seed: dict[str, tuple[Path, list[str], Any]] | None = None,
) -> bool:
    """
    Initialise Supabase au démarrage.
    Retourne True si lecture/écriture passent par Supabase.
    """
    global _enabled, _ready
    _enabled = False
    _ready = False

    if not configured():
        logger.info("Supabase non configuré → stockage CSV local")
        return False

    try:
        get_client()
    except Exception as e:
        logger.error("Client Supabase impossible : %s", e)
        return False

    if not tables_exist():
        logger.info("Tables absentes → création du schéma…")
        if not apply_schema():
            logger.error(
                "Tables Supabase introuvables. "
                "Renseigne SUPABASE_DB_PASSWORD dans .env "
                "ou exécute schema.sql dans le SQL Editor Supabase."
            )
            return False
        # Petit délai / re-check (cache PostgREST)
        if not tables_exist():
            # Force un reload du schéma côté API parfois nécessaire
            logger.warning(
                "Tables créées mais pas encore visibles via l'API. "
                "Attends quelques secondes ou recharge le schéma API "
                "dans Supabase (Settings → API)."
            )
            # On tente quand même
            pass

    _enabled = True
    _ready = True

    if csv_seed:
        try:
            # Seed dans l'ordre (clients d'abord pour les FK)
            order = ["clients", "notes", "analyses", "fournitures", "visites"]
            ordered = {k: csv_seed[k] for k in order if k in csv_seed}
            seed_from_csv_if_empty(ordered)
        except Exception as e:
            logger.warning("Seed CSV → Supabase : %s", e)

    logger.info("Stockage actif : Supabase (%s)", (os.getenv("SUPABASE_URL") or "").strip())
    return True
