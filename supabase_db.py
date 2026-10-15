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
                    ok_core = False
                    for stmt in statements:
                        try:
                            cur.execute(stmt)
                            conn.commit()
                            if "create table" in stmt.lower() and "clients" in stmt.lower():
                                ok_core = True
                        except Exception as stmt_err:
                            conn.rollback()
                            # Storage / policies optionnelles : on continue
                            msg = str(stmt_err).lower()
                            if "storage" in stmt.lower() or "storage" in msg:
                                logger.warning("Schéma Storage (ignoré) : %s", stmt_err)
                                continue
                            if "already exists" in msg or "duplicate" in msg:
                                continue
                            logger.warning("Statement schéma : %s — %s", stmt[:90], stmt_err)
                    if not ok_core:
                        # Au moins les tables métier doivent passer
                        cur.execute("select 1 from public.clients limit 1")
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


def read_row(table: str, row_id: int, fields: list[str] | None = None) -> dict[str, str] | None:
    """Lit une seule ligne par id (rapide)."""
    sb = get_client()
    resp = sb.table(table).select("*").eq("id", row_id).limit(1).execute()
    rows = resp.data or []
    if not rows:
        return None
    return _row_out(rows[0], fields)


def upsert_row(table: str, fields: list[str], row: dict[str, Any]) -> None:
    """Upsert d'une seule ligne — pour les clics checklist / chrono."""
    sb = get_client()
    payload = _row_in(row, fields)
    if payload.get("id") is None:
        raise ValueError("id manquant pour upsert_row")
    sb.table(table).upsert(payload).execute()


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

    url = (os.getenv("SUPABASE_URL") or "").strip()
    has_key = bool(_api_key())
    logger.info(
        "Supabase env : URL=%s KEY=%s SECRET=%s DB_PASS/DATABASE_URL=%s",
        "oui" if url else "non",
        "oui" if has_key else "non",
        "oui" if _secret_key() else "non",
        "oui" if (os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_PASSWORD")) else "non",
    )

    if not configured():
        logger.warning(
            "Supabase non configuré → CSV local. "
            "Sur Render : Environment → ajoute SUPABASE_URL et SUPABASE_KEY "
            "(le fichier .env local n’est pas déployé)."
        )
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
                "Tables Supabase introuvables et schéma non appliqué. "
                "Ajoute SUPABASE_DB_PASSWORD ou DATABASE_URL sur Render, "
                "ou exécute schema.sql dans le SQL Editor Supabase."
            )
            return False
        if not tables_exist():
            logger.warning(
                "Tables créées mais pas encore visibles via l'API. "
                "On active quand même le mode Supabase."
            )

    _enabled = True
    _ready = True

    # Nouvelles tables / colonnes / bucket si absents
    try:
        get_client().table("photos_clients").select("id").limit(1).execute()
    except Exception:
        logger.info("Table photos_clients absente → ré-application schéma…")
        apply_schema()
    try:
        get_client().table("clients").select("piscine_hivernee").limit(1).execute()
    except Exception:
        logger.info("Colonne piscine_hivernee absente → ajout…")
        _ensure_column_via_db(
            "alter table public.clients add column if not exists piscine_hivernee text default '0';"
        )
    try:
        get_client().table("clients").select("hivernage_en_cours").limit(1).execute()
    except Exception:
        logger.info("Colonne hivernage_en_cours absente → ajout…")
        _ensure_column_via_db(
            "alter table public.clients add column if not exists hivernage_en_cours text default '0';"
        )
    try:
        get_client().table("clients").select("options_checklists").limit(1).execute()
    except Exception:
        logger.info("Colonne options_checklists absente → ajout…")
        _ensure_column_via_db(
            "alter table public.clients add column if not exists options_checklists text default '{}';"
        )
    ensure_photos_bucket()

    if csv_seed:
        try:
            order = ["clients", "notes", "analyses", "fournitures", "visites"]
            ordered = {k: csv_seed[k] for k in order if k in csv_seed}
            seed_from_csv_if_empty(ordered)
        except Exception as e:
            logger.warning("Seed CSV → Supabase : %s", e)

    logger.info("Stockage actif : Supabase (%s)", url)
    return True


# --- Photos clients (Storage + métadonnées) ---

PHOTOS_BUCKET = "photos-clients"
PHOTOS_FIELDS = [
    "id", "client_id", "instruction_id", "storage_path",
    "mime_type", "size_bytes", "width", "height", "created_at",
]


def _storage_sb():
    """Préfère la clé secrète pour le bucket privé."""
    return get_admin_client() or get_client()


def _ensure_column_via_db(sql: str, *, max_urls: int = 6) -> bool:
    """Exécute un ALTER rapide via Postgres (évite le schéma complet)."""
    urls = _db_urls()[:max_urls]
    if not urls:
        return False
    try:
        import psycopg
    except ImportError:
        return False
    for url in urls:
        try:
            with psycopg.connect(url, connect_timeout=8) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                conn.commit()
            logger.info("SQL OK via Postgres (%s)", url.split("@")[-1])
            return True
        except Exception as e:
            logger.debug("SQL via DB échoué (%s) : %s", url.split("@")[-1], e)
    return False


def _ensure_photos_bucket_via_db() -> bool:
    """Crée le bucket privé via Postgres (fonctionne sans service_role)."""
    sql = """
    insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
    values (
      'photos-clients',
      'photos-clients',
      false,
      10485760,
      array['image/jpeg', 'image/png', 'image/webp']
    )
    on conflict (id) do update set
      public = excluded.public,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;
    """
    ok = _ensure_column_via_db(sql)
    if ok:
        logger.info("Bucket Storage assuré via Postgres : %s", PHOTOS_BUCKET)
    return ok


def ensure_photos_bucket() -> None:
    """Assure le bucket privé (Postgres de préférence — fiable sans service_role)."""
    if not enabled():
        return
    if _ensure_photos_bucket_via_db():
        return
    try:
        sb = _storage_sb()
        existing = sb.storage.list_buckets()
        names = set()
        for b in existing or []:
            name = getattr(b, "name", None) or getattr(b, "id", None)
            if isinstance(b, dict):
                name = b.get("name") or b.get("id")
            if name:
                names.add(str(name))
        if PHOTOS_BUCKET in names:
            return
        sb.storage.create_bucket(
            PHOTOS_BUCKET,
            options={
                "public": False,
                "file_size_limit": 10 * 1024 * 1024,
                "allowed_mime_types": ["image/jpeg", "image/png", "image/webp"],
            },
        )
        logger.info("Bucket Storage créé : %s", PHOTOS_BUCKET)
    except Exception as e:
        logger.warning(
            "Bucket %s non créé (%s). Ajoute SUPABASE_SECRET_KEY ou exécute schema.sql.",
            PHOTOS_BUCKET,
            e,
        )


def list_photos(client_id: int | None = None, instruction_id: str | None = None) -> list[dict[str, Any]]:
    sb = get_client()
    q = sb.table("photos_clients").select("*").order("created_at")
    if client_id is not None:
        q = q.eq("client_id", client_id)
    if instruction_id:
        q = q.eq("instruction_id", instruction_id)
    resp = q.execute()
    return list(resp.data or [])


def get_photo(photo_id: str) -> dict[str, Any] | None:
    sb = get_client()
    resp = sb.table("photos_clients").select("*").eq("id", photo_id).limit(1).execute()
    rows = resp.data or []
    return rows[0] if rows else None


def insert_photo_meta(row: dict[str, Any]) -> dict[str, Any]:
    sb = get_client()
    resp = sb.table("photos_clients").insert(row).execute()
    data = resp.data or []
    return data[0] if data else row


def delete_photo_meta(photo_id: str) -> None:
    sb = get_client()
    sb.table("photos_clients").delete().eq("id", photo_id).execute()


def upload_photo_bytes(storage_path: str, data: bytes, content_type: str = "image/webp") -> None:
    sb = _storage_sb()
    sb.storage.from_(PHOTOS_BUCKET).upload(
        storage_path,
        data,
        file_options={
            "content-type": content_type,
            "upsert": "false",
        },
    )


def download_photo_bytes(storage_path: str) -> bytes:
    sb = _storage_sb()
    return sb.storage.from_(PHOTOS_BUCKET).download(storage_path)


def remove_photo_file(storage_path: str) -> None:
    sb = _storage_sb()
    try:
        sb.storage.from_(PHOTOS_BUCKET).remove([storage_path])
    except Exception as e:
        logger.warning("Suppression Storage %s : %s", storage_path, e)


def delete_photos_for_clients(client_ids: set[int]) -> None:
    if not client_ids or not enabled():
        return
    sb = get_client()
    for cid in client_ids:
        try:
            resp = sb.table("photos_clients").select("id,storage_path").eq("client_id", cid).execute()
            rows = resp.data or []
            paths = [r["storage_path"] for r in rows if r.get("storage_path")]
            if paths:
                try:
                    _storage_sb().storage.from_(PHOTOS_BUCKET).remove(paths)
                except Exception as e:
                    logger.warning("Purge Storage client %s : %s", cid, e)
            sb.table("photos_clients").delete().eq("client_id", cid).execute()
        except Exception as e:
            logger.warning("Purge photos client %s : %s", cid, e)
