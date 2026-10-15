#!/usr/bin/env python3
"""
limitedtool.py -- MTG Limited draft tool.

Card Browser + real-time MTGA Draft Advisor in a single tabbed window.

Usage:
    python limitedtool.py [CSV_PATH]   # launch GUI (default: carddata_enriched.csv)
    python limitedtool.py --test       # run parser unit tests (no GUI)
"""

import csv
import json
import random
import re
import sys
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import ttk

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

ROOT        = Path(__file__).parent
SETS_DIR    = ROOT / "sets"
DEFAULT_SET = "SOS"
DEFAULT_CSV = SETS_DIR / DEFAULT_SET / "carddata_enriched.csv"
RAW_CSV     = SETS_DIR / DEFAULT_SET / "carddata_raw.csv"
TEST_LOG    = ROOT / "Player_test.log"
SET_DATA    = ROOT / "set_data.txt"
LOG_PATH    = Path.home() / "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
POLL_MS     = 500

# Card browser field definitions: (CSV key, display label)
FIELDS = [
    ("Name",    "Name"),
    ("Type",    "Type"),
    ("CMC",     "CMC"),
    ("Color",   "Color"),
    ("ALSA",    "Avg Last Seen"),
    ("ATA",     "Avg Taken"),
    ("OH WR",   "Opening Hand WR"),
    ("GIH WR",  "Games In Hand WR"),
    ("Best1",   "Best #1"),
    ("Best2",   "Best #2"),
    ("Best3",   "Best #3"),
    ("Best4",   "Best #4"),
]

# Fields that have a Pro counterpart shown inline as "value  (Pro: value)"
PRO_FIELDS: dict[str, str] = {
    "ALSA":   "ALSA Pro",
    "ATA":    "ATA Pro",
    "OH WR":  "OH WR Pro",
    "GIH WR": "GIH WR Pro",
}

# Signal system color constants
WUBRG = "WUBRG"
COMBOS2 = ["WU", "WB", "WR", "WG", "UB", "UR", "UG", "BR", "BG", "RG"]
COMBOS3 = ["WUB", "WUR", "WUG", "WBR", "WBG", "WRG", "UBR", "UBG", "URG", "BRG"]

# Open-lane signal: 5 fixed bicolor lanes + multicolor bucket.
# Pair names use WUBRG-sorted order (same as _sort_color) so data lookups match.
LANE_PAIRS        = ["WB", "BG", "UG", "UR", "WR"]
LANE_KEYS         = LANE_PAIRS + ["MULTI"]
LANE_WR_THRESHOLD = 55.0   # minimum GIH WR% in a pair for it to count as a card's "best pool"
# Which lanes each WUBRG color participates in
COLOR_TO_LANES: dict[str, list[str]] = {
    "W": ["WB", "WR"],
    "U": ["UG", "UR"],
    "B": ["WB", "BG"],
    "R": ["UR", "WR"],
    "G": ["BG", "UG"],
}


def _sort_color(color: str) -> str:
    """Normalize a color string to WUBRG order (e.g. 'BW' -> 'WB')."""
    return "".join(c for c in WUBRG if c in color)


def _parse_best_entry(val: str) -> tuple[str, float]:
    """Parse 'WB:GIH=57.9%:OH=58.4%' -> ('WB', 57.9).  Returns ('', 0.0) on failure."""
    if not val:
        return "", 0.0
    parts = val.split(":")
    pair = _sort_color(parts[0].rstrip("+"))   # strip trailing '+', normalize order
    for p in parts[1:]:
        if p.startswith("GIH="):
            try:
                return pair, float(p[4:].rstrip("%"))
            except ValueError:
                pass
    return pair, 0.0


def _card_lane_pools(card: dict) -> list[str]:
    """Return the lane pools (from LANE_KEYS) where this card's GIH WR > LANE_WR_THRESHOLD.

    Bicolor pairs found in LANE_PAIRS map to that lane; anything else (tri+, or an
    off-lane pair like WU) maps to MULTI.
    """
    pools: list[str] = []
    seen: set[str] = set()
    for key in ("Best1", "Best2", "Best3", "Best4"):
        pair, wr = _parse_best_entry(card.get(key, ""))
        if not pair or wr < LANE_WR_THRESHOLD or pair in seen:
            continue
        seen.add(pair)
        if len(pair) == 2 and pair in LANE_PAIRS:
            pools.append(pair)
        elif "MULTI" not in pools:
            pools.append("MULTI")
    return pools


def _extract_set_code(event_name: str) -> str | None:
    """Extract set code from an Arena event name.

    'QuickDraft_BLB_20260617'   -> 'BLB'
    'PremierDraft_SOS_20260421' -> 'SOS'
    """
    parts = event_name.split("_")
    return parts[1].upper() if len(parts) >= 3 else None


def _csv_paths_for_set(set_code: str) -> tuple[Path, Path]:
    """Return (enriched_csv, raw_csv) for a given set code."""
    folder = SETS_DIR / set_code
    return folder / "carddata_enriched.csv", folder / "carddata_raw.csv"


# ---------------------------------------------------------------------------
# Test log generation  (replaces make_test_log.py)
# ---------------------------------------------------------------------------

def generate_test_log(raw_csv: Path, output: Path) -> Path:
    """Generate a fake Premier Draft log and return the output path.

    Simulates: Pack 1 complete (14 picks), Pack 2 picks 1-7 done,
    Pack 2 Pick 8 currently showing.
    """
    ids: list[int] = []
    with raw_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            aid = row.get("arena_id", "").strip()
            if aid.isdigit():
                ids.append(int(aid))

    rng = random.Random(42)
    rng.shuffle(ids)

    def make_pack(pool: list, size: int = 14) -> list:
        return [pool.pop() for _ in range(size)]

    pool = list(ids)
    pack1 = make_pack(pool)
    pack2 = make_pack(pool)

    lines: list[str] = []
    ts = datetime(2026, 6, 16, 19, 30, 0)

    def stamp() -> str:
        return ts.strftime("%#m/%#d/%Y %I:%M:%S %p")

    def add_notify(pack_idx: int, pick_idx: int, cards: list[int]) -> None:
        lines.append(f"[UnityCrossThreadLogger]{stamp()}")
        lines.append("Draft.Notify")
        lines.append(json.dumps({
            "draftId": "fake-draft-abc123",
            "SelfPack": pack_idx,
            "SelfPick": pick_idx,
            "PackCards": ",".join(str(c) for c in cards),
        }))

    def add_pick(pack_idx: int, pick_idx: int, card_id: int) -> None:
        nonlocal ts
        ts += timedelta(seconds=rng.randint(10, 45))
        lines.append(f"[UnityCrossThreadLogger]{stamp()}")
        lines.append("==> EventPlayerDraftMakePick")
        lines.append(json.dumps({
            "id": "aaaabbbb-cccc-dddd-eeee-ffffffffffff",
            "request": json.dumps({
                "DraftId": "fake-draft-abc123",
                "GrpIds": [card_id],
                "Pack": pack_idx,
                "Pick": pick_idx,
            }),
        }))
        ts += timedelta(seconds=1)
        lines.append(f"[UnityCrossThreadLogger]{stamp()}")
        lines.append("<== EventPlayerDraftMakePick(aaaabbbb-cccc-dddd-eeee-ffffffffffff)")
        lines.append('{"IsPickSuccessful":true}')

    # Pack 1: all 14 picks done
    pack = list(pack1)
    for pi in range(14):
        add_notify(0, pi, pack)
        chosen = pack.pop(0)
        add_pick(0, pi, chosen)
        ts += timedelta(seconds=2)

    # Pack 2: 7 picks done, pick 7 currently showing
    pack = list(pack2)
    for pi in range(7):
        add_notify(1, pi, pack)
        chosen = pack.pop(0)
        add_pick(1, pi, chosen)
        ts += timedelta(seconds=2)

    add_notify(1, 7, pack)

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_cards(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_arena_ids(raw_path: Path) -> dict[int, str]:
    """Build {arena_id -> card_name} from the Scryfall flat CSV."""
    id_map: dict[int, str] = {}
    if not raw_path.exists():
        return id_map
    with raw_path.open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            aid = row.get("arena_id", "").strip()
            name = row.get("name", "").strip()
            if aid and name:
                try:
                    id_map[int(aid)] = name
                except ValueError:
                    pass
    return id_map


def _load_raw_fields(raw_path: Path) -> dict[str, dict]:
    """Build {lower card name -> {Rarity, ManaCost}} from the Scryfall raw CSV."""
    result: dict[str, dict] = {}
    if not raw_path.exists():
        return result
    with raw_path.open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            name = row.get("name", "").strip()
            if not name:
                continue
            result[name.lower()] = {
                "Rarity":   row.get("rarity",    "").strip().lower(),
                "ManaCost": row.get("mana_cost", "").strip(),
            }
    return result


def load_enriched(csv_path: Path, raw_path: Path | None = None) -> dict[str, dict]:
    """Load enriched CSV keyed by lower-cased card name.

    If raw_path is provided, 'Rarity' and 'ManaCost' are merged in from the raw CSV.
    """
    raw_fields = _load_raw_fields(raw_path) if raw_path else {}
    db: dict[str, dict] = {}
    if not csv_path.exists():
        return db
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = row["Name"].strip().lower()
            if raw_fields:
                extra = raw_fields.get(key, {})
                row["Rarity"]   = extra.get("Rarity", "")
                row["ManaCost"] = extra.get("ManaCost", "")
            db[key] = row
    return db


def load_set_data(path: Path) -> tuple[dict[str, float], float]:
    """Parse set_data.txt → ({combo: win_rate_%}, mean_wr).

    2-color and 3-color combos are loaded; mean is taken over all loaded entries
    with >= 100 games.
    """
    result: dict[str, float] = {}
    if not path.exists():
        return result, 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.search(r'\(([WUBRG]+)\)[^\t]*\t[\d]+\t([\d]+)\t([\d.]+)%', line)
        if not m:
            continue
        combo = _sort_color(m.group(1))
        if len(combo) not in (2, 3):
            continue
        try:
            games = int(m.group(2))
            wr    = float(m.group(3))
        except ValueError:
            continue
        if games >= 100:
            result[combo] = wr
    mean = sum(result.values()) / len(result) if result else 0.0
    return result, mean


def compute_density_baseline(enriched_db: dict) -> tuple[dict, float]:
    """
    Build per-color, per-pick expected high-WR fraction, and the global
    threshold (mean GIH WR across all cards with valid data).

    Returns (baseline, threshold) where
      baseline[color][pick_1based] = expected fraction of cards with GIH WR >= threshold
      that are still present at that pick (ALSA >= pick).
    """
    gihs: list[float] = []
    for row in enriched_db.values():
        try:
            g = float((row.get("GIH WR", "") or "").rstrip("%"))
            gihs.append(g)
        except (ValueError, TypeError):
            pass
    threshold = sum(gihs) / len(gihs) if gihs else 54.0

    # data[color][pick] = [total_cards, high_wr_cards]
    data: dict[str, dict[int, list[int]]] = {c: {p: [0, 0] for p in range(1, 15)} for c in WUBRG}
    for row in enriched_db.values():
        color = _sort_color(row.get("Color", ""))
        if len(color) != 1 or color not in WUBRG:
            continue
        try:
            alsa = float(row.get("ALSA", "") or 0)
            gih  = float((row.get("GIH WR", "") or "").rstrip("%"))
        except (ValueError, TypeError):
            continue
        if alsa <= 0:
            continue
        high = gih >= threshold
        for pick in range(1, 15):
            if alsa >= pick:
                data[color][pick][0] += 1
                if high:
                    data[color][pick][1] += 1

    baseline: dict[str, dict[int, float]] = {}
    for color, picks in data.items():
        baseline[color] = {}
        for pick, (total, high) in picks.items():
            baseline[color][pick] = high / total if total > 0 else 0.0
    return baseline, threshold


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> str:
    """Return the first complete JSON object found in text."""
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            start = i
            break
    if start == -1:
        return "{}"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == '\\' and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return "{}"


def _parse_chunk(chunk: str) -> list[tuple[str, dict]]:
    """
    Return a list of (event_type, data_dict) tuples parsed from a log chunk.
    A single chunk may emit multiple events (e.g. set_start + notify together).

    Human Premier Draft:
      Draft.Notify                   -> "notify" {pack, pick, cards}
      EventPlayerDraftMakePick       -> "pick"   {pack, pick, card}

    Quick Draft (bot):
      <== BotDraftDraftStatus/Pick   -> "set_start" (if EventName present) + "notify"
      ==> BotDraftDraftPick          -> "pick"

    Both draft types:
      <== EventJoin(...)             -> "set_start" {set: "BLB", event: "..."}

    All pack/pick numbers are 0-indexed in the log.
    """
    # -- Draft set detection (EventJoin response) --
    if "EventJoin(" in chunk and "<== " in chunk:
        try:
            obj = json.loads(_extract_json(chunk))
            course = obj.get("Course", {})
            event_name = course.get("InternalEventName", "")
            set_code = _extract_set_code(event_name)
            if set_code:
                return [("set_start", {"set": set_code, "event": event_name})]
        except (ValueError, TypeError):
            pass

    # -- Pack presentation (human draft) --
    if "Draft.Notify" in chunk:
        try:
            obj = json.loads(_extract_json(chunk))
        except (ValueError, TypeError):
            return []
        if "PackCards" not in obj and "SelfPack" not in obj:
            return []
        raw = obj.get("PackCards", "")
        if isinstance(raw, str):
            cards = [int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]
        elif isinstance(raw, list):
            cards = [int(x) for x in raw if str(x).strip().lstrip("-").isdigit()]
        else:
            cards = []
        return [("notify", {
            "pack":  int(obj.get("SelfPack", 0)),
            "pick":  int(obj.get("SelfPick", 0)),
            "cards": cards,
        })]

    # -- Pack presentation (bot draft) --
    if ("BotDraftDraftStatus" in chunk or "BotDraftDraftPick" in chunk) and "<== " in chunk:
        try:
            obj = json.loads(_extract_json(chunk))
            payload_raw = obj.get("Payload", "")
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        except (ValueError, TypeError):
            return []
        if payload.get("DraftStatus") != "PickNext":
            return []
        events: list[tuple[str, dict]] = []
        # Emit set_start if EventName is present — handles log-rotation case where
        # EventJoin was never seen in the current log window
        event_name = payload.get("EventName", "")
        set_code = _extract_set_code(event_name) if event_name else None
        if set_code:
            events.append(("set_start", {"set": set_code, "event": event_name}))
        cards = [int(x) for x in payload.get("DraftPack", [])
                 if str(x).strip().lstrip("-").isdigit()]
        events.append(("notify", {
            "pack":  int(payload.get("PackNumber", 0)),
            "pick":  int(payload.get("PickNumber", 0)),
            "cards": cards,
        }))
        return events

    # -- Pick confirmation (human draft) --
    if "EventPlayerDraftMakePick" in chunk:
        if "<== EventPlayerDraftMakePick(" in chunk:
            return []
        try:
            obj = json.loads(_extract_json(chunk))
        except (ValueError, TypeError):
            return []
        request_str = obj.get("request")
        if request_str:
            try:
                pick_info = json.loads(request_str)
            except (ValueError, TypeError):
                pick_info = obj
        else:
            pick_info = obj.get("PickInfo", obj)
        card_id = pick_info.get("CardId") or ((pick_info.get("GrpIds") or [None])[0])
        if card_id is None:
            return []
        pack = pick_info.get("PackNumber") if pick_info.get("PackNumber") is not None \
               else pick_info.get("Pack", 0)
        pick = pick_info.get("PickNumber") if pick_info.get("PickNumber") is not None \
               else pick_info.get("Pick", 0)
        return [("pick", {"pack": int(pack), "pick": int(pick), "card": int(card_id)})]

    # -- Pick confirmation (bot draft) --
    if "BotDraftDraftPick" in chunk and "==> " in chunk:
        try:
            obj = json.loads(_extract_json(chunk))
            request_str = obj.get("request", "{}")
            pick_info = json.loads(request_str) if isinstance(request_str, str) else request_str
            inner = pick_info.get("PickInfo", pick_info)
        except (ValueError, TypeError):
            return []
        card_ids = inner.get("CardIds", [])
        card_id = card_ids[0] if card_ids else None
        if card_id is None:
            return []
        return [("pick", {
            "pack": int(inner.get("PackNumber", 0)),
            "pick": int(inner.get("PickNumber", 0)),
            "card": int(card_id),
        })]

    return []


class LogReader:
    """Incrementally reads new content from Player.log."""

    def __init__(self, path: Path):
        self.path = path
        self._offset = 0

    def poll(self) -> list[tuple[str, dict]]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self._offset:   # log was rotated / MTGA restarted
            self._offset = 0
        if size == self._offset:
            return []
        with self.path.open(encoding="utf-8", errors="replace") as fh:
            fh.seek(self._offset)
            new_text = fh.read()
            self._offset = fh.tell()
        events: list[tuple[str, dict]] = []
        parts = re.split(r"(\[UnityCrossThreadLogger\])", new_text)
        i = 1
        while i < len(parts):
            chunk = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
            events.extend(_parse_chunk(chunk))
            i += 2
        return events


# ---------------------------------------------------------------------------
# Draft state
# ---------------------------------------------------------------------------

class DraftState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.pack = 0
        self.pick = 0
        self.current_pack: list[int] = []
        self.picks: list[tuple[int, int, int]] = []  # (pack, pick, arena_id)
        self.all_notifies: list[tuple[int, int, list[int]]] = []  # (pack, pick, cards)

    def apply(self, event_type: str, data: dict):
        if event_type == "notify":
            self.pack = data["pack"]
            self.pick = data["pick"]
            self.current_pack = list(data["cards"])
            self.all_notifies.append((data["pack"], data["pick"], list(data["cards"])))
        elif event_type == "pick":
            self.picks.append((self.pack, self.pick, data["card"]))
            try:
                self.current_pack.remove(data["card"])
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _autosize_tree(tree: ttk.Treeview) -> None:
    """Resize each column to fit its widest header or cell value."""
    font = tkfont.nametofont("TkDefaultFont")
    pad = 16
    cols = tree["columns"]
    widths = {col: font.measure(str(col)) + pad for col in cols}
    for iid in tree.get_children():
        for col, val in zip(cols, tree.item(iid, "values")):
            w = font.measure(str(val)) + pad
            if w > widths[col]:
                widths[col] = w
    for col, w in widths.items():
        tree.column(col, width=w, minwidth=w)


def _fmt_best(val: str) -> str:
    """'WB:GIH=57.9%:OH=58.4%' -> 'WB: 57.9%'"""
    if not val:
        return ""
    parts = val.split(":")
    combo = parts[0]          # e.g. "WB" or "WB+"
    for p in parts[1:]:
        if p.startswith("GIH="):
            return f"{combo}: {p[4:]}"
    return val


def _gih_sort_key(row: dict) -> float:
    try:
        return float(row.get("GIH WR", "").rstrip("%"))
    except (ValueError, AttributeError):
        return -1.0


# Color-group order that approximates Arena's visual pack layout:
#   mono W, U, B, R, G → multicolor (2-color) → 3+ color → colorless/artifact → land
_COLOR_GROUP_ORDER = {c: i for i, c in enumerate(WUBRG)}  # W=0 U=1 B=2 R=3 G=4

_RARITY_ORDER = {"mythic": 0, "rare": 1, "uncommon": 2, "common": 3}

# Cards that occupy the dedicated land slot in a booster (basic lands + SOS slot lands).
# These are always shown last in Arena's pack display regardless of rarity.
_LAND_SLOT_NAMES = {
    # Basic lands
    "plains", "island", "swamp", "mountain", "forest",
    # SOS slot lands
    "fields of strife", "forum of amity", "paradox gardens",
    "spectacle summit", "titan's grave",
}

def _is_land_slot(r: dict) -> bool:
    return r.get("Name", "").strip().lower() in _LAND_SLOT_NAMES

_BASIC_LAND_NAMES: frozenset[str] = frozenset(
    {"Plains", "Island", "Swamp", "Mountain", "Forest"}
)

def _is_basic_land(r: dict) -> bool:
    """True for basic lands regardless of how the Type field is stored."""
    return (r.get("Name", "").strip() in _BASIC_LAND_NAMES
            or "Basic" in r.get("Type", ""))

def _arena_sort_key(r: dict) -> tuple:
    """Sort key matching Arena's visual card ordering within a pack.

    Primary:  rarity (mythic → rare → uncommon → common → land slot)
    Secondary: WUBRG color order (mono W first, … mono G last, then multi, then colorless)

    Land-slot cards (basics + dedicated slot lands) are always last.
    Non-slot lands (e.g. a rare dual that appears in a normal slot) sort by
    their actual rarity and color like any other card.
    """
    if _is_land_slot(r):
        return (99, 99, r.get("Name", ""))

    rarity_key = _RARITY_ORDER.get(r.get("Rarity", "").strip().lower(), 3)

    col = _sort_color(r.get("Color", ""))
    wubrg_pips = [c for c in col if c in WUBRG]
    n = len(wubrg_pips)
    if n == 0:
        color_key = 20   # colorless / artifact — after all colored cards
    elif n == 1:
        color_key = _COLOR_GROUP_ORDER[wubrg_pips[0]]   # 0-4
    elif n == 2:
        color_key = 5 + _COLOR_GROUP_ORDER[wubrg_pips[0]]
    else:
        color_key = 15

    return (rarity_key, color_key, r.get("Name", ""))


# ---------------------------------------------------------------------------
# Scoring functions (module-level so draft_sim.py can import them)
# ---------------------------------------------------------------------------

def count_color_depth(picks: list, lookup_fn) -> dict[str, float]:
    """Weighted pick counts per WUBRG color.

    Multicolor cards contribute 0.5 per color pip.  Late-pack picks (likely
    sideboard/forced) fade from full weight at pick 9 down to 0.25 at pick 14.
    Pick position is the second element of each (pack, pick, arena_id) tuple.
    """
    depth: dict[str, float] = {c: 0.0 for c in WUBRG}
    for _, pick_pos, aid in picks:
        r = lookup_fn(aid)
        col = [c for c in _sort_color(r.get("Color", "")) if c in WUBRG]
        if not col:
            continue
        pick_weight = max(0.25, 1.0 - max(0, pick_pos - 9) * 0.15)
        color_weight = 1.0 if len(col) == 1 else 0.5
        w = pick_weight * color_weight
        for c in col:
            depth[c] += w
    return depth


def count_pair_synergy(picks: list, lookup_fn) -> dict[str, float]:
    """Weighted count of bicolor picks per exact color pair (sorted WUBRG key).

    Used to detect pair commitment beyond what individual depths show:
    e.g. 5 R/W bicolor picks signal R/W commitment even when raw R depth is
    low due to denial.  Same late-pick fade as count_color_depth.
    """
    synergy: dict[str, float] = {}
    for _, pick_pos, aid in picks:
        r = lookup_fn(aid)
        col = [c for c in _sort_color(r.get("Color", "")) if c in WUBRG]
        if len(col) != 2:
            continue
        pick_weight = max(0.25, 1.0 - max(0, pick_pos - 9) * 0.15)
        key = "".join(col)  # already WUBRG-sorted from _sort_color
        synergy[key] = synergy.get(key, 0.0) + pick_weight
    return synergy


def curve_fit_score(r: dict, crea_curve: list[int], spell_curve: list[int]) -> float:
    """Score how well this card fills a curve gap (-1 to +2)."""
    try:
        cmc = min(7, int(float(r.get("CMC", "3") or "3")))
    except (ValueError, TypeError):
        cmc = 3
    if "Creature" in r.get("Type", ""):
        targets = [0.5, 1.0, 2.5, 2.5, 2.0, 1.5, 0.5, 0.5]
        gap = targets[cmc] - crea_curve[cmc]
        return max(-1.0, min(2.0, gap * 0.7))
    else:
        current = spell_curve[cmc]
        return max(-1.0, -0.5 * max(0, current - 1))


def land_modifier(r: dict, depth: dict[str, float], picks: list, lookup_fn) -> float:
    """Score modifier for dual/multicolor lands, range -3 to +2.

    Computes two independent bonuses and returns the higher:

    A. Color-group commitment bonus
       For each land color, count picked cards containing that color:
         mono card = 1 pt, bicolor card = 2 pts, 3+-color card = 1 pt.
       bonus_A = -3 + 5 * min(min_surplus / 8, 1)   → range [-3, +2]

    B. Bridge / splashable-bomb bonus
       -3 base, rises to +2 when a splashable bomb has been picked.
       Bomb criteria: GIH WR >= 60, OH WR < GIH WR - 2 (late-game finisher),
       at most 1 color outside played colors (depth >= 2),
       and the land covers the bomb's splash color.
    """
    if "Land" not in r.get("Type", ""):
        return 0.0
    land_colors = [c for c in _sort_color(r.get("Color", "")) if c in WUBRG]
    if len(land_colors) < 2:
        # Colorless fixing land: derive colors from Best1-4 pair names
        best_colors: set[str] = set()
        for key in ("Best1", "Best2", "Best3", "Best4"):
            val = r.get(key, "")
            if val:
                pair = _sort_color(val.split(":")[0].strip().rstrip("+"))
                best_colors.update(c for c in pair if c in WUBRG)
        land_colors = [c for c in WUBRG if c in best_colors]
        if len(land_colors) < 2:
            return 0.0

    # ---- Bonus A: color-group commitment ----
    surplus: dict[str, float] = {c: 0.0 for c in land_colors}
    for _, _, aid in picks:
        pr = lookup_fn(aid)
        card_colors = [c for c in _sort_color(pr.get("Color", "")) if c in WUBRG]
        n = len(card_colors)
        weight = 2.0 if n == 2 else 1.0
        for c in land_colors:
            if c in card_colors:
                surplus[c] += weight
    min_surplus = min(surplus.values())
    bonus_a = -3.0 + 5.0 * min(min_surplus / 8.0, 1.0)

    # ---- Bonus B: bridge / splashable-bomb ----
    played = {c for c in WUBRG if depth.get(c, 0.0) >= 2.0}
    land_set = set(land_colors)
    has_bomb = False
    for _, _, aid in picks:
        pr = lookup_fn(aid)
        try:
            gih = float((pr.get("GIH WR", "") or "").rstrip("%"))
        except (ValueError, TypeError):
            continue
        if gih < 60.0:
            continue
        try:
            oh = float((pr.get("OH WR", "") or "").rstrip("%"))
        except (ValueError, TypeError):
            continue
        if oh >= gih - 2.0:
            continue
        bomb_colors = {c for c in _sort_color(pr.get("Color", "")) if c in WUBRG}
        non_played = bomb_colors - played
        if len(non_played) > 1:
            continue
        # Also check mana cost: at most 1 pip outside played colors.
        # e.g. {2}{W}{B} in a WB deck has 0 off-color pips → fine.
        # {3}{W}{G} in a WB deck has 1 G pip → still splashable.
        # {2}{G}{G} in a WB deck has 2 G pips → not splashable.
        mana_cost = pr.get("ManaCost", "") or ""
        off_pip_count = sum(
            mana_cost.upper().count(f"{{{c}}}")
            for c in WUBRG if c not in played
        )
        if off_pip_count > 1:
            continue
        if non_played and not non_played.issubset(land_set):
            continue
        if not bomb_colors & land_set:
            continue
        has_bomb = True
        break
    bonus_b = 2.0 if has_bomb else -3.0

    return max(bonus_a, bonus_b)


def _adjusted_depth(raw_depth: dict[str, float]) -> dict[str, float]:
    """Return depth minus avg-of-3-lowest WUBRG depths, clamped to 0.

    Subtracting the baseline noise from incidental splashes makes committed
    colors stand out more clearly, especially mid-to-late draft.
    """
    vals = sorted(raw_depth.get(c, 0.0) for c in WUBRG)
    baseline = sum(vals[:3]) / 3.0
    return {c: max(0.0, raw_depth.get(c, 0.0) - baseline) for c in WUBRG}


def _archetype_lock_penalty(r: dict, depth: dict[str, float],
                            picks: list, lookup_fn) -> float:
    """Penalty for cards with few viable archetypes (-3 to 0).

    n_pools <= 2 → base -3.0
    n_pools == 3 → base -1.5
    n_pools >= 4 → 0.0 (no penalty)

    Multi-only card (≥3 plus entries, no bicolor, top 2 both +) → base -3.0;
    relief from pool breadth (n_extra committed non-pair colors / 2, capped 1).

    Regular card relief: matching_picks / 7 (capped 1.0).
      mono pick : counts if its color is in the card's best-pool color union.
      bicolor pick: counts only if BOTH colors are in that union.
      3+ color picks: ignored.
    """
    if "Land" in r.get("Type", ""):
        return 0.0

    bicolor_wrs: dict[str, float] = {}
    plus_wrs:    dict[str, float] = {}
    for key in ("Best1", "Best2", "Best3", "Best4"):
        val = r.get(key, "")
        if not val:
            continue
        parts    = val.split(":")
        raw_pair = parts[0].strip()
        is_plus  = raw_pair.endswith("+")
        pair     = _sort_color(raw_pair.rstrip("+"))
        if len([c for c in pair if c in WUBRG]) < 2:
            continue
        entry_key = pair + "+" if is_plus else pair
        for p in parts[1:]:
            if p.startswith("GIH="):
                try:
                    wr = float(p[4:].rstrip("%"))
                    (plus_wrs if is_plus else bicolor_wrs)[entry_key] = wr
                except (ValueError, TypeError):
                    pass

    all_wrs = {**bicolor_wrs, **plus_wrs}
    n_pools = len(all_wrs)
    if n_pools == 0:
        return 0.0

    sorted_all = sorted(all_wrs.items(), key=lambda x: x[1], reverse=True)
    top2_keys  = [k for k, _ in sorted_all[:2]]
    # Multi-only: no bicolor homes at all — needs 3+ colors regardless of n_pools
    is_multi_only = len(bicolor_wrs) == 0

    # Truly flexible: 4+ total pools AND at least some bicolor homes.
    # Exception: if the pool is committed to a pair the card doesn't fit,
    # the "flexible" exemption doesn't apply — apply a scaled penalty.
    if n_pools >= 4 and not is_multi_only:
        max_depth = max((depth.get(c, 0.0) for c in WUBRG), default=0.0)
        if max_depth >= 3:
            sorted_by_depth = sorted(WUBRG, key=lambda c: depth.get(c, 0.0), reverse=True)
            second_depth = depth.get(sorted_by_depth[1], 0.0)
            if second_depth >= 2:
                top_pair = frozenset(sorted_by_depth[:2])
                # Does any bicolor pool entry fit the committed pair?
                has_fit = any(
                    frozenset(c for c in ek if c in WUBRG).issubset(top_pair)
                    for ek in bicolor_wrs
                )
                if not has_fit:
                    # Off-archetype: penalty scales with commitment and color divergence
                    card_colors_set = frozenset(c for c in r.get("Color", "") if c in WUBRG)
                    overlap = (len(card_colors_set & top_pair) / max(len(card_colors_set), 1)
                               if card_colors_set else 1.0)
                    commit_strength = min(1.0, second_depth / 5.0)
                    return -2.0 * (1.0 - overlap) * commit_strength
        return 0.0

    base = -3.0 if (is_multi_only or n_pools <= 2) else -1.5

    # Union of all colors that appear in any best-pool entry
    pool_color_set: set[str] = set()
    for entry_key in all_wrs:
        for c in entry_key.rstrip("+"):
            if c in WUBRG:
                pool_color_set.add(c)

    if is_multi_only:
        # Relief: breadth of committed non-pair colors in the pool
        best_pair_str  = sorted_all[0][0].rstrip("+")
        best_pair_cols = [c for c in best_pair_str if c in WUBRG]
        max_raw = max((depth.get(c, 0.0) for c in WUBRG), default=0.0)
        breadth_threshold = max(0.5, max_raw * 0.15)
        n_extra = sum(1 for c in WUBRG
                      if c not in best_pair_cols
                      and depth.get(c, 0.0) >= breadth_threshold)
        relief = min(1.0, n_extra / 2.0)   # full relief at 2 committed extra colors
    else:
        # Relief: count picks whose colors are within the card's pool color union
        matching = 0.0
        if picks and lookup_fn:
            for _, _, aid in picks:
                pr = lookup_fn(aid)
                card_colors = [c for c in _sort_color(pr.get("Color", "")) if c in WUBRG]
                n = len(card_colors)
                if n == 1:
                    if card_colors[0] in pool_color_set:
                        matching += 1.0
                elif n == 2:
                    if all(c in pool_color_set for c in card_colors):
                        matching += 1.0
                # 3+ color picks: ignored
        relief = min(1.0, matching / 7.0)

    return base * (1.0 - relief)


def _pool_adjusted_gih(r: dict, base_gih: float, depth: dict[str, float]) -> float:
    """Blend base GIH toward the card's best matching pool entry as the pool commits.

    A pool entry matches when all colors in the pair are committed (depth > 0).
    Card pip-colors must also be committed (multicolor cards only get blended
    when the pool actually supports all their colors).

    blend = min(pair_min_depth, 4.0) / 4.0  — bottlenecked by the weaker color
    in the matching pair.  This prevents over-blending when only one of the pair's
    colors is deeply committed (e.g. R:2 in a W:14 pool yields only 50% blend
    toward WR, not full blend).
    effective_gih = base_gih + blend × (best_matching_wr - base_gih)
    """
    card_colors = [c for c in _sort_color(r.get("Color", "")) if c in WUBRG]
    committed_colors = {c for c in WUBRG if depth.get(c, 0.0) > 0.0}

    # Card pip-color check: all card colors must be in pool (skip for colorless)
    if card_colors and not all(c in committed_colors for c in card_colors):
        return base_gih

    # Find the highest GIH among Best entries whose pair is ⊆ committed_colors
    best_pool_wr: float | None = None
    best_pair_cols: list[str] = []
    for key in ("Best1", "Best2", "Best3", "Best4"):
        val = r.get(key, "")
        if not val:
            continue
        parts    = val.split(":")
        raw_pair = parts[0].strip().rstrip("+")
        pair_colors = [c for c in _sort_color(raw_pair) if c in WUBRG]
        if len(pair_colors) < 2:
            continue
        if not all(c in committed_colors for c in pair_colors):
            continue
        for p in parts[1:]:
            if p.startswith("GIH="):
                try:
                    wr = float(p[4:].rstrip("%"))
                    if best_pool_wr is None or wr > best_pool_wr:
                        best_pool_wr = wr
                        best_pair_cols = pair_colors
                except (ValueError, TypeError):
                    pass

    if best_pool_wr is None:
        return base_gih

    # Blend is limited by the *weakest* committed color in the matching pair.
    # This prevents over-blending when only one of the pair's colors is deeply
    # committed (e.g. R:2 in a W:14 pool should not get full WR blend).
    pair_min_depth = min(depth.get(c, 0.0) for c in best_pair_cols) if best_pair_cols else 0.0
    blend = min(1.0, pair_min_depth / 4.0)
    return base_gih + blend * (best_pool_wr - base_gih)


def _fit_pair_multiplier(r: dict, base_gih: float,
                         depth: dict[str, float]) -> tuple[float, str]:
    """Fit multiplier in [0, 1]: how well does our pool match the card's best archetype?

    Caller multiplies by 3.0 to get a 0–3 fit contribution.

    Spread factor (determines the floor for uncommitted drafters):
      • If card has ≥2 bicolor (non-+) entries: use bicolor spread, scale /3 %.
        Low spread = generically good in all 2-color contexts → high floor.
      • Otherwise: floor = 0. Cards with only + entries, or only one bicolor entry,
        are archetype-specific and score low until the pool commits to their colors.

    Depth normalization:
      Uses raw depth / max(4.0, max_raw) so 3-color and 5-color pools don't
      collapse to zero (fixes the adjusted_depth problem in balanced multicolor decks).

    For + best-pair (card wants 3+ colors):
      our_commit = pair_depth × (0.5 + 0.5 × breadth_factor)
      breadth_threshold = max(0.5, max_raw × 0.15)
      A pure-pair drafter gets half credit; adding ≥1 extra color gives full credit.
    """
    raw = depth
    max_raw = max((raw.get(c, 0.0) for c in WUBRG), default=0.0)
    scale   = max(4.0, max_raw)   # ≥4 suppresses early-draft noise

    card_colors = [c for c in _sort_color(r.get("Color", "")) if c in WUBRG]

    # Parse Best1-4, keeping bicolor and + entries separate
    bicolor_wrs: dict[str, float] = {}
    plus_wrs:    dict[str, float] = {}
    for key in ("Best1", "Best2", "Best3", "Best4"):
        val = r.get(key, "")
        if not val:
            continue
        parts    = val.split(":")
        raw_pair = parts[0].strip()
        is_plus  = raw_pair.endswith("+")
        pair     = _sort_color(raw_pair.rstrip("+"))
        if len([c for c in pair if c in WUBRG]) < 2:
            continue
        entry_key = pair + "+" if is_plus else pair
        for p in parts[1:]:
            if p.startswith("GIH="):
                try:
                    wr = float(p[4:].rstrip("%"))
                    (plus_wrs if is_plus else bicolor_wrs)[entry_key] = wr
                except (ValueError, TypeError):
                    pass

    all_wrs = {**bicolor_wrs, **plus_wrs}

    # Pool uplift: two sources, take the max.
    #
    # 1. Matched uplift — player is already committed to one of the card's bicolor
    #    archetypes.  Uses that archetype's WR directly (no n_qual requirement).
    #    Rewards drafting into your lane regardless of how niche the card is.
    committed_colors = {c for c in WUBRG if raw.get(c, 0.0) > 0.0}
    matched_uplift = 0.0
    for pair_key, wr in sorted(bicolor_wrs.items(), key=lambda x: x[1], reverse=True):
        pair_colors = {c for c in pair_key if c in WUBRG}
        if pair_colors.issubset(committed_colors):
            matched_uplift = min(5.0, max(0.0, (wr - base_gih) * 2.0))
            break   # best matching bicolor entry wins

    # 2. General floor — card is good in ≥2 pools so it's worth taking even
    #    before committing.  Requires n_qual ≥ 2; 3+ pools get a larger floor.
    bi_sorted = sorted(bicolor_wrs.values(), reverse=True)
    plus_vals = list(plus_wrs.values())
    avg_plus = sum(plus_vals) / len(plus_vals) if plus_vals else None
    pool_floor = base_gih - 0.5
    qualifying = [wr for wr in bi_sorted if wr >= pool_floor]
    if avg_plus is not None and avg_plus >= pool_floor:
        qualifying.append(avg_plus)
    n_qual = len(qualifying)
    if n_qual >= 2:
        avg_qual = sum(qualifying) / n_qual
        min_floor = 1.0 if n_qual >= 3 else 0.5
        general_uplift = min(5.0, max(min_floor, (avg_qual - base_gih) * 2.0))
    else:
        general_uplift = 0.0

    pool_uplift = max(matched_uplift, general_uplift)

    # Case A / B: no pair WR data
    if not all_wrs:
        if not card_colors:
            return 1.0, 1.5, ""   # colorless, no data: default uplift
        commit = min(1.0, min(raw.get(c, 0.0) for c in card_colors) / scale)
        return commit, 1.5, ",".join(card_colors)

    # Case C: archetype-based — find best entry across all pools
    sorted_all = sorted(all_wrs.items(), key=lambda x: x[1], reverse=True)
    best_key, best_wr = sorted_all[0]
    is_best_plus     = best_key.endswith("+")
    best_pair        = best_key.rstrip("+")
    best_pair_colors = [c for c in best_pair if c in WUBRG]

    # Spread factor: uses bicolor spread when available (more reliable).
    # Divisor 2.0 %: at 2 %+ spread the floor collapses to 0 (archetype-specific).
    if len(bicolor_wrs) >= 2:
        spread_factor = min(1.0, (bi_sorted[0] - bi_sorted[1]) / 2.0)
    else:
        spread_factor = 1.0   # <2 bicolor entries → archetype-specific, no generic floor

    # Depth commitment: how deep are we in the card's best pair?
    pair_depth_commit = (
        min(1.0, min(raw.get(c, 0.0) for c in best_pair_colors) / scale)
        if best_pair_colors else 0.0
    )

    if is_best_plus:
        # + card needs breadth beyond the pair colors.
        # Half credit for the pair alone; full credit with ≥1 extra committed color.
        breadth_threshold = max(0.5, max_raw * 0.15)
        n_extra = sum(1 for c in WUBRG
                      if c not in best_pair_colors
                      and raw.get(c, 0.0) >= breadth_threshold)
        our_commit = pair_depth_commit * (0.5 + 0.5 * min(1.0, float(n_extra)))
    else:
        our_commit = pair_depth_commit

    wr_diff = best_wr - base_gih
    if wr_diff >= 0:
        bonus = min(0.15, wr_diff * 0.05)   # small upside, cap at ×1.15
    else:
        bonus = max(-1.0, wr_diff / 3.0)    # ×0 at −3 %

    # + cards need 3+ colors: no floor for purely-bicolor drafters.
    # Non-+ cards: floor = 1 − spread_factor (generic cards stay playable everywhere).
    # Exception: if a + card also has low-spread bicolor entries that qualify,
    # those archetypes are still real homes → give half the bicolor floor.
    if is_best_plus:
        if len(bi_sorted) >= 2:
            bi_floor = max(0.0, 1.0 - spread_factor) * 0.5
        else:
            bi_floor = 0.0
        not_committed_m = bi_floor
    else:
        not_committed_m = max(0.0, 1.0 - spread_factor)

    # Pool pressure: as the pool grows and diverges from this card's colors,
    # the floor decays. Empty pool → no pressure (pick 1 stays high).
    # overlap = fraction of total depth in the card's colors (0 = fully off-color).
    total_depth     = sum(raw.get(c, 0.0) for c in WUBRG)
    if total_depth > 0.0 and card_colors:
        card_color_depth = sum(raw.get(c, 0.0) for c in card_colors)
        overlap          = card_color_depth / total_depth
        pressure         = total_depth / (total_depth + 4.0)   # sigmoid, softens at low picks
        pool_lock_penalty = pressure * (1.0 - overlap)
        not_committed_m   = not_committed_m * (1.0 - pool_lock_penalty)

    committed_m = min(1.0, max(0.0, 1.0 + bonus))
    multiplier  = not_committed_m + (committed_m - not_committed_m) * our_commit

    return multiplier, pool_uplift, f"{best_key}@{our_commit:.1f}"


def _card_openness(r: dict, raw_gih: float, lane_fn) -> float:
    """Card-aware openness: weighted average of lane signals for arches where card overperforms.

    Only considers bicolor/plus lanes where the card's WR exceeds raw_gih.
    Lanes where the card performs below its base GIH are excluded — they offer
    no genuine benefit even if that lane is open for the format.
    Weight per lane = excess WR over base GIH (higher-performing lanes contribute more).
    """
    lane_excess: dict[str, float] = {}
    for key in ("Best1", "Best2", "Best3", "Best4"):
        val = r.get(key, "")
        if not val:
            continue
        parts = val.split(":")
        raw_pair = parts[0].strip()
        is_plus  = raw_pair.endswith("+")
        pair     = _sort_color(raw_pair.rstrip("+"))
        if len([c for c in pair if c in WUBRG]) < 2:
            continue
        lane_key = pair if (not is_plus and pair in LANE_PAIRS) else "MULTI"
        for p in parts[1:]:
            if p.startswith("GIH="):
                try:
                    wr     = float(p[4:].rstrip("%"))
                    excess = wr - raw_gih
                    if excess > 0:
                        lane_excess[lane_key] = max(lane_excess.get(lane_key, 0.0), excess)
                except (ValueError, TypeError):
                    pass
    if not lane_excess:
        return 0.0
    total_w = sum(lane_excess.values())
    if total_w <= 0:
        return 0.0
    return sum(lane_fn(k) * w for k, w in lane_excess.items()) / total_w


def _score_log_line(r: dict, depth: dict[str, float], total_picks: int,
                    crea_curve: list[int], spell_curve: list[int],
                    set_mean: float, picked_names: list[str],
                    openness_fn=None, pair_synergy=None,
                    picks=None, lookup_fn=None, lane_fn=None) -> str:
    """Return a one-line breakdown string mirroring card_score's components."""
    try:
        gih_raw = float((r.get("GIH WR", "") or "").rstrip("%"))
    except (ValueError, TypeError):
        gih_raw = set_mean
    gih = _pool_adjusted_gih(r, gih_raw, depth)

    wubrg_colors = [c for c in _sort_color(r.get("Color", "")) if c in WUBRG]
    n_pip = len(wubrg_colors)
    multi_min_depth = 0.0
    if n_pip >= 2:
        multi_min_depth = min(depth.get(c, 0.0) for c in wubrg_colors)

    # Fit: avg top-2 bicolor pool uplift × archetype-alignment multiplier
    fit_mul, pool_uplift, fit_tag = _fit_pair_multiplier(r, gih_raw, depth)
    fit = pool_uplift * fit_mul

    openness_weight = max(0.0, 1.0 - max(0, total_picks - 2) / 12.0)
    if lane_fn is not None and n_pip > 0 and openness_weight > 0:
        # Card-aware: only lanes where this card's WR > raw_gih contribute
        open_contrib = _card_openness(r, gih_raw, lane_fn) * openness_weight
    elif openness_fn is not None and n_pip > 0 and openness_weight > 0:
        # Legacy per-color path (used by tests)
        if n_pip == 1:
            openness = openness_fn(wubrg_colors[0])
        else:
            commit = min(1.0, multi_min_depth / 2.0)
            openness = sum(openness_fn(c) for c in wubrg_colors) / n_pip * commit
        open_contrib = openness * openness_weight
    else:
        open_contrib = 0.0

    # 4. Curve multiplier: boosts fit+openness only (up to +30%).
    #    Cards with no fit/openness contribution gain nothing from curve.
    curve_weight = max(0.0, min(1.0, (total_picks - 10) / 14.0))
    curve_val    = max(0.0, curve_fit_score(r, crea_curve, spell_curve))
    curve_mult   = curve_val * curve_weight * 0.15

    fix = (land_modifier(r, depth, picks, lookup_fn)
           if picks is not None and lookup_fn is not None else 0.0)
    lock_penalty = (_archetype_lock_penalty(r, depth, picks, lookup_fn)
                    if picks is not None and lookup_fn is not None else 0.0)

    bonus = fit + open_contrib
    total = gih + bonus * (1.0 + curve_mult) + fix + lock_penalty
    name = r.get("Name", "?")[:28]
    gih_label = f"GIH={gih_raw:.1f}"
    if abs(gih - gih_raw) >= 0.05:
        gih_label += f"→{gih:.1f}"
    parts = [gih_label]
    if abs(fit) >= 0.05 or abs(fit_mul) >= 0.05:
        parts.append(f"fit={fit:+.1f}(x{fit_mul:.2f})")
    if abs(open_contrib) >= 0.05:
        parts.append(f"open={open_contrib:+.2f}")
    if curve_mult >= 0.01:
        parts.append(f"curve=x{1.0+curve_mult:.2f}")
    if abs(fix) >= 0.05:
        parts.append(f"fix={fix:+.2f}")
    if abs(lock_penalty) >= 0.05:
        parts.append(f"lock={lock_penalty:+.1f}")
    return f"{total:5.1f}  {name:<28}  {'  '.join(parts)}"


def card_score(r: dict, depth: dict[str, float], total_picks: int,
               crea_curve: list[int], spell_curve: list[int],
               set_mean: float, picked_names: list[str],
               openness_fn=None, pair_synergy=None,
               picks=None, lookup_fn=None, lane_fn=None) -> float:
    """Contextual score: GIH WR adjusted for fit, openness, curve, duplicates.

    lane_fn: callable(lane_key: str) -> float — card-aware openness using per-lane
        signals; only lanes where the card overperforms its base GIH contribute.
        Takes precedence over openness_fn when both are provided.
    openness_fn: callable(color: str) -> float — legacy per-color openness (used by tests).
    picked_names: list of already-picked card names (lower-cased) for dup penalty.
    """
    # 1. Base winrate (blended toward best matching pool entry as pool commits)
    try:
        gih = float((r.get("GIH WR", "") or "").rstrip("%"))
    except (ValueError, TypeError):
        gih = set_mean
    raw_gih = gih
    gih = _pool_adjusted_gih(r, raw_gih, depth)

    # 2. Fit: avg top-2 bicolor pool uplift × archetype-alignment multiplier (0–1)
    wubrg_colors = [c for c in _sort_color(r.get("Color", "")) if c in WUBRG]
    n_pip = len(wubrg_colors)
    multi_min_depth = 0.0
    if n_pip >= 2:
        multi_min_depth = min(depth.get(c, 0.0) for c in wubrg_colors)

    fit_mul, pool_uplift, _ = _fit_pair_multiplier(r, raw_gih, depth)
    fit = pool_uplift * fit_mul

    # 3. Openness (fades to 0 by pick 14)
    openness_contrib = 0.0
    openness_weight = max(0.0, 1.0 - max(0, total_picks - 2) / 12.0)
    if lane_fn is not None and n_pip > 0 and openness_weight > 0:
        # Card-aware: only lanes where this card's WR > raw_gih contribute
        openness_contrib = _card_openness(r, raw_gih, lane_fn) * openness_weight
    elif openness_fn is not None and n_pip > 0 and openness_weight > 0:
        # Legacy per-color path (used by tests)
        if n_pip == 1:
            openness = openness_fn(wubrg_colors[0])
        else:
            commit = min(1.0, multi_min_depth / 2.0)
            openness = sum(openness_fn(c) for c in wubrg_colors) / n_pip * commit
        openness_contrib = openness * openness_weight

    # 4. Curve multiplier: boosts fit+openness only (up to +30%).
    #    Cards with no fit/openness contribution gain nothing from curve.
    curve_weight = max(0.0, min(1.0, (total_picks - 10) / 14.0))
    curve_val    = max(0.0, curve_fit_score(r, crea_curve, spell_curve))
    curve_mult   = curve_val * curve_weight * 0.15

    # 5. Land modifier (-3 to +2 for dual lands)
    fix = (land_modifier(r, depth, picks, lookup_fn)
           if picks is not None and lookup_fn is not None else 0.0)

    # 6. Archetype lock penalty (-3 to 0, fades as pool commits to card's archetypes)
    lock_penalty = (_archetype_lock_penalty(r, depth, picks, lookup_fn)
                    if picks is not None and lookup_fn is not None else 0.0)

    bonus = fit + openness_contrib
    return gih + bonus * (1.0 + curve_mult) + fix + lock_penalty


# ---------------------------------------------------------------------------
# Parser unit tests  (replaces test_parser.py)
# ---------------------------------------------------------------------------

_TEST_CASES = [
    (
        "notify: basic pack 0 pick 0",
        '[UnityCrossThreadLogger]Draft.Notify\n'
        '{"SelfPack": 0, "SelfPick": 0, "PackCards": "12345,67890,11111", "draftId": "draft-abc"}',
        "notify", {"pack": 0, "pick": 0, "cards": [12345, 67890, 11111]},
    ),
    (
        "notify: pack 1 pick 5",
        '[UnityCrossThreadLogger]Draft.Notify\n'
        '{"SelfPack": 1, "SelfPick": 5, "PackCards": "99999,88888", "draftId": "draft-456"}',
        "notify", {"pack": 1, "pick": 5, "cards": [99999, 88888]},
    ),
    (
        "notify: PackCards as JSON array",
        '[UnityCrossThreadLogger]Draft.Notify\n'
        '{"SelfPack": 2, "SelfPick": 3, "PackCards": [55555, 44444]}',
        "notify", {"pack": 2, "pick": 3, "cards": [55555, 44444]},
    ),
    (
        "pick: PickInfo plain format",
        '[UnityCrossThreadLogger]EventPlayerDraftMakePick\n'
        '{"PickInfo": {"CardId": 12345, "PackNumber": 0, "PickNumber": 0}}',
        "pick", {"pack": 0, "pick": 0, "card": 12345},
    ),
    (
        "pick: PickInfo with timestamp header",
        '[UnityCrossThreadLogger]2/25/2026 12:00:00 PM EventPlayerDraftMakePick\n'
        '{"PickInfo": {"CardId": 77777, "PackNumber": 0, "PickNumber": 1}}',
        "pick", {"pack": 0, "pick": 1, "card": 77777},
    ),
    (
        "pick: outbound request format with GrpIds/Pack/Pick",
        '[UnityCrossThreadLogger]==> EventPlayerDraftMakePick\n'
        '{"id": "b0114c5d", "request": "{\\"DraftId\\":\\"0784e646\\",\\"GrpIds\\":[100486],\\"Pack\\":1,\\"Pick\\":2}"}',
        "pick", {"pack": 1, "pick": 2, "card": 100486},
    ),
    (
        "ignore: <== EventPlayerDraftMakePick success response",
        '[UnityCrossThreadLogger]3/11/2026 9:44:16 PM\n'
        '<== EventPlayerDraftMakePick(b0114c5d-0462-4855-a7ab-d06ede720f93)\n'
        '{"IsPickSuccessful":true}',
        None, None,
    ),
    (
        "ignore: PickInfo missing CardId must not produce a pick",
        '[UnityCrossThreadLogger]EventPlayerDraftMakePick\n'
        '{"PickInfo": {"PackNumber": 0, "PickNumber": 0}}',
        None, None,
    ),
    (
        "bot notify: BotDraftDraftStatus PickNext",
        '[UnityCrossThreadLogger]2/25/2026 12:00:00 PM\n'
        '<== BotDraftDraftStatus(uuid)\n'
        '{"Payload": "{\\"DraftStatus\\":\\"PickNext\\",\\"PackNumber\\":0,\\"PickNumber\\":0,\\"DraftPack\\":[\\"12345\\",\\"67890\\",\\"11111\\"]}"}',
        "notify", {"pack": 0, "pick": 0, "cards": [12345, 67890, 11111]},
    ),
    (
        "bot notify: BotDraftDraftPick response PickNext (subsequent packs)",
        '[UnityCrossThreadLogger]2/25/2026 12:00:00 PM\n'
        '<== BotDraftDraftPick(uuid)\n'
        '{"Payload": "{\\"DraftStatus\\":\\"PickNext\\",\\"PackNumber\\":0,\\"PickNumber\\":1,\\"DraftPack\\":[\\"67890\\",\\"11111\\"]}"}',
        "notify", {"pack": 0, "pick": 1, "cards": [67890, 11111]},
    ),
    (
        "bot ignore: BotDraftDraftStatus non-PickNext status",
        '[UnityCrossThreadLogger]<== BotDraftDraftStatus(uuid)\n'
        '{"Payload": "{\\"DraftStatus\\":\\"Complete\\",\\"PackNumber\\":0,\\"PickNumber\\":0}"}',
        None, None,
    ),
]


def run_tests() -> int:
    passed = failed = 0
    for desc, chunk, exp_kind, exp_data in _TEST_CASES:
        kind, data = _parse_chunk(chunk)
        ok = (kind == exp_kind) and (exp_data is None or data == exp_data)
        if ok:
            passed += 1
            print(f"pass  {desc}")
        else:
            failed += 1
            print(f"FAIL  {desc}")
            print(f"      expected: kind={exp_kind!r}  data={exp_data}")
            print(f"      got:      kind={kind!r}  data={data}")
    print(f"\n{passed}/{passed + failed} tests passed",
          "OK" if not failed else "-- FAILURES ABOVE")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Card Browser tab  (replaces card_viewer.py)
# ---------------------------------------------------------------------------

class CardBrowserFrame(ttk.Frame):
    def __init__(self, parent, csv_path: Path, on_go_draft=None):
        super().__init__(parent)
        self._csv_path   = csv_path
        self._on_go_draft = on_go_draft
        self.all_cards: list[dict] = []
        self.filtered:  list[dict] = []
        self._build()
        self._load()

    def _build(self):
        # ---- Left panel: search + list ----
        left = ttk.Frame(self, padding=(8, 8, 4, 8))
        left.pack(side=tk.LEFT, fill=tk.Y)

        ttk.Label(left, text="Search card name:").pack(anchor="w")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_search)
        ttk.Entry(left, textvariable=self._search_var, width=32).pack(fill=tk.X, pady=(2, 4))

        if self._on_go_draft:
            ttk.Button(
                left, text="Go to Draft Advisor",
                command=self._on_go_draft,
            ).pack(fill=tk.X, pady=(0, 6))

        self._17l_aid = ""
        self._17l_btn = ttk.Button(
            left, text="View on 17lands",
            command=self._open_17lands, state=tk.DISABLED,
        )
        self._17l_btn.pack(fill=tk.X, pady=(0, 6))

        self._count_label = ttk.Label(left, text="", foreground="gray")
        self._count_label.pack(anchor="w", pady=(0, 4))

        list_frame = ttk.Frame(left)
        list_frame.pack(fill=tk.BOTH, expand=True)

        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self._listbox = tk.Listbox(
            list_frame,
            selectmode=tk.SINGLE,
            width=34,
            yscrollcommand=sb.set,
            font=("Segoe UI", 9),
            activestyle="dotbox",
            exportselection=False,
        )
        sb.config(command=self._listbox.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox.pack(fill=tk.BOTH, expand=True)
        self._listbox.bind("<<ListboxSelect>>", self._on_select)

        # ---- Vertical separator ----
        ttk.Separator(self, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=2)

        # ---- Right panel: card details ----
        right = ttk.Frame(self, padding=(12, 8, 8, 8))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._value_vars: dict[str, tk.StringVar] = {}
        row = 0
        for field, label in FIELDS:
            var = tk.StringVar()
            self._value_vars[field] = var

            if field == "Best1":
                ttk.Separator(right, orient=tk.HORIZONTAL).grid(
                    row=row, column=0, columnspan=2, sticky="ew", pady=(6, 4))
                row += 1
                ttk.Label(
                    right, text="17lands top win rates by colour pair:",
                    font=("Segoe UI", 9, "italic"), foreground="gray",
                ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 4))
                row += 1

            ttk.Label(
                right, text=label + ":",
                font=("Segoe UI", 10, "bold"),
                width=12, anchor="e",
            ).grid(row=row, column=0, sticky="ne", padx=(0, 10), pady=2)

            ttk.Label(
                right, textvariable=var,
                font=("Segoe UI", 10),
                anchor="w", wraplength=520, justify="left",
            ).grid(row=row, column=1, sticky="nw", pady=2)
            row += 1

        right.columnconfigure(1, weight=1)

    def _load(self):
        self.all_cards = load_cards(self._csv_path)
        self._refresh_list(self.all_cards)

    def _refresh_list(self, cards: list[dict]):
        self.filtered = cards
        self._listbox.delete(0, tk.END)
        for c in cards:
            self._listbox.insert(tk.END, c["Name"])
        self._count_label.config(text=f"{len(cards)} card(s)")
        if len(cards) == 1:
            self._listbox.selection_set(0)
            self._on_select()

    def _on_search(self, *_):
        q = self._search_var.get().strip().lower()
        self._refresh_list(
            self.all_cards if not q
            else [c for c in self.all_cards if q in c["Name"].lower()]
        )

    def _on_select(self, _event=None):
        sel = self._listbox.curselection()
        if not sel:
            return
        card = self.filtered[sel[0]]
        for field, _ in FIELDS:
            val = card.get(field, "")
            pro_key = PRO_FIELDS.get(field)
            if pro_key:
                pro_val = card.get(pro_key, "")
                if pro_val:
                    val = f"{val}  (Pro: {pro_val})" if val else f"(Pro: {pro_val})"
            self._value_vars[field].set(val)
        aid = card.get("ArenaId", "").strip()
        self._17l_btn.config(state=tk.NORMAL if aid else tk.DISABLED)
        self._17l_aid = aid

    def _open_17lands(self):
        if self._17l_aid:
            set_code = self._csv_path.parent.name
            webbrowser.open(
                f"https://www.17lands.com/card_data/details"
                f"?card_id={self._17l_aid}&expansion={set_code}&format=PremierDraft"
            )


# ---------------------------------------------------------------------------
# Draft Advisor tab  (replaces draft_advisor.py)
# ---------------------------------------------------------------------------

class DraftAdvisorFrame(ttk.Frame):
    def __init__(self, parent, csv_path: Path, log_path: Path = LOG_PATH):
        super().__init__(parent)
        self._current_set = DEFAULT_SET
        self._current_set = DEFAULT_SET
        self._csv_path   = csv_path
        raw_csv = csv_path.parent / "carddata_raw.csv"
        self._arena  = load_arena_ids(raw_csv)
        self._cards  = load_enriched(csv_path, raw_csv)
        self._density_baseline, self._density_threshold = compute_density_baseline(self._cards)
        self._set_pair_wr, self._set_pair_mean = load_set_data(SET_DATA)
        self._state  = DraftState()
        self._reader = LogReader(log_path)
        self._sig_sum: dict[str, float] = {}
        self._sig_cnt: dict[str, float] = {}  # weighted count
        self._prs_cnt: dict[str, int]   = {}  # pressure: absence hit counts per color (unweighted)
        self._dns_sum: dict[str, float] = {}  # density: observed-expected high-WR fraction sum
        self._dns_cnt: dict[str, float] = {}  # density: weighted count per color
        self._lane_sum: dict[str, float] = {}  # lane openness: weighted delta sum per LANE_KEY
        self._lane_cnt: dict[str, float] = {}  # lane openness: weighted count per LANE_KEY
        self._sig_n   = 0   # number of all_notifies entries already processed
        self._build()
        self._poll()

    def _build(self):
        # ---- Top status bar ----
        top = ttk.Frame(self, padding=(10, 6))
        top.pack(fill=tk.X)

        self._status_var = tk.StringVar(value="Waiting for draft...")
        ttk.Label(
            top, textvariable=self._status_var,
            font=("Segoe UI", 11, "bold"),
        ).pack(side=tk.LEFT)

        self._log_var = tk.StringVar(value="")
        ttk.Label(
            top, textvariable=self._log_var,
            font=("Segoe UI", 9), foreground="gray",
        ).pack(side=tk.LEFT, padx=12)

        ttk.Button(top, text="Load test situation",
                   command=self._load_test).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="Watch sim log",
                   command=self._watch_sim).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="Reset draft",
                   command=self._reset).pack(side=tk.RIGHT, padx=4)
        self._overlay_btn = ttk.Button(top, text="Open Overlay",
                                        command=self._toggle_overlay)
        self._overlay_btn.pack(side=tk.RIGHT, padx=4)
        self._17l_pack_btn = ttk.Button(top, text="View on 17lands",
                   command=self._open_pack_17lands, state=tk.DISABLED)
        self._17l_pack_btn.pack(side=tk.RIGHT, padx=4)

        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X)

        # ---- Paned area ----
        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        left_wrap = ttk.Frame(panes)
        panes.add(left_wrap, weight=3)

        left_panes = ttk.PanedWindow(left_wrap, orient=tk.VERTICAL)
        left_panes.pack(fill=tk.BOTH, expand=True)

        tree_wrap = ttk.Frame(left_panes)
        left_panes.add(tree_wrap, weight=3)
        ttk.Label(
            tree_wrap, text="Current Pack  \u2014  Arena order  (score log below is sorted by score)",
            font=("Segoe UI", 9, "italic"), foreground="gray",
        ).pack(anchor="w")
        self._pack_tree = self._make_tree(
            tree_wrap,
            columns=("Score", "Name", "Color", "In Hand", "Opening", "Best 1", "Best 2", "Best 3", "Best 4"),
            widths={"Score": 54, "Name": 190, "Color": 46, "In Hand": 90, "Opening": 90,
                    "Best 1": 110, "Best 2": 110, "Best 3": 110, "Best 4": 110},
        )
        self._pack_tree.tag_configure("top1", background="#2e7d32", foreground="white")
        self._pack_tree.tag_configure("top2", background="#388e3c", foreground="white")
        self._pack_tree.tag_configure("top3", background="#66bb6a", foreground="black")
        self._pack_tree.bind("<<TreeviewSelect>>", self._on_pack_tree_select)

        log_wrap = ttk.Frame(left_panes)
        left_panes.add(log_wrap, weight=1)
        ttk.Label(
            log_wrap, text="Score breakdown",
            font=("Segoe UI", 9, "italic"), foreground="gray",
        ).pack(anchor="w")
        log_inner = ttk.Frame(log_wrap)
        log_inner.pack(fill=tk.BOTH, expand=True)
        log_sb = ttk.Scrollbar(log_inner, orient=tk.VERTICAL)
        self._score_log = tk.Text(
            log_inner, height=6, wrap=tk.NONE, state=tk.DISABLED,
            font=("Consolas", 8), yscrollcommand=log_sb.set,
        )
        log_sb.config(command=self._score_log.yview)
        log_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._score_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        right_wrap = ttk.Frame(panes)
        panes.add(right_wrap, weight=0)
        ttk.Label(
            right_wrap, text="My Picks  \u2014  mana curve",
            font=("Segoe UI", 9, "italic"), foreground="gray",
        ).pack(anchor="w")
        CMC_COLS = ("CMC", "0", "1", "2", "3", "4", "5", "6", "7+")
        self._curve_tree = self._make_tree(
            right_wrap,
            columns=CMC_COLS,
            widths={c: 36 for c in CMC_COLS},
        )
        blank = ("",) * len(CMC_COLS)
        self._crea_iid  = self._curve_tree.insert("", tk.END, values=blank)
        self._other_iid = self._curve_tree.insert("", tk.END, values=blank)

        # ---- Pools section ----
        ttk.Separator(right_wrap, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(6, 2))
        ttk.Label(
            right_wrap, text="Pools  (pair \u2014 cards \u2014 avg GIH WR)",
            font=("Segoe UI", 9, "italic"), foreground="gray",
        ).pack(anchor="w")
        POOL_COLS = ("Pair", "Cards", "Avg GIH")
        self._pool_tree = self._make_tree(
            right_wrap,
            columns=POOL_COLS,
            widths={"Pair": 44, "Cards": 38, "Avg GIH": 58},
        )

        # ---- Color breakdown section ----
        ttk.Separator(right_wrap, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(6, 2))
        ttk.Label(
            right_wrap, text="Pick colors",
            font=("Segoe UI", 9, "italic"), foreground="gray",
        ).pack(anchor="w")
        self._col_vars = [tk.StringVar(value="") for _ in range(4)]
        for col_label, col_var in zip(
            ("Colorless:", "Mono:", "Bi-color:", "Multi(3+):"),
            self._col_vars,
        ):
            row_f = ttk.Frame(right_wrap)
            row_f.pack(fill=tk.X, anchor="w", pady=1)
            ttk.Label(row_f, text=col_label,
                      font=("Segoe UI", 9, "bold"), width=9, anchor="e").pack(side=tk.LEFT)
            ttk.Label(row_f, textvariable=col_var,
                      font=("Segoe UI", 9), anchor="w").pack(side=tk.LEFT, padx=(4, 0))

        # ---- Signal section ----
        ttk.Separator(right_wrap, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(6, 2))
        ttk.Label(
            right_wrap, text="Signals  (avg pick delta, + = open)",
            font=("Segoe UI", 9, "italic"), foreground="gray",
        ).pack(anchor="w")
        self._sig_vars = [tk.StringVar(value="") for _ in range(3)]
        for label, var in zip(
            ("Lanes:", "Pressure:", "Density:"),
            self._sig_vars,
        ):
            row_f = ttk.Frame(right_wrap)
            row_f.pack(fill=tk.X, anchor="w", pady=1)
            ttk.Label(row_f, text=label,
                      font=("Segoe UI", 9, "bold"), width=8, anchor="e").pack(side=tk.LEFT)
            ttk.Label(row_f, textvariable=var,
                      font=("Segoe UI", 9), anchor="w").pack(side=tk.LEFT, padx=(4, 0))

    @staticmethod
    def _make_tree(parent, columns, widths):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL)
        tree = ttk.Treeview(
            frame, columns=columns, show="headings",
            yscrollcommand=sb.set, selectmode="browse",
        )
        sb.config(command=tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=widths.get(col, 100), anchor="w", stretch=(col == "Name"))
        return tree

    def _lookup(self, arena_id: int) -> dict:
        name = self._arena.get(arena_id, f"#{arena_id}")
        key = name.strip().lower()
        r = self._cards.get(key)
        if r is None and " // " in key:
            # 17lands uses front-face name only for DFCs
            r = self._cards.get(key.split(" // ")[0].strip())
        if r is None:
            return {"Name": name, "Color": "", "GIH WR": "", "OH WR": "",
                    "GIH WR Pro": "", "OH WR Pro": "",
                    "Best1": "", "Best2": "", "Best3": "", "Best4": ""}
        return r

    def _reset_signals(self):
        self._sig_sum.clear()
        self._sig_cnt.clear()
        self._prs_cnt.clear()
        self._dns_sum.clear()
        self._dns_cnt.clear()
        self._lane_sum.clear()
        self._lane_cnt.clear()
        self._sig_n = 0
        self._score_log.config(state=tk.NORMAL)
        self._score_log.delete("1.0", tk.END)
        self._score_log.config(state=tk.DISABLED)

    def _on_pack_tree_select(self, _event=None):
        sel = self._pack_tree.selection()
        if not sel:
            self._17l_pack_btn.config(state=tk.DISABLED)
            return
        name = self._pack_tree.item(sel[0], "values")[1]
        card = self._cards.get(name.strip().lower(), {})
        aid  = card.get("ArenaId", "").strip()
        self._17l_pack_btn.config(state=tk.NORMAL if aid else tk.DISABLED)
        self._17l_pack_aid = aid

    def _open_pack_17lands(self):
        aid = getattr(self, "_17l_pack_aid", "")
        if aid:
            webbrowser.open(
                f"https://www.17lands.com/card_data/details"
                f"?card_id={aid}&expansion={self._current_set}&format=PremierDraft"
            )

    def _toggle_overlay(self):
        """Show or hide the always-on-top score overlay."""
        import threading
        if hasattr(self, "_overlay_thread") and self._overlay_thread.is_alive():
            try:
                self._overlay_obj.root.after(0, self._overlay_obj.root.destroy)
            except Exception:
                pass
            self._overlay_btn.config(text="Open Overlay")
            return

        from overlay import DraftOverlay

        def _run():
            self._overlay_obj = DraftOverlay(self._csv_path, self._reader.path)
            self._overlay_obj.root.mainloop()
            try:
                self.after(0, lambda: self._overlay_btn.config(text="Open Overlay"))
            except Exception:
                pass

        self._overlay_thread = threading.Thread(target=_run, daemon=True)
        self._overlay_thread.start()
        self._overlay_btn.config(text="Close Overlay")

    def _reset(self):
        """Reset and replay from the most recent draft-start event in the log."""
        self._state.reset()
        self._reset_signals()

        if not self._reader.path.exists():
            self._refresh()
            return

        with self._reader.path.open(encoding="utf-8", errors="replace") as fh:
            content = fh.read()
            end_offset = fh.tell()

        parts = re.split(r"(\[UnityCrossThreadLogger\])", content)
        chunks: list[str] = []
        i = 1
        while i < len(parts):
            chunks.append(parts[i] + (parts[i + 1] if i + 1 < len(parts) else ""))
            i += 2

        # Find the last chunk that signals a new draft session
        last_start = 0
        for idx, chunk in enumerate(chunks):
            for kind, data in _parse_chunk(chunk):
                if kind == "set_start" or (
                    kind == "notify" and data.get("pack") == 0 and data.get("pick") == 0
                ):
                    last_start = idx

        # Replay from that point
        for chunk in chunks[last_start:]:
            for kind, data in _parse_chunk(chunk):
                if kind == "set_start":
                    self._on_set_start(data["set"])
                else:
                    self._state.apply(kind, data)

        # Advance reader past what we've replayed so live polling only picks up new events
        self._reader._offset = end_offset
        self._refresh()

    def _load_test(self):
        """Generate Player_test.log and replay it immediately."""
        log_path = generate_test_log(RAW_CSV, TEST_LOG)
        self._state.reset()
        self._reset_signals()
        # Force-reload SOS data (test log uses SOS IDs; user may have switched to another set)
        enriched_csv, raw_csv = _csv_paths_for_set(DEFAULT_SET)
        self._current_set   = DEFAULT_SET
        self._arena         = load_arena_ids(raw_csv)
        self._cards         = load_enriched(enriched_csv, raw_csv)
        self._density_baseline, self._density_threshold = compute_density_baseline(self._cards)
        self._reader = LogReader(log_path)
        for kind, data in self._reader.poll():
            if kind != "set_start":
                self._state.apply(kind, data)
        self._refresh()

    def _watch_sim(self):
        """Switch to watching Player_sim.log from the start."""
        sim_log = ROOT / "Player_sim.log"
        self._state.reset()
        self._reset_signals()
        self._reader = LogReader(sim_log)
        self._refresh()

    def _poll(self):
        events = self._reader.poll()
        if not self._reader.path.exists():
            self._log_var.set(f"Log not found: {self._reader.path.name}")
        else:
            self._log_var.set("")
        if events:
            for kind, data in events:
                if kind == "set_start":
                    self._on_set_start(data["set"])
                    continue
                self._state.apply(kind, data)
                if kind == "notify" and data.get("cards"):
                    self._refresh()
                elif kind == "pick":
                    picked_name = self._lookup(data["card"]).get("Name", f"#{data['card']}")
                    self._score_log.config(state=tk.NORMAL)
                    self._score_log.insert(tk.END, f"  ➜ Picked: {picked_name}\n")
                    self._score_log.see(tk.END)
                    self._score_log.config(state=tk.DISABLED)
        self.after(POLL_MS, self._poll)

    def _on_set_start(self, set_code: str) -> None:
        """Reload card data when a new draft set is detected in the log."""
        if set_code == self._current_set:
            return
        enriched_csv, raw_csv = _csv_paths_for_set(set_code)
        if not enriched_csv.exists():
            self._status_var.set(
                f"No data for set {set_code} — add sets/{set_code}/carddata_enriched.csv"
            )
            return
        self._current_set = set_code
        self._csv_path    = enriched_csv
        self._arena  = load_arena_ids(raw_csv)
        self._cards  = load_enriched(enriched_csv, raw_csv)
        self._density_baseline, self._density_threshold = compute_density_baseline(self._cards)
        self._state.reset()
        self._reset_signals()
        self._status_var.set(f"Set: {set_code} — Waiting for draft…")

    def _refresh(self):
        s = self._state

        if s.current_pack:
            self._status_var.set(f"Pack {s.pack + 1}  \u2014  Pick {s.pick + 1}")
        else:
            self._status_var.set("Waiting for draft...")

        # Precompute pick stats (reused by pack scoring and curve display)
        total_picks = len(s.picks)
        depth = count_color_depth(s.picks, self._lookup)
        pair_synergy = count_pair_synergy(s.picks, self._lookup)
        picked_names = [self._lookup(aid).get("Name", "").lower()
                        for *_, aid in s.picks]
        crea  = [0] * 8
        other = [0] * 8
        for _, _, aid in s.picks:
            pr = self._lookup(aid)
            try:
                bucket = min(int(float(pr.get("CMC", "0") or "0")), 7)
            except (ValueError, TypeError):
                bucket = 0
            if "Creature" in pr.get("Type", ""):
                crea[bucket] += 1
            else:
                other[bucket] += 1

        self._pack_tree.delete(*self._pack_tree.get_children())
        lane_fn = self._make_lane_fn()
        scored = [
            (card_score(self._lookup(aid), depth, total_picks, crea, other,
                        self._density_threshold, picked_names,
                        lane_fn=lane_fn, pair_synergy=pair_synergy,
                        picks=s.picks, lookup_fn=self._lookup),
             self._lookup(aid))
            for aid in s.current_pack
            if not self._lookup(aid).get("Name", "#").startswith("#")
            and not _is_basic_land(self._lookup(aid))
        ]
        # Sort by Arena's visual order (color group then CMC)
        scored.sort(key=lambda x: _arena_sort_key(x[1]))
        # Determine top-3 by score (for highlighting), preserving pack order for display
        rank_tags = {}
        for tag, (_, r) in zip(("top1", "top2", "top3"),
                               sorted(scored, key=lambda x: x[0], reverse=True)[:3]):
            rank_tags[r.get("Name", "")] = tag
        log_lines = []
        for sc, r in scored:
            tag = rank_tags.get(r.get("Name", ""), "")
            self._pack_tree.insert("", tk.END, tags=(tag,), values=(
                f"{sc:.1f}",
                r.get("Name", ""),
                r.get("Color", ""),
                r.get("GIH WR", ""),
                r.get("OH WR", ""),
                _fmt_best(r.get("Best1", "")), _fmt_best(r.get("Best2", "")),
                _fmt_best(r.get("Best3", "")), _fmt_best(r.get("Best4", "")),
            ))
            log_lines.append(_score_log_line(r, depth, total_picks, crea, other,
                                             self._density_threshold, picked_names,
                                             None, pair_synergy,
                                             picks=s.picks, lookup_fn=self._lookup,
                                             lane_fn=lane_fn))
        _autosize_tree(self._pack_tree)

        # Update score breakdown log (sorted by score, independent of pack order)
        self._score_log.config(state=tk.NORMAL)
        sep = f"Pack {s.pack + 1}  Pick {s.pick + 1}"
        self._score_log.insert(tk.END, "=" * 40 + "  " + sep + "\n")
        for line in sorted(log_lines, reverse=True):
            self._score_log.insert(tk.END, line + "\n")
        self._score_log.see(tk.END)
        self._score_log.config(state=tk.DISABLED)

        # Mana curve display (reuses crea/other above)
        self._curve_tree.item(self._crea_iid,
            values=("Creatures",) + tuple(v if v else "-" for v in crea))
        self._curve_tree.item(self._other_iid,
            values=("Non-crea.",) + tuple(v if v else "-" for v in other))
        _autosize_tree(self._curve_tree)

        # Color breakdown of picks
        colorless_cnt = 0
        mono_cnts: dict[str, int] = {c: 0 for c in WUBRG}
        bi_cnts: dict[str, int] = {}
        multi_cnt = 0
        for _, _, aid in s.picks:
            pr = self._lookup(aid)
            col = [c for c in _sort_color(pr.get("Color", "")) if c in WUBRG]
            n = len(col)
            if n == 0:
                colorless_cnt += 1
            elif n == 1:
                mono_cnts[col[0]] += 1
            elif n == 2:
                key = "".join(col)
                bi_cnts[key] = bi_cnts.get(key, 0) + 1
            else:
                multi_cnt += 1
        self._col_vars[0].set(str(colorless_cnt) if colorless_cnt else "-")
        self._col_vars[1].set("  ".join(f"{c}:{mono_cnts[c]}" for c in WUBRG))
        bi_parts = [f"{k}:{v}" for k, v in bi_cnts.items() if v > 0]
        self._col_vars[2].set("  ".join(bi_parts) if bi_parts else "-")
        self._col_vars[3].set(str(multi_cnt) if multi_cnt else "-")

        # Pool membership: pair in Best1-4 with GIH >= 54%
        POOL_THRESHOLD = 54.0
        pool_cnt: dict[str, int]   = {}
        pool_sum: dict[str, float] = {}
        for _, _, aid in s.picks:
            pr = self._lookup(aid)
            seen_pairs: set[str] = set()
            for bkey in ("Best1", "Best2", "Best3", "Best4"):
                pair, wr = _parse_best_entry(pr.get(bkey, ""))
                if pair and wr >= POOL_THRESHOLD and pair not in seen_pairs:
                    seen_pairs.add(pair)
                    pool_cnt[pair] = pool_cnt.get(pair, 0) + 1
                    pool_sum[pair] = pool_sum.get(pair, 0.0) + wr
        # Rebuild treeview rows sorted by count desc, avg WR desc
        for iid in self._pool_tree.get_children():
            self._pool_tree.delete(iid)
        for pair in sorted(pool_cnt, key=lambda p: (-pool_cnt[p], -pool_sum[p] / pool_cnt[p])):
            cnt = pool_cnt[pair]
            avg = pool_sum[pair] / cnt
            self._pool_tree.insert("", tk.END, values=(pair, cnt, f"{avg:.1f}%"))
        _autosize_tree(self._pool_tree)

        self._last_depth = depth
        self._process_new_signals()
        self._update_signal_display()

    # ------------------------------------------------------------------
    # Signal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_weight(pick: int) -> float:
        """Unified pick weighting: 0 for picks 1-2, 0.3 for pick 3, 1.0 for picks 4+."""
        if pick <= 2:
            return 0.0
        if pick == 3:
            return 0.3
        return 1.0

    def _add_signal(self, key: str, weight: float, pw: float = 1.0) -> None:
        if pw <= 0.0:
            return
        self._sig_sum[key] = self._sig_sum.get(key, 0.0) + weight * pw
        self._sig_cnt[key] = self._sig_cnt.get(key, 0.0) + pw

    def _signal_score(self, key: str) -> float:
        c = self._sig_cnt.get(key, 0.0)
        return self._sig_sum.get(key, 0.0) / c if c else 0.0

    def _process_new_signals(self) -> None:
        """Process any notify events not yet scored."""
        notifies = self._state.all_notifies
        while self._sig_n < len(notifies):
            pack_idx, pick_idx, cards = notifies[self._sig_n]
            current_pick = pick_idx + 1  # convert 0-based log index to 1-based pick number

            # --- Absence (pressure) signal ---
            # Only meaningful early in the pack (picks 1-7); weight fades linearly.
            if current_pick <= 7:
                absence_weight = 1.0 - (current_pick - 1) / 7.0
                colors_present = set()
                for aid in cards:
                    r = self._lookup(aid)
                    for c in _sort_color(r.get("Color", "")):
                        if c in WUBRG:
                            colors_present.add(c)
                for c in WUBRG:
                    if c not in colors_present:
                        self._prs_cnt[c] = self._prs_cnt.get(c, 0) + round(absence_weight * 10)

            # --- Density signal: observed vs expected 55%+ WR fraction per color ---
            # Only from pick 4 onwards; multicolor cards count 0.5 per color.
            DNS_THRESHOLD = 55.0
            if current_pick >= 4:
                cards_by_color: dict[str, list[float]] = {}
                for aid in cards:
                    r = self._lookup(aid)
                    col = [c for c in _sort_color(r.get("Color", "")) if c in WUBRG]
                    if not col:
                        continue
                    try:
                        gih = float((r.get("GIH WR", "") or "").rstrip("%"))
                    except (ValueError, TypeError):
                        continue
                    w = 1.0 if len(col) == 1 else 0.5
                    for c in col:
                        cards_by_color.setdefault(c, []).append((gih, w))
                dns_weight = self._pick_weight(current_pick)
                if dns_weight > 0.0:
                    for color in WUBRG:
                        expected = self._density_baseline.get(color, {}).get(current_pick, 0.0)
                        if color in cards_by_color:
                            entries = cards_by_color[color]
                            total_w = sum(w for _, w in entries)
                            high_w  = sum(w for g, w in entries if g >= DNS_THRESHOLD)
                            observed = high_w / total_w if total_w > 0 else 0.0
                        elif expected > 0.0:
                            observed = 0.0
                        else:
                            continue
                        self._dns_sum[color] = self._dns_sum.get(color, 0.0) + (observed - expected) * dns_weight
                        self._dns_cnt[color] = self._dns_cnt.get(color, 0.0) + dns_weight

            # --- Lane openness signal ---
            # A card still in the pack at or past (ALSA - 1) signals the lane(s) where
            # it is a strong pick (GIH WR > LANE_WR_THRESHOLD in that pair) are open.
            lane_pw = self._pick_weight(current_pick)
            if lane_pw > 0.0:
                for aid in cards:
                    r = self._lookup(aid)
                    try:
                        alsa = float(r.get("ALSA", "") or 0)
                    except (ValueError, TypeError):
                        continue
                    if alsa <= 0:
                        continue
                    delta = current_pick - (alsa - 1.0)   # positive once card is "approaching" ALSA
                    if delta <= 0:
                        continue
                    for pool in _card_lane_pools(r):
                        self._lane_sum[pool] = self._lane_sum.get(pool, 0.0) + delta * lane_pw
                        self._lane_cnt[pool] = self._lane_cnt.get(pool, 0.0) + lane_pw
            self._sig_n += 1

    def _update_signal_display(self) -> None:
        # Row 0: the 6 lane pools, sorted descending by openness
        def lane_score(key: str) -> float:
            n = self._lane_cnt.get(key, 0.0)
            return self._lane_sum.get(key, 0.0) / n if n else 0.0
        def lane_score(key: str) -> float:
            n = self._lane_cnt.get(key, 0.0)
            return self._lane_sum.get(key, 0.0) / n if n else 0.0
        lane_parts = sorted(
            [(key, lane_score(key)) for key in LANE_KEYS],
            key=lambda x: x[1], reverse=True,
        )
        self._sig_vars[0].set("  ".join(
            f"{k}:{v:.1f}" if v >= 0.05 else f"{k}:-" for k, v in lane_parts
        ))
        # Row 1: pressure (absence counts, always show all colors)
        prs_parts = [f"{c}:{self._prs_cnt[c]}" if self._prs_cnt.get(c, 0) > 0 else f"{c}:-"
                     for c in WUBRG]
        self._sig_vars[1].set("  ".join(prs_parts))
        # Row 2: density
        def dns(c: str) -> float:
            n = self._dns_cnt.get(c, 0)
            return self._dns_sum.get(c, 0.0) / n if n else 0.0
        dns_parts = []
        for c in WUBRG:
            d = dns(c)
            if abs(d) >= 0.02:
                dns_parts.append(f"{c}:{'+' if d >= 0 else ''}{d:.2f}")
            else:
                dns_parts.append(f"{c}:-")
        self._sig_vars[2].set("  ".join(dns_parts))

    # ------------------------------------------------------------------
    # Card scoring (module-level functions; only openness stays here)
    # ------------------------------------------------------------------

    def _color_openness_score(self, c: str,
                               depth: dict[str, float] | None = None) -> float:
        """Combine ALSA-open, approach, density and pressure into a single value (+ = open).

        Approximate contribution ranges per signal so no single one dominates:
          open_val  0–3   × 0.5  → 0–1.5
          approach  0–3   × 0.4  → 0–1.2
          density  ±0.5   × 2.5  → ±1.25
          pressure  0–20  × 0.08 → 0–1.6  (negative, i.e. contested penalty)

        When depth is provided and set_data is loaded, the result is multiplied by
        a pair-WR modifier: the best partner color (by committed depth) whose pair
        WR deviates from the set mean scales the openness proportionally.
        blend = min(1, partner_depth / 5):  full effect at depth ≥ 5.
        modifier = 1 + (pair_wr - mean_wr) / mean_wr × blend
        """
        open_val = self._signal_score(c)   # legacy (kept for pair-WR modifier path)
        # Lane-based openness: average of the two lanes this color participates in
        lane_vals = [self._lane_sum[k] / self._lane_cnt[k]
                     for k in COLOR_TO_LANES.get(c, [])
                     if self._lane_cnt.get(k, 0.0) > 0]
        lane_open = sum(lane_vals) / len(lane_vals) if lane_vals else 0.0
        pressure = self._prs_cnt.get(c, 0)
        n_dns    = self._dns_cnt.get(c, 0.0)
        density  = self._dns_sum.get(c, 0.0) / n_dns if n_dns else 0.0
        base = lane_open * 0.9 + density * 2.5 - pressure * 0.08

        if depth is not None and self._set_pair_wr and self._set_pair_mean > 0:
            best_blend = 0.0
            best_modifier = 1.0
            partners = [p for p in WUBRG if p != c]
            # 2-color combos
            for partner in partners:
                combo = _sort_color(c + partner)
                if combo not in self._set_pair_wr:
                    continue
                pd = depth.get(partner, 0.0)
                if pd <= 0:
                    continue
                blend = min(1.0, pd / 5.0)
                deviation = (self._set_pair_wr[combo] - self._set_pair_mean) / self._set_pair_mean
                modifier = 1.0 + deviation * blend
                if blend > best_blend:
                    best_blend = blend
                    best_modifier = modifier
            # 3-color combos — blend scales with the weaker partner's depth
            for i, p1 in enumerate(partners):
                for p2 in partners[i + 1:]:
                    combo = _sort_color(c + p1 + p2)
                    if combo not in self._set_pair_wr:
                        continue
                    pd1 = depth.get(p1, 0.0)
                    pd2 = depth.get(p2, 0.0)
                    if pd1 <= 0 or pd2 <= 0:
                        continue
                    blend = min(1.0, min(pd1, pd2) / 5.0)
                    deviation = (self._set_pair_wr[combo] - self._set_pair_mean) / self._set_pair_mean
                    modifier = 1.0 + deviation * blend
                    if blend > best_blend:
                        best_blend = blend
                        best_modifier = modifier
            base *= best_modifier

        return base

    def _make_lane_fn(self):
        """Return a closure that gives the average openness signal for a lane key."""
        def fn(lane_key: str) -> float:
            n = self._lane_cnt.get(lane_key, 0.0)
            return self._lane_sum.get(lane_key, 0.0) / n if n else 0.0
        return fn

    def _make_openness_fn(self, depth: dict[str, float]):
        """Return a closure over depth for use as openness_fn in card_score."""
        def fn(c: str) -> float:
            return self._color_openness_score(c, depth)
        return fn

    # The remaining scoring functions live at module level (curve_fit_score,
    # fixing_bonus, pair_wr_adjustment, card_score) so draft_sim.py can import them.


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self, csv_path: Path, log_path: Path = LOG_PATH):
        super().__init__()
        self.title("Limited Tool")
        self.geometry("1600x700")
        self.minsize(1000, 500)

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True)

        advisor = DraftAdvisorFrame(nb, csv_path, log_path)
        browser = CardBrowserFrame(nb, csv_path, on_go_draft=lambda: nb.select(advisor))

        nb.add(browser, text="  Card Browser  ")
        nb.add(advisor, text="  Draft Advisor  ")

    def report_callback_exception(self, exc_type, exc_value, exc_tb):
        """Tkinter calls this for unhandled exceptions inside event callbacks."""
        import traceback
        _error_log = ROOT / "error.log"
        with _error_log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{'='*60}\n{datetime.now()} [callback]\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=fh)
        # Also print to stderr so it's visible in terminal
        traceback.print_exception(exc_type, exc_value, exc_tb)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if "--test" in sys.argv:
        sys.exit(run_tests())

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("csv",   nargs="?", default=str(DEFAULT_CSV))
    parser.add_argument("--log", default=str(LOG_PATH),
                        help="Player.log path (default: MTGA log)")
    args = parser.parse_args([a for a in sys.argv[1:] if a != "--test"])

    csv_path = Path(args.csv)
    log_path = Path(args.log)
    if not csv_path.exists():
        print(f"Error: file not found: '{csv_path}'")
        sys.exit(1)

    App(csv_path, log_path).mainloop()


if __name__ == "__main__":
    import traceback
    _ERROR_LOG = ROOT / "error.log"

    def _log_exception(exc_type, exc_value, exc_tb):
        with _ERROR_LOG.open("a", encoding="utf-8") as _ef:
            _ef.write(f"\n{'='*60}\n{datetime.now()}\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=_ef)
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _log_exception

    try:
        main()
    except Exception:
        with _ERROR_LOG.open("a", encoding="utf-8") as _ef:
            _ef.write(f"\n{'='*60}\n{datetime.now()}\n")
            traceback.print_exc(file=_ef)
        raise
