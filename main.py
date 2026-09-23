"""
API AquaClient — clients piscine, géoloc, check-up, notes & fournitures.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATA_DIR = Path(__file__).parent / "data"
CLIENTS_CSV = DATA_DIR / "clients.csv"
NOTES_CSV = DATA_DIR / "notes.csv"
FOURNITURES_CSV = DATA_DIR / "fournitures.csv"
ANALYSES_CSV = DATA_DIR / "analyses.csv"
VISITES_CSV = DATA_DIR / "visites.csv"
FACTURES_LEGACY = DATA_DIR / "factures.csv"
STATIC_DIR = Path(__file__).parent / "static"

DATA_DIR.mkdir(exist_ok=True)

CLIENT_FIELDS = [
    "id", "nom", "prenom", "adresse", "latitude", "longitude",
    "pompe", "filtre", "robot", "skimmer", "vanne_vidange", "injecteurs", "autres",
    "type_entretien", "frequence_passage", "date_debut_passage",
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
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


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
    normalized = [{k: r.get(k, "") for k in CLIENT_FIELDS} for r in rows]
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
    type: str  # depannage | entretien_simple | entretien_complet


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
        "quinze": "15",
        # « vingt » géré avant dans preparer_dates_orales
    }
    for mot, chiffre in nombres.items():
        t = re.sub(rf"\b{mot}\b", chiffre, t, flags=re.I)
    # « un/une bidon » → 1 bidon (seulement devant un produit)
    t = re.sub(
        r"\b(un|une)\s+(bidon|sac|cartouche|seau|litre|kg|paquet|boite|boîte)\b",
        r"1 \2",
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


def row_client(r: dict[str, str]) -> dict[str, Any]:
    freq = normalize_frequence(r.get("frequence_passage"))
    debut = (r.get("date_debut_passage") or "").strip()[:10]
    type_ent = normalize_type_entretien(r.get("type_entretien"))
    # Passage planifié seulement pour entretien complet
    if type_ent != "entretien_complet":
        freq = ""
        debut = ""
    prochaines = prochaines_dates_passage(debut, freq) if freq and debut else []
    return {
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
    }


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
        contenu = re.sub(r"^(une\s+)?(note|noter)[:\s,-]*", "", raw, count=1, flags=re.I).strip()
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
            r"^(une\s+)?(facture|facturer|fourniture|fournitures)[:\s,-]*",
            "",
            raw,
            count=1,
            flags=re.I,
        ).strip()
        body_l = body.lower()
        qty = 1.0
        # « 3 bidon… » ou « 3x bidon… » ou « x3 bidon… »
        m_qty = re.match(r"^(?:x\s*)?(\d+(?:[.,]\d+)?)\s*(?:x\s+)?(.+)$", body_l)
        if m_qty:
            qty = float(m_qty.group(1).replace(",", "."))
            desc = body[m_qty.start(2) : m_qty.start(2) + len(m_qty.group(2))]
        else:
            desc = body
        # retirer prix s'il est dit par habitude
        desc = re.sub(
            r"\s*(?:à|a|pour)?\s*\d+(?:[.,]\d+)?\s*(?:€|euros?|euro).*$",
            "",
            desc,
            flags=re.I,
        ).strip()
        desc = strip_date_from_text(desc)
        desc = re.sub(r"\s*l['']unité\s*$", "", desc, flags=re.I).strip()
        # Pluriel courant à l'oral : « bidons » déjà normalisé en « bidon »
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
    low = t.lower()
    m = re.search(
        r"\b(\d+(?:[.,]\d+)?)\s*(?:x\s*)?(bidons?|sacs?|seaux?|cartouches?|litres?|kg|paquets?|boites?|boîtes?)\b",
        low,
    )
    if m:
        qty = float(m.group(1).replace(",", "."))
        unite = _singulariser_unite(m.group(2))
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
    return [row_client(r) for r in read_csv(CLIENTS_CSV)]


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
    rows = read_csv(CLIENTS_CSV)
    new_id = next_id(rows)
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
    }
    rows.append(row)
    write_csv(CLIENTS_CSV, CLIENT_FIELDS, rows)
    return row_client(row)


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
    })
    write_csv(CLIENTS_CSV, CLIENT_FIELDS, rows)
    return row_client(target)


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


@app.delete("/api/clients/{client_id}")
def delete_client(client_id: int):
    rows = read_csv(CLIENTS_CSV)
    kept = [r for r in rows if int(r["id"]) != client_id]
    if len(kept) == len(rows):
        raise HTTPException(status_code=404, detail="Client introuvable")
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
            }
    raise HTTPException(status_code=404, detail="Client introuvable")


@app.post("/api/visites")
def create_visite(body: VisiteCreate):
    if body.type not in {"depannage", "entretien_simple", "entretien_complet"}:
        raise HTTPException(status_code=400, detail="Type de visite invalide")
    client = None
    for r in read_csv(CLIENTS_CSV):
        if int(r["id"]) == body.client_id:
            client = row_client(r)
            break
    if not client:
        raise HTTPException(status_code=404, detail="Client introuvable")
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


def _visite_payload(r: dict[str, str], client: dict[str, Any] | None = None) -> dict[str, Any]:
    if client is None:
        client = next(
            (row_client(c) for c in read_csv(CLIENTS_CSV) if int(c["id"]) == int(r["client_id"])),
            None,
        )
    duree = r.get("duree_minutes") or ""
    return {
        "id": int(r["id"]),
        "client_id": int(r["client_id"]),
        "date": r.get("date", ""),
        "type": r.get("type", ""),
        "checklist": json.loads(r.get("checklist_json") or "[]"),
        "debut": r.get("debut") or None,
        "fin": r.get("fin") or None,
        "duree_minutes": float(duree) if str(duree).strip() else None,
        "client": client,
    }


@app.get("/api/visites/{visite_id}")
def get_visite(visite_id: int):
    for r in read_csv(VISITES_CSV):
        if int(r["id"]) == visite_id:
            return _visite_payload(r)
    raise HTTPException(status_code=404, detail="Visite introuvable")


@app.post("/api/visites/{visite_id}/debut")
def debut_prestation(visite_id: int):
    rows = read_csv(VISITES_CSV)
    for r in rows:
        if int(r["id"]) == visite_id:
            if r.get("debut") and not r.get("fin"):
                return _visite_payload(r)
            r["debut"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            r["fin"] = ""
            r["duree_minutes"] = ""
            write_csv(VISITES_CSV, VISITE_FIELDS, rows)
            return _visite_payload(r)
    raise HTTPException(status_code=404, detail="Visite introuvable")


@app.post("/api/visites/{visite_id}/fin")
def fin_prestation(visite_id: int):
    rows = read_csv(VISITES_CSV)
    for r in rows:
        if int(r["id"]) == visite_id:
            if not r.get("debut"):
                raise HTTPException(status_code=400, detail="Appuyez d'abord sur Début de prestation.")
            if r.get("fin"):
                return _visite_payload(r)
            checklist = json.loads(r.get("checklist_json") or "[]")
            required = [i for i in checklist if not i.get("optional")]
            incomplete = [i for i in (required or checklist) if not i.get("done")]
            if incomplete:
                raise HTTPException(
                    status_code=400,
                    detail="Cochez toutes les étapes obligatoires avant de terminer.",
                )
            fin = datetime.now()
            r["fin"] = fin.strftime("%Y-%m-%d %H:%M:%S")
            try:
                debut = datetime.strptime(r["debut"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                debut = datetime.strptime(r["debut"][:16], "%Y-%m-%d %H:%M")
            minutes = round((fin - debut).total_seconds() / 60, 1)
            r["duree_minutes"] = str(minutes)
            write_csv(VISITES_CSV, VISITE_FIELDS, rows)
            return _visite_payload(r)
    raise HTTPException(status_code=404, detail="Visite introuvable")


@app.patch("/api/visites/{visite_id}/checklist")
def update_checklist(visite_id: int, body: ChecklistUpdate):
    rows = read_csv(VISITES_CSV)
    for r in rows:
        if int(r["id"]) == visite_id:
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
            write_csv(VISITES_CSV, VISITE_FIELDS, rows)
            return _visite_payload(r)
    raise HTTPException(status_code=404, detail="Visite introuvable")


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

    suffix = Path(audio.filename or "audio.wav").suffix.lower() or ".wav"
    raw = await audio.read()
    if not raw or len(raw) < 200:
        raise HTTPException(status_code=400, detail="Enregistrement trop court. Parlez au moins 4 secondes.")

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

        if samples is None or len(samples) < 8000:  # < 0.5 s
            raise HTTPException(status_code=400, detail="Audio trop court. Parlez un peu plus longtemps.")

        rms = float(np.sqrt(np.mean(samples ** 2)))
        peak = float(np.max(np.abs(samples)))
        if peak < 0.008 and rms < 0.0015:
            raise HTTPException(
                status_code=400,
                detail="Micro silencieux. Vérifiez le bon micro Windows et le volume d’entrée.",
            )

        model = get_whisper()
        prompt = (
            "Commandes piscine en français : note, fourniture, facture, analyse. "
            "Exemples : fourniture 1 bidon de chlore ; note le filtre est sale ; "
            "analyse pH 7,2 chlore 1,5 ; facture 2 bidons de chlore."
        )
        texte = ""
        for kwargs in (
            dict(vad_filter=False, no_speech_threshold=0.5, beam_size=5, temperature=0.0),
            dict(vad_filter=False, no_speech_threshold=0.7, beam_size=5, temperature=0.2),
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
            detail="Rien compris. Parlez clairement : note / fourniture / analyse…",
        )
    return {"texte": texte}


@app.get("/api/health")
def health():
    return {"ok": True}


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend manquant")
    return FileResponse(index_path)


@app.get("/favicon.ico")
def favicon():
    ico = STATIC_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(ico, media_type="image/x-icon")
    svg = STATIC_DIR / "logo.svg"
    if svg.exists():
        return FileResponse(svg, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Favicon manquant")

