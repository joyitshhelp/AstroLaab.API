#!/usr/bin/env python3
"""Validate the deployed AstroLaab FastAPI chart API against the repository's
established 128-case Swiss Ephemeris regression corpus.

This tests the ACTUAL deployed endpoint. It does not contain invented golden
values: birth cases come from workers/moon-sign/tests/independent-accuracy-cases.json,
the same corpus used by the existing independent accuracy workflow.

Usage:
  ASTROLAAB_API_URL="https://...run.app/api/v1/chart" \
  ASTROLAAB_API_KEY="$ASTROLAAB_API_KEY" \
  python tests/validate_deployed_full.py

Optional:
  ASTROLAAB_CASES_PATH=workers/moon-sign/tests/independent-accuracy-cases.json
  ASTROLAAB_TIMEOUT=30
  ASTROLAAB_RETRIES=2
  ASTROLAAB_INCLUDE_SOUTH_STYLE=1
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import swisseph as swe

URL = os.environ.get("ASTROLAAB_API_URL")
KEY = os.environ.get("ASTROLAAB_API_KEY")
TIMEOUT = float(os.environ.get("ASTROLAAB_TIMEOUT", "30"))
RETRIES = max(1, int(os.environ.get("ASTROLAAB_RETRIES", "3")))
MIN_INTERVAL = max(0.0, float(os.environ.get("ASTROLAAB_MIN_INTERVAL", "3.2")))
DEFAULT_CASES = Path("workers/moon-sign/tests/independent-accuracy-cases.json")

PLANETS = {
    "Sun": swe.SUN,
    "Moon": swe.MOON,
    "Mars": swe.MARS,
    "Mercury": swe.MERCURY,
    "Jupiter": swe.JUPITER,
    "Venus": swe.VENUS,
    "Saturn": swe.SATURN,
    "Rahu": swe.MEAN_NODE,
}
PLANET_TOL_ARCSEC = {
    "Sun": 0.1,
    "Moon": 0.5,
    "Mars": 0.1,
    "Mercury": 0.1,
    "Jupiter": 0.1,
    "Venus": 0.1,
    "Saturn": 0.1,
    "Rahu": 0.1,
    "Ketu": 0.1,
}
ASC_TOL_ARCSEC = 1.0
AYANAMSA_TOL_ARCSEC = 0.1
DELTAT_TOL_SECONDS = 0.5

SIGNS = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
]
YOGA_NAMES = [
    "Vishkambha", "Priti", "Ayushman", "Saubhagya", "Shobhana",
    "Atiganda", "Sukarma", "Dhriti", "Shula", "Ganda", "Vriddhi",
    "Dhruva", "Vyaghata", "Harshana", "Vajra", "Siddhi", "Vyatipata",
    "Variyana", "Parigha", "Shiva", "Siddha", "Sadhya", "Shubha",
    "Shukla", "Brahma", "Indra", "Vaidhriti",
]
TITHI_NAMES = [
    "Pratipada", "Dwitiya", "Tritiya", "Chaturthi", "Panchami",
    "Shashthi", "Saptami", "Ashtami", "Navami", "Dashami",
    "Ekadashi", "Dwadashi", "Trayodashi", "Chaturdashi",
]
KARANAS = ["Bava", "Balava", "Kaulava", "Taitila", "Garaja", "Vanija", "Vishti"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def angular_error(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    if d > 180.0:
        d = 360.0 - d
    return d * 3600.0


def local_and_utc(case: dict) -> tuple[datetime, datetime]:
    local = datetime.fromisoformat(
        f"{case['date']}T{case['time']}"
    ).replace(tzinfo=ZoneInfo(case["timeZone"]))
    return local, local.astimezone(timezone.utc)


def reference(case: dict) -> dict:
    swe.set_sid_mode(swe.SIDM_LAHIRI)
    local, utc = local_and_utc(case)
    hour = utc.hour + utc.minute / 60 + utc.second / 3600 + utc.microsecond / 3_600_000_000
    jd_ut = swe.julday(utc.year, utc.month, utc.day, hour)

    expected: dict[str, float] = {}
    speeds: dict[str, float] = {}
    for name, body_id in PLANETS.items():
        values, _ = swe.calc_ut(jd_ut, body_id, swe.FLG_SIDEREAL | swe.FLG_SPEED)
        expected[name] = float(values[0]) % 360.0
        speeds[name] = float(values[3])
    expected["Ketu"] = (expected["Rahu"] + 180.0) % 360.0
    speeds["Ketu"] = speeds["Rahu"]

    houses = swe.houses_ex(
        jd_ut,
        float(case["place"]["latitude"]),
        float(case["place"]["longitude"]),
        b"W",
        swe.FLG_SIDEREAL,
    )
    asc = float(houses[1][0]) % 360.0
    ayanamsa = float(swe.get_ayanamsa_ut(jd_ut))
    delta_t = float(swe.deltat(jd_ut) * 86400.0)

    sun = expected["Sun"]
    moon = expected["Moon"]
    diff = (moon - sun) % 360.0
    tithi = int(diff / 12.0) + 1
    yoga = int(((sun + moon) % 360.0) / (360.0 / 27.0)) + 1
    karana = int(diff / 6.0) + 1

    nak_size = 360.0 / 27.0
    nak_index = int(moon // nak_size)
    pada = int((moon % nak_size) / (nak_size / 4.0)) + 1
    moon_sign_index = int(moon // 30.0) + 1

    return {
        "local": local,
        "utc": utc,
        "jd_ut": jd_ut,
        "expected": expected,
        "speeds": speeds,
        "asc": asc,
        "ayanamsa": ayanamsa,
        "delta_t": delta_t,
        "tithi": tithi,
        "yoga": yoga,
        "karana": karana,
        "weekday": local.weekday(),
        "moon_sign_index": moon_sign_index,
        "nak_index": nak_index + 1,
        "pada": pada,
    }


def tithi_name(tithi: int) -> str:
    if tithi == 15:
        return "Purnima"
    if tithi == 30:
        return "Amavasya"
    return TITHI_NAMES[(tithi - 1) % 15]


def karana_name(karana: int) -> str:
    if karana == 1:
        return "Kimstughna"
    if karana >= 58:
        return ("Shakuni", "Chatushpada", "Naga")[karana - 58]
    return KARANAS[(karana - 2) % 7]


def planet_map(body: dict) -> dict[str, dict]:
    planets = body.get("Planets")
    return planets if isinstance(planets, dict) else {}


def post_chart(case: dict, chart_style: str = "north") -> tuple[int, dict | None, str | None]:
    payload = {
        "year": int(case["date"][0:4]),
        "month": int(case["date"][5:7]),
        "day": int(case["date"][8:10]),
        "hour": int(case["time"][0:2]),
        "minute": int(case["time"][3:5]),
        "second": int(case["time"][6:8]) if len(case["time"]) >= 8 else 0,
        "latitude": float(case["place"]["latitude"]),
        "longitude": float(case["place"]["longitude"]),
        "chart_style": chart_style,
        "timezone": case["timeZone"],
        "node": "mean",
    }
    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-key": KEY or ""},
        method="POST",
    )
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw), None
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            last_error = f"HTTP {exc.code}: {detail[:500]}"
            if exc.code in (401, 403, 422):
                break
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                try:
                    wait = max(3.2, float(retry_after)) if retry_after else 60.0
                except ValueError:
                    wait = 60.0
                time.sleep(wait + 0.2)
                continue
        except Exception as exc:
            last_error = repr(exc)
        if attempt < RETRIES:
            time.sleep(min(2.0 * attempt, 5.0))
    return 0, None, last_error


def validate_case(case: dict, body: dict, ref: dict) -> tuple[list[str], dict[str, float]]:
    failures: list[str] = []
    metrics: dict[str, float] = {}

    meta = body.get("meta", {})
    if meta.get("api_version") != "v1.0.1":
        failures.append(f"api_version={meta.get('api_version')!r}")
    if meta.get("ayanamsa") != "Lahiri":
        failures.append(f"ayanamsa={meta.get('ayanamsa')!r}")
    if meta.get("node") != "mean":
        failures.append(f"node={meta.get('node')!r}")
    if meta.get("house_system") != "W":
        failures.append(f"house_system={meta.get('house_system')!r}")

    expected_offset = int(ref["local"].utcoffset().total_seconds() / 60)
    actual_offset = meta.get("utc_offset_minutes")
    if actual_offset is None or abs(float(actual_offset) - expected_offset) > 0.001:
        failures.append(f"utc_offset={actual_offset!r}, expected {expected_offset}")

    try:
        actual_utc = datetime.fromisoformat(str(meta["utc"]))
        utc_error = abs((actual_utc - ref["utc"]).total_seconds())
        metrics["UTC_seconds"] = utc_error
        if utc_error > 0.5:
            failures.append(f"UTC error={utc_error:.3f}s")
    except Exception:
        failures.append("invalid meta.utc")

    if "delta_t_seconds" in meta:
        dt_error = abs(float(meta["delta_t_seconds"]) - ref["delta_t"])
        metrics["DeltaT_seconds"] = dt_error
        if dt_error > DELTAT_TOL_SECONDS:
            failures.append(f"DeltaT error={dt_error:.3f}s")
    else:
        failures.append("missing delta_t_seconds")

    actual_ayanamsa = meta.get("ayanamsa_degrees")
    if not isinstance(actual_ayanamsa, (int, float)):
        failures.append("missing ayanamsa_degrees")
    else:
        err = abs(float(actual_ayanamsa) - ref["ayanamsa"]) * 3600.0
        metrics["Ayanamsa_arcsec"] = err
        if err > AYANAMSA_TOL_ARCSEC:
            failures.append(f"Ayanamsa error={err:.6f}\"")

    planets = planet_map(body)
    if set(planets) != set(PLANET_TOL_ARCSEC):
        failures.append(
            "planet set mismatch: "
            f"got={sorted(planets)}, expected={sorted(PLANET_TOL_ARCSEC)}"
        )

    for name, tolerance in PLANET_TOL_ARCSEC.items():
        p = planets.get(name)
        if not isinstance(p, dict):
            continue
        actual = p.get("longitude")
        if not isinstance(actual, (int, float)):
            failures.append(f"{name}: missing longitude")
            continue
        err = angular_error(float(actual), ref["expected"][name])
        metrics[f"{name}_arcsec"] = err
        if err > tolerance:
            failures.append(f"{name} error={err:.6f}\" > {tolerance}\"")

        speed = p.get("speed")
        retro = p.get("retrograde")
        if not isinstance(speed, (int, float)) or not isinstance(retro, bool):
            failures.append(f"{name}: missing speed/retrograde")
        else:
            if bool(float(speed) < 0) != retro:
                failures.append(f"{name}: retrograde flag inconsistent with speed")

        sign_index = int(ref["expected"][name] // 30.0)
        expected_sign = SIGNS[sign_index]
        if p.get("sign") != expected_sign:
            failures.append(f"{name}: sign={p.get('sign')!r}, expected {expected_sign!r}")

        degree = p.get("degree")
        expected_degree = ref["expected"][name] - sign_index * 30.0
        if not isinstance(degree, (int, float)) or abs(float(degree) - expected_degree) * 3600 > 0.1:
            failures.append(f"{name}: degree inconsistent with longitude")

    rahu = planets.get("Rahu")
    ketu = planets.get("Ketu")
    if isinstance(rahu, dict) and isinstance(ketu, dict):
        node_error = angular_error(
            float(ketu["longitude"]),
            (float(rahu["longitude"]) + 180.0) % 360.0,
        )
        metrics["Rahu-Ketu_opposition_arcsec"] = node_error
        if node_error > 0.1:
            failures.append(f"Rahu/Ketu opposition error={node_error:.6f}\"")

    asc = body.get("Ascendant", {})
    if not isinstance(asc, dict) or not isinstance(asc.get("longitude"), (int, float)):
        failures.append("missing Ascendant.longitude")
    else:
        err = angular_error(float(asc["longitude"]), ref["asc"])
        metrics["Ascendant_arcsec"] = err
        if err > ASC_TOL_ARCSEC:
            failures.append(f"Ascendant error={err:.6f}\" > {ASC_TOL_ARCSEC}\"")
        sign_index = int(ref["asc"] // 30.0)
        if asc.get("sign") != SIGNS[sign_index]:
            failures.append(f"Ascendant sign={asc.get('sign')!r}, expected {SIGNS[sign_index]!r}")

    panchang = body.get("Panchang")
    if not isinstance(panchang, dict):
        failures.append("missing Panchang")
    else:
        checks = {
            "tithi": ref["tithi"],
            "tithi_name": tithi_name(ref["tithi"]),
            "paksha": "Shukla" if ref["tithi"] <= 15 else "Krishna",
            "yoga": ref["yoga"],
            "yoga_name": YOGA_NAMES[ref["yoga"] - 1],
            "karana": ref["karana"],
            "karana_name": karana_name(ref["karana"]),
            "weekday": WEEKDAYS[ref["weekday"]],
        }
        for key, expected in checks.items():
            if panchang.get(key) != expected:
                failures.append(f"Panchang.{key}={panchang.get(key)!r}, expected {expected!r}")

    layout = body.get("Chart_Layout")
    if not isinstance(layout, dict):
        failures.append("missing Chart_Layout")
    else:
        houses = layout.get("houses")
        if not isinstance(houses, dict) or len(houses) != 12:
            failures.append("Chart_Layout.houses must contain 12 houses")
        else:
            seen: list[str] = []
            asc_sign = asc.get("sign") if isinstance(asc, dict) else None
            for house_no in range(1, 13):
                h = houses.get(str(house_no), houses.get(house_no))
                if not isinstance(h, dict):
                    failures.append(f"missing house {house_no}")
                    continue
                if house_no == 1 and h.get("sign") != asc_sign:
                    failures.append(f"house 1 sign={h.get('sign')!r}, expected {asc_sign!r}")
                if h.get("sign") not in SIGNS:
                    failures.append(f"house {house_no}: invalid sign")
                for planet in h.get("planets", []):
                    seen.append(str(planet))
            if sorted(seen) != sorted(PLANET_TOL_ARCSEC):
                failures.append(f"house planet membership mismatch: {sorted(seen)}")

    moon = planets.get("Moon", {})
    if isinstance(moon, dict):
        expected_moon_sign = SIGNS[ref["moon_sign_index"] - 1]
        if moon.get("sign") != expected_moon_sign:
            failures.append(f"Moon sign={moon.get('sign')!r}, expected {expected_moon_sign!r}")

    dasha = body.get("Mahadasha_Timeline")
    current = body.get("Current_Running_Dasha")
    if not isinstance(dasha, list) or not dasha:
        failures.append("missing Mahadasha_Timeline")
    else:
        if len(dasha) != 9:
            failures.append(f"Mahadasha_Timeline length={len(dasha)}, expected 9")
        starts = []
        for period in dasha:
            if not all(k in period for k in ("planet", "start", "end")):
                failures.append("invalid dasha period schema")
                continue
            starts.append(period["start"])
            if period["start"] > period["end"]:
                failures.append(f"dasha period inverted: {period}")
        if starts != sorted(starts):
            failures.append("dasha timeline is not chronological")
        if current is not None and current not in [p.get("planet") for p in dasha]:
            failures.append(f"Current_Running_Dasha={current!r} not present in timeline")

    # The current FastAPI response does not expose a standalone D9/divisional
    # chart object, so this validator deliberately does not invent such a field.
    return failures, metrics


def run_case(case: dict) -> tuple[bool, dict[str, float], str]:
    ref = reference(case)
    if MIN_INTERVAL:
        time.sleep(MIN_INTERVAL)
    status, body, error = post_chart(case)
    if body is None:
        return False, {}, f"HTTP failure: {error or status}"
    if status != 200:
        return False, {}, f"HTTP {status}: response was not 200"
    failures, metrics = validate_case(case, body, ref)
    if failures:
        return False, metrics, "; ".join(failures)
    return True, metrics, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=os.environ.get("ASTROLAAB_CASES_PATH", str(DEFAULT_CASES)))
    parser.add_argument(
        "--include-south-style",
        action="store_true",
        default=os.environ.get("ASTROLAAB_INCLUDE_SOUTH_STYLE") == "1",
    )
    args = parser.parse_args()

    if not URL or not KEY:
        print("FAIL: set ASTROLAAB_API_URL and ASTROLAAB_API_KEY", file=sys.stderr)
        return 2

    cases_path = Path(args.cases)
    if not cases_path.exists():
        print(f"FAIL: cases file not found: {cases_path}", file=sys.stderr)
        return 2

    doc = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = doc.get("cases", [])
    if len(cases) != 128:
        print(
            f"FAIL: expected the established 128-case corpus, found {len(cases)} cases",
            file=sys.stderr,
        )
        return 2

    swe.set_sid_mode(swe.SIDM_LAHIRI)
    print("AstroLaab deployed accuracy validation")
    print(f"Endpoint: {URL}")
    print(f"Reference: repository 128-case corpus + pyswisseph {swe.version}")
    print("Scope: planets, nodes, Ascendant, Lahiri, Delta-T, timezone/UTC, Panchang, houses, dasha, schema")
    print("Not claimed: standalone divisional-chart output (current FastAPI response does not expose it)")
    print()

    passed = 0
    failed = 0
    all_metrics: dict[str, float] = {}
    failures_by_case: list[tuple[str, str]] = []

    for i, case in enumerate(cases, 1):
        ok, metrics, reason = run_case(case)
        for k, v in metrics.items():
            all_metrics[k] = max(all_metrics.get(k, 0.0), v)
        if ok:
            passed += 1
            print(f"[{i:03d}/{len(cases)}] PASS {case['id']}")
        else:
            failed += 1
            failures_by_case.append((case["id"], reason))
            print(f"[{i:03d}/{len(cases)}] FAIL {case['id']} — {reason}")

    if args.include_south_style:
        print("\nSouth-chart smoke coverage:")
        for case in cases[:2]:
            status, body, error = post_chart(case, chart_style="south")
            if status != 200 or body is None:
                failed += 1
                print(f"FAIL south {case['id']} — {error or status}")
                continue
            layout = body.get("Chart_Layout", {})
            signs = layout.get("signs")
            if not isinstance(signs, list) or len(signs) != 12:
                failed += 1
                print(f"FAIL south {case['id']} — expected 12 fixed signs")
            else:
                print(f"PASS south {case['id']}")

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"Cases:              {len(cases)}")
    print(f"Passed:             {passed}")
    print(f"Failed:             {failed}")
    print(f"Accuracy failures:  {sum(1 for _, r in failures_by_case if 'error=' in r)}")
    print(f"Schema/contract:    {sum(1 for _, r in failures_by_case if 'error=' not in r)}")
    print(f"Overall:            {'PASS' if failed == 0 else 'FAIL'}")

    interesting = [
        ("Moon max error", "Moon_arcsec"),
        ("Sun max error", "Sun_arcsec"),
        ("Mercury max error", "Mercury_arcsec"),
        ("Venus max error", "Venus_arcsec"),
        ("Mars max error", "Mars_arcsec"),
        ("Jupiter max error", "Jupiter_arcsec"),
        ("Saturn max error", "Saturn_arcsec"),
        ("Rahu max error", "Rahu_arcsec"),
        ("Ketu max error", "Ketu_arcsec"),
        ("Ascendant max error", "Ascendant_arcsec"),
        ("Ayanamsa max error", "Ayanamsa_arcsec"),
        ("Delta-T max error", "DeltaT_seconds"),
        ("UTC max error", "UTC_seconds"),
    ]
    print("\nMaximum observed deviations:")
    for label, key in interesting:
        if key in all_metrics:
            unit = "s" if key.endswith("_seconds") else '"'
            print(f"{label:24s}: {all_metrics[key]:.6f}{unit}")

    if failures_by_case:
        print("\nFailures:")
        for cid, reason in failures_by_case:
            print(f"- {cid}: {reason}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())