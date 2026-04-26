"""
Panelist Duplicate Check API
─────────────────────────────
Merges:
  • Original: in-memory EMAIL_LIST cache, .env DB config, lifespan refresh pattern
  • New:      decomposed email scorer, 5-param fingerprint scorer, enriched response

Run:  uvicorn main:app --reload --port 8000
Docs: http://localhost:8000/docs
"""

import asyncio
import ipaddress
import os
import re
import urllib.parse
from contextlib import asynccontextmanager
from math import cos, radians, sin, sqrt, atan2
from pathlib import Path
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr, field_validator
from rapidfuzz import fuzz
from sqlalchemy import create_engine, text


# ═══════════════════════════════════════════════════════════════════════════════
# ENV + DB  (your original pattern, unchanged)
# ═══════════════════════════════════════════════════════════════════════════════

env_path = Path(".env")
if env_path.exists():
    load_dotenv(dotenv_path=env_path, override=True)
    print("Loaded .env file")

username = os.getenv("DB_USERNAME", "")
password = os.getenv("DB_PASSWORD", "")
host     = os.getenv("DB_HOST",     "localhost")
database = os.getenv("DB_NAME",     "paneldb")
port     = os.getenv("DB_PORT",     "5432")

engine = None
if username and password:
    encoded_password = urllib.parse.quote_plus(password)
    conn_string = (
        # f"postgresql+psycopg2://{username}:{encoded_password}"
        # f"@{host}:{port}/{database}?sslmode=require"
        f"mysql+pymysql://{username}:{encoded_password}"
        f"@{host}:{port}/{database}"
    )
    engine = create_engine(conn_string, pool_pre_ping=True)


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG  (tune without touching code)
# ═══════════════════════════════════════════════════════════════════════════════

SIMILARITY_THRESHOLD   = int(os.getenv("SIMILARITY_THRESHOLD", 60))   # min score to include in matches[]
SIMILARITY_REJECT      = int(os.getenv("SIMILARITY_REJECT",    90))   # score → REJECTED
SIMILARITY_REVIEW      = int(os.getenv("SIMILARITY_REVIEW",    70))   # score → REVIEW
FINGERPRINT_REJECT     = int(os.getenv("FINGERPRINT_REJECT",   40))   # fp pts → REJECTED (2+ params)
FINGERPRINT_REVIEW     = int(os.getenv("FINGERPRINT_REVIEW",   20))   # fp pts → REVIEW   (1 param)
CACHE_REFRESH_SECS     = int(os.getenv("CACHE_REFRESH_SECS",   60))
LOCATION_PRECISION     = int(os.getenv("LOCATION_PRECISION",    4))   # decimal places for lat/lon match
IP_SUBNET_MATCH        = os.getenv("IP_SUBNET_MATCH", "true").lower() == "true"

FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "mail.com",  "icloud.com", "ymail.com",  "protonmail.com",
    "aol.com",   "live.com",   "msn.com",
}
DOMAIN_ALIASES = {
    "googlemail.com": "gmail.com",
    "pm.me":          "protonmail.com",
}


# ═══════════════════════════════════════════════════════════════════════════════
# IN-MEMORY CACHE  (your original EMAIL_LIST — extended with fingerprint fields)
# ═══════════════════════════════════════════════════════════════════════════════

PANELIST_CACHE: list[dict] = []
"""
Each entry:
  panelistId, email, normalized_email, alpha_username,
  ip_address, device_id, os, latitude, longitude, country
"""


def _build_cache_row(row) -> dict:
    email      = (row[1] or "").strip()
    decomposed = _decompose(email)
    return {
        "panelistId":       row[0],
        "email":            email,
        "normalized_email": decomposed.get("canonical", email),
        "alpha_username":   decomposed.get("alpha", ""),
        # fingerprint fields
        "ip_address": row[2],
        "device_id":  row[3],
        "os":         row[4],
        "latitude":   row[5],
        "longitude":  row[6],
        "country":    row[7],
    }


def load_cache():
    global PANELIST_CACHE
    print("Loading panelist cache from database...")
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                   select jt.ipAddress,
                    jt.panelistId,
                    jt.activitylog,
                    jt.deviceType,
                    jt.osType,
                    jt.latitude,
                    jt.longitude,
                    jt.country
                    from join_traffic jt
                        """))
            PANELIST_CACHE = [_build_cache_row(r) for r in result if r[1]]
        print(f"Cache loaded: {len(PANELIST_CACHE)} panelists")
    except Exception as e:
        print("Database error:", e)
        raise


async def refresh_cache():
    """Background task — refreshes cache every CACHE_REFRESH_SECS seconds."""
    while True:
        await asyncio.sleep(CACHE_REFRESH_SECS)
        try:
            print("Refreshing panelist cache...")
            load_cache()
        except Exception as e:
            print("Cache refresh error:", e)


# ═══════════════════════════════════════════════════════════════════════════════
# LIFESPAN  (your original pattern)
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_cache()
    asyncio.create_task(refresh_cache())
    yield
    print("Shutting down...")


# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL DECOMPOSITION  (your normalize_email + our cross-domain decomposer)
# ═══════════════════════════════════════════════════════════════════════════════

def _decompose(raw_email: str) -> dict:
    """
    Normalise and decompose an email into scoring parts.

    Combines your original normalisation (mr/dr prefix, dots, + alias)
    with our alpha/digit split and cross-domain detection.
    """
    email = raw_email.lower().strip()
    if "@" not in email:
        return {}

    local, domain = email.rsplit("@", 1)
    domain = DOMAIN_ALIASES.get(domain, domain)

    # ── Your original normalisations ──────────────────────────────────────
    local = re.sub(r'^(mr|mrs|ms|dr)\.?', '', local)   # strip titles
    local = local.split("+")[0]                          # strip + alias
    if domain == "gmail.com":
        local = local.replace(".", "")                   # gmail dot trick
    else:
        local = local.replace(".", "")                   # normalise dots on all domains

    # ── New: alpha / digit split ──────────────────────────────────────────
    alpha  = re.sub(r"\d", "", local)
    digits = re.sub(r"\D", "", local)

    return {
        "canonical":   f"{local}@{domain}",
        "local":       local,
        "alpha":       alpha,
        "digits":      digits,
        "domain":      domain,
        "is_freemail": domain in FREEMAIL_DOMAINS,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL SIMILARITY SCORER
# ═══════════════════════════════════════════════════════════════════════════════

def _email_similarity(email_a: str, email_b: str) -> tuple[float, list[str]]:
    """
    Returns (similarity_score 0–100, flag_reasons).

    Scoring weights:
      alpha_similarity  55 pts  — letters-only fuzzy match (WRatio)
      domain_signal     25 pts  — same domain bonus
      numeric_inverse   20 pts  — penalises identical digit padding (fraud signal)

    Special fast-paths:
      identical canonical      → 100.0
      same alpha, freemail
      but different provider   → 97.0  (cross-domain fraud)
    """
    a = _decompose(email_a)
    b = _decompose(email_b)
    reasons: list[str] = []

    if not a or not b:
        return 0.0, reasons

    # Fast-path: exact canonical match
    if a["canonical"] == b["canonical"]:
        reasons.append("exact_email_match")
        return 100.0, reasons

    # Fast-path: cross-domain freemail (same username, different provider)
    if (
        a["alpha"]
        and a["alpha"] == b["alpha"]
        and a["is_freemail"]
        and b["is_freemail"]
        and a["domain"] != b["domain"]
    ):
        reasons.append("cross_domain_username_match")
        return 97.0, reasons

    # Decomposed scoring
    alpha_sim = (
        fuzz.WRatio(a["alpha"], b["alpha"])
        if a["alpha"] and b["alpha"] else 0.0
    )
    domain_signal  = 100.0 if a["domain"] == b["domain"] else 0.0

    # Digit divergence: if alpha identical but digits differ → fraud amplifier
    if a["digits"] and b["digits"]:
        digit_sim     = fuzz.ratio(a["digits"], b["digits"])
        numeric_inv   = 100.0 - digit_sim    # high divergence = high fraud signal
    else:
        numeric_inv   = 50.0                 # no digits on either side → neutral

    score = round(
        alpha_sim    * 0.55 +
        domain_signal * 0.25 +
        numeric_inv  * 0.20,
        2,
    )

    if alpha_sim >= 90:
        reasons.append("high_alpha_similarity")
    if a["domain"] == b["domain"]:
        reasons.append("same_domain")
    if a["digits"] and b["digits"] and digit_sim < 40:
        reasons.append("numeric_padding_detected")

    return score, reasons


def _match_tier(score: float) -> str:
    if score >= SIMILARITY_REJECT:
        return "HIGH"
    if score >= SIMILARITY_REVIEW:
        return "MEDIUM"
    return "LOW"


# ═══════════════════════════════════════════════════════════════════════════════
# FINGERPRINT SCORER  (5 params × 20 pts = 100)
# ═══════════════════════════════════════════════════════════════════════════════

def _norm(v: Optional[str]) -> str:
    return (v or "").strip().lower()


def _ip_matches(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if IP_SUBNET_MATCH:
        try:
            return ipaddress.ip_address(b) in ipaddress.ip_network(f"{a}/24", strict=False)
        except ValueError:
            return False
    return False


def _location_matches(
    lat_a: Optional[float], lon_a: Optional[float],
    lat_b: Optional[float], lon_b: Optional[float],
) -> bool:
    """Exact coordinate match to LOCATION_PRECISION decimal places (~11m at 4dp)."""
    if any(v is None for v in [lat_a, lon_a, lat_b, lon_b]):
        return False
    return (
        round(lat_a, LOCATION_PRECISION) == round(lat_b, LOCATION_PRECISION)
        and round(lon_a, LOCATION_PRECISION) == round(lon_b, LOCATION_PRECISION)
    )


def _fingerprint_score(new_p: dict, existing: dict) -> tuple[int, dict, list[str]]:
    """
    Returns (score 0–100 in steps of 20, breakdown dict, flag_reasons).

    Param          Points
    ──────────     ──────
    device_id      20
    ip_address     20
    os             20
    location       20   (exact lat/lon to 4 dp)
    country        20
    """
    reasons: list[str] = []

    dev  = bool(new_p.get("device_id")  and _norm(new_p["device_id"])  == _norm(existing.get("device_id")))
    ip   = _ip_matches(new_p.get("ip_address"),  existing.get("ip_address"))
    os_m = bool(new_p.get("os")         and _norm(new_p["os"])          == _norm(existing.get("os")))
    loc  = _location_matches(
               new_p.get("latitude"),  new_p.get("longitude"),
               existing.get("latitude"), existing.get("longitude"))
    cnt  = bool(new_p.get("country")    and _norm(new_p["country"])     == _norm(existing.get("country")))

    if dev:  reasons.append("device_id_match")
    if ip:
        reasons.append(
            "exact_ip_match"
            if new_p.get("ip_address") == existing.get("ip_address")
            else "subnet_ip_match"
        )
    if os_m: reasons.append("os_match")
    if loc:  reasons.append("exact_location_match")
    if cnt:  reasons.append("country_match")

    breakdown = {"device_id": dev, "ip": ip, "os": os_m, "location": loc, "country": cnt}
    score     = sum([dev, ip, os_m, loc, cnt]) * 20

    return score, breakdown, reasons


# ═══════════════════════════════════════════════════════════════════════════════
# VERDICT ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def _verdict(sim: float, fp: int) -> str:
    """
    REJECTED : exact email  OR  (high sim + 2 fp params)  OR  4–5 fp params alone
    REVIEW   : medium sim   OR  1 fp param
    ACCEPTED : everything below threshold
    """
    if sim == 100.0:
        return "REJECTED"
    if sim >= SIMILARITY_REJECT and fp >= FINGERPRINT_REJECT:
        return "REJECTED"
    if fp >= 80:                         # 4–5 params = same physical machine
        return "REJECTED"
    if sim >= SIMILARITY_REVIEW or fp >= FINGERPRINT_REVIEW:
        return "REVIEW"
    return "ACCEPTED"


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEMAS
# ═══════════════════════════════════════════════════════════════════════════════

class DuplicateCheckRequest(BaseModel):
    email:           str
    panelistId:      Optional[int]   = None
    # fingerprint fields (all optional — only scored when present)
    ip_address:      Optional[str]   = None
    device_id:       Optional[str]   = None
    os:              Optional[str]   = None
    browser:         Optional[str]   = None
    browser_version: Optional[str]   = None
    device_type:     Optional[str]   = None
    latitude:        Optional[float] = None
    longitude:       Optional[float] = None
    country:         Optional[str]   = None


# ═══════════════════════════════════════════════════════════════════════════════
# CORE LOGIC
# ═══════════════════════════════════════════════════════════════════════════════

def check_panelist_similarity(req: DuplicateCheckRequest) -> dict:

    if not PANELIST_CACHE:
        raise Exception("Panelist cache not loaded")

    new_p = req.model_dump()
    matches: list[dict] = []

    for record in PANELIST_CACHE:

        # Skip self (your original logic, unchanged)
        if req.panelistId is not None and record["panelistId"] == req.panelistId:
            continue
        if req.panelistId in (0, None) and record["email"].lower() == req.email.lower():
            continue

        # ── Email similarity ──────────────────────────────────────────────
        sim_score, email_reasons = _email_similarity(req.email, record["email"])
        if sim_score < SIMILARITY_THRESHOLD:
            continue

        # ── Fingerprint score ─────────────────────────────────────────────
        fp_score, fp_breakdown, fp_reasons = _fingerprint_score(new_p, record)

        all_reasons = list(set(email_reasons + fp_reasons))

        matches.append({
            "panelistId":             record["panelistId"],
            "matched_email":          record["email"],
            "similarity_score":       sim_score,
            "fingerprint_score":      fp_score,
            "fingerprint_breakdown":  fp_breakdown,
            "match_tier":             _match_tier(sim_score),
            "flag_reasons":           all_reasons,
        })

    # Sort: most suspicious first
    matches.sort(key=lambda m: (m["similarity_score"], m["fingerprint_score"]), reverse=True)

    # Top-level aggregates — worst case across all matches
    top_sim = max((m["similarity_score"]  for m in matches), default=0.0)
    top_fp  = max((m["fingerprint_score"] for m in matches), default=0)

    verdict = _verdict(top_sim, top_fp)

    return {
        "input_email":       req.email,
        "panelistId":        req.panelistId,
        "match_count":       len(matches),
        "matches":           matches,
        "similarity_score":  top_sim,
        "fingerprint_score": top_fp,
        "verdict":           verdict,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(title="Panelist Duplicate Check API", lifespan=lifespan)


@app.post("/check-email")
def check_email(req: DuplicateCheckRequest):
    """
    Triggered on every new panelist registration.

    Returns similarity_score (email decomposition 0–100),
    fingerprint_score (5 params × 20 pts = 0–100),
    all matches above threshold, and a final verdict.

    Verdict:
      REJECTED  — high-confidence duplicate (auto-flag)
      REVIEW    — partial match, needs human review
      ACCEPTED  — no meaningful match found
    """
    try:
        return check_panelist_similarity(req)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok", "cache_size": len(PANELIST_CACHE)}
