from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator
from collections import deque
from datetime import datetime
from typing import Literal, Optional
import hashlib
import os
import threading
import traceback
import secrets
import time
import logging

# SlowAPI imports
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from slowapi.middleware import SlowAPIMiddleware

# Engine imports
from engine.astronomy import calculate_chart
from engine.dasha import calculate_vimshottari_dasha
from engine.panchang import calculate_panchang
from engine.charts import generate_chart_layout
from engine.timeutil import TimeInputError, resolve_local_time, SUPPORTED_MIN_YEAR, SUPPORTED_MAX_YEAR

# ====================================================
# Environment Detection
# ====================================================
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# ====================================================
# Logging Setup
# ====================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# ====================================================
# App Initialization
# ====================================================
if ENVIRONMENT == "production":
    app = FastAPI(
        title="AstroLaab Engine API",
        version="1.0.1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None
    )
else:
    app = FastAPI(
        title="AstroLaab Engine API",
        version="1.0.1",
        description="Indian Vedic Astrology Engine - Lahiri Ayanamsa"
    )

# ====================================================
# API Key (read before the limiter, which keys on it)
# ====================================================
API_KEY = os.getenv("ASTROLAAB_API_KEY")

# ====================================================
# Rate Limiter
# ====================================================
RATE_LIMIT = os.getenv("RATE_LIMIT", "20/minute")

def api_key_identifier(request: Request):
    """Key on a VERIFIED api key, otherwise on the client address.

    Keying on the raw header would let a caller dodge the limit by sending a
    different made-up key each time. Counters are per process: with several
    workers, set a proportionally lower RATE_LIMIT or use a shared storage_uri.
    """
    supplied = request.headers.get("x-api-key")
    if API_KEY and supplied and secrets.compare_digest(supplied, API_KEY):
        return "key:" + hashlib.sha256(supplied.encode()).hexdigest()[:16]
    return "anon:" + get_remote_address(request)

# default_limits is enforced by SlowAPIMiddleware on EVERY request, including
# ones rejected with 401, so wrong-key guessing is throttled too.
AUTH_FAILURE_GUARD = os.getenv("UNAUTHENTICATED_RATE_LIMIT", "30/minute")
limiter = Limiter(key_func=api_key_identifier, default_limits=[AUTH_FAILURE_GUARD])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ====================================================
# CORS
# ====================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://astrolaab.com",
        "https://www.astrolaab.com"
    ],
    allow_credentials=True,
    allow_methods=["POST"],
    allow_headers=["*"],
)

# ====================================================
# API Key verification
# ====================================================
AUTH_FAILURE_MAX = int(os.getenv("AUTH_FAILURE_MAX", "10"))   # failed keys per client per window
AUTH_FAILURE_WINDOW = 60.0
_auth_failures: "dict[str, deque]" = {}
_auth_lock = threading.Lock()


def _auth_throttled(client: str, record_failure: bool) -> bool:
    """Per-client failed-key throttle (the route limiter only runs after auth,
    so it cannot slow down key guessing by itself)."""
    now = time.monotonic()
    with _auth_lock:
        if len(_auth_failures) > 10_000:
            for k in [k for k, q in _auth_failures.items() if not q or now - q[-1] > AUTH_FAILURE_WINDOW]:
                _auth_failures.pop(k, None)
        q = _auth_failures.setdefault(client, deque())
        while q and now - q[0] > AUTH_FAILURE_WINDOW:
            q.popleft()
        if record_failure:
            q.append(now)
        return len(q) > AUTH_FAILURE_MAX


def verify_api_key(request: Request, x_api_key: str = Header(None)):
    if API_KEY is None:
        raise HTTPException(status_code=500, detail="API key not configured")
    client = get_remote_address(request)
    if _auth_throttled(client, record_failure=False):
        raise HTTPException(status_code=429, detail="Too many failed authentication attempts", headers={"Retry-After": "60"})
    if x_api_key is None:
        _auth_throttled(client, record_failure=True)
        raise HTTPException(status_code=401, detail="API key required")
    if not secrets.compare_digest(x_api_key, API_KEY):
        _auth_throttled(client, record_failure=True)
        raise HTTPException(status_code=401, detail="Invalid API key")

# ====================================================
# Request Logging Middleware
# ====================================================
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = round((time.time() - start_time) * 1000, 2)

    logger.info(
        f"{request.method} {request.url.path} | "
        f"Status: {response.status_code} | "
        f"Time: {process_time}ms"
    )
    return response

# ====================================================
# Pydantic Model
# ====================================================
class ChartRequest(BaseModel):
    # Supported range matches the validated scope (see engine/timeutil.py).
    year: int = Field(..., ge=SUPPORTED_MIN_YEAR, le=SUPPORTED_MAX_YEAR)
    month: int = Field(..., ge=1, le=12)
    day: int = Field(..., ge=1, le=31)
    hour: int = Field(..., ge=0, le=23)
    minute: int = Field(..., ge=0, le=59)
    second: int = Field(0, ge=0, le=59)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    chart_style: str = Field("north", pattern="^(north|south)$")
    # IANA timezone of the birth place. Defaults to IST for backwards compatibility.
    timezone: str = Field("Asia/Kolkata", min_length=1, max_length=64)
    # Optional explicit UTC offset; resolves DST-overlap ambiguity.
    utc_offset_minutes: Optional[int] = Field(None, ge=-840, le=840)
    # Mean node matches AstroSage and the Cloudflare Worker.
    node: Literal["mean", "true"] = "mean"

    @model_validator(mode="after")
    def validate_calendar_date(self):
        try:
            datetime(self.year, self.month, self.day, self.hour, self.minute, self.second)
        except ValueError as exc:
            raise ValueError("invalid calendar date/time") from exc
        return self

# ====================================================
# Health
# ====================================================
@app.get("/api/v1/health")
def health():
    return {
        "status": "running",
        "engine": "AstroLaab",
        "version": "v1.0.1",
        "ayanamsa": "Lahiri"
    }

# ====================================================
# Chart Endpoint
# ====================================================
# Decorator order matters: the route decorator must be outermost, otherwise
# slowapi wraps a function that is already registered and never limits it.
@app.post("/api/v1/chart")
@limiter.limit(RATE_LIMIT)
def generate_chart(
    request: Request,
    payload: ChartRequest,
    _: None = Depends(verify_api_key)
):
    # Resolve local time first so bad input is a clean 422, not a 500.
    try:
        local_dt, utc_dt, time_warnings = resolve_local_time(
            payload.year, payload.month, payload.day, payload.hour, payload.minute,
            payload.timezone, payload.utc_offset_minutes, second=payload.second,
        )
    except TimeInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        utc_decimal_hour = utc_dt.hour + utc_dt.minute / 60 + utc_dt.second / 3600

        chart_data = calculate_chart(
            utc_dt.year, utc_dt.month, utc_dt.day, utc_decimal_hour,
            payload.latitude, payload.longitude, payload.node,
        )

        panchang_data = calculate_panchang(
            utc_dt.year, utc_dt.month, utc_dt.day, utc_decimal_hour,
            weekday_index=local_dt.weekday(),  # civil weekday of the LOCAL date
        )

        dasha_data = calculate_vimshottari_dasha(
            chart_data["Planets"]["Moon"]["longitude"],
            datetime(local_dt.year, local_dt.month, local_dt.day),
        )

        layout = generate_chart_layout(chart_data, payload.chart_style)
        chart_meta = chart_data["meta"]

        return {
            "meta": {
                "api_version": "v1.0.1",
                "input_timezone": payload.timezone,
                "utc_offset_minutes": local_dt.utcoffset().total_seconds() / 60,
                "timezone_conversion": f"{payload.timezone} local time converted to UTC before Swiss Ephemeris",
                "ayanamsa": "Lahiri",
                "ayanamsa_degrees": chart_meta["ayanamsaDegrees"],
                "node": chart_meta["node"],
                "house_system": chart_meta["houseSystem"],
                "delta_t_seconds": chart_meta["deltaTSeconds"],
                "time_scales": {"planets": "UT input to Swiss Ephemeris (calc_ut applies Delta-T)", "houses": "UT"},
                "supported_birth_years": [SUPPORTED_MIN_YEAR, SUPPORTED_MAX_YEAR],
                "warnings": time_warnings,
                "utc": utc_dt.isoformat()
            },
            "Ascendant": chart_data["Ascendant"],
            "Planets": chart_data["Planets"],
            "Panchang": panchang_data,
            "Mahadasha_Timeline": dasha_data["timeline"],
            "Current_Running_Dasha": dasha_data["current"],
            "Chart_Layout": layout
        }

    except Exception:
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Internal calculation error")
