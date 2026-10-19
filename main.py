"""
API AquaClient — clients piscine, géoloc, check-up, notes & fournitures.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import supabase_db

load_dotenv(Path(__file__).parent / ".env")
logging.basicConfig(level=logging.INFO)

# Incrémenter à chaque déploiement visible (PC + téléphone / Render doivent afficher la même)
APP_VERSION = "1.8.6"

PHOTOS_BUCKET = "photos-clients"
MAX_PHOTO_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 Mo avant optimisation
MAX_PHOTO_EDGE = 1920
WEBP_QUALITY = 82
ALLOWED_PHOTO_MIMES = {"image/jpeg", "image/png", "image/webp"}

DATA_DIR = Path(__file__).parent / "data"
CLIENTS_CSV = DATA_DIR / "clients.csv"
NOTES_CSV = DATA_DIR / "notes.csv"
FOURNITURES_CSV = DATA_DIR / "fournitures.csv"
ANALYSES_CSV = DATA_DIR / "analyses.csv"
VISITES_CSV = DATA_DIR / "visites.csv"
FACTURES_LEGACY = DATA_DIR / "factures.csv"
PHOTOS_META_CSV = DATA_DIR / "photos_clients.csv"
PHOTOS_DIR = DATA_DIR / "photos"
STATIC_DIR = Path(__file__).parent / "static"

DATA_DIR.mkdir(exist_ok=True)
PHOTOS_DIR.mkdir(exist_ok=True)

PHOTOS_META_FIELDS = [
    "id", "client_id", "instruction_id", "storage_path",
    "mime_type", "size_bytes", "width", "height", "created_at",
]

CLIENT_FIELDS = [
    "id", "nom", "prenom", "adresse", "latitude", "longitude",
    "pompe", "filtre", "robot", "skimmer", "vanne_vidange", "injecteurs", "autres",
    "type_entretien", "frequence_passage", "date_debut_passage",
    "identifiant", "email", "telephone", "password_hash", "compte_actif", "auth_uid",
    "checklist_hivernage", "piscine_hivernee", "hivernage_en_cours", "options_checklists",
]

OPTION_CHECKLIST_KEYS = ("ouverture", "local_technique", "hivernage", "entretien")

DEFAULT_CHECKLIST_HIVERNAGE = [
    "Baisse du niveau d'eau",
    "Vidange des canalisations",
    "Purge pompe / filtre",
    "Produit d'hivernage",
    "Couverture / filet d'hiver",
    "Contrôle local technique",
    "Note de visite",
]
NOTE_FIELDS = ["id", "client_id", "date", "date_ref", "texte"]
FOURNITURE_FIELDS = [
    "id", "client_id", "date", "date_ref", "description", "quantite",
    "envoyee", "payee",
]
ANALYSE_FIELDS = ["id", "client_id", "date", "date_ref", "texte"]
VISITE_FIELDS = [
    "id", "client_id", "date", "type", "checklist_json",
    "debut", "fin", "duree_minutes",
]

RAYON_RECHERCHE_M = 1000

MOIS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    table = supabase_db.table_for_path(path)
    if table and supabase_db.enabled():
        supabase_db.write_table(table, fields, rows)
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def write_one_row(path: Path, fields: list[str], row: dict[str, Any], rows_fallback: list[dict[str, Any]] | None = None) -> None:
    """Persiste une seule ligne (Supabase upsert) — beaucoup plus rapide qu'un rewrite complet."""
    table = supabase_db.table_for_path(path)
    if table and supabase_db.enabled():
        supabase_db.upsert_row(table, fields, row)
        return
    if rows_fallback is not None:
        write_csv(path, fields, rows_fallback)
        return
    rows = read_csv(path)
    rid = int(row["id"])
    replaced = False
    for i, r in enumerate(rows):
        if int(r.get("id") or 0) == rid:
            rows[i] = {k: str(row.get(k, "")) for k in fields}
            replaced = True
            break
    if not replaced:
        rows.append({k: str(row.get(k, "")) for k in fields})
    write_csv(path, fields, rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    table = supabase_db.table_for_path(path)
    if table and supabase_db.enabled():
        return supabase_db.read_table(table, {
            "clients": CLIENT_FIELDS,
            "notes": NOTE_FIELDS,
            "fournitures": FOURNITURE_FIELDS,
            "analyses": ANALYSE_FIELDS,
            "visites": VISITE_FIELDS,
        }.get(table))
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_visite_row(visite_id: int) -> dict[str, str] | None:
    """Chargement ciblé d'une visite (évite de tout relire)."""
    if supabase_db.enabled():
        return supabase_db.read_row("visites", visite_id, VISITE_FIELDS)
    for r in read_csv(VISITES_CSV):
        if int(r["id"]) == visite_id:
            return r
    return None


def _ensure_csv(path: Path, fields: list[str]) -> None:
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


def _migrate_clients_columns() -> None:
    if not CLIENTS_CSV.exists():
        _ensure_csv(CLIENTS_CSV, CLIENT_FIELDS)
        return
    with CLIENTS_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if fieldnames == CLIENT_FIELDS:
        return
    normalized = []
    for r in rows:
        row = {k: r.get(k, "") for k in CLIENT_FIELDS}
        if not (row.get("identifiant") or "").strip():
            row["identifiant"] = "AC-" + secrets.token_hex(3).upper()
        if not (row.get("compte_actif") or "").strip():
            row["compte_actif"] = "1" if (row.get("password_hash") or "").strip() else "0"
        normalized.append(row)
    write_csv(CLIENTS_CSV, CLIENT_FIELDS, normalized)


def _migrate_fournitures() -> None:
    _ensure_csv(FOURNITURES_CSV, FOURNITURE_FIELDS)
    if not FACTURES_LEGACY.exists():
        return
    existing = read_csv(FOURNITURES_CSV)
    if existing:
        return
    legacy = read_csv(FACTURES_LEGACY)
    migrated = []
    for r in legacy:
        migrated.append({
            "id": r.get("id", ""),
            "client_id": r.get("client_id", ""),
            "date": (r.get("date") or "")[:10] if r.get("date") else "",
            "date_ref": r.get("date_ref", ""),
            "description": r.get("description", ""),
            "quantite": r.get("quantite", "1"),
            "envoyee": _norm_oui_non(r.get("envoyee")),
            "payee": _norm_oui_non(r.get("payee")),
        })
    if migrated:
        write_csv(FOURNITURES_CSV, FOURNITURE_FIELDS, migrated)


def _norm_oui_non(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in {"1", "true", "oui", "o", "yes", "y", "vrai"}:
        return "oui"
    return "non"


def _migrate_factures_statut() -> None:
    """Assure les colonnes envoyee / payee sur les fournitures (= factures)."""
    _ensure_csv(FOURNITURES_CSV, FOURNITURE_FIELDS)
    with FOURNITURES_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if fieldnames == FOURNITURE_FIELDS:
        # Normaliser valeurs vides
        changed = False
        for r in rows:
            e = _norm_oui_non(r.get("envoyee"))
            p = _norm_oui_non(r.get("payee"))
            if r.get("envoyee") != e or r.get("payee") != p:
                r["envoyee"] = e
                r["payee"] = p
                changed = True
        if changed:
            write_csv(FOURNITURES_CSV, FOURNITURE_FIELDS, rows)
        return
    normalized = []
    for r in rows:
        row = {k: r.get(k, "") for k in FOURNITURE_FIELDS}
        row["envoyee"] = _norm_oui_non(row.get("envoyee"))
        row["payee"] = _norm_oui_non(row.get("payee"))
        normalized.append(row)
    write_csv(FOURNITURES_CSV, FOURNITURE_FIELDS, normalized)


def _migrate_hist_date_ref() -> None:
    """Ajoute la colonne date_ref (date dite à l'oral) aux historiques."""
    for path, fields in (
        (NOTES_CSV, NOTE_FIELDS),
        (ANALYSES_CSV, ANALYSE_FIELDS),
        (FOURNITURES_CSV, FOURNITURE_FIELDS),
    ):
        _ensure_csv(path, fields)
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        if fieldnames == fields:
            continue
        normalized = [{k: r.get(k, "") for k in fields} for r in rows]
        write_csv(path, fields, normalized)


def _migrate_visites_columns() -> None:
    _ensure_csv(VISITES_CSV, VISITE_FIELDS)
    rows = read_csv(VISITES_CSV)
    with VISITES_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
    if fieldnames == VISITE_FIELDS:
        return
    normalized = [{k: r.get(k, "") for k in VISITE_FIELDS} for r in rows]
    write_csv(VISITES_CSV, VISITE_FIELDS, normalized)


_ensure_csv(NOTES_CSV, NOTE_FIELDS)
_ensure_csv(ANALYSES_CSV, ANALYSE_FIELDS)
_migrate_clients_columns()
_migrate_fournitures()
_migrate_hist_date_ref()
_migrate_factures_statut()
_migrate_visites_columns()

# Supabase : crée les tables si besoin, puis bascule lecture/écriture
_csv_read_local = lambda path: (  # noqa: E731
    list(csv.DictReader(path.open(newline="", encoding="utf-8"))) if path.exists() else []
)
supabase_db.init_supabase(
    csv_seed={
        "clients": (CLIENTS_CSV, CLIENT_FIELDS, _csv_read_local),
        "notes": (NOTES_CSV, NOTE_FIELDS, _csv_read_local),
        "analyses": (ANALYSES_CSV, ANALYSE_FIELDS, _csv_read_local),
        "fournitures": (FOURNITURES_CSV, FOURNITURE_FIELDS, _csv_read_local),
        "visites": (VISITES_CSV, VISITE_FIELDS, _csv_read_local),
    }
)

app = FastAPI(title="AquaClient")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Modèles ──────────────────────────────────────────────────────────────────

class NouveauClient(BaseModel):
    nom: str = Field(min_length=1)
    prenom: str = Field(min_length=1)
    adresse: str = Field(min_length=1)
    latitude: float | None = None
    longitude: float | None = None
    pompe: str = ""
    filtre: str = ""
    robot: str = ""
    skimmer: str = ""
    vanne_vidange: bool = False
    injecteurs: bool = False
    autres: str = ""
    type_entretien: str = "entretien_simple"  # entretien_simple | entretien_complet
    frequence_passage: str = ""  # hebdomadaire | bihebdomadaire | ""
    date_debut_passage: str = ""  # YYYY-MM-DD
    # Compte client (accès futur portail)
    creer_compte: bool = False
    email: str = ""
    telephone: str = ""
    password: str = ""  # clair à l'enregistrement ; stocké hashé
    # Checklist hivernage : libellés ou {id, label} (vide autorisé)
    checklist_hivernage: list[Any] = Field(default_factory=list)
    piscine_hivernee: bool | None = None
    # Checklists par option : { ouverture, local_technique, hivernage, entretien }
    options_checklists: dict[str, Any] | None = None


class ModifierClient(BaseModel):
    nom: str = Field(min_length=1)
    prenom: str = Field(min_length=1)
    adresse: str = Field(min_length=1)
    latitude: float | None = None
    longitude: float | None = None
    pompe: str = ""
    filtre: str = ""
    robot: str = ""
    skimmer: str = ""
    vanne_vidange: bool = False
    injecteurs: bool = False
    autres: str = ""
    type_entretien: str = "entretien_simple"
    frequence_passage: str = ""
    date_debut_passage: str = ""
    creer_compte: bool = False
    email: str = ""
    telephone: str = ""
    password: str = ""  # si non vide → nouveau mot de passe
    checklist_hivernage: list[Any] = Field(default_factory=list)
    piscine_hivernee: bool | None = None
    options_checklists: dict[str, Any] | None = None

class Position(BaseModel):
    latitude: float
    longitude: float


class CommandeIA(BaseModel):
    client_id: int
    texte: str
    intent_force: str | None = None  # note | fourniture | analyse


class PositionClient(BaseModel):
    latitude: float
    longitude: float


class VisiteCreate(BaseModel):
    client_id: int
    type: str  # depannage | entretien_simple | entretien_complet | hivernage


class ChecklistUpdate(BaseModel):
    item_id: str
    done: bool = True


class TexteUpdate(BaseModel):
    texte: str = Field(min_length=1)
    date: str | None = None
    date_ref: str | None = None


class FournitureUpdate(BaseModel):
    description: str = Field(min_length=1)
    quantite: float = 1
    date: str | None = None
    date_ref: str | None = None
    envoyee: bool | None = None
    payee: bool | None = None


class FactureStatutUpdate(BaseModel):
    envoyee: bool | None = None
    payee: bool | None = None


_whisper_model = None


def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise HTTPException(
                status_code=503,
                detail="Module faster-whisper manquant. pip install faster-whisper",
            ) from exc
        # small trop lent/lourd ici ; base est déjà en cache et fiable
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


def corriger_transcription(texte: str) -> str:
    """Corrige les erreurs fréquentes de Whisper sur le vocabulaire métier."""
    t = " ".join((texte or "").split()).strip()
    if not t:
        return t
    # Ponctuation → espaces (Whisper met souvent « Fourniture. 3 bidons »)
    t = re.sub(r"[.:;!?]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    remplacements = [
        (
            r"\b(furniture|formiture|forniture|fournitures?|fourni\s*tures?|"
            r"pour\s*natures?|for\s*natures?|fournir(?:\s*ture)?|pourriture|"
            r"format\s*ure|four\s*nitures?)\b",
            "fourniture",
        ),
        (r"\b(factures?|facturer|facturez)\b", "facture"),
        (r"\b(analyses?|analyser|annalyse|analise|analyce|analyzes?)\b", "analyse"),
        (r"\b(bidons?|biden|bidonnes?|bidonnes)\b", "bidon"),
        (r"\b(chlores?|clore|chlor|clor|klore|chlorez|chlors)\b", "chlore"),
        (r"\b(bromes?)\b", "brome"),
        (r"\b(filtres?|filtrez)\b", "filtre"),
        (r"\b(piscines?)\b", "piscine"),
    ]
    for pat, rep in remplacements:
        t = re.sub(pat, rep, t, flags=re.I)
    t = re.sub(r"^(notre|notes|noter)\b", "note", t, count=1, flags=re.I)

    # Dates orales AVANT le remplacement de « vingt » → 20
    t = preparer_dates_orales(t)

    # Nombres en lettres → chiffres (sauf un/une = articles fréquents)
    # Ne pas retoucher « vingt » déjà géré dans preparer_dates_orales
    nombres = {
        "zero": "0",
        "deux": "2",
        "trois": "3",
        "quatre": "4",
        "cinq": "5",
        "six": "6",
        "sept": "7",
        "huit": "8",
        "neuf": "9",
        "dix": "10",
        "onze": "11",
        "douze": "12",
        "treize": "13",
        "quatorze": "14",
        "quinze": "15",
        "seize": "16",
        # « vingt » géré avant dans preparer_dates_orales
    }
    for mot, chiffre in nombres.items():
        t = re.sub(rf"\b{mot}\b", chiffre, t, flags=re.I)
    # Erreurs Whisper / Web Speech fréquentes pour « 2 » / « 3 »
    # « deux » → souvent « de », « des », « to », « ceux » devant une unité
    _unite = (
        r"bidon|sac|seau|cartouche|litre|kg|paquet|boite|boîte|chlore|brome"
    )
    t = re.sub(
        rf"\b(to|too|two|ceux|d['’]?eux|deuxe?|deu)\s+({_unite})\b",
        r"2 \2",
        t,
        flags=re.I,
    )
    # « fourniture de bidon » = souvent « deux bidon » mal entendu
    t = re.sub(
        rf"\b(fourniture|facture|facturer)\s+(de|des|du)\s+({_unite})\b",
        r"\1 2 \3",
        t,
        flags=re.I,
    )
    t = re.sub(
        rf"^(de|des|du)\s+({_unite})\b",
        r"2 \2",
        t,
        flags=re.I,
    )
    t = re.sub(
        rf"\b(troie|troi)\s+({_unite})\b",
        r"3 \2",
        t,
        flags=re.I,
    )
    # « un/une bidon » → 1 bidon (seulement devant un produit)
    t = re.sub(
        r"\b(un|une)\s+(bidon|sac|cartouche|seau|litre|kg|paquet|boite|boîte)\b",
        r"1 \2",
        t,
        flags=re.I,
    )
    # « 2bidons », « x2 », « 2 x »
    t = re.sub(r"\b(\d+)\s*x\s*(?=\w)", r"\1 ", t, flags=re.I)
    t = re.sub(r"\bx\s*(\d+)\b", r"\1", t, flags=re.I)
    t = re.sub(
        r"\b(\d+)(bidon|sac|seau|cartouche|litre|paquet|boite|boîte)\b",
        r"\1 \2",
        t,
        flags=re.I,
    )

    # Si ça parle clairement d'un produit sans mot-clé → préfixe fourniture
    low = t.lower()
    has_intent = bool(
        re.search(r"\b(note|noter|facture|facturer|fourniture|analyse|analyser)\b", low)
    )
    has_produit = bool(
        re.search(
            r"\b(bidon|chlore|brome|sel|sable|cartouche|sac|floculant|algicide|oxygène)\b",
            low,
        )
    )
    if has_produit and not has_intent:
        t = f"fourniture {t}"

    return " ".join(t.split()).strip()


def normaliser_nombres_fr(texte: str) -> str:
    """Alias public pour les tests / parse."""
    return corriger_transcription(texte)


def audio_to_float32(path: str):
    import av
    import numpy as np

    container = av.open(path)
    resampler = av.audio.resampler.AudioResampler(format="flt", layout="mono", rate=16000)
    chunks: list = []
    try:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                arr = out.to_ndarray()
                if arr.ndim > 1:
                    arr = arr.mean(axis=0)
                chunks.append(arr.astype("float32", copy=False))
        for out in resampler.resample(None):
            arr = out.to_ndarray()
            if arr.ndim > 1:
                arr = arr.mean(axis=0)
            chunks.append(arr.astype("float32", copy=False))
    finally:
        container.close()
    if not chunks:
        return None
    audio = np.concatenate(chunks)
    if audio.size == 0:
        return None
    # normaliser fort : micros PC souvent très faibles
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if peak < 1e-4 and rms < 1e-4:
        return audio  # silence réel
    target = 0.35
    gain = target / max(peak, rms * 3, 1e-6)
    gain = min(gain, 80.0)
    audio = np.clip(audio * gain, -1.0, 1.0).astype("float32")
    return audio


def wav_bytes_to_float32(raw: bytes):
    """Lit un WAV PCM 16-bit mono/stéréo → float32 16 kHz."""
    import io
    import wave

    import numpy as np

    with wave.open(io.BytesIO(raw), "rb") as wf:
        n_channels = wf.getnchannels()
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        sampwidth = wf.getsampwidth()
        frames = wf.readframes(n_frames)

    if sampwidth != 2:
        return None
    audio = np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0
    if n_channels > 1:
        audio = audio.reshape(-1, n_channels).mean(axis=1)
    if sr != 16000 and len(audio) > 0:
        ratio = sr / 16000
        new_len = max(1, int(len(audio) / ratio))
        idx = (np.arange(new_len) * ratio).astype(int)
        idx = np.clip(idx, 0, len(audio) - 1)
        audio = audio[idx]
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
    if peak > 1e-4:
        gain = min(0.35 / max(peak, rms * 3, 1e-6), 80.0)
        audio = np.clip(audio * gain, -1.0, 1.0).astype("float32")
    return audio


def next_id(rows: list[dict[str, str]]) -> int:
    if not rows:
        return 1
    return max(int(r["id"]) for r in rows if str(r.get("id", "")).isdigit()) + 1


def bool_csv(v: Any) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    s = str(v or "").strip().lower()
    return "1" if s in {"1", "true", "oui", "yes"} else "0"


def from_bool_csv(v: str) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "oui", "yes"}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()
    return f"pbkdf2_sha256$120000${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt, digest = (stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(rounds_s),
        ).hex()
        return secrets.compare_digest(check, digest)
    except (ValueError, TypeError):
        return False


def generer_mot_de_passe(length: int = 10) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generer_identifiant(rows: list[dict[str, str]]) -> str:
    existants = {(r.get("identifiant") or "").strip().upper() for r in rows}
    for _ in range(40):
        code = "AC-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        if code not in existants:
            return code
    return f"AC-{secrets.token_hex(4).upper()}"


def normaliser_email(value: str | None) -> str:
    return (value or "").strip().lower()


def normaliser_telephone(value: str | None) -> str:
    raw = (value or "").strip()
    digits = re.sub(r"[^\d+]", "", raw)
    return digits


def valider_coords_compte(creer_compte: bool, email: str, telephone: str) -> tuple[str, str]:
    mail = normaliser_email(email)
    tel = normaliser_telephone(telephone)
    if mail and ("@" not in mail or "." not in mail.split("@")[-1]):
        raise HTTPException(status_code=400, detail="Adresse e-mail invalide.")
    if creer_compte and not mail and not tel:
        raise HTTPException(
            status_code=400,
            detail="Création de compte : indiquez un e-mail ou un numéro de téléphone.",
        )
    return mail, tel


def ensure_unique_login(
    rows: list[dict[str, str]],
    email: str,
    telephone: str,
    *,
    exclude_id: int | None = None,
) -> None:
    for r in rows:
        if exclude_id is not None and int(r.get("id") or 0) == exclude_id:
            continue
        if email and normaliser_email(r.get("email")) == email:
            raise HTTPException(status_code=400, detail="Cet e-mail est déjà utilisé.")
        if telephone and normaliser_telephone(r.get("telephone")) == telephone:
            raise HTTPException(status_code=400, detail="Ce numéro est déjà utilisé.")


def _new_instruction_id() -> str:
    return str(uuid.uuid4())


def parse_checklist_hivernage(raw: Any) -> list[dict[str, str]]:
    """Retourne [{id, label}, ...] pour les étapes hivernage."""
    items: list[Any]
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict) and (
        "steps" in raw or "items" in raw or "mode" in raw or "commentaire" in raw
    ):
        # Bloc hivernage {mode, commentaire, steps} → liste pour visite
        cfg = parse_hivernage_config(raw)
        return hivernage_steps_for_visite(cfg)
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        try:
            data = json.loads(text)
            if isinstance(data, dict) and (
                "steps" in data or "mode" in data or "commentaire" in data
            ):
                cfg = parse_hivernage_config(data)
                return hivernage_steps_for_visite(cfg)
            items = data if isinstance(data, list) else []
        except (json.JSONDecodeError, TypeError):
            items = [ln.strip() for ln in text.splitlines() if ln.strip()]

    out: list[dict[str, str]] = []
    for item in items:
        com = ""
        if isinstance(item, dict):
            lab = str(item.get("label") or item.get("texte") or "").strip()
            iid = str(item.get("id") or "").strip()
            com = str(item.get("commentaire") or "").strip()
            # Passif : ignorer les non sélectionnés si le flag est présent
            if "selected" in item and not item.get("selected"):
                continue
        else:
            lab = str(item).strip()
            iid = ""
        if not lab:
            continue
        entry: dict[str, str] = {"id": iid or _new_instruction_id(), "label": lab}
        if com:
            entry["commentaire"] = com
        out.append(entry)
    return out


def parse_hivernage_config(
    raw: Any, *, fallback_list: Any = None
) -> dict[str, Any]:
    """Normalise {mode, commentaire, steps:[{id,label,parent_id,selected,commentaire}]}."""
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                data = None
        else:
            data = None

    mode = "actif"
    commentaire = ""
    steps_raw: list[Any] = []

    if isinstance(data, dict) and (
        "mode" in data or "commentaire" in data or "steps" in data or "items" in data
    ):
        mode = "passif" if str(data.get("mode") or "").strip().lower() == "passif" else "actif"
        commentaire = str(data.get("commentaire") or "").strip()
        steps_raw = data.get("steps") if isinstance(data.get("steps"), list) else []
        if not steps_raw and isinstance(data.get("items"), list):
            steps_raw = data.get("items") or []
    elif isinstance(data, list):
        steps_raw = data
    elif fallback_list is not None:
        steps_raw = (
            fallback_list
            if isinstance(fallback_list, list)
            else parse_checklist_hivernage(fallback_list)
        )

    steps: list[dict[str, Any]] = []
    for item in steps_raw:
        step_com = ""
        if isinstance(item, dict):
            lab = str(item.get("label") or item.get("texte") or "").strip()
            iid = str(item.get("id") or "").strip() or _new_instruction_id()
            parent_id = str(item.get("parent_id") or "").strip() or None
            step_com = str(item.get("commentaire") or "").strip()
            selected = item.get("selected")
            if selected is None:
                selected = bool(lab) if mode == "actif" else False
            else:
                selected = bool(selected)
        else:
            lab = str(item or "").strip()
            if not lab:
                continue
            iid = _new_instruction_id()
            parent_id = None
            selected = True
        if not lab and mode == "actif":
            continue
        if not lab:
            continue
        steps.append(
            {
                "id": iid,
                "label": lab,
                "parent_id": parent_id,
                "selected": bool(selected),
                "commentaire": step_com,
            }
        )
    return {"mode": mode, "commentaire": commentaire, "steps": steps}


def hivernage_steps_for_visite(cfg: dict[str, Any] | None) -> list[dict[str, str]]:
    """Étapes à cocher en visite (passif = sélectionnées uniquement)."""
    cfg = cfg or {"mode": "actif", "steps": []}
    mode = cfg.get("mode") or "actif"
    out: list[dict[str, str]] = []
    for s in cfg.get("steps") or []:
        if not isinstance(s, dict):
            continue
        lab = str(s.get("label") or "").strip()
        if not lab:
            continue
        if mode == "passif" and not s.get("selected"):
            continue
        entry: dict[str, str] = {
            "id": str(s.get("id") or _new_instruction_id()),
            "label": lab,
        }
        com = str(s.get("commentaire") or "").strip()
        if com:
            entry["commentaire"] = com
        out.append(entry)
    return out


def serialize_checklist_hivernage(items: list[Any] | None) -> str:
    clean = parse_checklist_hivernage(items or [])
    return json.dumps(clean, ensure_ascii=False)


def empty_options_checklists() -> dict[str, Any]:
    return {k: [] for k in OPTION_CHECKLIST_KEYS}


def parse_options_checklists(raw: Any, *, fallback_hivernage: Any = None) -> dict[str, Any]:
    """Retourne {option: [{id, label}, ...]} + hivernage_config pour l’UI."""
    out: dict[str, Any] = empty_options_checklists()
    data: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                data = {}
        else:
            data = {}
    hiv_raw = None
    if isinstance(data, dict):
        for key in OPTION_CHECKLIST_KEYS:
            if key == "hivernage":
                hiv_raw = data.get("hivernage")
                continue
            out[key] = parse_checklist_hivernage(data.get(key))
    hiv_cfg = parse_hivernage_config(hiv_raw, fallback_list=fallback_hivernage)
    if not hiv_cfg["steps"] and fallback_hivernage is not None and hiv_raw is None:
        hiv_cfg = parse_hivernage_config(None, fallback_list=fallback_hivernage)
    out["hivernage"] = hivernage_steps_for_visite(hiv_cfg)
    out["hivernage_config"] = hiv_cfg
    return out


def serialize_options_checklists(data: dict[str, Any] | None) -> str:
    clean: dict[str, Any] = empty_options_checklists()
    if isinstance(data, dict):
        for key in OPTION_CHECKLIST_KEYS:
            if key == "hivernage":
                continue
            clean[key] = parse_checklist_hivernage(data.get(key))
        # Hivernage : objet {mode, commentaire, steps} ou liste legacy
        if isinstance(data.get("hivernage_config"), dict):
            cfg = parse_hivernage_config(data.get("hivernage_config"))
        else:
            cfg = parse_hivernage_config(data.get("hivernage"))
        clean["hivernage"] = {
            "mode": cfg["mode"],
            "commentaire": cfg["commentaire"],
            "steps": cfg["steps"],
        }
    return json.dumps(clean, ensure_ascii=False)


def _options_fields_from_body(body: Any, existing: dict[str, Any] | None = None) -> dict[str, str]:
    """Persiste options_checklists + synchronise checklist_hivernage."""
    existing = existing or {}
    if getattr(body, "options_checklists", None) is not None:
        raw_opts = body.options_checklists
        opts_for_ser = raw_opts if isinstance(raw_opts, dict) else {}
        parsed = parse_options_checklists(raw_opts)
        checklist = parsed.get("hivernage") or []
    else:
        opts_for_ser = parse_options_checklists(
            existing.get("options_checklists"),
            fallback_hivernage=existing.get("checklist_hivernage"),
        )
        if getattr(body, "checklist_hivernage", None) is not None:
            cfg = parse_hivernage_config(
                opts_for_ser.get("hivernage_config"),
                fallback_list=body.checklist_hivernage,
            )
            # Remplacer les steps par la checklist envoyée (legacy)
            cfg["steps"] = [
                {
                    "id": s["id"],
                    "label": s["label"],
                    "parent_id": None,
                    "selected": True,
                    "commentaire": str(s.get("commentaire") or "").strip(),
                }
                for s in parse_checklist_hivernage(body.checklist_hivernage)
            ]
            opts_for_ser = {
                **{k: opts_for_ser.get(k) or [] for k in OPTION_CHECKLIST_KEYS if k != "hivernage"},
                "hivernage_config": cfg,
            }
        checklist = hivernage_steps_for_visite(
            opts_for_ser.get("hivernage_config")
            or parse_hivernage_config(opts_for_ser.get("hivernage"))
        )
    return {
        "options_checklists": serialize_options_checklists(opts_for_ser),
        "checklist_hivernage": serialize_checklist_hivernage(checklist),
    }


def checklist_storage_needs_ids(raw: Any) -> bool:
    """True si la checklist stockée n'a pas encore d'ids stables."""
    if isinstance(raw, list):
        if not raw:
            return False
        return any(not (isinstance(x, dict) and str(x.get("id") or "").strip()) for x in raw)
    text = str(raw or "").strip()
    if not text:
        return False
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return True  # lignes libres → migrer
    if not isinstance(data, list) or not data:
        return False
    return any(not (isinstance(x, dict) and str(x.get("id") or "").strip()) for x in data)


def migrate_checklist_hivernage_ids() -> None:
    """Persiste des UUID sur les étapes hivernage encore en simple liste de libellés."""
    try:
        rows = read_csv(CLIENTS_CSV)
    except Exception as e:
        logging.getLogger("aquaclient").warning("Migration checklist ids : %s", e)
        return
    changed = False
    for r in rows:
        raw = r.get("checklist_hivernage")
        if not checklist_storage_needs_ids(raw):
            continue
        steps = parse_checklist_hivernage(raw)
        r["checklist_hivernage"] = serialize_checklist_hivernage(steps)
        changed = True
    if changed:
        write_csv(CLIENTS_CSV, CLIENT_FIELDS, rows)
        logging.getLogger("aquaclient").info(
            "Migration checklist hivernage : ids persistés pour les clients concernés"
        )


def checklist_labels(items: list[Any] | None) -> list[str]:
    return [x["label"] for x in parse_checklist_hivernage(items or [])]


def default_checklist_hivernage_items() -> list[dict[str, str]]:
    return [{"id": _new_instruction_id(), "label": lab} for lab in DEFAULT_CHECKLIST_HIVERNAGE]


def slug_checklist_id(label: str, index: int) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (label or "").lower())
    base = base.strip("_")[:40] or "etape"
    return f"hiv_{index}_{base}"


def detect_image_mime(data: bytes) -> str | None:
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def optimize_photo(data: bytes) -> tuple[bytes, int, int]:
    """Redimensionne et convertit en WebP pour limiter l'espace Storage."""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")
    w, h = img.size
    longest = max(w, h) or 1
    if longest > MAX_PHOTO_EDGE:
        scale = MAX_PHOTO_EDGE / longest
        img = img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY, method=6)
    ow, oh = img.size
    return buf.getvalue(), ow, oh


def photo_public(row: dict[str, Any]) -> dict[str, Any]:
    pid = str(row.get("id") or "")
    return {
        "id": pid,
        "client_id": int(row.get("client_id") or 0),
        "instruction_id": str(row.get("instruction_id") or ""),
        "mime_type": str(row.get("mime_type") or "image/webp"),
        "size_bytes": int(float(row.get("size_bytes") or 0)),
        "width": int(float(row["width"])) if row.get("width") not in (None, "") else None,
        "height": int(float(row["height"])) if row.get("height") not in (None, "") else None,
        "created_at": str(row.get("created_at") or ""),
        "url": f"/api/photos/{pid}/file",
    }


def read_photos_meta() -> list[dict[str, str]]:
    if supabase_db.enabled():
        rows = supabase_db.list_photos()
        out: list[dict[str, str]] = []
        for r in rows:
            out.append({k: "" if r.get(k) is None else str(r.get(k)) for k in PHOTOS_META_FIELDS})
        return out
    if not PHOTOS_META_CSV.exists():
        return []
    with PHOTOS_META_CSV.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_photos_meta(rows: list[dict[str, Any]]) -> None:
    with PHOTOS_META_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PHOTOS_META_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in PHOTOS_META_FIELDS})


def list_client_photos(client_id: int, instruction_id: str | None = None) -> list[dict[str, Any]]:
    if supabase_db.enabled():
        rows = supabase_db.list_photos(client_id, instruction_id)
        return [photo_public(r) for r in rows]
    rows = read_photos_meta()
    out = []
    for r in rows:
        if int(r.get("client_id") or 0) != client_id:
            continue
        if instruction_id and (r.get("instruction_id") or "") != instruction_id:
            continue
        out.append(photo_public(r))
    return out


def get_photo_row(photo_id: str) -> dict[str, Any] | None:
    if supabase_db.enabled():
        return supabase_db.get_photo(photo_id)
    for r in read_photos_meta():
        if str(r.get("id")) == str(photo_id):
            return r
    return None


def load_photo_bytes(row: dict[str, Any]) -> bytes:
    path = str(row.get("storage_path") or "")
    if supabase_db.enabled() and not path.startswith("local:"):
        return supabase_db.download_photo_bytes(path)
    local = Path(path[6:]) if path.startswith("local:") else PHOTOS_DIR / path
    if not local.is_file():
        # chemin relatif stocké tel quel
        alt = PHOTOS_DIR / path
        if alt.is_file():
            return alt.read_bytes()
        raise FileNotFoundError(path)
    return local.read_bytes()


def persist_photo(
    *,
    client_id: int,
    instruction_id: str,
    raw: bytes,
) -> dict[str, Any]:
    mime = detect_image_mime(raw)
    if mime not in ALLOWED_PHOTO_MIMES:
        raise HTTPException(
            status_code=400,
            detail="Format non supporté. Utilisez JPG, PNG ou WebP.",
        )
    if len(raw) > MAX_PHOTO_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Fichier trop volumineux (max {MAX_PHOTO_UPLOAD_BYTES // (1024 * 1024)} Mo).",
        )
    try:
        optimized, width, height = optimize_photo(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Image invalide : {e}") from e

    photo_id = str(uuid.uuid4())
    storage_path = f"{client_id}/{instruction_id}/{photo_id}.webp"
    created = datetime.now().isoformat(timespec="seconds")

    if supabase_db.enabled():
        try:
            supabase_db.upload_photo_bytes(storage_path, optimized, "image/webp")
            meta = {
                "id": photo_id,
                "client_id": client_id,
                "instruction_id": instruction_id,
                "storage_path": storage_path,
                "mime_type": "image/webp",
                "size_bytes": len(optimized),
                "width": width,
                "height": height,
                "created_at": created,
            }
            saved = supabase_db.insert_photo_meta(meta)
            return photo_public(saved)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Upload Storage impossible : {e}",
            ) from e

    # Fallback local (mode CSV)
    dest_dir = PHOTOS_DIR / str(client_id) / instruction_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{photo_id}.webp"
    dest.write_bytes(optimized)
    rel = f"{client_id}/{instruction_id}/{photo_id}.webp"
    meta = {
        "id": photo_id,
        "client_id": str(client_id),
        "instruction_id": instruction_id,
        "storage_path": rel,
        "mime_type": "image/webp",
        "size_bytes": str(len(optimized)),
        "width": str(width),
        "height": str(height),
        "created_at": created,
    }
    rows = read_photos_meta()
    rows.append(meta)
    write_photos_meta(rows)
    return photo_public(meta)


def delete_photo_row(photo_id: str) -> None:
    row = get_photo_row(photo_id)
    if not row:
        raise HTTPException(status_code=404, detail="Photo introuvable")
    path = str(row.get("storage_path") or "")
    if supabase_db.enabled() and not path.startswith("local:"):
        supabase_db.remove_photo_file(path)
        supabase_db.delete_photo_meta(photo_id)
        return
    local = PHOTOS_DIR / path
    if local.is_file():
        local.unlink()
    rows = [r for r in read_photos_meta() if str(r.get("id")) != str(photo_id)]
    write_photos_meta(rows)


def normalize_type_entretien(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in {"entretien_complet", "complet"}:
        return "entretien_complet"
    return "entretien_simple"


def normalize_frequence(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in {"hebdomadaire", "semaine", "1s", "7"}:
        return "hebdomadaire"
    if v in {"bihebdomadaire", "bihebdo", "2semaines", "14", "quinzaine"}:
        return "bihebdomadaire"
    return ""


def parse_iso_date(value: str | None) -> datetime | None:
    raw = (value or "").strip()[:10]
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def frequence_jours(freq: str) -> int | None:
    if freq == "hebdomadaire":
        return 7
    if freq == "bihebdomadaire":
        return 14
    return None


def prochaines_dates_passage(
    date_debut: str,
    frequence: str,
    *,
    depuis: datetime | None = None,
    horizon_jours: int = 90,
    max_items: int = 12,
) -> list[str]:
    """Génère les prochaines dates de passage (ISO) à partir de la date de début."""
    start = parse_iso_date(date_debut)
    step = frequence_jours(frequence)
    if not start or not step:
        return []
    ref = (depuis or datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    cur = start
    if cur < ref:
        delta = (ref - cur).days
        n = (delta + step - 1) // step
        cur = cur + timedelta(days=n * step)
        while cur < ref:
            cur = cur + timedelta(days=step)
    fin = ref + timedelta(days=horizon_jours)
    out: list[str] = []
    while cur <= fin and len(out) < max_items:
        out.append(cur.strftime("%Y-%m-%d"))
        cur = cur + timedelta(days=step)
    return out


def row_client(r: dict[str, str], *, password_clair: str | None = None) -> dict[str, Any]:
    freq = normalize_frequence(r.get("frequence_passage"))
    debut = (r.get("date_debut_passage") or "").strip()[:10]
    type_ent = normalize_type_entretien(r.get("type_entretien"))
    # Passage planifié seulement pour entretien complet
    if type_ent != "entretien_complet":
        freq = ""
        debut = ""
    prochaines = prochaines_dates_passage(debut, freq) if freq and debut else []
    out: dict[str, Any] = {
        "id": int(r["id"]),
        "nom": r.get("nom", ""),
        "prenom": r.get("prenom", ""),
        "adresse": r.get("adresse", ""),
        "latitude": float(r["latitude"]) if r.get("latitude") else None,
        "longitude": float(r["longitude"]) if r.get("longitude") else None,
        "pompe": r.get("pompe", ""),
        "filtre": r.get("filtre", ""),
        "robot": r.get("robot", ""),
        "skimmer": r.get("skimmer", ""),
        "vanne_vidange": from_bool_csv(r.get("vanne_vidange", "")),
        "injecteurs": from_bool_csv(r.get("injecteurs", "")),
        "autres": r.get("autres", ""),
        "type_entretien": type_ent,
        "frequence_passage": freq,
        "date_debut_passage": debut,
        "prochain_passage": prochaines[0] if prochaines else None,
        "prochains_passages": prochaines,
        "identifiant": (r.get("identifiant") or "").strip(),
        "email": (r.get("email") or "").strip(),
        "telephone": (r.get("telephone") or "").strip(),
        "compte_actif": from_bool_csv(r.get("compte_actif", "")),
        "auth_uid": (r.get("auth_uid") or "").strip(),
        "has_password": bool(
            (r.get("password_hash") or "").strip() or (r.get("auth_uid") or "").strip()
        ),
        "checklist_hivernage": parse_checklist_hivernage(r.get("checklist_hivernage")),
        "piscine_hivernee": from_bool_csv(r.get("piscine_hivernee", "")),
        "hivernage_en_cours": from_bool_csv(r.get("hivernage_en_cours", "")),
        **(
            lambda opts: {
                "options_checklists": {
                    k: opts.get(k) or [] for k in OPTION_CHECKLIST_KEYS
                },
                "hivernage_config": opts.get("hivernage_config")
                or {"mode": "actif", "commentaire": "", "steps": []},
            }
        )(
            parse_options_checklists(
                r.get("options_checklists"),
                fallback_hivernage=r.get("checklist_hivernage"),
            )
        ),
    }
    if password_clair:
        out["password_clair"] = password_clair
    return out


async def geocode_ban(adresse: str) -> tuple[float, float]:
    url = "https://api-adresse.data.gouv.fr/search/"
    params: dict[str, Any] = {"q": adresse, "limit": 1}
    cp = re.search(r"\b(\d{5})\b", adresse)
    if cp:
        params["postcode"] = cp.group(1)
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    feats = data.get("features") or []
    if not feats:
        raise HTTPException(status_code=400, detail="Adresse introuvable.")
    lon, lat = feats[0]["geometry"]["coordinates"]
    return float(lat), float(lon)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def preparer_dates_orales(texte: str) -> str:
    """Normalise les dates dictées (lettres, espaces) avant parsing."""
    t = " ".join((texte or "").lower().split())
    if not t:
        return t

    # Jours composés AVANT « vingt » seul (sinon « vingt et un » → « 20 et un »)
    jours_lettres = [
        ("trente et un", "31"),
        ("trente-et-un", "31"),
        ("trente", "30"),
        ("vingt et un", "21"),
        ("vingt-et-un", "21"),
        ("vingt deux", "22"),
        ("vingt-deux", "22"),
        ("vingt trois", "23"),
        ("vingt-trois", "23"),
        ("vingt quatre", "24"),
        ("vingt-quatre", "24"),
        ("vingt cinq", "25"),
        ("vingt-cinq", "25"),
        ("vingt six", "26"),
        ("vingt-six", "26"),
        ("vingt sept", "27"),
        ("vingt-sept", "27"),
        ("vingt huit", "28"),
        ("vingt-huit", "28"),
        ("vingt neuf", "29"),
        ("vingt-neuf", "29"),
        ("vingt", "20"),
        ("dix neuf", "19"),
        ("dix-neuf", "19"),
        ("dix huit", "18"),
        ("dix-huit", "18"),
        ("dix sept", "17"),
        ("dix-sept", "17"),
        ("seize", "16"),
        ("quinze", "15"),
        ("quatorze", "14"),
        ("treize", "13"),
        ("douze", "12"),
        ("onze", "11"),
        ("dix", "10"),
        ("premier", "1"),
        ("1er", "1"),
    ]
    for mot, ch in jours_lettres:
        t = re.sub(rf"\b{re.escape(mot)}\b", ch, t)

    # « deux mille vingt-six » / « deux mille 26 » → 2026
    t = re.sub(r"\bdeux mille\s+(\d{2})\b", lambda m: str(2000 + int(m.group(1))), t)

    # « le 21 09 2026 » ou « 21 09 2026 » (année obligatoire si pas de « le »)
    def _espaces_vers_slash(m: re.Match) -> str:
        d, mo = int(m.group(1)), int(m.group(2))
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            return m.group(0)
        if m.group(3):
            yy = int(m.group(3))
            if yy < 100:
                yy += 2000
            return f"le {d:02d}/{mo:02d}/{yy}"
        return f"le {d:02d}/{mo:02d}"

    t = re.sub(
        r"\ble\s+(\d{1,2})\s+(\d{1,2})(?:\s+(\d{2,4}))?\b",
        _espaces_vers_slash,
        t,
    )
    t = re.sub(
        r"\b(\d{1,2})\s+(\d{1,2})\s+(\d{2,4})\b",
        _espaces_vers_slash,
        t,
    )
    return t


def parse_date_fr(texte: str) -> str | None:
    """Extrait une date FR → YYYY-MM-DD. Sinon None."""
    lower = preparer_dates_orales(texte)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    year = today.year

    # Relatives
    if re.search(r"\bavant[- ]hier\b", lower):
        return (today - timedelta(days=2)).strftime("%Y-%m-%d")
    if re.search(r"\bhier\b", lower):
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if re.search(r"\baujourd['']?hui\b", lower):
        return today.strftime("%Y-%m-%d")
    if re.search(r"\bdemain\b", lower):
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")

    m = re.search(
        r"\b(?:le\s+)?(\d{1,2})\s+(" + "|".join(MOIS.keys()) + r")(?:\s+(\d{4}))?\b",
        lower,
    )
    if m:
        day = int(m.group(1))
        month = MOIS[m.group(2)]
        if m.group(3):
            year = int(m.group(3))
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    m = re.search(r"\b(?:le\s+)?(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?\b", lower)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if not (1 <= day <= 31 and 1 <= month <= 12):
            return None
        if m.group(3):
            y = int(m.group(3))
            year = y if y > 99 else 2000 + y
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def strip_date_from_text(texte: str) -> str:
    prepared = preparer_dates_orales(texte)
    lower_patterns = [
        r"\s*(?:le\s+)?\d{1,2}\s+(?:" + "|".join(MOIS.keys()) + r")(?:\s+\d{4})?\s*$",
        r"\s*(?:le\s+)?\d{1,2}[/\-.]\d{1,2}(?:[/\-.]\d{2,4})?\s*$",
        r"\s*(?:le\s+)?\d{1,2}\s+\d{1,2}(?:\s+\d{2,4})?\s*$",
        r"\s*\b(?:avant[- ]hier|hier|aujourd['']?hui|demain)\b\s*$",
        r"\s*(?:en date du|dat[ée]e? du|pour le)\s+.+$",
    ]
    out = prepared
    for p in lower_patterns:
        out = re.sub(p, "", out, flags=re.I).strip()
    return out.strip(" ,.-")


def parse_commande(texte: str, intent_force: str | None = None) -> dict[str, Any]:
    raw = corriger_transcription(texte.strip())
    lower = re.sub(r"\b(euh|hum|ben|donc)\b", " ", raw.lower())
    lower = re.sub(r"\s+", " ", lower).strip()
    # date = jour de saisie ; date_ref = date dite à l'oral (si présente)
    date_saisie = datetime.now().strftime("%Y-%m-%d")
    date_ref = parse_date_fr(lower) or ""

    intent = intent_force
    if not intent:
        if re.match(r"^(une\s+)?(note|noter)\b", lower) or lower.startswith("note"):
            intent = "note"
        elif re.search(r"\b(facture|facturer|fourniture|fournitures)\b", lower):
            intent = "fourniture"
        elif re.match(r"^(une\s+)?(analyse|analyser|annalyse|analise)\b", lower) or lower.startswith(
            ("analyser", "analyse", "annalyse")
        ):
            intent = "analyse"
        elif re.search(
            r"\b(bidon|chlore|brome|sel|sable|cartouche|floculant|algicide)\b",
            lower,
        ):
            # « 3 bidon de chlore » sans mot-clé → fourniture
            intent = "fourniture"
        else:
            return {
                "intent": "inconnu",
                "ok": False,
                "message": 'Dites « note… », « facture… » / « fourniture… » ou « analyse… ».',
            }

    if intent == "note":
        contenu = re.sub(
            r"\b(une\s+)?(notes?|noter)\b[:\s,-]*",
            " ",
            raw,
            count=1,
            flags=re.I,
        )
        contenu = re.sub(r"\s+", " ", contenu).strip(" ,.-")
        contenu = strip_date_from_text(contenu)
        if not contenu and not intent_force:
            return {"intent": "note", "ok": False, "message": "Que souhaitez-vous noter ?"}
        if not contenu:
            contenu = raw
        return {
            "intent": "note",
            "ok": True,
            "texte": contenu,
            "date": date_saisie,
            "date_ref": date_ref,
        }

    if intent == "analyse":
        contenu = re.sub(r"^(une\s+)?(analyse|analyser)[:\s,-]*", "", raw, count=1, flags=re.I).strip()
        contenu = strip_date_from_text(contenu)
        if not contenu:
            contenu = raw if intent_force else ""
        if not contenu:
            return {"intent": "analyse", "ok": False, "message": "Décrivez l'analyse (pH, chlore…)."}
        return {
            "intent": "analyse",
            "ok": True,
            "texte": contenu,
            "date": date_saisie,
            "date_ref": date_ref,
        }

    if intent == "fourniture":
        body = re.sub(
            r"\b(une\s+)?(facture|facturer|fourniture|fournitures)\b[:\s,-]*",
            " ",
            raw,
            flags=re.I,
        )
        body = re.sub(r"\s+", " ", body).strip(" ,.-")
        qty, desc = _extraire_quantite(body)
        # retirer prix s'il est dit par habitude
        desc = re.sub(
            r"\s*(?:à|a|pour)?\s*\d+(?:[.,]\d+)?\s*(?:€|euros?|euro).*$",
            "",
            desc,
            flags=re.I,
        ).strip()
        desc = strip_date_from_text(desc)
        desc = re.sub(r"\s*l['']unité\s*$", "", desc, flags=re.I).strip()
        desc = re.sub(
            r"\b(facture|facturer|fourniture|fournitures)\b",
            " ",
            desc,
            flags=re.I,
        )
        desc = re.sub(r"\s+", " ", desc).strip(" ,.-")
        if not desc:
            return {
                "intent": "fourniture",
                "ok": False,
                "message": "Ex. : fourniture 3 bidon de chlore",
            }
        return {
            "intent": "fourniture",
            "ok": True,
            "description": desc,
            "quantite": qty,
            "date": date_saisie,
            "date_ref": date_ref,
        }

    return {"intent": "inconnu", "ok": False, "message": "Commande non reconnue."}


_DATE_HINT = re.compile(
    r"\b("
    r"janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
    r"septembre|octobre|novembre|décembre|decembre|"
    r"hier|avant[- ]hier|aujourd|demain|"
    r"\d{1,2}\s*[/\-.]\s*\d{1,2}|"
    r"\d{1,2}\s+\d{1,2}\s+\d{2,4}"
    r")\b",
    re.I,
)

_FILLER = re.compile(
    r"\b("
    r"j\s*['’]?\s*ai|jai|viens de|viens|"
    r"mettre|mis|ajoute|ajouter|ajout|"
    r"prendre|pris|pose|poser|utiliser|utilisé|utilise|"
    r"besoin|encore|aussi|puis|ensuite|voila|voilà|"
    r"pour le client|chez le client"
    r")\b",
    re.I,
)


def _texte_contient_reste_date(texte: str) -> bool:
    t = (texte or "").lower()
    if not t:
        return False
    if _DATE_HINT.search(t):
        return True
    if re.search(r"\ble\s+\d{1,2}\b", t):
        return True
    return False


def _commande_incomplete(parsed: dict[str, Any], texte: str) -> bool:
    """True si les règles ont loupé une date / un contenu douteux → bascule IA."""
    if not parsed.get("ok"):
        return True
    contenu = parsed.get("texte") or parsed.get("description") or ""
    if _texte_contient_reste_date(contenu):
        return True
    if _DATE_HINT.search(texte or "") and not (parsed.get("date_ref") or "").strip():
        return True
    # Quantité mal lue : « j'ai mis 2 bidons » → qty 1
    if parsed.get("intent") == "fourniture":
        m = re.search(
            r"\b(\d+(?:[.,]\d+)?)\s*(?:x\s*)?(bidons?|sacs?|seaux?|cartouches?|litres?|kg|paquets?)\b",
            (texte or "").lower(),
        )
        if m:
            try:
                attendu = float(m.group(1).replace(",", "."))
            except ValueError:
                attendu = None
            if attendu and abs(float(parsed.get("quantite") or 0) - attendu) > 0.01:
                return True
        if re.search(r"\b(j['’]?ai|ajoute|mis|prendre|pris)\b", (texte or "").lower()):
            if re.search(r"\b(j['’]?ai|ajoute|mis|prendre|pris)\b", (contenu or "").lower()):
                return True
    return False


def _valider_parsed_ia(data: dict[str, Any], intent_force: str | None = None) -> dict[str, Any] | None:
    intent = (intent_force or data.get("intent") or "").strip().lower()
    if intent not in {"note", "analyse", "fourniture"}:
        return None
    date_saisie = datetime.now().strftime("%Y-%m-%d")
    date_ref = (data.get("date_ref") or "").strip()[:10]
    if date_ref and not re.match(r"^\d{4}-\d{2}-\d{2}$", date_ref):
        date_ref = parse_date_fr(str(data.get("date_ref"))) or ""
    if not date_ref:
        # seconde chance sur le contenu brut
        blob = " ".join(
            str(x)
            for x in (
                data.get("texte"),
                data.get("description"),
                data.get("_raw"),
            )
            if x
        )
        date_ref = parse_date_fr(blob) or ""
    if intent == "note":
        texte = strip_date_from_text(str(data.get("texte") or "").strip())
        texte = _nettoyer_corps(texte)
        if not texte:
            return None
        return {
            "intent": "note",
            "ok": True,
            "texte": texte,
            "date": date_saisie,
            "date_ref": date_ref,
            "via": data.get("via", "ia"),
        }
    if intent == "analyse":
        texte = strip_date_from_text(str(data.get("texte") or "").strip())
        texte = _nettoyer_corps(texte)
        if not texte:
            return None
        return {
            "intent": "analyse",
            "ok": True,
            "texte": texte,
            "date": date_saisie,
            "date_ref": date_ref,
            "via": data.get("via", "ia"),
        }
    # fourniture
    desc = strip_date_from_text(str(data.get("description") or "").strip())
    desc = _nettoyer_corps(desc)
    if not desc:
        return None
    try:
        qty = float(str(data.get("quantite", 1)).replace(",", "."))
    except ValueError:
        qty = 1.0
    if qty <= 0:
        qty = 1.0
    return {
        "intent": "fourniture",
        "ok": True,
        "description": desc,
        "quantite": qty,
        "date": date_saisie,
        "date_ref": date_ref,
        "via": data.get("via", "ia"),
    }


def _nettoyer_corps(texte: str) -> str:
    t = strip_date_from_text(texte or "")
    t = re.sub(
        r"\b(?:le\s+)?\d{1,2}\s+(?:" + "|".join(MOIS.keys()) + r")(?:\s+\d{4})?\b",
        " ",
        t,
        flags=re.I,
    )
    t = re.sub(r"\b(?:le\s+)?\d{1,2}[/\-.]\d{1,2}(?:[/\-.]\d{2,4})?\b", " ", t)
    t = re.sub(r"\b(?:le\s+)?\d{1,2}\s+\d{1,2}(?:\s+\d{2,4})?\b", " ", t)
    t = re.sub(r"\b(?:avant[- ]hier|hier|aujourd['']?hui|demain)\b", " ", t, flags=re.I)
    t = re.sub(r"\b(?:en date du|dat[ée]e? du|pour le|pour la|pour)\b", " ", t, flags=re.I)
    t = _FILLER.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-")
    # « de chlore » seul → garder ; « chlore bidon » → « bidon de chlore » si inversé
    t = re.sub(r"\b(chlore|brome|sel|sable)\s+(bidon|sac|seau|cartouche)\b", r"\2 de \1", t, flags=re.I)
    return t


def _singulariser_unite(mot: str) -> str:
    m = (mot or "").lower()
    mapping = {
        "bidons": "bidon",
        "bidon": "bidon",
        "sacs": "sac",
        "sac": "sac",
        "seaux": "seau",
        "seau": "seau",
        "cartouches": "cartouche",
        "cartouche": "cartouche",
        "litres": "litre",
        "litre": "litre",
        "paquets": "paquet",
        "paquet": "paquet",
        "boites": "boite",
        "boîte": "boite",
        "boites": "boite",
        "boîtes": "boite",
        "kg": "kg",
    }
    return mapping.get(m, m.rstrip("s"))


def _extraire_quantite(texte: str) -> tuple[float, str]:
    """Trouve une quantité n'importe où : « j'ai mis 2 bidons de chlore »."""
    t = texte or ""
    # « de/des bidon » en tête = souvent « deux » mal reconnu
    t = re.sub(
        r"^(de|des|du)\s+(bidon|sac|seau|cartouche|litre|paquet|boite|boîte)\b",
        r"2 \2",
        t.strip(),
        flags=re.I,
    )
    low = t.lower()
    unites = (
        r"bidons?|sacs?|seaux?|cartouches?|litres?|kg|paquets?|boites?|boîtes?|"
        r"chlore|brome|sel|sable|floculant|algicide"
    )
    m = re.search(
        rf"\b(\d+(?:[.,]\d+)?)\s*(?:x\s*)?(?:de\s+)?({unites})\b",
        low,
    )
    if m:
        qty = float(m.group(1).replace(",", "."))
        unite_raw = m.group(2)
        # Produit seul (chlore…) : garder le mot ; unité (bidon…) : singulariser
        if re.match(
            r"^(chlore|brome|sel|sable|floculant|algicide)$",
            unite_raw,
            flags=re.I,
        ):
            unite = unite_raw
        else:
            unite = _singulariser_unite(unite_raw)
        avant = t[: m.start()].strip()
        apres = t[m.end() :].strip()
        desc = " ".join(p for p in (avant, unite, apres) if p)
        desc = re.sub(r"\s+", " ", desc).strip(" ,.-")
        return qty, desc

    m = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:x|fois)\b", low)
    if m:
        qty = float(m.group(1).replace(",", "."))
        desc = (t[: m.start()] + " " + t[m.end() :]).strip()
        return qty, re.sub(r"\s+", " ", desc).strip(" ,.-")

    m = re.match(r"^(?:x\s*)?(\d+(?:[.,]\d+)?)\s+(?:x\s+)?(.+)$", low)
    if m:
        qty = float(m.group(1).replace(",", "."))
        desc = t[m.start(2) : m.end(2)].strip()
        return qty, desc

    # Chiffre seul en tête + reste sans unité collée : « 2 chlore liquide »
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s+(.+)$", low)
    if m and not re.match(r"^\d", m.group(2)):
        qty = float(m.group(1).replace(",", "."))
        desc = t[m.start(2) : m.end(2)].strip()
        if desc:
            return qty, desc

    # « quantité 2 », « 2 au total » en fin de phrase
    m = re.search(r"\b(?:quantit[ée]|qte|qty)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\b", low)
    if m:
        qty = float(m.group(1).replace(",", "."))
        desc = (t[: m.start()] + " " + t[m.end() :]).strip()
        return qty, re.sub(r"\s+", " ", desc).strip(" ,.-")

    return 1.0, t


def comprendre_locale(texte: str, intent_force: str | None = None) -> dict[str, Any] | None:
    """
    Petite IA locale : comprend phrase libre, dates orales, quantités au milieu.
    """
    raw = corriger_transcription(texte.strip())
    low = re.sub(r"\b(euh|hum|ben|donc)\b", " ", raw.lower())
    low = re.sub(r"\s+", " ", low).strip()
    date_ref = parse_date_fr(low) or ""

    intent = intent_force
    if not intent:
        if re.search(r"\b(note|noter|notes)\b", low):
            intent = "note"
        elif re.search(r"\b(analyse|analyser)\b", low):
            intent = "analyse"
        elif re.search(
            r"\b(fourniture|facture|facturer|bidon|chlore|brome|sel|sable|cartouche|"
            r"floculant|algicide|produit|sac|seau|ajoute|ajouter|mis)\b",
            low,
        ):
            intent = "fourniture"
        else:
            return None

    corps = raw
    if intent == "note":
        corps = re.sub(r"\b(une\s+)?(note|noter|notes)\b[:\s,-]*", " ", corps, flags=re.I)
    elif intent == "analyse":
        corps = re.sub(r"\b(une\s+)?(analyse|analyser)\b[:\s,-]*", " ", corps, flags=re.I)
    else:
        corps = re.sub(
            r"\b(une\s+)?(facture|facturer|fourniture|fournitures)\b[:\s,-]*",
            " ",
            corps,
            flags=re.I,
        )
    corps = re.sub(r"\s+", " ", corps).strip(" ,.-")

    if intent in {"note", "analyse"}:
        corps = _nettoyer_corps(corps)
        if not corps:
            return None
        return _valider_parsed_ia(
            {
                "intent": intent,
                "texte": corps,
                "date_ref": date_ref,
                "via": "ia_locale",
                "_raw": raw,
            },
            intent_force,
        )

    qty, desc = _extraire_quantite(corps)
    desc = _nettoyer_corps(desc)
    desc = re.sub(
        r"\s*(?:à|a|pour)?\s*\d+(?:[.,]\d+)?\s*(?:€|euros?|euro).*$",
        "",
        desc,
        flags=re.I,
    ).strip(" ,.-")
    if not desc:
        return None
    return _valider_parsed_ia(
        {
            "intent": "fourniture",
            "description": desc,
            "quantite": qty,
            "date_ref": date_ref,
            "via": "ia_locale",
            "_raw": raw,
        },
        intent_force,
    )


def comprendre_llm(texte: str, intent_force: str | None = None) -> dict[str, Any] | None:
    """Petite IA externe (OpenAI ou Ollama) si dispo — sinon None."""
    today = datetime.now().strftime("%Y-%m-%d")
    system = (
        "Tu es l'assistant terrain AquaClient pour techniciens piscine. "
        "Extrais UNE commande JSON strict sans markdown. Schéma:\n"
        '{"intent":"note|analyse|fourniture","texte":"...","description":"...",'
        '"quantite":1,"date_ref":"YYYY-MM-DD ou vide"}\n'
        f"Aujourd'hui={today}. date_ref = date mentionnée (hier, 21/09/2026, "
        "vingt et un septembre…), sinon \"\". "
        "Pour note/analyse remplis texte. Pour fourniture: description produit + quantite. "
        "Ne laisse jamais la date ni les mots j'ai/mis/ajoute dans texte/description. "
        "Ex: « j'ai mis 2 bidons de chlore hier » → "
        '{"intent":"fourniture","description":"bidon de chlore","quantite":2,'
        f'"date_ref":"{(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")}"}}'
    )
    user = f"Commande: {texte}"
    if intent_force:
        user += f"\nIntent forcé: {intent_force}"

    api_key = (os.environ.get("OPENAI_API_KEY") or os.environ.get("AQUACLIENT_OPENAI_KEY") or "").strip()
    ollama = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")
    model = (os.environ.get("AQUACLIENT_LLM_MODEL") or "").strip()

    try:
        if api_key:
            url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
            payload = {
                "model": model or "gpt-4o-mini",
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            with httpx.Client(timeout=12.0) as client:
                r = client.post(url, headers=headers, json=payload)
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
        else:
            # Ollama local optionnel (silencieux si absent)
            url = f"{ollama}/api/chat"
            payload = {
                "model": model or "llama3.2",
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            with httpx.Client(timeout=2.5) as client:
                try:
                    r = client.post(url, json=payload)
                except Exception:
                    return None
                if r.status_code >= 400:
                    return None
                content = r.json().get("message", {}).get("content", "")

        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        data["via"] = "ia_llm"
        data["_raw"] = texte
        return _valider_parsed_ia(data, intent_force)
    except Exception:
        return None


def comprendre_commande(texte: str, intent_force: str | None = None) -> dict[str, Any]:
    """Règles rapides → petite IA locale → LLM optionnel (Ollama/OpenAI)."""
    parsed = parse_commande(texte, intent_force)
    if parsed.get("ok") and not _commande_incomplete(parsed, texte):
        parsed["via"] = "regles"
        return parsed

    locale = comprendre_locale(texte, intent_force)
    if locale and locale.get("ok") and not _commande_incomplete(locale, texte):
        return locale

    if locale and locale.get("ok") and parsed.get("ok"):
        merged = {**parsed, "via": "ia_locale"}
        if (locale.get("date_ref") or "") and not (merged.get("date_ref") or ""):
            merged["date_ref"] = locale["date_ref"]
        contenu_key = "texte" if merged["intent"] in {"note", "analyse"} else "description"
        if locale.get(contenu_key):
            merged[contenu_key] = locale[contenu_key]
        if locale.get("quantite") is not None and merged.get("intent") == "fourniture":
            merged["quantite"] = locale["quantite"]
        if not _commande_incomplete(merged, texte):
            return merged

    llm = comprendre_llm(texte, intent_force)
    if llm and llm.get("ok"):
        return llm

    if locale and locale.get("ok"):
        return locale
    return parsed


def build_checklist(client: dict[str, Any], type_visite: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def add(iid: str, label: str, *, warn: str | None = None, optional: bool = False):
        items.append({
            "id": iid,
            "label": label,
            "warn": warn,
            "optional": optional,
            "done": False,
        })

    if type_visite == "depannage":
        add("diagnostic", "Diagnostic / constat")
        add("intervention", "Intervention effectuée")
        add("test_final", "Test final")
        add("fournitures", "Fournitures notées", optional=True)
        add("note_finale", "Note de visite", optional=True)
    elif type_visite == "hivernage":
        steps = parse_checklist_hivernage(client.get("checklist_hivernage"))
        photos_by_inst: dict[str, list[dict[str, Any]]] = {}
        try:
            cid = int(client.get("id") or 0)
            for ph in list_client_photos(cid):
                photos_by_inst.setdefault(ph["instruction_id"], []).append(ph)
        except Exception:
            photos_by_inst = {}
        for idx, step in enumerate(steps, start=1):
            iid = step.get("id") or slug_checklist_id(step["label"], idx)
            entry: dict[str, Any] = {
                "id": iid,
                "label": step["label"],
                "warn": None,
                "optional": False,
                "done": False,
                "photos": photos_by_inst.get(iid, []),
            }
            com = str(step.get("commentaire") or "").strip()
            if com:
                entry["commentaire"] = com
            items.append(entry)
    elif type_visite == "entretien_simple":
        # Entretien classique : analyse + lavage/rinçage + paniers éventuels
        add("analyse", "Analyse")
        add("lavage_filtre", "Lavage et rinçage du filtre")
        add("paniers", "Nettoyage des paniers de skimmer", optional=True)
    else:
        # entretien complet
        add("analyse", "Lancer l'analyse")
        if client.get("vanne_vidange"):
            add(
                "vanne_ouvrir",
                "Ouvrir la vanne de vidange",
                warn="Attention : vanne de vidange présente",
            )
        add("lavage_filtre", "Lavage / rinçage du filtre")
        if client.get("vanne_vidange"):
            add("vanne_fermer", "Fermer la vanne de vidange")
        add("paniers", "Nettoyage des paniers / skimmer")
        add("robot", "Nettoyage du robot")
        add("parois", "Brossage des parois")
        add("ligne_eau", "Nettoyage ligne d'eau")
        add("local_technique", "Contrôle local technique")
        if client.get("injecteurs"):
            add("injecteurs", "Inverser les injecteurs")
        add("traitement", "Traitement fait", optional=True)
        add("remplissage", "Remplissage effectué", optional=True)
        add("fournitures", "Fournitures notées", optional=True)
        add("note_finale", "Note de visite", optional=True)

    return items


try:
    migrate_checklist_hivernage_ids()
except Exception as e:
    logging.getLogger("aquaclient").warning("Migration checklist ids ignorée : %s", e)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/api/adresses")
async def suggere_adresses(q: str = ""):
    query = q.strip()
    if len(query) < 3:
        return []
    params: dict[str, Any] = {"q": query, "limit": 7}
    cp = re.search(r"\b(\d{5})\b", query)
    if cp:
        params["postcode"] = cp.group(1)
    async with httpx.AsyncClient(timeout=6.0) as client:
        resp = await client.get("https://api-adresse.data.gouv.fr/search/", params=params)
        resp.raise_for_status()
        data = resp.json()
    out = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or [None, None]
        label = props.get("label") or ""
        if not label:
            continue
        out.append({
            "label": label,
            "postcode": props.get("postcode") or "",
            "city": props.get("city") or "",
            "latitude": float(coords[1]),
            "longitude": float(coords[0]),
        })
    # priorité code postal saisi
    if cp:
        code = cp.group(1)
        out.sort(key=lambda x: (0 if x.get("postcode") == code else 1, x["label"]))
    return out


@app.get("/api/clients")
def list_clients():
    """Liste toujours depuis le stockage actif (Supabase si connecté)."""
    rows = read_csv(CLIENTS_CSV)
    return [row_client(r) for r in rows]


@app.get("/api/agenda")
def agenda_passages(jours: int = 60):
    """Agenda des passages planifiés (entretien complet)."""
    horizon = max(7, min(int(jours or 60), 180))
    events: list[dict[str, Any]] = []
    for r in read_csv(CLIENTS_CSV):
        client = row_client(r)
        if not client.get("frequence_passage") or not client.get("date_debut_passage"):
            continue
        for d in client.get("prochains_passages") or []:
            # filtrer selon horizon
            dt = parse_iso_date(d)
            if not dt:
                continue
            if (dt - datetime.now()).days > horizon:
                continue
            events.append({
                "date": d,
                "frequence": client["frequence_passage"],
                "client": {
                    "id": client["id"],
                    "nom": client["nom"],
                    "prenom": client["prenom"],
                    "adresse": client["adresse"],
                    "type_entretien": client["type_entretien"],
                },
            })
    events.sort(key=lambda e: (e["date"], e["client"]["nom"]))
    return {"passages": events}


@app.post("/api/clients")
async def create_client(body: NouveauClient):
    if body.latitude is not None and body.longitude is not None:
        lat, lon = body.latitude, body.longitude
    else:
        if len(body.adresse.strip()) < 5:
            raise HTTPException(status_code=400, detail="Adresse ou GPS requis.")
        lat, lon = await geocode_ban(body.adresse)

    email, telephone = valider_coords_compte(body.creer_compte, body.email, body.telephone)
    rows = read_csv(CLIENTS_CSV)
    ensure_unique_login(rows, email, telephone)

    password_clair = None
    password_hash = ""
    compte_actif = "0"
    auth_uid = ""
    identifiant = generer_identifiant(rows)
    new_id = next_id(rows)

    if body.creer_compte:
        password_clair = (body.password or "").strip() or generer_mot_de_passe()
        if len(password_clair) < 6:
            raise HTTPException(status_code=400, detail="Mot de passe : 6 caractères minimum.")
        try:
            auth_uid = supabase_db.create_auth_user(
                email=email,
                telephone=telephone,
                password=password_clair,
                identifiant=identifiant,
                prenom=body.prenom.strip(),
                nom=body.nom.strip(),
                client_id=new_id,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        compte_actif = "1"
        # Mot de passe géré par Supabase Auth — pas de hash local
        password_hash = ""

    row = {
        "id": new_id,
        "nom": body.nom.strip(),
        "prenom": body.prenom.strip(),
        "adresse": body.adresse.strip(),
        "latitude": f"{lat:.6f}",
        "longitude": f"{lon:.6f}",
        "pompe": body.pompe.strip(),
        "filtre": body.filtre.strip(),
        "robot": body.robot.strip(),
        "skimmer": body.skimmer.strip(),
        "vanne_vidange": bool_csv(body.vanne_vidange),
        "injecteurs": bool_csv(body.injecteurs),
        "autres": body.autres.strip(),
        "type_entretien": normalize_type_entretien(body.type_entretien),
        "frequence_passage": normalize_frequence(body.frequence_passage)
        if normalize_type_entretien(body.type_entretien) == "entretien_complet"
        else "",
        "date_debut_passage": (body.date_debut_passage or "").strip()[:10]
        if normalize_type_entretien(body.type_entretien) == "entretien_complet"
        else "",
        "identifiant": identifiant,
        "email": email,
        "telephone": telephone,
        "password_hash": password_hash,
        "compte_actif": compte_actif,
        "auth_uid": auth_uid,
        **_options_fields_from_body(body),
        "piscine_hivernee": bool_csv(bool(body.piscine_hivernee)),
        "hivernage_en_cours": "0",
    }
    rows.append(row)
    write_csv(CLIENTS_CSV, CLIENT_FIELDS, rows)
    out = row_client(row, password_clair=password_clair)
    out["storage"] = "supabase" if supabase_db.enabled() else "csv"
    out["photos"] = []
    return out


@app.put("/api/clients/{client_id}")
async def update_client(client_id: int, body: ModifierClient):
    rows = read_csv(CLIENTS_CSV)
    target = None
    for r in rows:
        if int(r["id"]) == client_id:
            target = r
            break
    if target is None:
        raise HTTPException(status_code=404, detail="Client introuvable")

    if body.latitude is not None and body.longitude is not None:
        lat, lon = body.latitude, body.longitude
    elif body.adresse.strip() != (target.get("adresse") or "").strip():
        lat, lon = await geocode_ban(body.adresse)
    else:
        try:
            lat = float(target["latitude"])
            lon = float(target["longitude"])
        except (TypeError, ValueError, KeyError):
            lat, lon = await geocode_ban(body.adresse)

    email, telephone = valider_coords_compte(body.creer_compte, body.email, body.telephone)
    ensure_unique_login(rows, email, telephone, exclude_id=client_id)

    password_clair = None
    password_hash = (target.get("password_hash") or "").strip()
    auth_uid = (target.get("auth_uid") or "").strip()
    compte_actif = "1" if body.creer_compte else "0"

    if not (target.get("identifiant") or "").strip():
        target["identifiant"] = generer_identifiant(rows)
    identifiant = target["identifiant"]

    if body.creer_compte:
        pwd = (body.password or "").strip()
        if pwd and len(pwd) < 6:
            raise HTTPException(status_code=400, detail="Mot de passe : 6 caractères minimum.")
        try:
            if auth_uid:
                supabase_db.update_auth_user(
                    auth_uid,
                    email=email,
                    telephone=telephone,
                    password=pwd or None,
                    identifiant=identifiant,
                    prenom=body.prenom.strip(),
                    nom=body.nom.strip(),
                    client_id=client_id,
                    disable=False,
                )
                if pwd:
                    password_clair = pwd
                    password_hash = ""
            else:
                password_clair = pwd or generer_mot_de_passe()
                auth_uid = supabase_db.create_auth_user(
                    email=email,
                    telephone=telephone,
                    password=password_clair,
                    identifiant=identifiant,
                    prenom=body.prenom.strip(),
                    nom=body.nom.strip(),
                    client_id=client_id,
                )
                password_hash = ""
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    elif auth_uid:
        # Compte désactivé côté fiche : ban Auth (réversible)
        try:
            supabase_db.update_auth_user(
                auth_uid,
                email=email or (target.get("email") or ""),
                telephone=telephone or (target.get("telephone") or ""),
                identifiant=identifiant,
                prenom=body.prenom.strip(),
                nom=body.nom.strip(),
                client_id=client_id,
                disable=True,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    target.update({
        "nom": body.nom.strip(),
        "prenom": body.prenom.strip(),
        "adresse": body.adresse.strip(),
        "latitude": f"{lat:.6f}",
        "longitude": f"{lon:.6f}",
        "pompe": body.pompe.strip(),
        "filtre": body.filtre.strip(),
        "robot": body.robot.strip(),
        "skimmer": body.skimmer.strip(),
        "vanne_vidange": bool_csv(body.vanne_vidange),
        "injecteurs": bool_csv(body.injecteurs),
        "autres": body.autres.strip(),
        "type_entretien": normalize_type_entretien(body.type_entretien),
        "frequence_passage": normalize_frequence(body.frequence_passage)
        if normalize_type_entretien(body.type_entretien) == "entretien_complet"
        else "",
        "date_debut_passage": (body.date_debut_passage or "").strip()[:10]
        if normalize_type_entretien(body.type_entretien) == "entretien_complet"
        else "",
        "email": email,
        "telephone": telephone,
        "password_hash": password_hash,
        "compte_actif": compte_actif,
        "auth_uid": auth_uid,
        **_options_fields_from_body(body, existing=target),
        "piscine_hivernee": bool_csv(
            bool(body.piscine_hivernee)
            if body.piscine_hivernee is not None
            else from_bool_csv(target.get("piscine_hivernee", ""))
        ),
        "hivernage_en_cours": (
            "0"
            if body.piscine_hivernee is True
            else target.get("hivernage_en_cours", "0")
        ),
    })
    write_csv(CLIENTS_CSV, CLIENT_FIELDS, rows)
    out = row_client(target, password_clair=password_clair)
    out["photos"] = list_client_photos(client_id)
    return out


def _purge_client_data(client_ids: set[int]) -> None:
    def purge(path: Path, fields: list[str]) -> None:
        data = read_csv(path)
        write_csv(
            path,
            fields,
            [r for r in data if int(r.get("client_id") or 0) not in client_ids],
        )

    purge(NOTES_CSV, NOTE_FIELDS)
    purge(FOURNITURES_CSV, FOURNITURE_FIELDS)
    purge(ANALYSES_CSV, ANALYSE_FIELDS)
    purge(VISITES_CSV, VISITE_FIELDS)
    if supabase_db.enabled():
        supabase_db.delete_photos_for_clients(client_ids)
    else:
        rows = [
            r for r in read_photos_meta()
            if int(r.get("client_id") or 0) not in client_ids
        ]
        write_photos_meta(rows)
        for cid in client_ids:
            folder = PHOTOS_DIR / str(cid)
            if folder.is_dir():
                for f in folder.rglob("*"):
                    if f.is_file():
                        f.unlink()
                # nettoyage dossiers vides
                for d in sorted(folder.rglob("*"), reverse=True):
                    if d.is_dir():
                        try:
                            d.rmdir()
                        except OSError:
                            pass
                try:
                    folder.rmdir()
                except OSError:
                    pass


def _delete_auth_for_clients(rows: list[dict[str, str]], client_ids: set[int]) -> None:
    for r in rows:
        try:
            cid = int(r.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if cid not in client_ids:
            continue
        uid = (r.get("auth_uid") or "").strip()
        if uid:
            supabase_db.delete_auth_user(uid)


@app.delete("/api/clients/{client_id}")
def delete_client(client_id: int):
    rows = read_csv(CLIENTS_CSV)
    kept = [r for r in rows if int(r["id"]) != client_id]
    if len(kept) == len(rows):
        raise HTTPException(status_code=404, detail="Client introuvable")
    _delete_auth_for_clients(rows, {client_id})
    write_csv(CLIENTS_CSV, CLIENT_FIELDS, kept)
    _purge_client_data({client_id})
    return {"ok": True, "id": client_id}


class ClientsSuppression(BaseModel):
    ids: list[int]


@app.post("/api/clients/supprimer")
def delete_clients_bulk(body: ClientsSuppression):
    ids: set[int] = set()
    for i in body.ids:
        try:
            ids.add(int(i))
        except (TypeError, ValueError):
            continue
    if not ids:
        raise HTTPException(status_code=400, detail="Aucun client sélectionné")
    rows = read_csv(CLIENTS_CSV)
    kept = [r for r in rows if int(r["id"]) not in ids]
    deleted = len(rows) - len(kept)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Aucun client trouvé")
    _delete_auth_for_clients(rows, ids)
    write_csv(CLIENTS_CSV, CLIENT_FIELDS, kept)
    _purge_client_data(ids)
    return {"ok": True, "supprimes": deleted}

@app.post("/api/clients/proche")
def client_proche(pos: Position):
    rows = read_csv(CLIENTS_CSV)
    meilleur = None
    meilleure_dist = float("inf")
    for r in rows:
        if not r.get("latitude") or not r.get("longitude"):
            continue
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except ValueError:
            continue
        d = haversine_m(pos.latitude, pos.longitude, lat, lon)
        if d < meilleure_dist:
            meilleure_dist = d
            meilleur = r
    if meilleur is None:
        return {"trouve": False, "message": "Aucun client en base",
                "votre_position": {"latitude": pos.latitude, "longitude": pos.longitude}}
    client = row_client(meilleur)
    dist = round(meilleure_dist)
    payload = {
        "distance_m": dist,
        "client": client,
        "votre_position": {"latitude": pos.latitude, "longitude": pos.longitude},
    }
    if meilleure_dist <= RAYON_RECHERCHE_M:
        return {"trouve": True, **payload}
    return {"trouve": False, "message": "Aucun client dans 1 km", "plus_proche": client, **payload}


@app.patch("/api/clients/{client_id}/position")
def update_position(client_id: int, pos: PositionClient):
    rows = read_csv(CLIENTS_CSV)
    for r in rows:
        if int(r["id"]) == client_id:
            r["latitude"] = f"{pos.latitude:.6f}"
            r["longitude"] = f"{pos.longitude:.6f}"
            write_csv(CLIENTS_CSV, CLIENT_FIELDS, rows)
            return row_client(r)
    raise HTTPException(status_code=404, detail="Client introuvable")


@app.get("/api/clients/{client_id}/photos")
def list_photos_client(client_id: int, instruction_id: str | None = None):
    rows = read_csv(CLIENTS_CSV)
    if not any(int(r["id"]) == client_id for r in rows):
        raise HTTPException(status_code=404, detail="Client introuvable")
    return {"photos": list_client_photos(client_id, instruction_id)}


@app.post("/api/clients/{client_id}/photos")
async def upload_photo_client(
    client_id: int,
    instruction_id: str = Form(...),
    file: UploadFile = File(...),
):
    rows = read_csv(CLIENTS_CSV)
    target = next((r for r in rows if int(r["id"]) == client_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Client introuvable")
    iid = (instruction_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="instruction_id requis")
    steps = parse_checklist_hivernage(target.get("checklist_hivernage"))
    known_ids = {s["id"] for s in steps}
    if known_ids and iid not in known_ids:
        raise HTTPException(
            status_code=400,
            detail="Instruction introuvable sur ce client. Enregistrez la checklist d'abord.",
        )
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Fichier vide")
    return persist_photo(client_id=client_id, instruction_id=iid, raw=raw)


@app.get("/api/photos/{photo_id}/file")
def serve_photo_file(photo_id: str):
    row = get_photo_row(photo_id)
    if not row:
        raise HTTPException(status_code=404, detail="Photo introuvable")
    try:
        data = load_photo_bytes(row)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Fichier photo manquant") from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lecture photo : {e}") from e
    mime = str(row.get("mime_type") or "image/webp")
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: str):
    delete_photo_row(photo_id)
    return {"ok": True, "id": photo_id}


@app.get("/api/clients/{client_id}")
def get_client(client_id: int):
    for r in read_csv(CLIENTS_CSV):
        if int(r["id"]) == client_id:
            notes = [n for n in read_csv(NOTES_CSV) if int(n["client_id"]) == client_id]
            fournitures = [f for f in read_csv(FOURNITURES_CSV) if int(f["client_id"]) == client_id]
            analyses = [a for a in read_csv(ANALYSES_CSV) if int(a["client_id"]) == client_id]
            visites = [v for v in read_csv(VISITES_CSV) if int(v["client_id"]) == client_id]
            return {
                **row_client(r),
                "notes": notes,
                "fournitures": fournitures,
                "analyses": analyses,
                "visites": visites,
                "photos": list_client_photos(client_id),
            }
    raise HTTPException(status_code=404, detail="Client introuvable")


def _checklist_is_complete(checklist: list[Any] | None) -> bool:
    items = checklist or []
    required = [i for i in items if isinstance(i, dict) and not i.get("optional")]
    pool = required if required else [i for i in items if isinstance(i, dict)]
    if not pool:
        return True  # aucune étape → considéré terminé
    return all(bool(i.get("done")) for i in pool)


def _visite_duree_prev(r: dict[str, str]) -> float:
    try:
        return float(str(r.get("duree_minutes") or "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _find_resumable_hivernage(client_id: int) -> dict[str, str] | None:
    """Dernière visite hivernage non terminée (checklist incomplète)."""
    best: dict[str, str] | None = None
    best_id = -1
    for r in read_csv(VISITES_CSV):
        try:
            if int(r.get("client_id") or 0) != int(client_id):
                continue
        except (TypeError, ValueError):
            continue
        if (r.get("type") or "") != "hivernage":
            continue
        try:
            checklist = json.loads(r.get("checklist_json") or "[]")
        except json.JSONDecodeError:
            checklist = []
        if _checklist_is_complete(checklist):
            continue
        try:
            vid = int(r.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if vid > best_id:
            best_id = vid
            best = r
    return best


def _set_client_hivernage_statut(client_id: int, *, hivernee: bool, en_cours: bool) -> None:
    try:
        if supabase_db.enabled():
            crow = supabase_db.read_row("clients", client_id, CLIENT_FIELDS)
            if crow:
                crow["piscine_hivernee"] = bool_csv(hivernee)
                crow["hivernage_en_cours"] = bool_csv(en_cours)
                write_one_row(CLIENTS_CSV, CLIENT_FIELDS, crow)
            return
        clients = read_csv(CLIENTS_CSV)
        for c in clients:
            if int(c["id"]) == client_id:
                c["piscine_hivernee"] = bool_csv(hivernee)
                c["hivernage_en_cours"] = bool_csv(en_cours)
                write_csv(CLIENTS_CSV, CLIENT_FIELDS, clients)
                break
    except Exception:
        logging.getLogger("aquaclient").exception(
            "Maj statut hivernage client %s", client_id
        )


def _ensure_facture_hivernage(client_id: int, visite_id: int) -> dict[str, str] | None:
    """Ajoute 1× Hivernage en facture (idempotent via date_ref=v{visite_id})."""
    marker = f"v{visite_id}"
    rows = read_csv(FOURNITURES_CSV)
    for r in rows:
        try:
            if int(r.get("client_id") or 0) != int(client_id):
                continue
        except (TypeError, ValueError):
            continue
        if (r.get("date_ref") or "").strip() == marker:
            return r
        desc = (r.get("description") or "").strip().lower()
        if desc == "hivernage" and (r.get("date_ref") or "").strip() == marker:
            return r
    fid = next_id(rows)
    row = {
        "id": fid,
        "client_id": client_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "date_ref": marker,
        "description": "Hivernage",
        "quantite": "1",
        "envoyee": "non",
        "payee": "non",
    }
    rows.append(row)
    write_csv(FOURNITURES_CSV, FOURNITURE_FIELDS, rows)
    return row


@app.post("/api/visites")
def create_visite(body: VisiteCreate):
    if body.type not in {"depannage", "entretien_simple", "entretien_complet", "hivernage"}:
        raise HTTPException(status_code=400, detail="Type de visite invalide")
    client = None
    for r in read_csv(CLIENTS_CSV):
        if int(r["id"]) == body.client_id:
            client = row_client(r)
            break
    if not client:
        raise HTTPException(status_code=404, detail="Client introuvable")

    # Reprise d'un hivernage déjà commencé (checklist incomplète)
    if body.type == "hivernage":
        existing = _find_resumable_hivernage(body.client_id)
        if existing:
            return _visite_payload(existing, client)

    checklist = build_checklist(client, body.type)
    rows = read_csv(VISITES_CSV)
    vid = next_id(rows)
    row = {
        "id": vid,
        "client_id": body.client_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "type": body.type,
        "checklist_json": json.dumps(checklist, ensure_ascii=False),
        "debut": "",
        "fin": "",
        "duree_minutes": "",
    }
    rows.append(row)
    write_csv(VISITES_CSV, VISITE_FIELDS, rows)
    return _visite_payload(row, client)


def _visite_payload(
    r: dict[str, str],
    client: dict[str, Any] | None = None,
    *,
    include_client: bool = True,
    include_photos: bool = True,
) -> dict[str, Any]:
    if include_client and client is None:
        # Un seul client, pas toute la table si possible
        cid = int(r["client_id"])
        if supabase_db.enabled():
            crow = supabase_db.read_row("clients", cid, CLIENT_FIELDS)
            client = row_client(crow) if crow else None
        else:
            client = next(
                (row_client(c) for c in read_csv(CLIENTS_CSV) if int(c["id"]) == cid),
                None,
            )
    duree = r.get("duree_minutes") or ""
    checklist = json.loads(r.get("checklist_json") or "[]")
    if include_photos and (r.get("type") or "") == "hivernage":
        try:
            photos = list_client_photos(int(r["client_id"]))
            by_inst: dict[str, list[dict[str, Any]]] = {}
            for ph in photos:
                by_inst.setdefault(ph["instruction_id"], []).append(ph)
            for item in checklist:
                if isinstance(item, dict):
                    item["photos"] = by_inst.get(str(item.get("id") or ""), [])
        except Exception:
            pass
    out: dict[str, Any] = {
        "id": int(r["id"]),
        "client_id": int(r["client_id"]),
        "date": r.get("date", ""),
        "type": r.get("type", ""),
        "checklist": checklist,
        "debut": r.get("debut") or None,
        "fin": r.get("fin") or None,
        "duree_minutes": float(duree) if str(duree).strip() else None,
    }
    if include_client:
        out["client"] = client
    return out


@app.get("/api/visites/{visite_id}")
def get_visite(visite_id: int):
    r = read_visite_row(visite_id)
    if not r:
        raise HTTPException(status_code=404, detail="Visite introuvable")
    return _visite_payload(r)


@app.post("/api/visites/{visite_id}/debut")
def debut_prestation(visite_id: int):
    r = read_visite_row(visite_id)
    if not r:
        raise HTTPException(status_code=404, detail="Visite introuvable")
    if r.get("debut") and not r.get("fin"):
        return _visite_payload(r, include_client=False, include_photos=False)

    is_hiv = (r.get("type") or "") == "hivernage"
    try:
        checklist = json.loads(r.get("checklist_json") or "[]")
    except json.JSONDecodeError:
        checklist = []
    resume_hiv = bool(is_hiv and r.get("fin") and not _checklist_is_complete(checklist))

    r["debut"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    r["fin"] = ""
    if not resume_hiv:
        # Nouvelle session (hors reprise hivernage) : reset du chrono
        r["duree_minutes"] = ""
    # Reprise : on conserve duree_minutes déjà accumulée
    write_one_row(VISITES_CSV, VISITE_FIELDS, r)
    return _visite_payload(r, include_client=False, include_photos=False)


@app.post("/api/visites/{visite_id}/fin")
def fin_prestation(visite_id: int):
    r = read_visite_row(visite_id)
    if not r:
        raise HTTPException(status_code=404, detail="Visite introuvable")
    if not r.get("debut"):
        raise HTTPException(status_code=400, detail="Appuyez d'abord sur Début de prestation.")
    if r.get("fin"):
        return _visite_payload(r, include_client=False, include_photos=False)

    try:
        checklist = json.loads(r.get("checklist_json") or "[]")
    except json.JSONDecodeError:
        checklist = []
    is_hiv = (r.get("type") or "") == "hivernage"
    complete = _checklist_is_complete(checklist)

    # Hors hivernage : toutes les étapes obligatoires requises
    if not is_hiv and not complete:
        raise HTTPException(
            status_code=400,
            detail="Cochez toutes les étapes obligatoires avant de terminer.",
        )

    fin = datetime.now()
    try:
        debut = datetime.strptime(r["debut"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        debut = datetime.strptime(r["debut"][:16], "%Y-%m-%d %H:%M")
    session_min = round((fin - debut).total_seconds() / 60, 1)
    total_min = round(_visite_duree_prev(r) + session_min, 1)
    r["fin"] = fin.strftime("%Y-%m-%d %H:%M:%S")
    r["duree_minutes"] = str(total_min)
    write_one_row(VISITES_CSV, VISITE_FIELDS, r)

    facture = None
    if is_hiv:
        cid = int(r["client_id"])
        if complete:
            _set_client_hivernage_statut(cid, hivernee=True, en_cours=False)
            facture = _ensure_facture_hivernage(cid, int(r["id"]))
        else:
            _set_client_hivernage_statut(cid, hivernee=False, en_cours=True)

    payload = _visite_payload(r, include_client=False, include_photos=False)
    if is_hiv:
        payload["hivernage_complete"] = complete
        payload["hivernage_en_cours"] = not complete
        if facture:
            payload["facture_hivernage"] = {
                "id": int(facture["id"]),
                "description": facture.get("description") or "Hivernage",
                "quantite": facture.get("quantite") or "1",
            }
    return payload


@app.patch("/api/visites/{visite_id}/checklist")
def update_checklist(visite_id: int, body: ChecklistUpdate):
    r = read_visite_row(visite_id)
    if not r:
        raise HTTPException(status_code=404, detail="Visite introuvable")
    checklist = json.loads(r.get("checklist_json") or "[]")
    by_id = {i["id"]: i for i in checklist}
    if body.item_id == "lavage_filtre" and body.done:
        if "vanne_ouvrir" in by_id and not by_id["vanne_ouvrir"].get("done"):
            raise HTTPException(
                status_code=400,
                detail="Ouvrez d'abord la vanne de vidange avant le lavage du filtre.",
            )
    if body.item_id == "vanne_fermer" and body.done:
        if "lavage_filtre" in by_id and not by_id["lavage_filtre"].get("done"):
            raise HTTPException(
                status_code=400,
                detail="Faites d'abord le lavage du filtre avant de fermer la vanne.",
            )
    found = False
    for item in checklist:
        if item["id"] == body.item_id:
            item["done"] = body.done
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="Étape introuvable")
    r["checklist_json"] = json.dumps(checklist, ensure_ascii=False)
    write_one_row(VISITES_CSV, VISITE_FIELDS, r)
    # Réponse légère : pas de rechargement photos / client
    return _visite_payload(r, include_client=False, include_photos=False)


@app.post("/api/ia")
def commande_ia(body: CommandeIA):
    clients = {int(r["id"]) for r in read_csv(CLIENTS_CSV)}
    if body.client_id not in clients:
        raise HTTPException(status_code=404, detail="Client introuvable")

    parsed = comprendre_commande(body.texte, body.intent_force)
    if not parsed.get("ok"):
        return {"success": False, **parsed}

    date = parsed.get("date") or datetime.now().strftime("%Y-%m-%d")
    date_ref = (parsed.get("date_ref") or "").strip()[:10]

    if parsed["intent"] == "note":
        rows = read_csv(NOTES_CSV)
        nid = next_id(rows)
        row = {
            "id": nid,
            "client_id": body.client_id,
            "date": date,
            "date_ref": date_ref,
            "texte": parsed["texte"],
        }
        rows.append(row)
        write_csv(NOTES_CSV, NOTE_FIELDS, rows)
        return {"success": True, "intent": "note", "note": row}

    if parsed["intent"] == "analyse":
        rows = read_csv(ANALYSES_CSV)
        aid = next_id(rows)
        row = {
            "id": aid,
            "client_id": body.client_id,
            "date": date,
            "date_ref": date_ref,
            "texte": parsed["texte"],
        }
        rows.append(row)
        write_csv(ANALYSES_CSV, ANALYSE_FIELDS, rows)
        return {"success": True, "intent": "analyse", "analyse": row}

    if parsed["intent"] == "fourniture":
        rows = read_csv(FOURNITURES_CSV)
        fid = next_id(rows)
        row = {
            "id": fid,
            "client_id": body.client_id,
            "date": date,
            "date_ref": date_ref,
            "description": parsed["description"],
            "quantite": parsed["quantite"],
            "envoyee": "non",
            "payee": "non",
        }
        rows.append(row)
        write_csv(FOURNITURES_CSV, FOURNITURE_FIELDS, rows)
        return {"success": True, "intent": "fourniture", "fourniture": row}

    return {"success": False, **parsed}


def _find_row(path: Path, item_id: int) -> tuple[list[dict[str, str]], dict[str, str] | None]:
    rows = read_csv(path)
    for r in rows:
        if str(r.get("id", "")).isdigit() and int(r["id"]) == item_id:
            return rows, r
    return rows, None


@app.put("/api/notes/{note_id}")
def update_note(note_id: int, body: TexteUpdate):
    rows, row = _find_row(NOTES_CSV, note_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Note introuvable")
    row["texte"] = body.texte.strip()
    if body.date is not None:
        row["date"] = body.date.strip()[:10]
    if body.date_ref is not None:
        row["date_ref"] = body.date_ref.strip()[:10]
    write_csv(NOTES_CSV, NOTE_FIELDS, rows)
    return row


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: int):
    rows, row = _find_row(NOTES_CSV, note_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Note introuvable")
    write_csv(NOTES_CSV, NOTE_FIELDS, [r for r in rows if int(r["id"]) != note_id])
    return {"ok": True, "id": note_id}


@app.put("/api/analyses/{analyse_id}")
def update_analyse(analyse_id: int, body: TexteUpdate):
    rows, row = _find_row(ANALYSES_CSV, analyse_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Analyse introuvable")
    row["texte"] = body.texte.strip()
    if body.date is not None:
        row["date"] = body.date.strip()[:10]
    if body.date_ref is not None:
        row["date_ref"] = body.date_ref.strip()[:10]
    write_csv(ANALYSES_CSV, ANALYSE_FIELDS, rows)
    return row


@app.delete("/api/analyses/{analyse_id}")
def delete_analyse(analyse_id: int):
    rows, row = _find_row(ANALYSES_CSV, analyse_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Analyse introuvable")
    write_csv(ANALYSES_CSV, ANALYSE_FIELDS, [r for r in rows if int(r["id"]) != analyse_id])
    return {"ok": True, "id": analyse_id}


@app.put("/api/fournitures/{fourniture_id}")
def update_fourniture(fourniture_id: int, body: FournitureUpdate):
    rows, row = _find_row(FOURNITURES_CSV, fourniture_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Fourniture introuvable")
    row["description"] = body.description.strip()
    row["quantite"] = body.quantite
    if body.date is not None:
        row["date"] = body.date.strip()[:10]
    if body.date_ref is not None:
        row["date_ref"] = body.date_ref.strip()[:10]
    if body.envoyee is not None:
        row["envoyee"] = "oui" if body.envoyee else "non"
    if body.payee is not None:
        row["payee"] = "oui" if body.payee else "non"
    row["envoyee"] = _norm_oui_non(row.get("envoyee"))
    row["payee"] = _norm_oui_non(row.get("payee"))
    write_csv(FOURNITURES_CSV, FOURNITURE_FIELDS, rows)
    return row


@app.delete("/api/fournitures/{fourniture_id}")
def delete_fourniture(fourniture_id: int):
    rows, row = _find_row(FOURNITURES_CSV, fourniture_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Fourniture introuvable")
    write_csv(FOURNITURES_CSV, FOURNITURE_FIELDS, [r for r in rows if int(r["id"]) != fourniture_id])
    return {"ok": True, "id": fourniture_id}


def _facture_enrichie(row: dict[str, str], clients_by_id: dict[int, dict[str, str]]) -> dict[str, Any]:
    cid = int(row["client_id"]) if str(row.get("client_id", "")).isdigit() else 0
    client = clients_by_id.get(cid) or {}
    return {
        "id": int(row["id"]) if str(row.get("id", "")).isdigit() else row.get("id"),
        "client_id": cid,
        "client_nom": (client.get("nom") or "").strip(),
        "client_prenom": (client.get("prenom") or "").strip(),
        "client_label": f"{(client.get('nom') or '').strip()} {(client.get('prenom') or '').strip()}".strip()
        or f"Client #{cid}",
        "date": (row.get("date") or "")[:10],
        "date_ref": (row.get("date_ref") or "")[:10],
        "description": row.get("description") or "",
        "quantite": row.get("quantite") or "1",
        "envoyee": _norm_oui_non(row.get("envoyee")),
        "payee": _norm_oui_non(row.get("payee")),
    }


@app.get("/api/dashboard")
def dashboard_overview():
    """Vue d'ensemble : clients, impayés, statut hivernage."""
    clients_raw = read_csv(CLIENTS_CSV)
    factures = read_csv(FOURNITURES_CSV)
    visites = read_csv(VISITES_CSV)

    unpaid_by: dict[int, int] = {}
    amount_hint_by: dict[int, int] = {}
    for f in factures:
        try:
            cid = int(f.get("client_id") or 0)
        except (TypeError, ValueError):
            continue
        if _norm_oui_non(f.get("payee")) == "oui":
            continue
        unpaid_by[cid] = unpaid_by.get(cid, 0) + 1
        amount_hint_by[cid] = amount_hint_by.get(cid, 0) + 1

    last_hiv_by: dict[int, str] = {}
    last_visite_by: dict[int, str] = {}
    for v in visites:
        try:
            cid = int(v.get("client_id") or 0)
        except (TypeError, ValueError):
            continue
        date = (v.get("date") or v.get("fin") or "")[:16]
        if date and (cid not in last_visite_by or date > last_visite_by[cid]):
            last_visite_by[cid] = date
        if (v.get("type") or "") == "hivernage" and (v.get("fin") or "").strip():
            try:
                cl = json.loads(v.get("checklist_json") or "[]")
            except json.JSONDecodeError:
                cl = []
            if not _checklist_is_complete(cl):
                continue
            fin = (v.get("fin") or date)[:16]
            if fin and (cid not in last_hiv_by or fin > last_hiv_by[cid]):
                last_hiv_by[cid] = fin

    rows: list[dict[str, Any]] = []
    for r in clients_raw:
        c = row_client(r)
        cid = c["id"]
        unpaid = unpaid_by.get(cid, 0)
        rows.append({
            "id": cid,
            "nom": c["nom"],
            "prenom": c["prenom"],
            "label": f'{c["prenom"]} {c["nom"]}'.strip(),
            "adresse": c["adresse"],
            "piscine_hivernee": bool(c.get("piscine_hivernee")),
            "hivernage_en_cours": bool(c.get("hivernage_en_cours")),
            "factures_impayees": unpaid,
            "a_impaye": unpaid > 0,
            "derniere_visite": last_visite_by.get(cid) or None,
            "dernier_hivernage": last_hiv_by.get(cid) or None,
            "type_entretien": c.get("type_entretien") or "",
            "prochain_passage": c.get("prochain_passage"),
        })

    rows.sort(key=lambda x: (x["nom"] or "").lower())
    hivernees = sum(1 for x in rows if x["piscine_hivernee"])
    en_cours = sum(1 for x in rows if x["hivernage_en_cours"] and not x["piscine_hivernee"])
    avec_impayes = sum(1 for x in rows if x["a_impaye"])
    return {
        "total_clients": len(rows),
        "piscines_hivernees": hivernees,
        "hivernages_en_cours": en_cours,
        "clients_impayes": avec_impayes,
        "factures_impayees_total": sum(unpaid_by.values()),
        "clients": rows,
    }


class HivernageStatutUpdate(BaseModel):
    piscine_hivernee: bool


@app.patch("/api/clients/{client_id}/hivernage-statut")
def patch_hivernage_statut(client_id: int, body: HivernageStatutUpdate):
    """Bascule rapide du statut piscine hivernée."""
    rows = read_csv(CLIENTS_CSV)
    for r in rows:
        if int(r["id"]) == client_id:
            r["piscine_hivernee"] = bool_csv(body.piscine_hivernee)
            if body.piscine_hivernee:
                r["hivernage_en_cours"] = "0"
            write_csv(CLIENTS_CSV, CLIENT_FIELDS, rows)
            return row_client(r)
    raise HTTPException(status_code=404, detail="Client introuvable")


@app.get("/api/factures")
def liste_factures(filtre: str = "toutes", limit: int = 0):
    """
    Liste des factures (fournitures) avec filtres :
    toutes | dernieres | envoyees | non_envoyees | payees | non_payees
    """
    clients_by_id = {
        int(c["id"]): c for c in read_csv(CLIENTS_CSV) if str(c.get("id", "")).isdigit()
    }
    rows = [_facture_enrichie(r, clients_by_id) for r in read_csv(FOURNITURES_CSV)]
    # Plus récentes d'abord
    rows.sort(key=lambda r: (r.get("date") or "", int(r["id"]) if isinstance(r["id"], int) else 0), reverse=True)

    f = (filtre or "toutes").strip().lower()
    if f in {"envoyees", "envoyee", "envoyées", "envoyée"}:
        rows = [r for r in rows if r["envoyee"] == "oui"]
    elif f in {"non_envoyees", "non_envoyee", "a_envoyer"}:
        rows = [r for r in rows if r["envoyee"] != "oui"]
    elif f in {"payees", "payee", "payées", "payée"}:
        rows = [r for r in rows if r["payee"] == "oui"]
    elif f in {"non_payees", "non_payee", "impayees", "impayées"}:
        rows = [r for r in rows if r["payee"] != "oui"]
    elif f in {"dernieres", "récentes", "recentes"}:
        rows = rows[:40]
    # toutes : pas de filtre

    if limit and limit > 0:
        rows = rows[:limit]

    return {
        "filtre": f,
        "total": len(rows),
        "factures": rows,
    }


@app.patch("/api/factures/{facture_id}")
def update_facture_statut(facture_id: int, body: FactureStatutUpdate):
    rows, row = _find_row(FOURNITURES_CSV, facture_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Facture introuvable")
    if body.envoyee is not None:
        row["envoyee"] = "oui" if body.envoyee else "non"
        # Si on annule l'envoi, on ne force pas le paiement
    if body.payee is not None:
        row["payee"] = "oui" if body.payee else "non"
        # Si payée, considérer envoyée
        if body.payee:
            row["envoyee"] = "oui"
    row["envoyee"] = _norm_oui_non(row.get("envoyee"))
    row["payee"] = _norm_oui_non(row.get("payee"))
    write_csv(FOURNITURES_CSV, FOURNITURE_FIELDS, rows)
    clients_by_id = {
        int(c["id"]): c for c in read_csv(CLIENTS_CSV) if str(c.get("id", "")).isdigit()
    }
    return _facture_enrichie(row, clients_by_id)


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    import numpy as np

    name = (audio.filename or "audio.webm").lower()
    suffix = Path(name).suffix.lower() or ".webm"
    # iOS envoie souvent audio/mp4 sans extension fiable
    ctype = (audio.content_type or "").lower()
    if suffix in {".bin", ".dat", ""} or suffix == ".wav" and "mp4" in ctype:
        if "mp4" in ctype or "aac" in ctype or "m4a" in ctype:
            suffix = ".mp4"
        elif "ogg" in ctype:
            suffix = ".ogg"
        elif "webm" in ctype:
            suffix = ".webm"
        elif "mpeg" in ctype or "mp3" in ctype:
            suffix = ".mp3"

    raw = await audio.read()
    if not raw or len(raw) < 120:
        raise HTTPException(
            status_code=400,
            detail="Enregistrement trop court. Maintenez le bouton et parlez 2–3 secondes.",
        )

    tmp_path = None
    try:
        samples = None
        if suffix == ".wav" or raw[:4] == b"RIFF":
            samples = wav_bytes_to_float32(raw)

        if samples is None:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name
            samples = audio_to_float32(tmp_path)

        if samples is None or len(samples) < 4800:  # ~0.3 s @ 16 kHz
            raise HTTPException(
                status_code=400,
                detail="Audio trop court. Maintenez plus longtemps en parlant.",
            )

        rms = float(np.sqrt(np.mean(samples ** 2)))
        peak = float(np.max(np.abs(samples)))
        if peak < 0.004 and rms < 0.0008:
            raise HTTPException(
                status_code=400,
                detail="Micro silencieux. Autorisez le micro et parlez près du téléphone.",
            )

        model = get_whisper()
        prompt = (
            "Commandes piscine en français : note, fourniture, facture, analyse. "
            "Exemples : fourniture 1 bidon de chlore ; note le filtre est sale ; "
            "analyse pH 7,2 chlore 1,5 ; facture 2 bidons de chlore."
        )
        texte = ""
        for kwargs in (
            dict(vad_filter=False, no_speech_threshold=0.45, beam_size=5, temperature=0.0),
            dict(vad_filter=False, no_speech_threshold=0.65, beam_size=5, temperature=0.2),
            dict(vad_filter=True, no_speech_threshold=0.5, beam_size=3, temperature=0.0),
        ):
            segments, _info = model.transcribe(
                samples,
                language="fr",
                condition_on_previous_text=False,
                initial_prompt=prompt,
                **kwargs,
            )
            texte = " ".join(seg.text.strip() for seg in segments).strip()
            texte = corriger_transcription(texte)
            if texte:
                break
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Transcription impossible: {exc}") from exc
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

    if not texte:
        raise HTTPException(
            status_code=400,
            detail="Rien compris. Dites clairement : note / fourniture / analyse…",
        )
    return {"texte": texte}


@app.get("/api/health")
def health():
    storage = "supabase" if supabase_db.enabled() else "csv"
    reason = None
    if storage == "csv":
        if not supabase_db.configured():
            reason = "missing_env"
        else:
            reason = "init_failed"
    clients_n = 0
    try:
        clients_n = len(read_csv(CLIENTS_CSV))
    except Exception:
        clients_n = -1
    return {
        "ok": True,
        "version": APP_VERSION,
        "storage": storage,
        "storage_reason": reason,
        "clients": clients_n,
        "supabase_configured": supabase_db.configured(),
        "supabase_auth_admin": supabase_db.has_service_role(),
    }


@app.get("/api/version")
def version():
    return {"version": APP_VERSION, "storage": "supabase" if supabase_db.enabled() else "csv"}


@app.post("/api/admin/init-supabase")
def admin_init_supabase():
    """Recrée / vérifie les tables Supabase (nécessite SUPABASE_DB_PASSWORD)."""
    if not supabase_db.configured():
        raise HTTPException(status_code=400, detail="SUPABASE_URL / SUPABASE_KEY manquants dans .env")
    ok_schema = True
    if not supabase_db.tables_exist():
        ok_schema = supabase_db.apply_schema()
        if not ok_schema:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Impossible de créer les tables. "
                    "Ajoute SUPABASE_DB_PASSWORD dans .env "
                    "(Supabase → Settings → Database → Database password) "
                    "ou colle le contenu de schema.sql dans le SQL Editor."
                ),
            )
    ready = supabase_db.init_supabase(
        csv_seed={
            "clients": (CLIENTS_CSV, CLIENT_FIELDS, _csv_read_local),
            "notes": (NOTES_CSV, NOTE_FIELDS, _csv_read_local),
            "analyses": (ANALYSES_CSV, ANALYSE_FIELDS, _csv_read_local),
            "fournitures": (FOURNITURES_CSV, FOURNITURE_FIELDS, _csv_read_local),
            "visites": (VISITES_CSV, VISITE_FIELDS, _csv_read_local),
        }
    )
    return {
        "ok": ready,
        "storage": "supabase" if ready else "csv",
        "schema_applied": ok_schema,
    }


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_api_and_index(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") or path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/")
def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend manquant")
    html = index_path.read_text(encoding="utf-8")
    # Cache-bust assets + version injectée pour PC = téléphone
    html = html.replace("/static/styles.css", f"/static/styles.css?v={APP_VERSION}")
    html = html.replace("/static/app.js", f"/static/app.js?v={APP_VERSION}")
    html = html.replace("__APP_VERSION__", APP_VERSION)
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/favicon.ico")
def favicon():
    ico = STATIC_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(ico, media_type="image/x-icon")
    svg = STATIC_DIR / "logo.svg"
    if svg.exists():
        return FileResponse(svg, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Favicon manquant")

