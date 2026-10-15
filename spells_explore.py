#!/usr/bin/env python3
"""
spells_explore.py — Fetch per-archetype card stats via spells-mtg and save to CSV.

# AGENT NOTE (2026-07-24):
# ─────────────────────────────────────────────────────────────────────────────
# SET-TYPE AUTO-DETECTION (from sets/<SET>/set_data.txt):
#
#   The script reads the archetype win-rate table to classify the set:
#
#   • Bicolor set  — majority of viable (>WR_VIABLE%) archetypes are 2-color
#     → query all 10 two-color pairs; MULTI = 3+ color decks combined
#
#   • Tricolor set — majority of viable archetypes are 3-color triples
#     → query all 10 two-color pairs AND all 10 three-color triples;
#        MULTI = 4+ color decks combined
#
#   • 5-color set  — at least one 4/5-color archetype is viable
#     → adds a separate WUBRG query on top of the above
#
#   Examples:
#     DSK → bicolor  (only WU/WR above threshold; triple/5C rates are low)
#     SOS → bicolor with 5-color (WB/RW dominate; 5C decks viable)
#
# OUTPUT FORMAT:
#   CSV columns: pair, name, color, num_oh, oh_wr, num_gih, gih_wr
#   "pair" values: 2-letter pairs ("WU"), 3-letter triples ("WUB"),
#                  "MULTI" (catch-all splash bucket), or "5C" (5-color decks)
#
#   In limitedtool.py's _card_lane_pools(), any label that isn't a 2-letter
#   LANE_PAIR is mapped to the MULTI lane, so "WUB", "MULTI", and "5C" all
#   correctly fall into the MULTI lane bucket.
#
# USAGE:
#   python spells_explore.py [SET]           (default: DSK)
#
# FIRST-TIME SETUP:
#   spells add <SET>    — downloads 17Lands draft+game data (~250 MB gz)
# ─────────────────────────────────────────────────────────────────────────────
"""

import sys
from pathlib import Path

import polars as pl
from spells import summon

# ── Constants ────────────────────────────────────────────────────────────────

EXPANSION = sys.argv[1] if len(sys.argv) > 1 else "DSK"
MIN_GIH   = 100          # minimum GIH game count to include a card
WR_VIABLE = 55.0         # win-rate threshold that marks an archetype as "viable"

# All 2-color pairs in canonical WUBRG order
PAIRS: list[str] = ["WU", "WB", "WR", "WG", "UB", "UR", "UG", "BR", "BG", "RG"]

# All 3-color triples in canonical WUBRG order
TRIPLES: list[str] = [
    "WUB", "WUR", "WUG", "WBR", "WBG", "WRG",
    "UBR", "UBG", "URG", "BRG",
]


# ── Set-type detection ────────────────────────────────────────────────────────

def _detect_set_type(expansion: str) -> tuple[str, bool]:
    """
    Parse sets/<expansion>/set_data.txt and return:
      (main_type, has_5color)

    main_type : "bicolor"  — primary archetypes are 2-color pairs
                "tricolor" — primary archetypes are 3-color triples
    has_5color: True if at least one 4/5-color archetype is viable (> WR_VIABLE%)

    Detection heuristic:
      Count the number of viable archetypes (WR > WR_VIABLE) in the 2-color,
      3-color, and 4/5-color buckets.  The bucket with the most viable entries
      determines the main type.  Any viable 4/5-color archetype sets has_5color.
    """
    path = Path("sets") / expansion / "set_data.txt"
    if not path.exists():
        print(f"  [warn] {path} not found — defaulting to bicolor, no 5C")
        return "bicolor", False

    viable: dict[str, int] = {"2": 0, "3": 0, "4+": 0}
    current_bucket: str | None = None

    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("Color"):
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            label, _wins, _games, wr_str = parts[0], parts[1], parts[2], parts[3]

            # Section headers
            if label.startswith("Two-color"):
                current_bucket = "2"
                continue
            if label.startswith("Three-color"):
                current_bucket = "3"
                continue
            if label.startswith(("Four-color", "Five-color")):
                current_bucket = "4+"
                continue

            # Skip non-archetype rows
            skip_prefixes = ("Mono", "All Decks", "Not-")
            if any(label.startswith(p) for p in skip_prefixes):
                continue

            if current_bucket is None:
                continue

            try:
                wr = float(wr_str.rstrip("%"))
            except ValueError:
                continue

            if wr >= WR_VIABLE:
                viable[current_bucket] += 1

    print(
        f"  Set-type detection — viable archetypes: "
        f"2-color={viable['2']}, 3-color={viable['3']}, 4/5-color={viable['4+']}"
    )

    main_type = "tricolor" if viable["3"] > viable["2"] else "bicolor"
    has_5color = viable["4+"] > 0
    print(f"  → main_type={main_type}, has_5color={has_5color}")
    return main_type, has_5color


# ── Data fetching ─────────────────────────────────────────────────────────────

def _fetch_combo(expansion: str, combo: str, min_gih: int) -> pl.DataFrame:
    """
    Query spells for a single color combination (e.g. "WU", "WUB", "MULTI", "5C").
    Returns a DataFrame with a 'pair' label column added.
    """
    if combo == "MULTI":
        # All decks with more colors than the main archetype — determined by caller
        raise ValueError("Use _fetch_multi() for the MULTI bucket")

    df = (
        summon(
            expansion,
            columns=["color", "num_oh", "oh_wr", "num_gih", "gih_wr"],
            filter_spec={"main_colors": combo},
        )
        .filter(pl.col("num_gih") >= min_gih)
        .with_columns(pl.lit(combo).alias("pair"))
    )
    return df


def _fetch_multi(expansion: str, min_colors: int, min_gih: int, label: str) -> pl.DataFrame:
    """
    Fetch card stats from decks with at least min_colors colors (the splash/MULTI bucket).
    """
    df = (
        summon(
            expansion,
            columns=["color", "num_oh", "oh_wr", "num_gih", "gih_wr"],
            filter_spec={"lhs": "num_colors", "op": ">=", "rhs": min_colors},
        )
        .filter(pl.col("num_gih") >= min_gih)
        .with_columns(pl.lit(label).alias("pair"))
    )
    return df


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n{'='*60}")
    print(f"spells_explore — {EXPANSION}")
    print('='*60)

    # ── Detect set type ───────────────────────────────────────────────────────
    main_type, has_5color = _detect_set_type(EXPANSION)

    combos_to_fetch: list[str] = list(PAIRS)
    if main_type == "tricolor":
        combos_to_fetch += TRIPLES
    # MULTI and 5C are fetched separately below

    # ── Archetype overview ────────────────────────────────────────────────────
    print(f"\n--- Archetype win rates (num_colors = {2 if main_type == 'bicolor' else 3}) ---")
    n_colors = 2 if main_type == "bicolor" else 3
    arch = (
        summon(
            EXPANSION,
            columns=["num_games", "game_wr"],
            group_by=["main_colors"],
            filter_spec={"num_colors": n_colors},
        )
        .sort("game_wr", descending=True)
    )
    print(arch)

    # ── Per-archetype card data ───────────────────────────────────────────────
    print(f"\n--- Fetching per-archetype card data (min {MIN_GIH} GIH games) ---")
    frames: list[pl.DataFrame] = []

    for combo in combos_to_fetch:
        print(f"  {combo} ...", end=" ", flush=True)
        df = _fetch_combo(EXPANSION, combo, MIN_GIH)
        frames.append(df)
        print(f"{len(df)} cards")

    # MULTI bucket: decks with more colors than the main archetype
    multi_min_colors = 3 if main_type == "bicolor" else 4
    print(f"  MULTI (num_colors >= {multi_min_colors}) ...", end=" ", flush=True)
    multi_df = _fetch_multi(EXPANSION, multi_min_colors, MIN_GIH, "MULTI")
    frames.append(multi_df)
    print(f"{len(multi_df)} cards")

    # Optional dedicated 5-color bucket
    if has_5color:
        print(f"  5C (num_colors = 5) ...", end=" ", flush=True)
        five_df = _fetch_multi(EXPANSION, 5, MIN_GIH, "5C")
        frames.append(five_df)
        print(f"{len(five_df)} cards")

    combined = pl.concat(frames)
    print(f"\nTotal rows: {len(combined)}")
    print(f"Pairs/buckets: {sorted(combined['pair'].unique().to_list())}")

    # ── Sample output ─────────────────────────────────────────────────────────
    print("\n--- Top 5 by GIH WR per archetype ---\n")
    for label in combos_to_fetch + ["MULTI"] + (["5C"] if has_5color else []):
        top = (
            combined
            .filter(pl.col("pair") == label)
            .sort("gih_wr", descending=True)
            .head(5)
            .select(["name", "color", "num_gih", "gih_wr", "oh_wr"])
        )
        if len(top) == 0:
            continue
        print(f"=== {label} ===")
        print(top)
        print()

    # ── Save ──────────────────────────────────────────────────────────────────
    out_path = f"sets/{EXPANSION}/spells_{EXPANSION}_by_pair.csv"
    combined.write_csv(out_path)
    print(f"Full archetype data saved to {out_path}")


if __name__ == "__main__":
    main()

