"""
Build src/booking_agent/data/airports.csv from the OurAirports dataset (public domain).

    curl -o airports_raw.csv https://davidmegginson.github.io/ourairports-data/airports.csv
    uv run python scripts/build_airports.py airports_raw.csv

Only the curated airports below are kept: each needs a correct IANA time zone so the
simulated supplier can produce real local departure/arrival times. Name, city, country
and coordinates come from OurAirports.
"""

import csv
import sys
from pathlib import Path

OUT = Path(__file__).parent.parent / "src" / "booking_agent" / "data" / "airports.csv"

IN = "Asia/Kolkata"
CURATED: dict[str, str] = {
    # India (48)
    **{
        code: IN
        for code in ["DEL", "BOM", "BLR", "MAA", "CCU", "HYD", "AMD", "COK", "GOI", "GOX", "PNQ", "JAI", "LKO", "TRV", "IXC", "SXR", "ATQ", "VNS", "PAT", "GAU", "BBI", "IXB", "NAG", "IDR", "BHO", "CJB", "IXE", "IXM", "TRZ", "VTZ", "IXR", "RPR", "DED", "IXZ", "IXL", "UDR", "JDH", "STV", "BDQ", "HSR", "IXJ", "VGA", "IXA", "DIB", "GAY", "CCJ", "TIR", "IXU"]
    },
    # Gulf
    "DXB": "Asia/Dubai", "AUH": "Asia/Dubai", "SHJ": "Asia/Dubai", "DOH": "Asia/Qatar",
    "BAH": "Asia/Bahrain", "KWI": "Asia/Kuwait", "MCT": "Asia/Muscat",
    "RUH": "Asia/Riyadh", "JED": "Asia/Riyadh", "DMM": "Asia/Riyadh",
    # South Asia
    "CMB": "Asia/Colombo", "KTM": "Asia/Kathmandu", "DAC": "Asia/Dhaka", "MLE": "Indian/Maldives",
    # Southeast and East Asia
    "SIN": "Asia/Singapore", "KUL": "Asia/Kuala_Lumpur", "BKK": "Asia/Bangkok",
    "DMK": "Asia/Bangkok", "HKT": "Asia/Bangkok", "CGK": "Asia/Jakarta", "DPS": "Asia/Makassar",
    "MNL": "Asia/Manila", "SGN": "Asia/Ho_Chi_Minh", "HAN": "Asia/Ho_Chi_Minh",
    "HKG": "Asia/Hong_Kong", "NRT": "Asia/Tokyo", "ICN": "Asia/Seoul", "PVG": "Asia/Shanghai",
    # Europe
    "LHR": "Europe/London", "LGW": "Europe/London", "MAN": "Europe/London",
    "BHX": "Europe/London", "FRA": "Europe/Berlin", "MUC": "Europe/Berlin",
    "CDG": "Europe/Paris", "AMS": "Europe/Amsterdam", "ZRH": "Europe/Zurich",
    "VIE": "Europe/Vienna", "FCO": "Europe/Rome", "MXP": "Europe/Rome",
    "IST": "Europe/Istanbul", "MAD": "Europe/Madrid", "BCN": "Europe/Madrid",
    "HEL": "Europe/Helsinki",
    # North America
    "JFK": "America/New_York", "EWR": "America/New_York", "IAD": "America/New_York",
    "BOS": "America/New_York", "ATL": "America/New_York", "ORD": "America/Chicago",
    "IAH": "America/Chicago", "DFW": "America/Chicago", "SFO": "America/Los_Angeles",
    "LAX": "America/Los_Angeles", "SEA": "America/Los_Angeles", "YYZ": "America/Toronto",
    "YVR": "America/Vancouver",
    # Oceania and Africa
    "SYD": "Australia/Sydney", "MEL": "Australia/Melbourne",
    "JNB": "Africa/Johannesburg", "NBO": "Africa/Nairobi", "ADD": "Africa/Addis_Ababa",
}

REGION_BY_COUNTRY = {
    "IN": "IN",
    **{c: "GULF" for c in ("AE", "QA", "BH", "KW", "OM", "SA")},
    **{c: "SASIA" for c in ("LK", "NP", "BD", "MV")},
    **{c: "SEA" for c in ("SG", "MY", "TH", "ID", "PH", "VN")},
    **{c: "EASIA" for c in ("HK", "JP", "KR", "CN")},
    **{c: "EUR" for c in ("GB", "DE", "FR", "NL", "CH", "AT", "IT", "TR", "ES", "FI")},
    **{c: "NAM" for c in ("US", "CA")},
    **{c: "OCE" for c in ("AU",)},
    **{c: "AFR" for c in ("ZA", "KE", "ET")},
}


def main(raw_csv: str) -> None:
    with open(raw_csv, encoding="utf-8") as f:
        rows = {
            r["iata_code"]: r
            for r in csv.DictReader(f)
            if r["iata_code"] in CURATED and r["scheduled_service"] == "yes"
        }
    missing = sorted(set(CURATED) - set(rows))
    if missing:
        sys.exit(f"Not found with scheduled service in OurAirports: {missing}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["iata", "name", "city", "country", "region", "lat", "lon", "tz"])
        for code in sorted(CURATED):
            r = rows[code]
            w.writerow([
                code, r["name"], r["municipality"], r["iso_country"],
                REGION_BY_COUNTRY[r["iso_country"]],
                round(float(r["latitude_deg"]), 4), round(float(r["longitude_deg"]), 4),
                CURATED[code],
            ])
    n_in = sum(1 for c in CURATED if CURATED[c] == IN)
    print(f"Wrote {len(CURATED)} airports ({n_in} India, {len(CURATED) - n_in} international) to {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
