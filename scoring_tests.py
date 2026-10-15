#!/usr/bin/env python3
"""
scoring_tests.py -- Parameterized scoring scenario tests for limitedtool.

Each test defines two made-up cards and a draft situation, then asserts which
card should score higher.  Run with:
    python scoring_tests.py
    python scoring_tests.py -v      # verbose: always print breakdown

A card definition is a plain dict with the same keys the scoring engine reads:
  Name, Color, Type, CMC, GIH WR, OH WR, Best1..Best4

A scenario definition is a dict with:
  picks   : list of (pack, pick_pos, arena_id) tuples
  depth   : {W/U/B/R/G: float}  -- overrides computed depth if provided
  openness: {W/U/B/R/G: float}  -- per-color openness score injected directly

Usage pattern:
  def openness_fn_for(scores: dict):
      return lambda c: scores.get(c, 0.0)
"""

import sys
from limitedtool import (
    card_score, _score_log_line,
    count_color_depth, WUBRG,
    _is_land_slot, _is_basic_land,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FLAT_CURVE = [0] * 8
SET_MEAN   = 54.0


def _score_card(card: dict, situation: dict, verbose: bool = False) -> float:
    """Score a single card in the given situation, printing breakdown if verbose."""
    depth     = situation.get("depth", {c: 0.0 for c in WUBRG})
    picks     = situation.get("picks", [])
    openness  = situation.get("openness", {})
    lane_openness = situation.get("lane_openness", {})
    total_picks = situation.get("total_picks", len(picks))
    crea_curve  = situation.get("crea_curve",  FLAT_CURVE)
    spell_curve = situation.get("spell_curve", FLAT_CURVE)

    def openness_fn(c: str) -> float:
        return openness.get(c, 0.0)

    def _lane_fn(k: str) -> float:
        return lane_openness.get(k, 0.0)

    use_lane = bool(lane_openness)

    # Build a tiny lookup from the picks list so archetype_lock_penalty can work
    pick_lookup: dict[int, dict] = situation.get("_pick_lookup", {})
    def lookup_fn(aid: int) -> dict:
        return pick_lookup.get(aid, {"Name": f"#{aid}", "Color": "", "GIH WR": "", "OH WR": "",
                                     "Best1": "", "Best2": "", "Best3": "", "Best4": ""})

    sc = card_score(
        card, depth, total_picks, crea_curve, spell_curve,
        SET_MEAN, situation.get("picked_names", []),
        openness_fn=(openness_fn if openness and not use_lane else None),
        lane_fn=(_lane_fn if use_lane else None),
        picks=picks, lookup_fn=lookup_fn,
    )
    if verbose:
        log = _score_log_line(
            card, depth, total_picks, crea_curve, spell_curve,
            SET_MEAN, situation.get("picked_names", []),
            (openness_fn if openness and not use_lane else None), None,
            picks=picks, lookup_fn=lookup_fn,
            lane_fn=(_lane_fn if use_lane else None),
        )
        print(f"    {log}")
    return sc


def _run_scenario(desc: str, card_a: dict, card_b: dict,
                  situation: dict, expect: str,
                  verbose: bool = False) -> bool:
    """
    expect: 'A' or 'B' -- which card should score higher.
    Returns True if passed.
    """
    print(f"\n  Scenario: {desc}")
    sc_a = _score_card(card_a, situation, verbose=True)
    sc_b = _score_card(card_b, situation, verbose=True)
    print(f"    → {card_a['Name']}: {sc_a:.2f}   {card_b['Name']}: {sc_b:.2f}   (expect {expect} higher)")
    if expect == "A":
        ok = sc_a > sc_b
    else:
        ok = sc_b > sc_a
    status = "pass" if ok else "FAIL"
    print(f"    {status}")
    return ok


def _run_cross(desc: str, card_a: dict, sit_a: dict,
              card_b: dict, sit_b: dict,
              verbose: bool = False) -> bool:
    """Compare card_a in sit_a vs card_b in sit_b; assert a > b.

    card_a and card_b may be the same dict (same-card, different situation).
    """
    label_a = f"{card_a['Name']} (sit-A)"
    label_b = f"{card_b['Name']} (sit-B)"
    print(f"\n  Cross-scenario: {desc}")
    sc_a = _score_card(card_a, sit_a, verbose=True)
    sc_b = _score_card(card_b, sit_b, verbose=True)
    print(f"    \u2192 {label_a}: {sc_a:.2f}   {label_b}: {sc_b:.2f}   (expect A > B)")
    ok = sc_a > sc_b
    print(f"    {'pass' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# Card factory helpers
# ---------------------------------------------------------------------------

def _parse_bi_entry(entry: str):
    """Extract (pair, wr) from a _bi() string. Returns None for + entries or missing GIH."""
    if not entry:
        return None
    parts = entry.split(":")
    pair = parts[0].strip()
    if pair.endswith("+"):
        return None   # already a plus entry, don't cascade
    for p in parts[1:]:
        if p.startswith("GIH="):
            try:
                return pair, float(p[4:].rstrip("%"))
            except ValueError:
                pass
    return None


def _card(name: str, color: str, gih: float, *,
          best1="", best2="", best3="", best4="",
          ctype="Creature", cmc=3, auto_plus: bool = True) -> dict:
    """Build a minimal card dict for scoring.

    When auto_plus=True (default), empty best3/best4 slots are auto-filled
    with 3+-color versions of best1/best2 at −4 / −3 WR respectively,
    modelling realistic performance in 3-color decks that include this card's
    best archetypes.  Pass auto_plus=False to keep exact pool counts as given.
    """
    if auto_plus:
        if not best3:
            parsed = _parse_bi_entry(best1)
            if parsed:
                pair, wr = parsed
                best3 = _plus(pair, wr - 4.0)
        if not best4:
            parsed = _parse_bi_entry(best2)
            if parsed:
                pair, wr = parsed
                best4 = _plus(pair, wr - 3.0)
    return {
        "Name":    name,
        "Color":   color,
        "Type":    ctype,
        "CMC":     str(cmc),
        "GIH WR":  f"{gih}%",
        "OH WR":   f"{gih - 2.0}%",
        "Best1":   best1,
        "Best2":   best2,
        "Best3":   best3,
        "Best4":   best4,
        "ALSA":    "3.0",
    }


def _bi(pair: str, wr: float) -> str:
    """Shorthand: bicolor Best entry string, e.g. _bi('WB', 57.3)."""
    return f"{pair}:GIH={wr}%:OH={wr - 2}%"


def _plus(pair: str, wr: float) -> str:
    """Shorthand: 3+-color Best entry string, e.g. _plus('WB', 57.3)."""
    return f"{pair}+:GIH={wr}%:OH={wr - 2}%"


def _empty_situation(total_picks: int = 0) -> dict:
    return {
        "depth":       {c: 0.0 for c in WUBRG},
        "picks":       [],
        "openness":    {},
        "total_picks": total_picks,
    }


def _situation(depth: dict, total_picks: int, openness: dict | None = None,
               picks: list | None = None, pick_lookup: dict | None = None,
               lane_openness: dict | None = None) -> dict:
    full_depth = {c: depth.get(c, 0.0) for c in WUBRG}
    return {
        "depth":        full_depth,
        "picks":        picks or [],
        "openness":     openness or {},
        "lane_openness": lane_openness or {},
        "total_picks":  total_picks,
        "_pick_lookup": pick_lookup or {},
    }


def _picks_for_colors(color_counts: dict) -> tuple:
    """Create fake mono-color picks matching integer color counts.

    Returns (picks_list, pick_lookup) for injecting into a situation.
    All picks get position 0-N (early draft weight = 1.0).
    Used so _archetype_lock_penalty can count matching picks for relief.
    """
    picks = []
    pick_lookup = {}
    next_aid = 1000
    pick_pos = 0
    for color in WUBRG:
        count = color_counts.get(color, 0)
        for _ in range(count):
            aid = next_aid
            next_aid += 1
            pick_lookup[aid] = {
                "Name": f"Fake {color} Card",
                "Color": color,
                "GIH WR": "55.0%", "OH WR": "53.0%",
                "Best1": "", "Best2": "", "Best3": "", "Best4": "",
            }
            picks.append((0, pick_pos, aid))
            pick_pos += 1
    return picks, pick_lookup


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

# ── EARLY DRAFT ──────────────────────────────────────────────────────────────

def sc_early_flexible_vs_single_pool_small_diff():
    """
    Early pick 1 (empty pool).
    Flexible card (55% GIH, uniformly strong in 4 pools avg ~57.3%) beats
    a focused card (56% GIH, one excellent pool / one mediocre pool).

    With fit = avg_top2_uplift × multiplier:
      Flexible: uplift ≈ 2.35, tight spread → floor 0.85 → fit ≈ 2.0  → 57.0
      Focused:  uplift ≈ 0.5,  wide spread  → floor 0    → fit = 0    → 56.0
    Flexible wins because its FLOOR is still great, not just its ceiling.
    """
    flexible = _card("Flexible Card", "W", 55.0,
                     best1=_bi("WU", 57.5), best2=_bi("WB", 57.2),
                     best3=_bi("WR", 57.0), best4=_bi("WG", 56.8))
    focused  = _card("Focused Card", "W", 56.0,
                     best1=_bi("WB", 58.5), best2=_bi("WU", 54.5))
    return flexible, focused, _empty_situation(0), "A"


def sc_early_flexible_vs_single_pool_large_diff():
    """
    Early pick 1 (empty pool).
    Flexible card (55% GIH, excellent in 4 pools avg ~58.3%) beats a
    focused card (56.5% GIH) that has a sky-high ceiling but a terrible
    floor (avg_top2 = 56.5% = base GIH → uplift = 0).

    Flexible: uplift ≈ 3.35, tight spread → floor 0.85 → fit ≈ 2.85 → 57.85
    Focused:  uplift = 0 (avg_top2 matches GIH)  → fit = 0             → 56.5
    """
    flexible = _card("Flexible Card", "U", 55.0,
                     best1=_bi("UB", 58.5), best2=_bi("UR", 58.2),
                     best3=_bi("UG", 58.0), best4=_bi("WU", 57.8))
    focused  = _card("Focused Card", "U", 56.5,
                     best1=_bi("UB", 59.5), best2=_bi("UR", 53.5))
    return flexible, focused, _empty_situation(0), "A"


def sc_early_flexible_vs_locked_60pct():
    """
    Early pick 1 (empty pool).
    Flexible 55% card beats a 57% card that has only 1 bicolor pool and
    receives a -3 lock penalty.  The lock penalty (archetype lock) plus zero
    fit bonus (single-pool floor = 0) drops it below the flexible card.

    Locked: 57 + 0 fit - 3 lock = 54
    Flexible: 55 + ~0.81 fit = 55.81  → flexible wins
    """
    flexible = _card("Flexible Card", "R", 55.0,
                     best1=_bi("RW", 56.0), best2=_bi("RG", 55.8),
                     best3=_bi("UR", 55.5), best4=_bi("BR", 55.3))
    locked   = _card("Locked Card", "B", 57.0,
                     best1=_bi("WB", 59.0))   # single pool
    return flexible, locked, _empty_situation(0), "A"


def sc_early_flexible_vs_multicolor_only_60pct():
    """
    Early pick 1 (empty pool).
    Flexible 55% card beats a 57% card only playable in 3+-color decks.
    The multi-only card has no bicolor entries (uplift=0) and a lock penalty of
    -3, bringing it to 54.  The flexible card's pools around 56% give fit≈0.8.
    """
    flexible = _card("Flexible Card", "G", 55.0,
                     best1=_bi("BG", 56.2), best2=_bi("GU", 55.8),
                     best3=_bi("RG", 55.5), best4=_bi("WG", 55.3))
    multi_only = _card("Multi-Only Card", "WUB", 57.0,
                       best1=_plus("WU", 59.5), best2=_plus("WB", 59.0),
                       best3=_plus("UB", 58.5))
    return flexible, multi_only, _empty_situation(0), "A"


def sc_early_best_pool_wr_beats_spread():
    """
    Early pick 1 (empty pool).  Real data from a draft pick:
      Eager Glyphmage  W  GIH=56.0%  WB=56.9%, WR=56.0%, WR+=50.2%, WB+=48.7%
      Heated Argument  R  GIH=55.5%  UR=56.0%, WR=55.8%, UR+=54.7%, RG+=54.1%

    Current engine gives Heated Argument a higher score because its two bicolor
    pools have a tighter spread (0.2%) vs Glyphmage (0.9%), producing a higher
    not_committed_m floor.  However, Glyphmage's WORST bicolor pool (WR=56.0%)
    is still equal to Heated Argument's BEST pool (UR=56.0%), so Glyphmage is
    the correct pick regardless of which two-color deck you end up in.

    Fix direction: the floor should incorporate the absolute level of the pool
    WRs (e.g. use the lower bicolor WR relative to base GIH), not just the
    spread between them.
    """
    glyphmage = _card("Eager Glyphmage", "W", 56.0,
                      best1=_bi("WB", 56.9), best2=_bi("WR", 56.0),
                      best3=_plus("WR", 50.2), best4=_plus("WB", 48.7),
                      auto_plus=False)
    heated    = _card("Heated Argument", "R", 55.5,
                      best1=_bi("UR", 56.0), best2=_bi("WR", 55.8),
                      best3=_plus("UR", 54.7), best4=_plus("RG", 54.1),
                      auto_plus=False)
    return glyphmage, heated, _empty_situation(0), "A"


def sc_early_bicolor_flexible_beats_single_bicolor_colorless():
    """
    Early pick 1 (empty pool).
    A colorless 55% card with only 1 bicolor pool loses to a red 54% card
    with 2 strong bicolor pools (RW=57%, RB=56.5%).

    Colorless: 1 bicolor entry → single-entry floor=0 → fit=0. Score=55.
    Red: avg_top2=56.75, uplift=2.75, tight spread → floor=0.75 → fit≈2.06. Score=56.06.

    A card with two genuinely strong archetypes scores higher than a colorless
    card that lacks the bicolor breadth to earn a flexibility bonus.
    """
    colorless = _card("Colorless Artifact", "", 55.0,
                      best1=_bi("WU", 56.0),
                      best2=_plus("WU", 55.5), best3=_plus("WB", 55.0), best4=_plus("UB", 54.5),
                      ctype="Artifact Creature", auto_plus=False)
    red_card  = _card("Red Card", "R", 54.0,
                      best1=_bi("RW", 57.0), best2=_bi("RB", 56.5),
                      best3=_plus("RW", 54.0), best4=_plus("RB", 53.9),
                      auto_plus=False)
    return colorless, red_card, _empty_situation(0), "B"


# ── MID DRAFT ────────────────────────────────────────────────────────────────

def sc_mid_flexible_vs_in_color():
    """
    Mid-draft: committed to WB (depth W=5, B=5, total=10).
    A flexible 55% card that works everywhere but outside WB should now
    lose against a 55% card that fits well in WB.
    """
    in_color  = _card("In-Color Card", "WB", 55.0,
                      best1=_bi("WB", 57.0), best2=_bi("WU", 55.5))
    off_color = _card("Off-Color Flex", "RG", 55.0,
                      best1=_bi("RG", 56.5), best2=_bi("GU", 56.0),
                      best3=_bi("BR", 55.5), best4=_bi("WR", 55.2))
    picks, plookup = _picks_for_colors({"W": 5, "B": 5})
    sit = _situation({"W": 5.0, "B": 5.0}, total_picks=10,
                     picks=picks, pick_lookup=plookup)
    return in_color, off_color, sit, "A"


def sc_mid_bicolor_one_color_in_pool():
    """
    Mid-draft: deep in W (depth W=5), a little B (B=2).
    A WB bicolor 58% card should score as well as a pure-W 55% card.
    """
    bicolor_wb = _card("Bicolor WB Card", "WB", 58.0,
                       best1=_bi("WB", 59.5), best2=_bi("WU", 57.5))
    mono_w     = _card("Mono-W Card", "W", 55.0,
                       best1=_bi("WB", 57.0), best2=_bi("WU", 56.5),
                       best3=_bi("WR", 56.0), best4=_bi("WG", 55.8))
    picks, plookup = _picks_for_colors({"W": 5, "B": 2})
    sit = _situation({"W": 5.0, "B": 2.0}, total_picks=8,
                     picks=picks, pick_lookup=plookup)
    return bicolor_wb, mono_w, sit, "A"


def sc_mid_multicolor_pool_recognized():
    """
    Mid-draft: we're going wide (W=3, U=3, B=3 — 9 picks spread across 3 colors).
    A 3+-color card (57% GIH) should beat a mono-color card committed to only one
    of our colors (55% GIH) once pool breadth is established.
    """
    multi_card = _card("3-Color Card", "WUB", 57.0,
                       best1=_plus("WUB", 58.5), best2=_plus("UBR", 58.0),
                       best3=_plus("WUR", 57.5))
    mono_card  = _card("Mono-B Card", "B", 55.0,
                       best1=_bi("WB", 57.0), best2=_bi("UB", 56.5),
                       best3=_bi("BR", 56.0), best4=_bi("BG", 55.8))
    sit = _situation({"W": 3.0, "U": 3.0, "B": 3.0}, total_picks=9)
    return multi_card, mono_card, sit, "A"


# ── LATE DRAFT ───────────────────────────────────────────────────────────────

def sc_late_committed_pair_vs_flexible():
    """
    Late draft (end of pack 1): deep WB (W=8, B=8, 16 picks).
    A 53% WB card beats a 55% flexible card that works everywhere.
    """
    wb_card   = _card("WB Committed", "WB", 53.0,
                      best1=_bi("WB", 56.0), best2=_bi("WU", 52.5))
    flex_card = _card("Flexible Card", "U", 55.0,
                      best1=_bi("UB", 56.5), best2=_bi("UR", 56.2),
                      best3=_bi("UG", 56.0), best4=_bi("WU", 55.8))
    picks, plookup = _picks_for_colors({"W": 8, "B": 8})
    sit = _situation({"W": 8.0, "B": 8.0}, total_picks=16,
                     picks=picks, pick_lookup=plookup)
    return wb_card, flex_card, sit, "A"


def sc_late_committed_pair_vs_off_color_high_wr():
    """
    Late draft: deep WB (W=8, B=8, 16 picks).
    A 53% WB card beats a 58% card that's completely off-color (RG).

    The off-color card has a 5-point raw GIH advantage but zero color overlap
    with the committed WB pool.  The pool rejection penalty (-3.0 at full
    commitment, 0 overlap) brings its effective score below the WB card.
    """
    wb_card   = _card("WB Committed", "WB", 53.0,
                      best1=_bi("WB", 56.0), best2=_bi("WU", 52.5))
    off_card  = _card("Off-Color Card", "RG", 58.0,
                      best1=_bi("RG", 60.0), best2=_bi("GU", 58.5))
    picks, plookup = _picks_for_colors({"W": 8, "B": 8})
    sit = _situation({"W": 8.0, "B": 8.0}, total_picks=16,
                     picks=picks, pick_lookup=plookup)
    return wb_card, off_card, sit, "A"


def sc_late_denied_color_partner_valued():
    """
    Mid-draft: WR commitment with R shallower than W due to denial pressure.
    Depth W=4, R=3 (7 picks: 4 mono-W + 3 mono-R, modelling a pool where W is
    ahead but RW archetype is clearly intended).

    Red RW card (55.5% GIH, best pair RW): best pair aligns with both pool colors
    → pair_depth_commit = min(R=3,W=4)/4 = 0.75.
    White card (55% GIH, best pair WB): best pair WB has B=0 depth
    → pair_depth_commit = 0, only the high-floor from flexibility carries it.

    Red card should score higher because its best pair directly benefits from
    the committed R+W depth, while white's best pair (WB) has no B support.
    """
    red_card   = _card("Red RW Card", "R", 55.5,
                       best1=_bi("RW", 57.5), best2=_bi("UR", 55.0))
    white_card = _card("White WB/WR Card", "W", 55.0,
                       best1=_bi("WB", 57.0), best2=_bi("RW", 56.5),
                       best3=_bi("WU", 56.0), best4=_bi("WG", 55.5))
    picks, plookup = _picks_for_colors({"W": 4, "R": 3})
    sit = _situation({"W": 4.0, "R": 3.0}, total_picks=7,
                     picks=picks, pick_lookup=plookup)
    return red_card, white_card, sit, "A"


# ── OPENNESS TESTS (expected to fail initially) ───────────────────────────────

def sc_openness_tiebreak_early():
    """
    Early draft, two cards ~1 score apart (56 vs 55 raw).
    Openness strongly favors the lower-WR card's color.
    With enough openness, the 55% card should win.
    Mark: XFAIL — openness algorithm may not be strong enough yet.
    """
    better_wr = _card("Higher WR Card", "B", 56.0,
                      best1=_bi("WB", 57.5), best2=_bi("UB", 56.5),
                      best3=_bi("BR", 56.0), best4=_bi("BG", 55.8))
    open_card = _card("Open Color Card", "R", 55.0,
                      best1=_bi("RW", 56.5), best2=_bi("UR", 56.0),
                      best3=_bi("BR", 55.5), best4=_bi("RG", 55.3))
    sit = _situation({"W": 0.0, "U": 0.0, "B": 0.0, "R": 0.0, "G": 0.0},
                     total_picks=0,
                     openness={"B": 0.2, "R": 1.8})   # R is very open
    return better_wr, open_card, sit, "B"


def sc_openness_pivot_from_wb_to_rw():
    """
    Mid-draft: started WB (W=4, B=3).
    R is now very open, we have some W.  RW is a strong archetype (56.9% in SOS).
    A good R card fitting RW should beat a B card if B's openness is poor.
    Mark: XFAIL — signal routing may not be strong enough yet.
    """
    r_card = _card("Red RW Card", "R", 56.0,
                   best1=_bi("RW", 58.0), best2=_bi("UR", 56.0),
                   best3=_bi("BR", 55.5), best4=_bi("RG", 55.0))
    b_card = _card("Black WB Card", "B", 56.0,
                   best1=_bi("WB", 58.0), best2=_bi("UB", 56.5),
                   best3=_bi("BR", 55.5), best4=_bi("BG", 55.0))
    sit = _situation({"W": 4.0, "B": 3.0},
                     total_picks=7,
                     openness={"R": 2.5, "W": 1.0, "B": 0.1})
    return r_card, b_card, sit, "A"


def sc_openness_no_pivot_wb_to_gu_weak_signal():
    """
    Mid-draft: committed WB (W=4, B=4).
    GU is only slightly more open than WB.  Should NOT pivot.
    A WB card should still beat a GU card.
    Mark: XFAIL — may require stronger signal discrimination.
    """
    wb_card = _card("WB Card", "WB", 56.0,
                    best1=_bi("WB", 58.0), best2=_bi("WU", 56.5))
    gu_card = _card("GU Card", "GU", 56.0,
                    best1=_bi("GU", 57.5), best2=_bi("UB", 56.5))
    sit = _situation({"W": 4.0, "B": 4.0},
                     total_picks=8,
                     openness={"G": 1.2, "U": 1.0, "W": 0.6, "B": 0.4})
    return wb_card, gu_card, sit, "A"


# ── ADDITIONAL COVERAGE ──────────────────────────────────────────────────────

def sc_colorless_unaffected_by_pool_pressure():
    """
    Colorless (artifact) card vs same-GIH colored card in a deeply committed
    off-color pool (U=5, B=5).  The colored R card gets its generic floor
    reduced by pool pressure; the colorless card does not (card_colors is empty).
    Both have 4 pools so no lock penalty.  Expect colorless > colored.
    """
    colorless = _card("Colorless Artifact", "", 56.0,
                      best1=_bi("WB", 57.5), best2=_bi("GU", 57.0),
                      best3=_bi("UR", 56.5), best4=_bi("RG", 56.0),
                      ctype="Artifact Creature")
    colored_r = _card("Red Card", "R", 56.0,
                      best1=_bi("WB", 57.5), best2=_bi("GU", 57.0),
                      best3=_bi("UR", 56.5), best4=_bi("RG", 56.0))
    sit = _situation({"U": 5.0, "B": 5.0}, total_picks=10)
    return colorless, colored_r, sit, "A"


def sc_colorless_competes_in_committed_pool():
    """
    In a fully committed RW pool (R=5, W=5, 10 picks), a colorless artifact
    (GIH=55.1, RW pool WR=56.0) should beat a red card (GIH=55.5, RW pool
    WR=55.9).

    At blend=10/8=1.0 the effective GIH of the artifact becomes 56.0 and the
    red card becomes 55.9, reversing the raw GIH ordering.
    Both get full fit (committed_m=1.0 for RW card, colorless always fits),
    so the blend is the deciding factor.
    """
    colorless = _card("Colorless Artifact", "", 55.1,
                      best1=_bi("RW", 56.0), best2=_bi("GU", 55.5),
                      best3=_bi("WB", 55.2), best4=_bi("BR", 55.0),
                      ctype="Artifact Creature")
    red_card  = _card("Red RW Card", "R", 55.5,
                      best1=_bi("RW", 55.9), best2=_bi("UR", 55.5),
                      best3=_bi("BR", 55.2), best4=_bi("RG", 55.0))
    picks, plookup = _picks_for_colors({"R": 5, "W": 5})
    sit = _situation({"R": 5.0, "W": 5.0}, total_picks=10,
                     picks=picks, pick_lookup=plookup)
    return colorless, red_card, sit, "A"


def sc_three_pool_lighter_lock_than_two_pool():
    """
    Early draft, empty pool, same GIH.  A 3-pool card (lock base -1.5) beats
    a 2-pool card (lock base -3.0).  Spread is identical so fit is the same;
    only the lock differs.

    auto_plus=False keeps pool counts exact so the lock difference is isolated.
    """
    card_3pool = _card("Three-Pool Card", "W", 56.0,
                       best1=_bi("WB", 57.5), best2=_bi("WU", 57.0),
                       best3=_bi("WR", 56.5), auto_plus=False)
    card_2pool = _card("Two-Pool Card", "W", 56.0,
                       best1=_bi("WB", 57.5), best2=_bi("WU", 57.0),
                       auto_plus=False)
    return card_3pool, card_2pool, _empty_situation(0), "A"


# --- Cross-situation scenarios (same card, different context) ---

def sc_pool_pressure_decays_in_off_color_pool():
    """
    Same generic R card: scores higher in an empty pool than in a deeply
    committed UB pool.  Validates that pool pressure reduces the generic
    floor as the pool diverges from the card's colors.
    """
    card = _card("Generic R Card", "R", 55.5,
                 best1=_bi("RW", 57.5), best2=_bi("BR", 56.5),
                 best3=_bi("UR", 56.0), best4=_bi("RG", 55.5))
    sit_empty = _empty_situation(0)
    picks_ub, plookup_ub = _picks_for_colors({"U": 5, "B": 5})
    sit_ub = _situation({"U": 5.0, "B": 5.0}, total_picks=10,
                        picks=picks_ub, pick_lookup=plookup_ub)
    return card, sit_empty, card, sit_ub


def sc_multicolor_card_gains_with_breadth():
    """
    Same 3-color WUB card scores higher in a matching WUB pool (W=4,U=4,B=4)
    than in a bicolor WB pool (W=5,B=5).  In the WB pool, pair_depth_commit
    collapses to 0 because U=0; in the WUB pool it reaches 1.0, giving a
    full 0.5 our_commit via the breadth factor → fit +1.65 vs fit +0.
    """
    card = _card("3-Color WUB Card", "WUB", 57.0,
                 best1=_plus("WUB", 59.0), best2=_plus("WUR", 58.5),
                 best3=_plus("UBR", 58.0))
    picks_wb,  plookup_wb  = _picks_for_colors({"W": 5, "B": 5})
    picks_wub, plookup_wub = _picks_for_colors({"W": 4, "U": 4, "B": 4})
    sit_bicolor  = _situation({"W": 5.0, "B": 5.0}, total_picks=10,
                               picks=picks_wb,  pick_lookup=plookup_wb)
    sit_tricolor = _situation({"W": 4.0, "U": 4.0, "B": 4.0}, total_picks=12,
                               picks=picks_wub, pick_lookup=plookup_wub)
    return card, sit_tricolor, card, sit_bicolor


# ── LAND SLOT TESTS ──────────────────────────────────────────────────────────

def _land_card(name: str, best1="", best2="", best3="", best4="",
               gih: float = 55.0) -> dict:
    """Build a minimal colorless dual-land card dict for scoring."""
    return {
        "Name":     name,
        "Color":    "",
        "Type":     "Land",
        "CMC":      "0",
        "GIH WR":   f"{gih}%",
        "OH WR":    f"{gih - 2.0}%",
        "ManaCost": "",
        "Best1":    best1,
        "Best2":    best2,
        "Best3":    best3,
        "Best4":    best4,
        "ALSA":     "3.0",
    }


# Fake Paradox Gardens: colorless land best in UR (bicolor) and UR+ (3-color)
_PARADOX_GARDENS = _land_card(
    "Paradox Gardens",
    best1=_bi("UR", 57.5),
    best2=_plus("UR", 58.0),
)


def sc_slot_land_beats_weak_card_in_matching_pool():
    """
    Paradox Gardens (55% GIH) in a UR pool should comfortably beat a card
    with only 20% GIH WR, even though 20% cards don't exist in practice.

    Gardens in UR pool (depth U=5, R=5):
      land_modifier bonus_A = -3 + 5*(5/8) = +0.125
      score ≈ 55 + 0.125 + fit_from_pool_blend = ~59.4

    Weak card (GIH=20%): score ≈ 20 (minus pool rejection etc.)
    """
    weak_card = _card("Terrible Card", "B", 20.0,
                      best1=_bi("WB", 22.0), best2=_bi("UB", 21.0))
    picks, plookup = _picks_for_colors({"U": 5, "R": 5})
    sit = _situation({"U": 5.0, "R": 5.0}, total_picks=10,
                     picks=picks, pick_lookup=plookup)
    return _PARADOX_GARDENS, weak_card, sit, "A"


def sc_slot_land_loses_in_wrong_pool():
    """
    Paradox Gardens in a BG pool should lose to a mediocre 53% BG card.

    Gardens in BG pool (depth B=5, G=5):
      land_modifier bonus_A = -3 (no U or R picks → min_surplus=0)
      score = 55 - 3 = 52

    BG card (GIH=53%) in BG pool: score ≈ 53 + some fit ≥ 53
    """
    bg_card = _card("BG Card", "BG", 53.0,
                    best1=_bi("BG", 55.5), best2=_bi("GU", 54.5))
    picks, plookup = _picks_for_colors({"B": 5, "G": 5})
    sit = _situation({"B": 5.0, "G": 5.0}, total_picks=10,
                     picks=picks, pick_lookup=plookup)
    return bg_card, _PARADOX_GARDENS, sit, "A"


def sc_slot_land_good_in_three_color_ur_pool():
    """
    Paradox Gardens in a 3-color UBR pool (has both U and R) should score
    meaningfully higher than in a WBG pool (neither U nor R).

    UBR pool (U=3, B=4, R=3): land surplus[U]=3, surplus[R]=3
      bonus_A = -3 + 5*(3/8) = -1.125 → score ≈ 58.75

    WBG pool (W=3, B=4, G=3): surplus[U]=0, surplus[R]=0
      bonus_A = -3.0 → score = 52.0
    """
    picks_ubr, plookup_ubr = _picks_for_colors({"U": 3, "B": 4, "R": 3})
    picks_wbg, plookup_wbg = _picks_for_colors({"W": 3, "B": 4, "G": 3})
    sit_ubr = _situation({"U": 3.0, "B": 4.0, "R": 3.0}, total_picks=10,
                         picks=picks_ubr, pick_lookup=plookup_ubr)
    sit_wbg = _situation({"W": 3.0, "B": 4.0, "G": 3.0}, total_picks=10,
                         picks=picks_wbg, pick_lookup=plookup_wbg)
    return _PARADOX_GARDENS, sit_ubr, _PARADOX_GARDENS, sit_wbg


def sc_openness_card_aware_good_lane_beats_bad_lane():
    """
    Card-aware openness: open signal only benefits a card in lanes where it
    actually overperforms its base GIH.

    Same Rubble-Rouser-like card (R, GIH=57.9, WR=59.3%, UR=55.4%) in two
    situations:
    • sit_WR_open : WR lane signal = 2.0  → WR qualifies (59.3 > 57.9, +1.4),
                    UR excluded (55.4 < 57.9).  openness_contrib ≈ 2.0.
    • sit_UR_open : UR lane signal = 2.0  → UR excluded (55.4 < 57.9),
                    WR qualifies but signal = 0.  openness_contrib = 0.

    The card scores significantly higher when its *good* archetype (WR) is open
    than when only its *underperforming* archetype (UR) is open.  With the old
    per-color approach both situations gave the same (wrong) openness bonus.
    """
    rubble_like = _card("Rubble Rouser", "R", 57.9,
                        best1=_bi("WR", 59.3), best2=_bi("UR", 55.4),
                        auto_plus=False)
    sit_wr_open = _situation({}, total_picks=0, lane_openness={"WR": 2.0})
    sit_ur_open = _situation({}, total_picks=0, lane_openness={"UR": 2.0})
    return rubble_like, sit_wr_open, rubble_like, sit_ur_open


def sc_screenshot_wb_pool_ignores_off_color_r():
    """
    Reproduces the screenshot scenario (Pack 4 Pick 4, deep WB pool).

    Pool: W=17, B=8, R=2, U=1, G=3 (W:14 mono + WB:3 bicolor + UG:1 bicolor etc.)
    WB card (Scolding Administrator-like, GIH=60.4) should beat an off-color
    R card (Impractical Joke-like, GIH=58.5) by a large margin.

    Key mechanisms:
    • WB card: full pair-min blend (min(17,8)/4 = 1.0) → effective GIH ≈ 61.3;
      matched_uplift from WB ⊆ committed pool.
    • R card: restricted blend (pair_min = min(17,2)/4 = 0.5); lock=-2.0 because
      n_pools≥4 but no bicolor entry fits the top committed pair {W,B}.
    """
    wb_card = _card("WB Commander", "WB", 60.4,
                    best1=_bi("WB", 61.3), best2=_plus("WB", 52.1),
                    auto_plus=False)
    r_card  = _card("Impractical Joke", "R", 58.5,
                    best1=_bi("UR", 57.8), best2=_bi("WR", 59.5),
                    best3=_plus("UR", 57.9), best4=_plus("WR", 55.8),
                    auto_plus=False)
    picks, plookup = _picks_for_colors({"W": 17, "B": 8, "R": 2, "U": 1, "G": 3})
    sit = _situation({"W": 17.0, "B": 8.0, "R": 2.0, "U": 1.0, "G": 3.0},
                     total_picks=31, picks=picks, pick_lookup=plookup)
    return wb_card, r_card, sit, "A"


def sc_screenshot_wb_pool_ignores_off_color_u():
    """
    Same screenshot scenario — deep WB pool vs high-GIH off-color U card.

    U card (Pensive Professor-like, GIH=58.6) has 4 pools (UR:59.2, UG:58.8,
    UG+:57.5, UR+:57.6) — n_pools≥4 — but none fit committed top pair {W,B}.
    Off-archetype lock penalty: lock=-2.0.  Pool blend limited (U depth=1,
    pair_min=1 → blend=0.25), so effective GIH barely moves.
    """
    wb_card = _card("WB Commander", "WB", 60.4,
                    best1=_bi("WB", 61.3), best2=_plus("WB", 52.1),
                    auto_plus=False)
    u_card  = _card("Pensive Professor", "U", 58.6,
                    best1=_bi("UR", 59.2), best2=_bi("UG", 58.8),
                    best3=_plus("UG", 57.5), best4=_plus("UR", 57.6),
                    auto_plus=False)
    picks, plookup = _picks_for_colors({"W": 17, "B": 8, "R": 2, "U": 1, "G": 3})
    sit = _situation({"W": 17.0, "B": 8.0, "R": 2.0, "U": 1.0, "G": 3.0},
                     total_picks=31, picks=picks, pick_lookup=plookup)
    return wb_card, u_card, sit, "A"


_SCENARIOS = [
    # (fn, is_xfail)
    (sc_early_flexible_vs_single_pool_small_diff,    False),
    (sc_early_flexible_vs_single_pool_large_diff,    False),
    (sc_early_flexible_vs_locked_60pct,              False),
    (sc_early_flexible_vs_multicolor_only_60pct,     False),
    (sc_early_bicolor_flexible_beats_single_bicolor_colorless, False),
    (sc_early_best_pool_wr_beats_spread,             False),
    (sc_mid_flexible_vs_in_color,                    False),
    (sc_mid_bicolor_one_color_in_pool,               False),
    (sc_mid_multicolor_pool_recognized,              True),   # XFAIL: engine bug - multi-only relief checks for colors outside the wedge instead of inside it
    (sc_late_committed_pair_vs_flexible,             False),
    (sc_late_committed_pair_vs_off_color_high_wr,    False),
    (sc_late_denied_color_partner_valued,            False),
    (sc_openness_tiebreak_early,                     True),   # XFAIL
    (sc_openness_pivot_from_wb_to_rw,                True),   # XFAIL
    (sc_openness_no_pivot_wb_to_gu_weak_signal,      True),   # XFAIL
    # Additional two-card same-situation tests
    (sc_colorless_unaffected_by_pool_pressure,        False),
    (sc_colorless_competes_in_committed_pool,          False),
    (sc_three_pool_lighter_lock_than_two_pool,         False),
    # Land slot
    (sc_slot_land_beats_weak_card_in_matching_pool,   False),
    (sc_slot_land_loses_in_wrong_pool,                False),
    # Screenshot regression: deep WB pool must outrank high-GIH off-color cards
    (sc_screenshot_wb_pool_ignores_off_color_r,       False),
    (sc_screenshot_wb_pool_ignores_off_color_u,       False),
]


# Cross-situation scenarios: fn() returns (card_a, sit_a, card_b, sit_b)
# Always asserts score(card_a, sit_a) > score(card_b, sit_b)
_CROSS_SCENARIOS = [
    (sc_pool_pressure_decays_in_off_color_pool,  False),
    (sc_multicolor_card_gains_with_breadth,      False),
    # Land slot cross-situation
    (sc_slot_land_good_in_three_color_ur_pool,   False),
    # Card-aware openness: good-lane-open beats bad-lane-open for same card
    (sc_openness_card_aware_good_lane_beats_bad_lane, False),
]


# Filter checks: (description, bool_expr_fn) — verify card identification logic
_FILTER_CHECKS = [
    ("basic lands are identified as land-slot cards",
     lambda: all(_is_land_slot({"Name": n, "Type": "Basic Land"})
                 for n in ("Plains", "Island", "Swamp", "Mountain", "Forest"))),
    ("basic lands filtered by name via _is_basic_land",
     lambda: all(_is_basic_land({"Name": n, "Type": "Land"})
                 for n in ("Plains", "Island", "Swamp", "Mountain", "Forest"))),
    ("basic lands filtered by type via _is_basic_land",
     lambda: _is_basic_land({"Name": "Plains", "Type": "Basic Land — Plains"})),
    ("non-basic land NOT filtered by _is_basic_land",
     lambda: not _is_basic_land({"Name": "Paradox Gardens", "Type": "Land"})),
    ("normal spell NOT filtered by _is_basic_land",
     lambda: not _is_basic_land({"Name": "Lightning Bolt", "Type": "Instant"})),
    ("SOS slot lands are identified as land-slot cards",
     lambda: all(_is_land_slot({"Name": n, "Type": "Land"})
                 for n in ("Fields of Strife", "Forum of Amity",
                            "Paradox Gardens", "Spectacle Summit", "Titan's Grave"))),
    ("non-slot lands are NOT identified as land-slot cards",
     lambda: not _is_land_slot({"Name": "Restless Bivouac", "Type": "Land"})),
    ("normal spells are NOT identified as land-slot cards",
     lambda: not _is_land_slot({"Name": "Lightning Bolt", "Type": "Instant"})),
]


def _run_one(fn, is_xfail, counters, verbose):
    passed, failed, xfailed, xpassed = counters
    ok, label = fn(verbose)
    if is_xfail:
        if ok:
            xpassed += 1
            print(f"    (XPASS — was expected to fail, now passes!)")
        else:
            xfailed += 1
            print(f"    (xfail — known limitation, not counted as failure)")
    else:
        if ok:
            passed += 1
        else:
            failed += 1
    return passed, failed, xfailed, xpassed


def run_scoring_tests(verbose: bool = False) -> int:
    passed = failed = xfailed = xpassed = 0
    print("=" * 60)
    print("Scoring scenario tests")
    print("=" * 60)
    for fn, is_xfail in _SCENARIOS:
        card_a, card_b, situation, expect = fn()
        ok = _run_scenario(fn.__name__, card_a, card_b, situation, expect,
                           verbose=verbose)
        if is_xfail:
            if ok:
                xpassed += 1
                print(f"    (XPASS — was expected to fail, now passes!)")
            else:
                xfailed += 1
                print(f"    (xfail — known limitation, not counted as failure)")
        else:
            if ok:
                passed += 1
            else:
                failed += 1

    print("\n" + "-" * 60)
    print("Cross-situation scenarios")
    print("-" * 60)
    for fn, is_xfail in _CROSS_SCENARIOS:
        card_a, sit_a, card_b, sit_b = fn()
        ok = _run_cross(fn.__name__, card_a, sit_a, card_b, sit_b, verbose=verbose)
        if is_xfail:
            if ok:
                xpassed += 1
                print(f"    (XPASS — was expected to fail, now passes!)")
            else:
                xfailed += 1
                print(f"    (xfail — known limitation, not counted as failure)")
        else:
            if ok:
                passed += 1
            else:
                failed += 1

    total = passed + failed
    print("\n" + "=" * 60)
    print(f"{passed}/{total} required tests passed"
          + (" OK" if not failed else " -- FAILURES ABOVE"))
    if xfailed or xpassed:
        print(f"{xfailed} known-failing (xfail)  |  {xpassed} newly passing (xpass)")
    print("=" * 60)
    return 1 if failed else 0


def run_filter_checks() -> int:
    """Run direct assertions on card-identification helpers."""
    passed = failed = 0
    print("\n" + "-" * 60)
    print("Filter / identification checks")
    print("-" * 60)
    for desc, fn in _FILTER_CHECKS:
        print(f"\n  Check: {desc}")
        try:
            ok = bool(fn())
        except Exception as exc:
            ok = False
            print(f"    ERROR: {exc}")
        print(f"    {'pass' if ok else 'FAIL'}")
        if ok:
            passed += 1
        else:
            failed += 1
    print(f"\n{passed}/{passed + failed} filter checks passed"
          + (" OK" if not failed else " -- FAILURES ABOVE"))
    return 1 if failed else 0


if __name__ == "__main__":
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    rc1 = run_scoring_tests(verbose=verbose)
    rc2 = run_filter_checks()
    sys.exit(rc1 or rc2)
