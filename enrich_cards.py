#!/usr/bin/env python3
"""
enrich_cards.py — Build an enriched limited-format card stats CSV.

Steps:
  1. Read carddata_pro.csv (draft stats), drop ignored columns.
  2. Look each card up by name in carddata_raw.csv (Scryfall flat export),
     add Type and CMC.
  3. Query 17lands for all 26 valid colour combinations (pairs, triples, quads, 5C).
     collect OH WR / GIH WR per card per filter, keep the top 4 by GIH WR.
     Each Best column is a single parseable cell: "LABEL:GIH%:OH%"
     e.g. "UR+:67.0%:65.5%"  or  "UR+:67.0%:"  when OH WR is absent.
  4. Write carddata_enriched.csv.

Usage:
    python enrich_cards.py [-s STATS] [-r RAW] [-o OUTPUT] [-e EXPANSION]

Options:
    -s / --stats      Draft stats CSV              (default: carddata_pro.csv)
    -r / --raw        Scryfall flat CSV             (default: carddata_raw.csv)
    -o / --output     Output CSV                    (default: carddata_enriched.csv)
    -e / --expansion  Set code for 17lands queries  (default: SOS)
"""

import argparse
import csv
import json
import sys
import time
import urllib.request
from itertools import combinations
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FORMAT_17L = "PremierDraft"

# All colour combinations from 2 to 5 colours in canonical WUBRG order.
# Each entry is the colours string used as the `colors=` query parameter.
_COLORS_ORDER = "WUBRG"
QUERIES_17L: list[str] = [
    "".join(combo)
    for n in range(2, 6)
    for combo in combinations(_COLORS_ORDER, n)
]  # 10 pairs + 10 triples + 5 quads + 1 five-colour = 26 queries

STATS_KEEP    = ["Name", "Color", "ALSA", "ATA", "OH WR", "GIH WR"]
OUTPUT_FIELDS = [
    "Name", "Type", "CMC", "Color", "Rarity",
    "ALSA", "ATA", "OH WR", "GIH WR",
    "ALSA Pro", "ATA Pro", "OH WR Pro", "GIH WR Pro",
    "Best1", "Best2", "Best3", "Best4",
    "ArenaId",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalise(name: str) -> str:
    return " ".join(name.lower().split())


def fmt_pct(value: float | None) -> str:
    """Format a 0-1 float as '63.2%', or '' when absent."""
    if value is None:
        return ""
    return f"{value * 100:.1f}%"


# ---------------------------------------------------------------------------
# Step 0 – auto-fetch Scryfall raw card data if absent
# ---------------------------------------------------------------------------

# Columns written to carddata_raw.csv by fetch_scryfall_raw().
# enrich_cards.py only reads: name, arena_id, type_line, cmc.
# Extra columns are kept for future use and human inspection.
_RAW_CSV_FIELDS = [
    "name", "arena_id", "mtgo_id", "set", "rarity",
    "cmc", "type_line", "mana_cost", "colors", "color_identity",
    "layout", "oracle_id",
]


def fetch_scryfall_raw(expansion: str, output_path: Path, force: bool = False) -> None:
    """
    If output_path does not exist (or force=True), page through the Scryfall search API
    for the given set and write a minimal flat CSV.

    Use force=True (--refresh-raw CLI flag) when Scryfall has updated its data for a
    new set and you need to re-fetch arena_id values that were initially missing.
    """
    if output_path.exists() and not force:
        print(f"  Raw CSV already present at '{output_path}' — skipping Scryfall fetch.")
        return
    if output_path.exists() and force:
        print(f"  --refresh-raw: deleting existing '{output_path}' and re-fetching.")
        output_path.unlink()

    set_code = expansion.lower()
    base_url = (
        "https://api.scryfall.com/cards/search"
        "?format=json&include_extras=false&include_multilingual=false"
        f"&include_variations=false&order=name&q=%28set%3A{set_code}%29&unique=cards"
    )
    headers = {
        "User-Agent": "LimitedTool/1.0 (personal use)",
        "Accept": "application/json",
    }

    print(f"\nFetching Scryfall card data for set '{expansion}' ...")
    all_cards: list[dict] = []
    url: str | None = base_url
    page = 1
    while url:
        print(f"  Page {page} ...", end=" ", flush=True)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"FAILED ({exc})")
            break
        batch = data.get("data", [])
        all_cards.extend(batch)
        print(f"{len(batch)} cards")
        url = data.get("next_page") if data.get("has_more") else None
        page += 1
        if url:
            time.sleep(0.15)  # be polite to Scryfall

    print(f"  Total fetched: {len(all_cards)} cards")
    if not all_cards:
        print(f"  [warn] No cards returned from Scryfall for set '{expansion}'.")
        return

    # Abort if Scryfall has no arena_id for any card — without it the Draft
    # Advisor cannot map pack card integers from the MTGA log to card names,
    # making the enriched file useless for live drafts.
    n_with_arena = sum(1 for c in all_cards if c.get("arena_id"))
    if n_with_arena == 0:
        cards_on_arena = any("arena" in (c.get("games") or []) for c in all_cards)
        if cards_on_arena:
            msg = (
                f"\nERROR: Scryfall returned {len(all_cards)} cards for '{expansion}' "
                f"but none have an arena_id yet.\n"
                f"  The set is listed as available on Arena, so Scryfall's data is\n"
                f"  likely still being populated (common with newly-released sets).\n"
                f"  Wait a few days and re-run with --refresh-raw once Scryfall\n"
                f"  has added the Arena IDs."
            )
        else:
            msg = (
                f"\nERROR: No arena_id found for any card in set '{expansion}'.\n"
                f"  This set may not be available on MTG Arena.\n"
                f"  The enriched CSV would be useless for live draft advising."
            )
        print(msg, file=sys.stderr)
        sys.exit(1)

    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_RAW_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for card in all_cards:
            writer.writerow({
                "name":           card.get("name", ""),
                "arena_id":       card.get("arena_id") or "",
                "mtgo_id":        card.get("mtgo_id") or "",
                "set":            (card.get("set") or "").upper(),
                "rarity":         card.get("rarity", ""),
                "cmc":            card.get("cmc", ""),
                "type_line":      card.get("type_line", ""),
                "mana_cost":      card.get("mana_cost", ""),
                "colors":         "".join(card.get("colors") or []),
                "color_identity": "".join(card.get("color_identity") or []),
                "layout":         card.get("layout", ""),
                "oracle_id":      card.get("oracle_id", ""),
            })
    print(f"  Wrote {len(all_cards)} cards to '{output_path}'.")


def _weighted_avg(
    entries: list[tuple[float, float | None, int, int]],
) -> tuple[float, float | None]:
    """Compute pick-weighted GIH WR and OH WR from (gih, oh, edgc, ohgc) tuples."""
    total_edgc = sum(t[2] for t in entries)
    if total_edgc > 0:
        gih_avg = sum(t[0] * t[2] for t in entries) / total_edgc
    else:
        gih_avg = sum(t[0] for t in entries) / len(entries)
    oh_pairs = [(t[1], t[3]) for t in entries if t[1] is not None]
    total_ohgc = sum(p for _, p in oh_pairs)
    oh_avg: float | None = (
        sum(v * p for v, p in oh_pairs) / total_ohgc if total_ohgc > 0 else None
    )
    return gih_avg, oh_avg


# ---------------------------------------------------------------------------
# Step 1b – load spells per-pair data (preferred source for Best1-4)
# ---------------------------------------------------------------------------

def load_spells_pairs(spells_path: Path) -> dict[str, list[str]]:
    """
    Read a spells_<SET>_by_pair.csv (columns: pair, name, color, num_oh, oh_wr,
    num_gih, gih_wr) and return a dict:
      normalised_name -> [Best1_cell, Best2_cell, Best3_cell, Best4_cell]

    Best cells are formatted as "WU:GIH=63.2%:OH=61.5%" (OH omitted if null).
    Pairs are ranked by gih_wr descending, top 4 returned.
    """
    if not spells_path.exists():
        return {}

    # Group rows by name
    from collections import defaultdict
    rows_by_name: dict[str, list[tuple[str, float, float | None]]] = defaultdict(list)

    with spells_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = row.get("name", "").strip()
            pair = row.get("pair", "").strip()
            if not name or not pair:
                continue
            try:
                gih = float(row.get("gih_wr", "") or "")
            except ValueError:
                continue
            try:
                oh: float | None = float(row.get("oh_wr", "") or "")
            except ValueError:
                oh = None
            rows_by_name[normalise(name)].append((pair, gih, oh))

    result: dict[str, list[str]] = {}
    for name_key, entries in rows_by_name.items():
        entries.sort(key=lambda x: x[1], reverse=True)
        cells = [
            f"{pair}:GIH={fmt_pct(gih)}:OH={fmt_pct(oh)}"
            for pair, gih, oh in entries[:4]
        ]
        while len(cells) < 4:
            cells.append("")
        result[name_key] = cells

    return result


# ---------------------------------------------------------------------------
# Step 1 – load Scryfall raw CSV
# ---------------------------------------------------------------------------

def load_raw(raw_path: Path) -> dict[str, dict]:
    db: dict[str, dict] = {}
    with raw_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            full_name: str = row.get("name", "")
            if not full_name:
                continue
            key = normalise(full_name)
            if key not in db:
                db[key] = row
            # Index front face of DFCs separately
            if " // " in full_name:
                front_key = normalise(full_name.split(" // ")[0])
                if front_key not in db:
                    db[front_key] = row
    return db


# ---------------------------------------------------------------------------
# Step 2 – fetch 17lands data
# ---------------------------------------------------------------------------

def fetch_17lands(expansion: str) -> dict[str, list[tuple[str, float, float | None]]]:
    """
    Returns a dict: card_name_lower -> list of (label, gih_wr, oh_wr).

    A card entry from a query is only included if:
      - ever_drawn_win_rate is not None
      - the card's own colour set (from the API response) is a subset of
        the query colour set  (e.g. a UG card is included for UBG but not UB)
      Colorless / artifact cards (empty colour field) are valid in all queries.
    """
    base = "https://www.17lands.com/card_ratings/data"
    card_data: dict[str, list] = {}

    headers = {
        "User-Agent": "LimitedToolScript/1.0 (personal use)",
        "Accept": "application/json",
    }

    total = len(QUERIES_17L)
    for idx, query_colors in enumerate(QUERIES_17L, 1):
        url = (
            f"{base}?expansion={expansion}"
            f"&format={FORMAT_17L}"
            f"&colors={query_colors}"
        )
        print(f"  [{idx}/{total}] Fetching {query_colors} ... ", end="", flush=True)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            print(f"FAILED ({exc})")
            continue

        query_set = set(query_colors)
        count = 0
        for card in data:
            name: str = card.get("name", "")
            if not name:
                continue
            gih_wr: float | None = card.get("ever_drawn_win_rate")
            oh_wr:  float | None = card.get("opening_hand_win_rate")
            if gih_wr is None:
                continue
            # Only use this data point if the card's colours fit the queried archetype.
            card_color_set = set(card.get("color") or "")
            if not card_color_set.issubset(query_set):
                continue
            edgc: int = card.get("ever_drawn_game_count") or 0
            ohgc: int = card.get("opening_hand_game_count") or 0
            key = normalise(name)
            card_data.setdefault(key, []).append((query_colors, gih_wr, oh_wr, edgc, ohgc))
            count += 1

        print(f"{count} cards with GIH WR data.")
        if idx < total:
            time.sleep(0.4)   # be polite

    return card_data


def aggregate_and_rank(
    entries: list[tuple[str, float, float | None, int, int]],
    card_color: str,
) -> list[str]:
    """
    For each 'anchor' colour combo (smallest superset of the card's colours at
    the query level), produce two buckets:
      - Exact: query colour set == anchor  -> label = anchor (e.g. "WB")
      - Splash (+): query is strict superset of anchor -> label = anchor + "+"

    Anchor size = max(2, len(card_colour_set)).
    Mono/colorless cards get one anchor per 2-colour pair that contains them
    (up to 4 anchors for mono, 10 for colorless).
    Each bucket is a game-count-weighted average.
    All non-empty buckets are ranked by total game count, top 4 returned.
    Cells formatted as "WB:GIH=58.0%:OH=56.2%".
    """
    card_set = set(card_color)
    anchor_size = max(2, len(card_set))

    # All anchor combos of the right size that are supersets of card_set.
    anchor_combos: list[str] = [
        "".join(combo)
        for combo in combinations(_COLORS_ORDER, anchor_size)
        if card_set.issubset(set(combo))
    ]

    cells: list[tuple[str, float, float | None, int]] = []  # (label, gih, oh, total_gc)

    for anchor in anchor_combos:
        anchor_set = set(anchor)
        exact_raw: list[tuple[float, float | None, int, int]] = []
        supra_raw: list[tuple[float, float | None, int, int]] = []

        for qc, gih, oh, edgc, ohgc in entries:
            qset = set(qc)
            if qset == anchor_set:
                exact_raw.append((gih, oh, edgc, ohgc))
            elif anchor_set.issubset(qset):
                supra_raw.append((gih, oh, edgc, ohgc))

        if exact_raw:
            gih_avg, oh_avg = _weighted_avg(exact_raw)
            total_gc = sum(t[2] for t in exact_raw)
            cells.append((anchor, gih_avg, oh_avg, total_gc))

        if supra_raw:
            gih_avg, oh_avg = _weighted_avg(supra_raw)
            total_gc = sum(t[2] for t in supra_raw)
            cells.append((anchor + "+", gih_avg, oh_avg, total_gc))

    # Rank by total game count descending, return top 4.
    cells.sort(key=lambda x: x[3], reverse=True)
    return [
        f"{lbl}:GIH={fmt_pct(gih)}:OH={fmt_pct(oh)}"
        for lbl, gih, oh, _ in cells[:4]
    ]


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def load_stats(path: Path) -> dict[str, dict]:
    """Load a stats CSV into a dict keyed by normalised name."""
    db: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = normalise(row.get("Name", ""))
            if key:
                db[key] = row
    return db


def enrich(stats_path: Path, medium_path: Path | None, raw_path: Path, output_path: Path, expansion: str, spells_path: Path | None = None, refresh_raw: bool = False) -> None:
    # --- Step 0: fetch raw Scryfall data if not present (or refresh requested) ---
    fetch_scryfall_raw(expansion, raw_path, force=refresh_raw)

    # --- Step 1: raw card data ---
    print(f"Loading raw card data from '{raw_path}' ...")
    db = load_raw(raw_path)
    print(f"  -> {len(db):,} name entries loaded.")

    # --- Step 1b: pro stats (supplemental lookup) ---
    pro_db: dict[str, dict] = {}
    if stats_path is not None and stats_path.exists():
        pro_db = load_stats(stats_path)
        print(f"Loading pro data from '{stats_path}' ... {len(pro_db):,} entries.")

    # --- Step 1c: spells per-pair data (preferred Best source) ---
    spells_db: dict[str, list[str]] = {}
    if spells_path is not None:
        spells_db = load_spells_pairs(spells_path)
        print(f"Loading spells pair data from '{spells_path}' ... {len(spells_db):,} cards.")
    else:
        # Auto-detect: look for spells_<EXPANSION>_by_pair.csv next to the output
        auto = output_path.parent / f"spells_{expansion}_by_pair.csv"
        if auto.exists():
            spells_db = load_spells_pairs(auto)
            print(f"Auto-loaded spells pair data from '{auto}' ... {len(spells_db):,} cards.")

    # --- Step 2: 17lands ---
    print(f"\nFetching 17lands data for expansion '{expansion}' ...")
    lands_data = fetch_17lands(expansion)
    print(f"  -> {len(lands_data):,} cards with at least one GIH WR entry.")

    # --- Step 3: build output ---
    not_found_raw: list[str] = []
    not_found_17l: list[str] = []

    if medium_path is None or not medium_path.exists():
        print(f"Error: medium stats file not found: '{medium_path}'", file=sys.stderr)
        sys.exit(1)

    with (
        medium_path.open(newline="", encoding="utf-8-sig") as in_fh,
        output_path.open("w", newline="", encoding="utf-8") as out_fh,
    ):
        reader = csv.DictReader(in_fh)
        writer = csv.DictWriter(out_fh, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()

        for row in reader:
            name: str = row.get("Name", "")
            key = normalise(name)

            # Pro values: look up separately, never modify the medium row.
            pro = pro_db.get(key, {})

            # Scryfall enrichment
            raw = db.get(key)
            if raw is None:
                not_found_raw.append(name)
                card_type, cmc = "", ""
            else:
                card_type = raw.get("type_line", "")
                cmc       = raw.get("cmc", "")

            # Best1-4: spells per-pair data preferred; fall back to 17lands API
            bests = spells_db.get(key, [])
            if not bests:
                entries = lands_data.get(key, [])
                bests = aggregate_and_rank(entries, row.get("Color", ""))
                if not bests:
                    not_found_17l.append(name)
            while len(bests) < 4:
                bests.append("")

            writer.writerow({
                "Name":       name,
                "Type":       card_type,
                "CMC":        cmc,
                "Color":      row.get("Color", ""),
                "Rarity":     row.get("Rarity", ""),
                "ALSA":       row.get("ALSA", ""),
                "ATA":        row.get("ATA", ""),
                "OH WR":      row.get("OH WR", ""),
                "GIH WR":     row.get("GIH WR", ""),
                "ALSA Pro":   pro.get("ALSA", ""),
                "ATA Pro":    pro.get("ATA", ""),
                "OH WR Pro":  pro.get("OH WR", ""),
                "GIH WR Pro": pro.get("GIH WR", ""),
                "Best1":      bests[0],
                "Best2":      bests[1],
                "Best3":      bests[2],
                "Best4":      bests[3],
                "ArenaId":    raw.get("arena_id", "") if raw else "",
            })

    print(f"\nOutput written to '{output_path}'.")

    if not_found_raw:
        print(f"\n  {len(not_found_raw)} card(s) not found in raw Scryfall data:")
        for n in not_found_raw:
            print(f"    - {n}")

    if not_found_17l:
        print(f"\n  {len(not_found_17l)} card(s) had no 17lands GIH WR in any filter:")
        for n in not_found_17l:
            print(f"    - {n}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich limited draft stats CSV with type, CMC, and 17lands colour-pair win rates.",
    )
    parser.add_argument("-s", "--stats",     default="carddata_pro.csv",     help="Draft stats CSV (default: carddata_pro.csv)")
    parser.add_argument("-m", "--medium",    default="carddata_medium.csv",   help="Medium fallback stats CSV (default: carddata_medium.csv)")
    parser.add_argument("-r", "--raw",       default="carddata_raw.csv",      help="Scryfall flat CSV (default: carddata_raw.csv)")
    parser.add_argument("-o", "--output",    default="carddata_enriched.csv", help="Output CSV (default: carddata_enriched.csv)")
    parser.add_argument("-e", "--expansion", default="SOS",                   help="17lands expansion code (default: SOS)")
    parser.add_argument("-p", "--spells",       default=None,                    help="spells per-pair CSV (default: auto-detect spells_<SET>_by_pair.csv)")
    parser.add_argument("--refresh-raw",         action="store_true",             help="Delete and re-fetch carddata_raw.csv from Scryfall (use when arena_id was missing on first fetch)")
    args = parser.parse_args()

    stats_path  = Path(args.stats)
    medium_path = Path(args.medium) if args.medium else None
    raw_path    = Path(args.raw)
    output_path = Path(args.output)
    spells_path = Path(args.spells) if args.spells else None

    for path, label in [(medium_path, "Medium stats CSV")]:
        if path and not path.exists():
            print(f"Error: {label} not found: '{path}'", file=sys.stderr)
            sys.exit(1)

    enrich(stats_path, medium_path, raw_path, output_path, args.expansion, spells_path, refresh_raw=args.refresh_raw)


if __name__ == "__main__":
    main()
