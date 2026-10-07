#!/usr/bin/env python3
"""20-case AstroSage -> deployed AstroLaab comparison.

Scrapes the published AstroSage celebrity chart pages, replays their exact
published birth inputs against the deployed AstroLaab API, and compares
published planetary/Ascendant longitudes plus Moon Rashi/Nakshatra/Pada.

This is an external-source comparison, not a replacement for the independent
Swiss Ephemeris oracle.
"""

import os, re, sys, time, math
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

API_URL = os.environ["ASTROLAAB_API_URL"]
API_KEY = os.environ["ASTROLAAB_API_KEY"]
MIN_INTERVAL = float(os.environ.get("ASTROLAAB_MIN_INTERVAL", "3.2"))

URLS = [
    "https://celebrity.astrosage.com/bappi-lahiri-birth-chart.asp",
    "https://celebrity.astrosage.com/sunil-lahri-birth-chart.asp",
    "https://celebrity.astrosage.com/rekha-birth-chart.asp",
    "https://celebrity.astrosage.com/aishwarya-rai-birth-chart.asp",
    "https://celebrity.astrosage.com/govinda-birth-chart.asp",
    "https://celebrity.astrosage.com/rashi-khanna-birth-chart.asp",
    "https://celebrity.astrosage.com/tamannaah-bhatia-birth-chart.asp",
    "https://celebrity.astrosage.com/nitish-bharadwaj-birth-chart.asp",
    "https://celebrity.astrosage.com/aamir-khan-birth-chart.asp",
    "https://celebrity.astrosage.com/sachin-tendulkar-birth-chart.asp",
    "https://celebrity.astrosage.com/salman-khan-birth-chart.asp",
    "https://celebrity.astrosage.com/shahrukh-khan-birth-chart.asp",
    "https://celebrity.astrosage.com/deepika-padukone-birth-chart.asp",
    "https://celebrity.astrosage.com/priyanka-chopra-birth-chart.asp",
    "https://celebrity.astrosage.com/rajinikanth-birth-chart.asp",
    "https://celebrity.astrosage.com/hrithik-roshan-birth-chart.asp",
    "https://celebrity.astrosage.com/alia-bhatt-birth-chart.asp",
    "https://celebrity.astrosage.com/ms-dhoni-birth-chart.asp",
    "https://celebrity.astrosage.com/kareena-kapoor-birth-chart.asp",
    "https://celebrity.astrosage.com/katrina-kaif-birth-chart.asp",
]

PLANETS = {"Sun":"Sun","Moon":"Moon","Mars":"Mars","Merc":"Mercury",
           "Jupt":"Jupiter","Venu":"Venus","Satn":"Saturn","Rahu":"Rahu","Ketu":"Ketu"}

SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpion",
         "Sagittarius","Capricorn","Aquarius","Pisces"]

def dms(s):
    m = re.match(r"\s*(\d+)\s*[-:]\s*(\d+)\s*[-:]\s*(\d+)", s)
    if not m:
        raise ValueError(f"bad DMS {s!r}")
    return int(m.group(1)) + int(m.group(2))/60 + int(m.group(3))/3600

def coord(s):
    s = re.sub(r"\s+", " ", s.strip())
    m = re.match(r"([0-9]+)\s*([EWNS])\s*([0-9]+)?$", s, re.I)
    if not m:
        raise ValueError(f"bad coordinate {s!r}")
    deg, hemi, mins = int(m.group(1)), m.group(2).upper(), int(m.group(3) or 0)
    v = deg + mins/60
    if hemi in ("W","S"): v = -v
    return v

def field(text, label, next_label=None):
    if next_label:
        pat = rf"{re.escape(label)}\s*:?\s*(.*?)\s*(?={re.escape(next_label)}\s*:)"
    else:
        pat = rf"{re.escape(label)}\s*:?\s*([^\n]+)"
    m = re.search(pat, text, re.I)
    if not m:
        raise ValueError(f"missing {label}")
    return m.group(1).strip()

def name_hint_from_url(url):
    slug = url.rsplit("/",1)[-1].replace("-birth-chart.asp","")
    return slug.replace("-", " ").title()

def parse_page(url):
    r = requests.get(url, timeout=30, headers={"User-Agent":"AstroLaab external validation/1.0"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n", strip=True)

    # Use the explicit birth-data section, not the earlier search/filter controls.
    # Do not depend on exact celebrity spelling in the heading.
    marker_pos = text.find("Birth Chart / Kundali")
    start = text.find("Name:", marker_pos if marker_pos >= 0 else 0)
    if start < 0:
        raise ValueError("birth-data section not found")
    section = text[start:]
    def section_field(label):
        m = re.search(rf"{re.escape(label)}:\s*(.*?)\s*(?=Date of Birth:|Time of Birth:|Place of Birth:|Longitude:|Latitude:|Time Zone:|Information Source:|$)", section, re.S)
        if not m: raise ValueError(f"missing {label}")
        return m.group(1).strip()
    name = section_field("Name")
    dob = section_field("Date of Birth")
    tob = section_field("Time of Birth")
    place = section_field("Place of Birth")
    lon_s = section_field("Longitude")
    lat_s = section_field("Latitude")
    tz_s = section_field("Time Zone")

    md = re.search(r"([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})", dob)
    if not md: raise ValueError(f"bad date {dob}")
    months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
    month, day, year = months[md.group(1)], int(md.group(2)), int(md.group(3))
    mt = re.search(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", tob)
    if not mt: raise ValueError(f"bad time {tob}")
    hour, minute, second = int(mt.group(1)), int(mt.group(2)), int(mt.group(3) or 0)

    # Parse the planetary table by its actual header names. AstroSage's
    # rendered table is:
    # Planets | C | R | Rashi | Longitude | Nakshatra | Pada | Relation
    expected = {}
    moon_sign = moon_nak = moon_pada = None
    rows = soup.find_all("tr")
    planet_table_found = False
    table_idx = None

    for tr in rows:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        normalized = [re.sub(r"\s+", " ", x).strip().lower() for x in cells]

        if not planet_table_found:
            if "planets" in normalized and "rashi" in normalized and "longitude" in normalized and "nakshatra" in normalized and "pada" in normalized:
                planet_table_found = True
                table_idx = {name: normalized.index(name) for name in ("rashi", "longitude", "nakshatra", "pada")}
            continue

        if not cells:
            continue

        key = cells[0].strip()
        if key not in ("Asc", *PLANETS.keys()):
            # End of the planetary table.
            if expected:
                break
            continue

        try:
            # Header-derived indexes, rather than fixed cell positions.
            idx = table_idx
            sign = cells[idx["rashi"]].strip()
            sign = {"Scorpion": "Scorpio"}.get(sign, sign)
            sign_index = SIGNS.index(sign)
            expected[key] = sign_index * 30.0 + dms(cells[idx["longitude"]])

            if key == "Moon":
                moon_sign = sign
                # Some AstroSage HTML variants expose blank/header cells in
                # the rendered row. Prefer the header-derived columns, then
                # fall back to the row's known Nakshatra/Pada value types.
                moon_nak = cells[idx["nakshatra"]].strip() or None
                moon_pada = cells[idx["pada"]].strip() or None
                row_text = tr.get_text(" ", strip=True)
                nak_names = {
                        "Ashvini","Bharani","Krittika","Rohini","Mrigasira",
                        "Ardra","Punarvasu","Pushya","Ashlesha","Magha",
                        "Purva Phalguni","Uttara Phalguni","Hasta","Chitra",
                        "Swati","Vishakha","Anuradha","Jyeshtha","Mula",
                        "Purva Ashadha","Uttara Ashadha","Shravana","Dhanishta",
                        "Satabhisa","Purva Bhadrapada","Uttara Bhadrapada","Revati",
                }
                if not moon_nak:
                    for nak in sorted(nak_names, key=len, reverse=True):
                        if nak.lower() in row_text.lower():
                            moon_nak = nak
                            break
                if not moon_pada:
                    mp = re.search(r"\b([1-4])\b\s*(?:$|\s)", row_text)
                    if mp:
                        moon_pada = mp.group(1)
        except (ValueError, IndexError, KeyError):
            continue

    if not planet_table_found:
        raise ValueError("AstroSage planetary table header not found")
    if "Moon" not in expected:
        raise ValueError("AstroSage Moon row not parsed")

    return {
        "url": url, "name": name, "date": f"{year:04d}-{month:02d}-{day:02d}",
        "time": f"{hour:02d}:{minute:02d}:{second:02d}",
        "latitude": coord(lat_s), "longitude": coord(lon_s),
        "timezone_offset": float(tz_s), "expected": expected,
        "moon_sign": moon_sign, "moon_nakshatra": moon_nak, "moon_pada": moon_pada,
    }

def err(a,b):
    d=abs((float(a)-float(b))%360)
    return min(d,360-d)*3600

def call(c):
    payload = {
        "year": int(c["date"][:4]), "month": int(c["date"][5:7]),
        "day": int(c["date"][8:10]), "hour": int(c["time"][:2]),
        "minute": int(c["time"][3:5]), "second": int(c["time"][6:8]),
        "latitude": c["latitude"], "longitude": c["longitude"],
        "chart_style":"north","timezone":"Asia/Kolkata","node":"mean"
    }
    # AstroSage publishes a numeric fixed UTC offset. Use it explicitly so the
    # historical comparison is not altered by modern timezone rules.
    payload["utc_offset_minutes"] = round(c["timezone_offset"] * 60)
    r=requests.post(API_URL,json=payload,headers={"x-api-key":API_KEY},timeout=30)
    return r.status_code, r.json()

def main():
    cases=[]
    urls = ["https://celebrity.astrosage.com/sachin-tendulkar-birth-chart.asp"] if "--one" in sys.argv else URLS
    label = "1 known chart" if "--one" in sys.argv else "20 published charts"
    print(f"AstroLaab vs AstroSage — {label}")
    print("="*72)
    for u in urls:
        try:
            c=parse_page(u)
            if not (1950 <= int(c["date"][:4]) <= 2050):
                raise ValueError("outside deployed supported birth-year range")
            cases.append(c)
            print(f"loaded {c['name']}: {c['date']} {c['time']} {c['latitude']:.4f},{c['longitude']:.4f}")
        except Exception as e:
            print(f"LOAD FAIL {u}: {e}")
    print(f"Loaded {len(cases)}/{len(urls)} cases")
    if len(cases) != len(urls):
        return 2

    passed=failed=0
    maxerr=0.0
    for i,c in enumerate(cases,1):
        try:
            if "--one" in sys.argv:
                print(f"AstroSage inputs: {c['name']} | {c['date']} {c['time']} | lat={c['latitude']:.6f} lon={c['longitude']:.6f} tz={c['timezone_offset']}")
                print("AstroSage Moon:", c["moon_sign"], c["moon_nakshatra"], "pada", c["moon_pada"])
                print("AstroSage longitudes:", {k: round(c["expected"][k], 8) for k in ("Asc", *PLANETS) if k in c["expected"]})
            status, body=call(c)
            if status != 200: raise RuntimeError(f"HTTP {status}: {body}")
            failures=[]
            for k,api_name in PLANETS.items():
                if k in c["expected"]:
                    actual=body["Planets"][api_name]["longitude"]
                    e=err(actual,c["expected"][k]); maxerr=max(maxerr,e)
                    # AstroSage publishes to 1 arcsec; allow 2 arcsec for display rounding.
                    if e > 2.0: failures.append(f"{api_name} {e:.3f}\"")
            if "Asc" in c["expected"]:
                e=err(body["Ascendant"]["longitude"],c["expected"]["Asc"])
                maxerr=max(maxerr,e)
                if e > 2.0: failures.append(f"Asc {e:.3f}\"")
            moon=body["Planets"]["Moon"]
            if c["moon_sign"] and moon.get("sign") != c["moon_sign"]:
                failures.append(f"Moon sign {moon.get('sign')} != {c['moon_sign']}")
            if c["moon_nakshatra"] and moon.get("nakshatra") != c["moon_nakshatra"]:
                failures.append(f"Moon nak {moon.get('nakshatra')} != {c['moon_nakshatra']}")
            if c["moon_pada"] and str(moon.get("pada")) != str(c["moon_pada"]):
                failures.append(f"Moon pada {moon.get('pada')} != {c['moon_pada']}")
            if failures:
                failed += 1
                print(f"[{i:02d}/20] FAIL {c['name']}: " + "; ".join(failures))
            else:
                passed += 1
                print(f"[{i:02d}/20] PASS {c['name']}")
        except Exception as e:
            failed += 1
            print(f"[{i:02d}/20] ERROR {c['name']}: {e}")
        if i < len(cases): time.sleep(MIN_INTERVAL)

    print("="*72)
    print(f"Cases: {len(cases)}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Max displayed-longitude deviation: {maxerr:.6f}\"")
    print("External-source gate: " + ("PASS" if failed == 0 else "FAIL"))
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
