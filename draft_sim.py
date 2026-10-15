#!/usr/bin/env python3
"""
draft_sim.py -- Simulate an 8-player draft pod for testing the Draft Advisor.

Builds 8 packs (1 rare/mythic, 3 uncommons, 10 commons with at least one per
color), then writes pick events to a log file so the Draft Advisor can be
observed live. All 8 seats use the same card_score logic as the player, with
their own accumulated pick context (depth, curve, duplicates).

Usage:
    python draft_sim.py [--log PATH] [--seed N]

    --log      Output log path. Default: Player.log in MTGA folder.
    --seed     Random seed for reproducibility.

To connect the Draft Advisor, either:
  - Launch:   python limitedtool.py --log Player_sim.log
  - Or click  "Watch sim log" in the Draft Advisor tab after starting this script.
"""

import argparse
import csv
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT      = Path(__file__).parent
SIM_LOG   = Path.home() / "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
WUBRG     = "WUBRG"
PACK_SIZE = 14
N_PLAYERS = 8

# Import scoring from limitedtool (same folder).
# Paths are resolved at runtime after the user selects a set.
try:
    from limitedtool import (
        load_enriched, compute_density_baseline,
        count_color_depth, count_pair_synergy, card_score,
    )
    _SCORING_AVAILABLE = True
except ImportError:
    _SCORING_AVAILABLE = False

# Available sets for the selector (code, display name).
# Add new sets here as they are added to the sets/ directory.
SETS: list[tuple[str, str]] = [
    ("BLB", "Bloomburrow"),
    ("DSK", "Duskmourn: House of Horror"),
    ("EOE", "Edge of Eternities"),
    ("SOS", "SOS"),
]


# ---------------------------------------------------------------------------
# Set selector
# ---------------------------------------------------------------------------

def _pointer_select(options: list[tuple[str, str]], title: str = "Select a set:") -> str:
    """Interactive arrow-key selection menu.

    Moves a '>' pointer up/down with arrow keys; Enter confirms.
    Requires Windows Console with VT100 support (Windows Terminal, VS Code
    terminal, modern PowerShell).  Falls back to a numbered list otherwise.
    """
    import ctypes

    # Enable VT100/ANSI processing on the Windows console handle.
    # Without this, escape codes print as literal characters.
    def _enable_ansi() -> bool:
        try:
            STD_OUTPUT_HANDLE              = -11
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32 = ctypes.windll.kernel32
            handle   = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            mode     = ctypes.c_ulong()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            return bool(kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            ))
        except Exception:
            return False

    try:
        import msvcrt
        ansi_ok = _enable_ansi()
    except ImportError:
        ansi_ok = False

    if not ansi_ok:
        # Plain numbered fallback (non-Windows or unsupported terminal)
        print(f"\n{title}")
        for i, (code, name) in enumerate(options, 1):
            print(f"  {i}. {code}  {name}")
        while True:
            raw = input("Enter number: ").strip()
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(options):
                    return options[idx][0]
            except ValueError:
                pass
            print(f"  Please enter 1\u2013{len(options)}")

    n        = len(options)
    selected = 0

    # ANSI helpers
    HIDE     = "\033[?25l"   # hide cursor (cleaner while navigating)
    SHOW     = "\033[?25h"   # restore cursor
    GREEN    = "\033[32m"
    DIM      = "\033[2m"
    RESET    = "\033[0m"

    def _draw() -> None:
        sys.stdout.write(f"{title}\n")
        for i, (code, name) in enumerate(options):
            if i == selected:
                sys.stdout.write(f"  {GREEN}> {code:<6} {name}{RESET}\n")
            else:
                sys.stdout.write(f"  {DIM}  {code:<6} {name}{RESET}\n")
        sys.stdout.flush()

    lines = n + 1   # title line + n option lines

    sys.stdout.write(f"\n{HIDE}")
    sys.stdout.flush()
    _draw()

    try:
        while True:
            key = msvcrt.getch()
            if key == b"\xe0":               # extended-key prefix (arrow keys)
                arrow = msvcrt.getch()
                if arrow == b"H":            # ↑
                    selected = (selected - 1) % n
                elif arrow == b"P":          # ↓
                    selected = (selected + 1) % n
                else:
                    continue
                # Move cursor back to the first line of the menu, clear below,
                # then redraw.
                sys.stdout.write(f"\033[{lines}A\033[J")
                sys.stdout.flush()
                _draw()
            elif key in (b"\r", b"\n"):      # Enter — confirm selection
                sys.stdout.write(f"\n{SHOW}")
                sys.stdout.flush()
                return options[selected][0]
    except Exception:
        sys.stdout.write(SHOW)
        sys.stdout.flush()
        raise



# ---------------------------------------------------------------------------
# Card loading
# ---------------------------------------------------------------------------

def load_cards(enriched_csv: Path) -> list[dict]:
    """Return list of {arena_id, name, color, rarity, gih_wr} from carddata_enriched.csv."""
    cards: list[dict] = []
    if not enriched_csv.exists():
        return cards
    with enriched_csv.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = row.get("Name", "").strip()
            aid_str = row.get("ArenaId", "").strip()
            if not name or not aid_str.isdigit():
                continue
            cards.append({
                "arena_id": int(aid_str),
                "name":     name,
                "color":    row.get("Color",   "").strip(),
                "rarity":   row.get("Rarity",  "C").strip() or "C",
                "gih_wr":   row.get("GIH WR",  "").strip(),
            })
    return cards


# ---------------------------------------------------------------------------
# Pack building
# ---------------------------------------------------------------------------

PACK_LAND_BASIC_CHANCE = 0.5
LAND_RARITY_WEIGHTS = [("C", 0.75), ("U", 0.20), ("R", 0.04), ("M", 0.01)]


def build_packs(cards: list[dict], rng: random.Random,
                enriched_db: dict | None = None) -> list[list[int]]:
    """Build N_PLAYERS packs following Arena slot order.

    Slots per pack: 1 R/M · 3 U · 10 C (5 guaranteed mono-color) · 1 land
    Land slot: 50% basic (omitted) · 50% non-basic rolled by rarity.
    Order returned: [rare, uncommon×3, common×10, land?]
    """
    def _is_land(card: dict) -> bool:
        if enriched_db is not None:
            info = enriched_db.get(card["name"].lower(), {})
            return "Land" in info.get("Type", "")
        return not card["color"] and not card.get("gih_wr", "")

    by_rarity:   dict[str, list[dict]] = {r: [] for r in "CURM"}
    mono:        dict[str, list[dict]] = {c: [] for c in WUBRG}
    land_by_rar: dict[str, list[dict]] = {r: [] for r in "CURM"}

    for card in cards:
        r = card["rarity"]
        if _is_land(card):
            if r in land_by_rar:
                land_by_rar[r].append(card)
        else:
            if r in by_rarity:
                by_rarity[r].append(card)
            if r == "C" and len(card["color"]) == 1 and card["color"] in WUBRG:
                mono[card["color"]].append(card)

    rare_pool = by_rarity["R"] + by_rarity["M"]

    packs: list[list[int]] = []
    for _ in range(N_PLAYERS):
        used: set[int] = set()

        def draw(pool: list[dict]) -> dict | None:
            available = [c for c in pool if c["arena_id"] not in used]
            if not available:
                return None
            c = rng.choice(available)
            used.add(c["arena_id"])
            return c

        rare_slot:     list[int] = []
        uncommon_slot: list[int] = []
        common_slot:   list[int] = []
        land_slot:     list[int] = []

        r = draw(rare_pool)
        if r:
            rare_slot.append(r["arena_id"])

        for _ in range(3):
            u = draw(by_rarity["U"])
            if u:
                uncommon_slot.append(u["arena_id"])

        for color in WUBRG:
            c = draw(mono[color])
            if c:
                common_slot.append(c["arena_id"])
        for _ in range(max(0, 10 - len(common_slot))):
            c = draw(by_rarity["C"])
            if c:
                common_slot.append(c["arena_id"])

        if rng.random() >= PACK_LAND_BASIC_CHANCE:
            rarities = [r for r, _ in LAND_RARITY_WEIGHTS]
            weights  = [w for _, w in LAND_RARITY_WEIGHTS]
            chosen_r = rng.choices(rarities, weights=weights)[0]
            land = draw(land_by_rar.get(chosen_r, []))
            if land is None:
                for fallback in ("C", "U", "R", "M"):
                    land = draw(land_by_rar.get(fallback, []))
                    if land:
                        break
            if land:
                land_slot.append(land["arena_id"])

        packs.append(rare_slot + uncommon_slot + common_slot + land_slot)

    return packs


# ---------------------------------------------------------------------------
# Seat color assignment
# ---------------------------------------------------------------------------

def assign_colors(seats_arg: str | None, rng: random.Random) -> list[str]:
    """Return list of 7 color chars for seats 1-7."""
    if seats_arg:
        colors = [c.upper() for c in seats_arg if c.upper() in WUBRG]
        while len(colors) < 7:
            colors.append(rng.choice(WUBRG))
        return colors[:7]
    # Default: all 5 colors present, 2 extras random
    base = list(WUBRG) + [rng.choice(WUBRG), rng.choice(WUBRG)]
    rng.shuffle(base)
    return base


# ---------------------------------------------------------------------------
# Pick selection
# ---------------------------------------------------------------------------

def _gih(aid: int, id_info: dict) -> float:
    try:
        return float(id_info.get(aid, {}).get("gih_wr", "0").rstrip("%") or 0)
    except (ValueError, TypeError):
        return 0.0


def choose_pick(pack: list[int], color_pref: str | None, id_info: dict) -> int:
    """Pick highest GIH WR card, preferring color_pref if given."""
    if color_pref:
        matching = [a for a in pack if color_pref in id_info.get(a, {}).get("color", "")]
        if matching:
            return max(matching, key=lambda a: _gih(a, id_info))
    return max(pack, key=lambda a: _gih(a, id_info))


# ---------------------------------------------------------------------------
# Log writing
# ---------------------------------------------------------------------------

def _stamp(ts: datetime) -> str:
    return ts.strftime("%m/%d/%Y %I:%M:%S %p")


def write_set_event(fh, ts: datetime, set_code: str) -> None:
    """Write a fake EventJoin entry so the Draft Advisor detects the set."""
    event_name = f"QuickDraft_{set_code}_20000101"
    fh.write(f"[UnityCrossThreadLogger]{_stamp(ts)}\n")
    fh.write("<== EventJoin(...)\n")
    fh.write(json.dumps({"Course": {"InternalEventName": event_name}}) + "\n")
    fh.flush()


def write_notify(fh, ts: datetime, pick_idx: int, cards: list[int]) -> None:
    fh.write(f"[UnityCrossThreadLogger]{_stamp(ts)}\n")
    fh.write("Draft.Notify\n")
    fh.write(json.dumps({
        "draftId":  "sim-draft-001",
        "SelfPack": 0,
        "SelfPick": pick_idx,
        "PackCards": ",".join(str(c) for c in cards),
    }) + "\n")
    fh.flush()


def write_pick(fh, ts: datetime, pick_idx: int, card_id: int) -> None:
    fh.write(f"[UnityCrossThreadLogger]{_stamp(ts)}\n")
    fh.write("==> EventPlayerDraftMakePick\n")
    fh.write(json.dumps({
        "id":      "sim-pick-00000000",
        "request": json.dumps({
            "DraftId": "sim-draft-001",
            "GrpIds":  [card_id],
            "Pack":    0,
            "Pick":    pick_idx,
        }),
    }) + "\n")
    fh.flush()


# ---------------------------------------------------------------------------
# Simulation loop
# ---------------------------------------------------------------------------

def simulate(
    initial_packs: list[list[int]],
    id_info:       dict[int, dict],
    log_path:      Path,
    set_code:      str = "SOS",
    enriched_db:   dict | None = None,
    set_mean:      float = 54.0,
) -> None:
    packs = [list(p) for p in initial_packs]
    ts    = datetime.now()

    # Per-seat pick tracking
    seat_picks: list[list[tuple]] = [[] for _ in range(N_PLAYERS)]
    seat_crea:  list[list[int]]   = [[0] * 8 for _ in range(N_PLAYERS)]
    seat_other: list[list[int]]   = [[0] * 8 for _ in range(N_PLAYERS)]

    def lookup(aid: int) -> dict:
        if enriched_db is None:
            return id_info.get(aid, {})
        info = id_info.get(aid, {})
        return enriched_db.get(info.get("name", "").lower(), info)

    def score_for(seat: int, aid: int) -> float:
        if not _SCORING_AVAILABLE or enriched_db is None:
            return _gih(aid, id_info)
        r = lookup(aid)
        picks = seat_picks[seat]
        depth = count_color_depth(picks, lookup)
        pair_synergy = count_pair_synergy(picks, lookup)
        picked_names = [lookup(a).get("Name", "").lower() for *_, a in picks]
        return card_score(r, depth, len(picks),
                          seat_crea[seat], seat_other[seat],
                          set_mean, picked_names,
                          openness_fn=None, pair_synergy=pair_synergy,
                          picks=picks, lookup_fn=lookup)

    def record_pick(seat: int, pick_idx: int, aid: int) -> None:
        pr = lookup(aid)
        try:
            bucket = min(int(float(pr.get("CMC", "0") or "0")), 7)
        except (ValueError, TypeError):
            bucket = 0
        if "Creature" in pr.get("Type", ""):
            seat_crea[seat][bucket] += 1
        else:
            seat_other[seat][bucket] += 1
        seat_picks[seat].append((0, pick_idx, aid))

    print(f"\nWriting to {log_path}")
    print("Open the Draft Advisor and click 'Watch sim log'")
    print(f"(or launch:  python limitedtool.py --log \"{log_path}\")")
    if _SCORING_AVAILABLE and enriched_db is not None:
        print("[Scoring: all 8 seats use card_score from limitedtool]")
    else:
        print("[Scoring: GIH WR only (carddata_enriched.csv not found)]")
    print()

    with log_path.open("w", encoding="utf-8") as fh:
        write_set_event(fh, ts, set_code)
        ts += timedelta(seconds=1)
        for pick_idx in range(PACK_SIZE):
            # Show player their current pack
            write_notify(fh, ts, pick_idx, packs[0])
            ts += timedelta(seconds=1)

            # Best card by score (for highlighting)
            best_aid = max(packs[0], key=lambda a: score_for(0, a))
            best_num = packs[0].index(best_aid) + 1

            # Display in Arena slot order: rare · uncommons · commons · land
            print(f"Pack 1, Pick {pick_idx + 1}  ({len(packs[0])} cards):")
            for i, aid in enumerate(packs[0], 1):
                info = id_info.get(aid, {})
                sc   = score_for(0, aid)
                rar  = info.get("rarity", "?")
                marker = " *" if aid == best_aid else "  "
                print(f"  [{i:2d}]{marker} {info.get('name', f'#{aid}'):36s} "
                      f"{info.get('color', '?'):4s}  {rar}  "
                      f"score: {sc:.1f}  GIH: {info.get('gih_wr', '?')}")

            print(f"  -> Enter = pick [{best_num}] (best score), or type a number:")
            player_pick = None
            while player_pick is None:
                try:
                    raw = input().strip()
                except (KeyboardInterrupt, EOFError):
                    print("\nInterrupted.")
                    return
                if raw == "":
                    player_pick = best_aid
                else:
                    try:
                        n = int(raw)
                        if 1 <= n <= len(packs[0]):
                            player_pick = packs[0][n - 1]
                        else:
                            print(f"  Enter a number between 1 and {len(packs[0])}:")
                    except ValueError:
                        print(f"  Enter a number or press Enter:")

            packs[0].remove(player_pick)
            record_pick(0, pick_idx, player_pick)
            write_pick(fh, ts, pick_idx, player_pick)
            ts += timedelta(seconds=2)
            picked_name = id_info.get(player_pick, {}).get('name', player_pick)
            print(f"  => Picked: {picked_name}\n"
                  f"     Other seats:")

            # Other seats: score-based pick using their own context
            for seat in range(1, N_PLAYERS):
                pick = max(packs[seat], key=lambda a, s=seat: score_for(s, a))
                packs[seat].remove(pick)
                record_pick(seat, pick_idx, pick)
                depth = count_color_depth(seat_picks[seat], lookup) if _SCORING_AVAILABLE else {}
                top_colors = sorted(depth.items(), key=lambda x: x[1], reverse=True)
                color_str = "/".join(c for c, v in top_colors if v >= 1.0) or "?"
                print(f"     Seat {seat} ({color_str}): "
                      f"{id_info.get(pick, {}).get('name', pick)}")

            # Rotate packs left: seat i's pack goes to seat i+1
            if pick_idx < PACK_SIZE - 1:
                new_packs: list[list[int]] = [None] * N_PLAYERS  # type: ignore
                for i in range(N_PLAYERS):
                    new_packs[(i + 1) % N_PLAYERS] = packs[i]
                packs = new_packs

            print()

    print("Pack 1 simulation complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--log",  default=str(SIM_LOG),
                        help=f"Output log path (default: {SIM_LOG.name})")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducibility")
    args = parser.parse_args()

    rng      = random.Random(args.seed)
    log_path = Path(args.log)

    set_code     = _pointer_select(SETS, "Select a set:")
    sets_dir     = ROOT / "sets" / set_code
    enriched_csv = sets_dir / "carddata_enriched.csv"
    print(f"  Set: {set_code}  ({sets_dir})")

    print("Loading cards ...")
    cards = load_cards(enriched_csv)
    if not cards:
        print("ERROR: No cards loaded. "
              "Ensure carddata_raw.csv and carddata_medium.csv are present.")
        sys.exit(1)
    print(f"  {len(cards)} cards loaded.")

    enriched_db = None
    set_mean    = 54.0
    if _SCORING_AVAILABLE and enriched_csv.exists():
        enriched_db = load_enriched(enriched_csv)
        _, set_mean = compute_density_baseline(enriched_db)
        print(f"  Enriched CSV loaded ({len(enriched_db)} cards, set mean {set_mean:.1f}%).")
    elif _SCORING_AVAILABLE:
        print(f"  {enriched_csv.name} not found — falling back to GIH WR only.")

    print("Building 8 packs ...")
    initial_packs = build_packs(cards, rng, enriched_db=enriched_db)
    print(f"  Done. Pack sizes: {[len(p) for p in initial_packs]}")

    id_info = {c["arena_id"]: c for c in cards}
    simulate(initial_packs, id_info, log_path,
             set_code=set_code, enriched_db=enriched_db, set_mean=set_mean)


if __name__ == "__main__":
    main()
