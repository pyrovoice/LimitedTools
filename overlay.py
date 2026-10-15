#!/usr/bin/env python3
"""
overlay.py -- Always-on-top draft score overlay for MTG Arena.

Displays a floating, semi-transparent, borderless window with card scores
arranged in the same grid order as the Arena draft pack (7 cards per row).
Position it over your Arena window so the scores sit below each card.

Requirements (all optional, gracefully degraded if absent):
    pip install pywin32     # auto-detect + follow Arena window position

Usage:
    python overlay.py                        # use default CSV + MTGA log
    python overlay.py carddata_enriched.csv  # explicit CSV
    python overlay.py --log path/to/log      # explicit log path

Or launch from limitedtool.py via the "Open Overlay" button.
"""

import ctypes
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

# Windows click-through constants (no pywin32 needed)
_GWL_EXSTYLE       = -20
_WS_EX_TRANSPARENT = 0x00000020

# ---------------------------------------------------------------------------
# Shared imports from limitedtool
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from limitedtool import (
    DEFAULT_CSV, LOG_PATH, RAW_CSV, SET_DATA, POLL_MS,
    load_enriched, load_arena_ids, load_set_data,
    compute_density_baseline,
    count_color_depth, count_pair_synergy, card_score,
    DraftState, LogReader,
    _sort_color, _fmt_best, WUBRG, _arena_sort_key, _is_land_slot,
)

# Optional pywin32 for Arena window detection
try:
    import win32gui  # type: ignore
    import win32con  # type: ignore
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

# ---------------------------------------------------------------------------
# Layout / style constants
# ---------------------------------------------------------------------------

COLS          = 5       # cards per row (Arena draft = 3 × 5)
CELL_W        = 144     # px width per card badge  (+ 3px pad each side = 150px per slot)
CELL_H        = 24      # px height per card badge (+ 3px pad each side = 30px per row)
BADGE_PAD_X   = 3       # px between badges horizontally
BADGE_PAD_Y   = 80     # px between badges vertically (card row spacing)

# Snap-to-Arena offsets: distance from Arena window's top-left corner
# to where the overlay's top-left corner should sit.
SNAP_X_OFFSET = 120     # px right of Arena left edge
SNAP_Y_OFFSET = 80      # px below Arena top edge
BAR_H         = 26      # px header drag bar height
DEFAULT_ALPHA = 0.88    # window transparency (0 = invisible, 1 = opaque)

BG_DARK   = "#010101"   # key colour — made fully transparent via -transparentcolor
BAR_BG    = "#222222"
BAR_FG    = "#cccccc"
SCORE_FG  = "#ffffff"
NAME_FG   = "#dddddd"

# Score gap colours (gap = best_score - this_score)
def _gap_bg(gap: float) -> str:
    if gap < 0.5:  return "#1b5e20"   # #1 pick
    if gap < 2.0:  return "#2e7d32"   # close 2nd
    if gap < 4.0:  return "#558b2f"   # reasonable
    if gap < 7.0:  return "#e65100"   # below avg
    return             "#7f0000"      # clearly worse

# Arena window titles to search for
ARENA_TITLES = ["MTGA", "MTG Arena", "Magic: The Gathering Arena"]


def _find_arena_rect() -> tuple[int, int, int, int] | None:
    """Return (x, y, w, h) of the Arena window, or None if not found."""
    if not HAS_WIN32:
        return None
    for title in ARENA_TITLES:
        hwnd = win32gui.FindWindow(None, title)
        if hwnd and win32gui.IsWindowVisible(hwnd):
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            if r - l > 200 and b - t > 200:
                return l, t, r - l, b - t
    return None


# ---------------------------------------------------------------------------
# Overlay window
# ---------------------------------------------------------------------------

class DraftOverlay:
    """Borderless, always-on-top overlay window that shows draft pack scores."""

    def __init__(self, csv_path: Path = DEFAULT_CSV, log_path: Path = LOG_PATH):
        self._csv_path  = csv_path
        self._log_path  = log_path
        self._arena     = load_arena_ids(RAW_CSV)
        self._cards     = load_enriched(csv_path, RAW_CSV)
        self._density_baseline, self._density_threshold = compute_density_baseline(self._cards)
        self._set_pair_wr, _                            = load_set_data(SET_DATA)
        self._state     = DraftState()
        self._reader    = LogReader(log_path)
        self._alpha     = DEFAULT_ALPHA
        self._following = False   # True while "snap to Arena" is active
        self._locked    = False   # True = click-through when mouse outside header
        self._drag_x    = 0
        self._drag_y    = 0
        self._build()
        self._poll()
        self._update_click_through()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build(self):
        self.root = tk.Tk()
        self.root.title("Draft Overlay")
        self.root.configure(bg=BG_DARK)
        self.root.overrideredirect(True)          # no title bar / frame
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self._alpha)
        self.root.attributes("-transparentcolor", BG_DARK)

        # --- Drag / header bar ---
        bar = tk.Frame(self.root, bg=BAR_BG, height=BAR_H, cursor="fleur")
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)

        self._title_lbl = tk.Label(
            bar, text="Draft Overlay — waiting…",
            bg=BAR_BG, fg=BAR_FG,
            font=("Segoe UI", 8, "bold"),
            anchor="w", padx=6,
        )
        self._title_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Transparency slider in header
        tk.Label(bar, text="α", bg=BAR_BG, fg=BAR_FG,
                 font=("Segoe UI", 7)).pack(side=tk.LEFT)
        self._alpha_scale = tk.Scale(
            bar, from_=30, to=100, orient=tk.HORIZONTAL,
            length=60, sliderlength=10, showvalue=False,
            bg=BAR_BG, fg=BAR_FG, troughcolor="#444",
            highlightthickness=0, bd=0,
            command=self._on_alpha_change,
        )
        self._alpha_scale.set(int(DEFAULT_ALPHA * 100))
        self._alpha_scale.pack(side=tk.LEFT, padx=(0, 4))

        # Snap-to-Arena button
        self._snap_btn = tk.Button(
            bar, text="⊞",
            bg=BAR_BG, fg=BAR_FG,
            font=("Segoe UI", 9),
            relief=tk.FLAT, bd=0, padx=4,
            command=self._snap_to_arena,
        )
        self._snap_btn.pack(side=tk.LEFT)

        # Lock / unlock click-through button
        self._lock_btn = tk.Button(
            bar, text="🔓",
            bg=BAR_BG, fg=BAR_FG,
            font=("Segoe UI", 9),
            relief=tk.FLAT, bd=0, padx=4,
            command=self._toggle_lock,
        )
        self._lock_btn.pack(side=tk.RIGHT)

        # Close button
        tk.Button(
            bar, text="✕",
            bg=BAR_BG, fg="#ff6b6b",
            font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, bd=0, padx=6,
            command=self.root.destroy,
        ).pack(side=tk.RIGHT)

        # Drag bindings on bar and title
        for w in (bar, self._title_lbl):
            w.bind("<ButtonPress-1>",   self._drag_start)
            w.bind("<B1-Motion>",       self._drag_motion)

        # --- Card grid ---
        self._grid_frame = tk.Frame(self.root, bg=BG_DARK, padx=BADGE_PAD_X, pady=BADGE_PAD_Y)
        self._grid_frame.pack(fill=tk.BOTH, expand=True)

        # Initial placeholder
        self._set_placeholder("Waiting for pack…")

        # Position: try to snap to Arena, else center on screen
        self.root.update_idletasks()
        self._snap_to_arena()

    # ------------------------------------------------------------------
    # Drag support
    # ------------------------------------------------------------------

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_motion(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    def _on_alpha_change(self, val):
        self._alpha = int(val) / 100.0
        self.root.attributes("-alpha", self._alpha)

    def _set_click_through(self, enabled: bool):
        """Enable/disable Windows click-through (WS_EX_TRANSPARENT)."""
        if not sys.platform.startswith("win"):
            return
        try:
            hwnd  = self.root.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            if enabled:
                style |= _WS_EX_TRANSPARENT
            else:
                style &= ~_WS_EX_TRANSPARENT
            ctypes.windll.user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style)
        except Exception:
            pass

    def _toggle_lock(self):
        """Toggle between interactive mode and click-through (draft) mode."""
        self._locked = not self._locked
        self._lock_btn.config(
            text="🔒" if self._locked else "🔓",
            fg="#ffcc44" if self._locked else BAR_FG,
        )

    def _update_click_through(self):
        """Poll every 100 ms: pass clicks to Arena except when mouse is in the header bar."""
        if self._locked:
            try:
                mx, my = self.root.winfo_pointerxy()
                rx = self.root.winfo_x()
                ry = self.root.winfo_y()
                rw = self.root.winfo_width()
                in_header = (rx <= mx <= rx + rw) and (ry <= my <= ry + BAR_H)
                self._set_click_through(not in_header)
            except Exception:
                pass
        else:
            self._set_click_through(False)
        self.root.after(100, self._update_click_through)

    def _snap_to_arena(self):
        """Try to position the overlay over the Arena draft card area."""
        rect = _find_arena_rect()
        if rect:
            ax, ay, aw, ah = rect
            ox = ax + SNAP_X_OFFSET
            oy = ay + SNAP_Y_OFFSET
            self.root.geometry(f"+{ox}+{oy}")
            self._snap_btn.config(fg="#aaffaa")
            msg = f"win x={ax} y={ay} w={aw} h={ah} | overlay x={ox} y={oy}"
            if HAS_WIN32:
                for title in ARENA_TITLES:
                    hwnd = win32gui.FindWindow(None, title)
                    if hwnd and win32gui.IsWindowVisible(hwnd):
                        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
                        cx, cy = win32gui.ClientToScreen(hwnd, (cl, ct))
                        msg += f" | client x={cx} y={cy} w={cr-cl} h={cb-ct}"
                        break
            self._title_lbl.config(text=msg)
        else:
            # Fall back: center of screen
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            ow = COLS * (CELL_W + BADGE_PAD_X) + BADGE_PAD_X
            oh = 2 * (CELL_H + BADGE_PAD_Y) + BADGE_PAD_Y + BAR_H
            self.root.geometry(f"+{(sw - ow) // 2}+{(sh - oh) // 2}")
            self._snap_btn.config(fg=BAR_FG)

    # ------------------------------------------------------------------
    # Grid rendering
    # ------------------------------------------------------------------

    def _set_placeholder(self, text: str):
        for w in self._grid_frame.winfo_children():
            w.destroy()
        tk.Label(
            self._grid_frame, text=text,
            bg=BG_DARK, fg="#555555",
            font=("Segoe UI", 9, "italic"),
            padx=12, pady=8,
        ).pack()

    def _render_pack(self, scored: list[tuple[float, dict, int]]):
        """Render card badges. scored = [(score, card_row, arena_id), ...] in log/Arena order."""
        for w in self._grid_frame.winfo_children():
            w.destroy()

        if not scored:
            self._set_placeholder("No cards in pack")
            return

        best_score = max(sc for sc, _, _ in scored)

        cols = min(COLS, len(scored))
        rows = (len(scored) + cols - 1) // cols

        score_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        for idx, (sc, r, _) in enumerate(scored):
            row_i = idx // cols
            col_i = idx % cols

            gap = best_score - sc
            bg  = _gap_bg(gap)

            cell = tk.Frame(
                self._grid_frame,
                bg=bg,
                width=CELL_W,
                height=CELL_H,
                padx=2, pady=0,
            )
            cell.grid(
                row=row_i, column=col_i,
                padx=BADGE_PAD_X, pady=BADGE_PAD_Y,
                sticky="nsew",
            )
            cell.pack_propagate(False)

            # Score only
            tk.Label(
                cell, text=f"{sc:.1f}",
                bg=bg, fg=SCORE_FG,
                font=score_font,
                anchor="center",
            ).pack(fill=tk.BOTH, expand=True)

        # Make all grid cells expand equally
        for c in range(cols):
            self._grid_frame.columnconfigure(c, weight=1)

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    def _lookup(self, arena_id: int) -> dict:
        name = self._arena.get(arena_id, f"#{arena_id}")
        key  = name.strip().lower()
        r    = self._cards.get(key)
        if r is None and " // " in key:
            r = self._cards.get(key.split(" // ")[0].strip())
        if r is None:
            return {"Name": name, "Color": "", "GIH WR": "", "OH WR": "",
                    "Best1": "", "Best2": "", "Best3": "", "Best4": ""}
        return r

    def _make_openness_fn(self, depth: dict[str, float]):
        """Return a simple depth-based openness function (no signal history)."""
        # A lightweight approximation: open if depth is low for a color
        max_d = max(depth.values(), default=1.0) or 1.0
        def fn(c: str) -> float:
            return max(0.0, 1.0 - depth.get(c, 0.0) / max_d)
        return fn

    # ------------------------------------------------------------------
    # Poll + refresh
    # ------------------------------------------------------------------

    def _poll(self):
        events = self._reader.poll()
        refreshed = False
        for kind, data in events:
            self._state.apply(kind, data)
            if kind == "notify" and data.get("cards"):
                self._refresh()
                refreshed = True
        if not refreshed and not self._state.current_pack:
            pass  # stay on last state
        self.root.after(POLL_MS, self._poll)

    def _refresh(self):
        s = self._state
        if not s.current_pack:
            self._title_lbl.config(text="Draft Overlay — waiting for pack…")
            self._set_placeholder("Waiting for pack…")
            return

        pack_num  = s.pack + 1
        pick_num  = s.pick + 1
        self._title_lbl.config(text=f"Draft Overlay — Pack {pack_num}  Pick {pick_num}")

        # Score each card (same logic as DraftAdvisorFrame._refresh)
        total_picks = len(s.picks)
        depth       = count_color_depth(s.picks, self._lookup)
        pair_syn    = count_pair_synergy(s.picks, self._lookup)
        picked_names = [self._lookup(aid).get("Name", "").lower() for *_, aid in s.picks]

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

        openness_fn = self._make_openness_fn(depth)

        scored: list[tuple[float, dict, int]] = []
        for aid in s.current_pack:
            r = self._lookup(aid)
            name = r.get("Name", "")
            if name.startswith("#") or "Basic Land" in r.get("Type", ""):
                continue
            sc = card_score(
                r, depth, total_picks, crea, other,
                self._density_threshold, picked_names,
                openness_fn=openness_fn, pair_synergy=pair_syn,
                picks=s.picks, lookup_fn=self._lookup,
            )
            scored.append((sc, r, aid))

        # Determine best score for colour grading, but keep Arena visual order for rendering
        scored.sort(key=lambda x: _arena_sort_key(x[1]))
        self._render_pack(scored)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def launch(csv_path: Path = DEFAULT_CSV, log_path: Path = LOG_PATH):
    """Create and run the overlay (blocking mainloop)."""
    ov = DraftOverlay(csv_path, log_path)
    ov.root.mainloop()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="MTG Arena draft score overlay")
    parser.add_argument("csv",   nargs="?", default=str(DEFAULT_CSV))
    parser.add_argument("--log", default=str(LOG_PATH))
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Error: CSV not found: {csv_path}")
        sys.exit(1)

    launch(csv_path, Path(args.log))


if __name__ == "__main__":
    main()
