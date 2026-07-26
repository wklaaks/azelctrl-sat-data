#!/usr/bin/env python3
"""
Generate pre-cached SatNOGS satellite status and frequency data for AZELCtrl.

Pulls the CelesTrak amateur satellite list, queries SatNOGS DB API for
status and transmitter data, and writes two compact CSV files.

Usage:
    python3 generate.py [--outdir sat-data] [--filter-amateur-bands]
"""

import csv
import io
import json
import sys
import time
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Configuration ──────────────────────────────────────────────────────────

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=csv"
SATNOGS_BASE = "https://db.satnogs.org/api"
RATE_LIMIT = 0.3  # seconds between SatNOGS API calls (be polite)

# Amateur radio bands (MHz) for optional filtering
AMATEUR_BANDS = [
    (0.03, 0.035),   # 160m
    (0.04, 0.041),   # 160m
    (0.041, 0.043),  # 160m
    (0.07, 0.073),   # 40m
    (0.101, 0.1015), # 30m
    (0.14, 0.148),   # 20m
    (0.1807, 0.1812),# 17m
    (0.21, 0.2145),  # 15m
    (0.249, 0.25),   # 12m
    (0.28, 0.297),   # 10m
    (0.50, 0.52),    # 6m
    (0.902, 0.928),  # 33cm
    (1.24, 1.3),     # 23cm
    (1.26, 1.27),    # 23cm
    (2.2, 2.3),      # 13cm
    (2.4, 2.5),      # 11cm
    (3.3, 3.5),      # 9cm
    (5.6, 5.9),      # 6cm
    (10.0, 10.5),    # 3cm
]

# ── CelesTrak parser ───────────────────────────────────────────────────────

def parse_celestrak(text):
    """Parse CelesTrak CSV into list of (norad_cat_id, name) tuples.
    
    CelesTrak now returns CSV format with columns:
    OBJECT_NAME, OBJECT_ID, ..., NORAD_CAT_ID, ...
    """
    satellites = []
    reader = csv.reader(io.StringIO(text))
    header = None
    for row in reader:
        if header is None:
            header = row
            continue
        # Skip empty rows
        if not row or len(row) < 13:
            continue
        try:
            name = row[0]  # OBJECT_NAME
            cat_id = int(row[11])  # NORAD_CAT_ID
            satellites.append((cat_id, name))
        except (ValueError, IndexError):
            continue
    return satellites


def download_celestrak():
    """Download and parse the amateur satellite list from CelesTrak."""
    print(f"Downloading CelesTrak amateur satellite list...")
    req = urllib.request.Request(CELESTRAK_URL)
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    satellites = parse_celestrak(text)
    print(f"  Found {len(satellites)} satellites")
    return satellites


# ── SatNOGS API ────────────────────────────────────────────────────────────

def satnogs_get(path):
    """Make a GET request to SatNOGS API with rate limiting."""
    time.sleep(RATE_LIMIT)
    url = f"{SATNOGS_BASE}{path}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} for {path}")
        return None
    except Exception as e:
        print(f"  Error fetching {path}: {e}")
        return None


def get_satellite_status(cat_id, name):
    """Get satellite status from SatNOGS."""
    data = satnogs_get(f"/satellites/?norad_cat_id={cat_id}")
    if data and len(data) > 0:
        sat = data[0]
        return {
            "norad_cat_id": cat_id,
            "name": sat.get("name", name),
            "status": sat.get("status", "unknown"),
            "last_updated": sat.get("updated", ""),
        }
    return None


def get_transmitters(cat_id, name, filter_amateur=False):
    """Get transmitter data from SatNOGS."""
    data = satnogs_get(f"/transmitters/?satellite__norad_cat_id={cat_id}")
    if not data:
        return []

    transmitters = []
    for tx in data:
        # Extract frequencies in MHz
        ul_low = tx.get("uplink_low")
        ul_high = tx.get("uplink_high")
        dl_low = tx.get("downlink_low")
        dl_high = tx.get("downlink_high")

        # Convert Hz to MHz
        ul_low_mhz = ul_low / 1_000_000 if ul_low else None
        ul_high_mhz = ul_high / 1_000_000 if ul_high else None
        dl_low_mhz = dl_low / 1_000_000 if dl_low else None
        dl_high_mhz = dl_high / 1_000_000 if dl_high else None

        # Optional: filter to amateur bands only
        if filter_amateur:
            if not is_amateur_freq(ul_low_mhz, ul_high_mhz, dl_low_mhz, dl_high_mhz):
                continue

        transmitters.append({
            "norad_cat_id": cat_id,
            "name": name,
            "description": tx.get("description", ""),
            "type": tx.get("type", ""),
            "mode": tx.get("mode", ""),
            "uplink_mode": tx.get("uplink_mode", ""),
            "uplink_low_mhz": ul_low_mhz,
            "uplink_high_mhz": ul_high_mhz,
            "downlink_low_mhz": dl_low_mhz,
            "downlink_high_mhz": dl_high_mhz,
            "invert": str(tx.get("invert", False)).lower(),
            "alive": str(tx.get("alive", False)).lower(),
            "last_updated": tx.get("updated", ""),
        })
    return transmitters


def is_amateur_freq(ul_low, ul_high, dl_low, dl_high):
    """Check if any frequency falls within amateur radio bands."""
    freqs = [f for f in [ul_low, ul_high, dl_low, dl_high] if f is not None]
    for f in freqs:
        for band_low, band_high in AMATEUR_BANDS:
            if band_low <= f <= band_high:
                return True
    return False


# ── CSV writers ────────────────────────────────────────────────────────────

STATUS_FIELDS = ["norad_cat_id", "name", "status", "last_updated"]
FREQ_FIELDS = [
    "norad_cat_id", "name", "description", "type", "mode",
    "uplink_mode", "uplink_low_mhz", "uplink_high_mhz",
    "downlink_low_mhz", "downlink_high_mhz", "invert", "alive", "last_updated"
]


def write_csv(filepath, records, fieldnames):
    """Write records to CSV file."""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            # Convert None to empty string for CSV
            row = {k: ("{}" if record[k] is not None else "") for k in fieldnames}
            cleaned = {k: (record[k] if record[k] is not None else "") for k in fieldnames}
            writer.writerow(cleaned)
    print(f"  Written {len(records)} records to {filepath}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate SatNOGS data CSVs for AZELCtrl")
    parser.add_argument("--outdir", default=".", help="Output directory (default: current dir)")
    parser.add_argument("--filter-amateur-bands", action="store_true",
                        help="Only include transmitters on amateur radio bands")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse CelesTrak list but don't query SatNOGS")
    args = parser.parse_args()

    import os
    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    # Step 1: Get satellite list
    satellites = download_celestrak()
    if not satellites:
        print("Error: No satellites found")
        sys.exit(1)

    if args.dry_run:
        print(f"Dry run: would process {len(satellites)} satellites")
        for cat_id, name in satellites[:5]:
            print(f"  {cat_id}: {name}")
        if len(satellites) > 5:
            print(f"  ... and {len(satellites) - 5} more")
        sys.exit(0)

    # Step 2: Query SatNOGS for each satellite
    print(f"\nQuerying SatNOGS for {len(satellites)} satellites...")
    status_records = []
    freq_records = []

    for idx, (cat_id, name) in enumerate(satellites, 1):
        print(f"\r  [{idx}/{len(satellites)}] {cat_id}: {name}", end="", flush=True)

        # Status
        status = get_satellite_status(cat_id, name)
        if status:
            status_records.append(status)
        else:
            # Satellite not in SatNOGS — mark as unknown
            status_records.append({
                "norad_cat_id": cat_id,
                "name": name,
                "status": "unknown",
                "last_updated": "",
            })

        # Transmitters
        transmitters = get_transmitters(cat_id, name, filter_amateur=args.filter_amateur_bands)
        freq_records.extend(transmitters)

    print()  # newline after progress

    # Step 3: Write CSV files
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status_path = os.path.join(outdir, "sat-status.csv")
    freq_path = os.path.join(outdir, "sat-freqs.csv")

    print(f"\nWriting CSV files to {outdir}/")
    write_csv(status_path, status_records, STATUS_FIELDS)
    write_csv(freq_path, freq_records, FREQ_FIELDS)

    # Step 4: Write metadata
    meta_path = os.path.join(outdir, "metadata.json")
    metadata = {
        "generated": timestamp,
        "source_celestrak": CELESTRAK_URL,
        "source_satnogs": SATNOGS_BASE,
        "satellite_count": len(satellites),
        "status_records": len(status_records),
        "frequency_records": len(freq_records),
        "filter_amateur_bands": args.filter_amateur_bands,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    print(f"  Written metadata to {meta_path}")

    print(f"\nDone! Generated {len(status_records)} status records and {len(freq_records)} frequency records.")


if __name__ == "__main__":
    main()
