#!/usr/bin/env python3
"""
analyze_draft.py -- Analyze draft score logs for openness signal effectiveness.

Reads draft log text (the score-log output from limitedtool) from a file or
stdin and reports:
  1. Average openness contribution per pick (when openness is non-zero)
  2. Picks where openness changed the winner (card B beats card A after openness
     but would have lost without it)
  3. Counterfactual: if you had committed to whichever color openness was
     pushing hardest, which card would have been the top-scorer at each pick?

Usage:
    python analyze_draft.py draft_log.txt
    python analyze_draft.py          # reads from stdin / paste then Ctrl-Z
"""

import re
import sys
from collections import defaultdict


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

PACK_PICK_RE = re.compile(r"={5,}\s*Pack\s+(\d+)\s+Pick\s+(\d+)")
CARD_LINE_RE = re.compile(
    r"^\s*([-\d.]+)\s{2,}(.+?)\s{2,}"   # score + name
    r"(?:.*?GIH=([\d.]+)(?:→([\d.]+))?)?"  # GIH raw and optional blended
    r"(?:.*?fit=([+-][\d.]+)\(x([\d.]+)\))?"  # fit
    r"(?:.*?open=([+-][\d.]+))?"             # openness
    r"(?:.*?pool=([+-][\d.]+))?"             # pool rejection
    r"(?:.*?lock=([+-][\d.]+))?"             # lock
)


def _parse_component(line: str, prefix: str) -> float:
    """Extract a named component like open=+1.23 from a log line."""
    m = re.search(rf"{prefix}=([+-]?[\d.]+)", line)
    return float(m.group(1)) if m else 0.0


def parse_log(text: str) -> list[dict]:
    """Return list of pick dicts, each containing a list of card dicts."""
    picks = []
    current = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        # Pack/pick header
        m = PACK_PICK_RE.search(line)
        if m:
            if current:
                picks.append(current)
            current = {
                "pack": int(m.group(1)),
                "pick": int(m.group(2)),
                "cards": [],
            }
            continue

        if current is None:
            continue

        # Card line: starts with optional whitespace + a number
        stripped = line.lstrip()
        if not stripped or not (stripped[0].isdigit() or stripped[0] == "-"):
            continue
        parts = stripped.split()
        try:
            score = float(parts[0])
        except (ValueError, IndexError):
            continue

        # Extract name (everything between score col and first double-space or component)
        # The log format is: "SCORE  NAME                         components"
        # Name ends at the first two-space gap after the score
        after_score = stripped[len(parts[0]):].lstrip()
        name_end = re.search(r"  (?:GIH=|fit=|open=|pool=|lock=|curve=|dup=|fix=|\Z)", after_score)
        name = after_score[:name_end.start()].strip() if name_end else after_score.strip()

        gih_raw   = _parse_component(line, "GIH")
        open_val  = _parse_component(line, r"open")
        pool_val  = _parse_component(line, r"pool")
        lock_val  = _parse_component(line, r"lock")
        fit_val   = 0.0
        fit_m = re.search(r"fit=([+-][\d.]+)\(", line)
        if fit_m:
            fit_val = float(fit_m.group(1))

        # Blended GIH: GIH=56.0→57.2 means raw=56.0 blended=57.2
        gih_blend_m = re.search(r"GIH=([\d.]+)→([\d.]+)", line)
        if gih_blend_m:
            gih_raw    = float(gih_blend_m.group(1))
            gih_actual = float(gih_blend_m.group(2))
        else:
            gih_actual = gih_raw

        current["cards"].append({
            "name":       name,
            "score":      score,
            "gih_raw":    gih_raw,
            "gih_actual": gih_actual,
            "fit":        fit_val,
            "open":       open_val,
            "pool":       pool_val,
            "lock":       lock_val,
            # score without openness:
            "score_no_open": score - open_val,
        })

    if current:
        picks.append(current)
    return picks


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def winner(cards: list[dict], key: str = "score") -> dict | None:
    return max(cards, key=lambda c: c[key]) if cards else None


def fmt_card(c: dict) -> str:
    parts = [f"{c['score']:.1f}  {c['name']}  GIH={c['gih_raw']:.1f}"]
    if abs(c["fit"]) >= 0.05:
        parts.append(f"fit={c['fit']:+.1f}")
    if abs(c["open"]) >= 0.05:
        parts.append(f"open={c['open']:+.2f}")
    if abs(c["pool"]) >= 0.05:
        parts.append(f"pool={c['pool']:+.1f}")
    if abs(c["lock"]) >= 0.05:
        parts.append(f"lock={c['lock']:+.1f}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(picks: list[dict]) -> None:
    # --- 1. Average openness stats ---
    all_open = [c["open"] for p in picks for c in p["cards"] if abs(c["open"]) >= 0.05]
    pos_open = [v for v in all_open if v > 0]
    neg_open = [v for v in all_open if v < 0]
    total_cards = sum(len(p["cards"]) for p in picks)

    print("=" * 70)
    print("  OPENNESS SUMMARY")
    print("=" * 70)
    print(f"  Total picks analyzed : {len(picks)}")
    print(f"  Total card evals     : {total_cards}")
    print(f"  Cards with open≠0    : {len(all_open)}  ({100*len(all_open)/max(1,total_cards):.0f}%)")
    if pos_open:
        print(f"  Positive open  avg={sum(pos_open)/len(pos_open):+.3f}  "
              f"max={max(pos_open):+.3f}  n={len(pos_open)}")
    if neg_open:
        print(f"  Negative open  avg={sum(neg_open)/len(neg_open):+.3f}  "
              f"min={min(neg_open):+.3f}  n={len(neg_open)}")

    # Per-pick openness on the winning card
    open_on_winner = [winner(p["cards"])["open"] for p in picks if winner(p["cards"])]
    avg_winner_open = sum(open_on_winner) / len(open_on_winner) if open_on_winner else 0
    print(f"  Avg open on actual winner: {avg_winner_open:+.3f}")
    print()

    # --- 2. Picks where openness changed the winner ---
    print("=" * 70)
    print("  OPENNESS CHANGED THE WINNER")
    print("=" * 70)
    changed = []
    for p in picks:
        cards = p["cards"]
        if len(cards) < 2:
            continue
        w_open   = winner(cards, "score")
        w_noopen = winner(cards, "score_no_open")
        if w_open["name"] != w_noopen["name"]:
            margin = w_open["score"] - w_noopen["score"]
            changed.append((p, w_noopen, w_open, margin))

    if not changed:
        print("  None — openness never flipped the top pick.\n")
    else:
        for p, loser_without, winner_with, margin in changed:
            print(f"  Pack {p['pack']}  Pick {p['pick']}  "
                  f"(margin {margin:+.1f})")
            print(f"    Without open  → {fmt_card(loser_without)}")
            print(f"    WITH    open  → {fmt_card(winner_with)}")
            print()

    # --- 3. Counterfactual: best card if committed to the 'open' color ---
    # At each pick, determine which color openness is pushing (max open card's
    # color estimate).  Then ask: what's the best card in that color family?
    # We approximate color from the card name's gih data vs open signal.
    # Since we don't have explicit color in the log, we use a heuristic:
    # the card with the highest positive open is assumed to represent an "open"
    # color direction, and we identify which cards share that archetype (same
    # high-open cards).
    print("=" * 70)
    print("  COUNTERFACTUAL: FOLLOWING OPENNESS SIGNAL")
    print("=" * 70)
    print("  (What would top pick be if we re-ranked with open weighted x3?)\n")

    counterfactual_gains = []
    for p in picks:
        cards = p["cards"]
        if len(cards) < 2:
            continue
        # Re-rank: open × 3 to simulate committing to the open color
        for c in cards:
            c["score_cf"] = c["score"] + 2.0 * c["open"]  # extra 2× on top of existing

        w_actual = winner(cards, "score")
        w_cf     = winner(cards, "score_cf")

        if w_cf["name"] != w_actual["name"]:
            delta = w_cf["gih_raw"] - w_actual["gih_raw"]
            counterfactual_gains.append((p, w_actual, w_cf, delta))
            direction = "better" if delta > 0 else "worse"
            print(f"  Pack {p['pack']}  Pick {p['pick']}  raw GIH delta {delta:+.1f} ({direction})")
            print(f"    Actual pick → {fmt_card(w_actual)}")
            print(f"    CF pick     → {fmt_card(w_cf)}")
            print()

    if not counterfactual_gains:
        print("  No picks would change with 3× openness weight.\n")

    # Summary: when CF differs, is it usually a better or worse raw card?
    gains = [d for *_, d in counterfactual_gains if d > 0]
    losses = [d for *_, d in counterfactual_gains if d < 0]
    if counterfactual_gains:
        print(f"  CF changes {len(counterfactual_gains)} picks: "
              f"{len(gains)} with higher raw GIH, {len(losses)} with lower raw GIH")
        if gains:
            print(f"    Average raw GIH gain when CF is better : "
                  f"+{sum(gains)/len(gains):.2f}%")
        if losses:
            print(f"    Average raw GIH loss when CF is worse  : "
                  f"{sum(losses)/len(losses):.2f}%")

    print()

    # --- 4. Per-pick open breakdown table ---
    print("=" * 70)
    print("  PER-PICK OPENNESS BREAKDOWN")
    print("=" * 70)
    print(f"  {'Pack':>4}  {'Pick':>4}  {'#cards':>6}  "
          f"{'winner':28}  {'open on winner':>14}  {'max open in pack':>16}")
    print(f"  {'-'*4}  {'-'*4}  {'-'*6}  {'-'*28}  {'-'*14}  {'-'*16}")
    for p in picks:
        cards = p["cards"]
        w = winner(cards)
        if not w:
            continue
        max_open_card = max(cards, key=lambda c: c["open"])
        print(f"  {p['pack']:>4}  {p['pick']:>4}  {len(cards):>6}  "
              f"{w['name'][:28]:28}  {w['open']:>+14.2f}  "
              f"{max_open_card['open']:>+.2f} ({max_open_card['name'][:20]})")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) > 1:
        text = open(sys.argv[1], encoding="utf-8").read()
    else:
        print("Paste draft score log, then press Ctrl-Z (Windows) or Ctrl-D (Unix) + Enter:")
        text = sys.stdin.read()

    picks = parse_log(text)
    if not picks:
        print("No picks found. Check that the log contains '=== Pack X  Pick Y' headers.")
        sys.exit(1)

    analyze(picks)


if __name__ == "__main__":
    main()
