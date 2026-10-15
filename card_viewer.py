#!/usr/bin/env python3
"""
card_viewer.py  --  Read-only card browser for limited draft data.

Usage:
    python card_viewer.py [CSV_PATH]

Defaults to carddata_enriched.csv in the same directory.
"""

import csv
import subprocess
import sys
import tkinter as tk
from tkinter import ttk
from pathlib import Path

DEFAULT_CSV = Path(__file__).parent / "carddata_enriched.csv"

# (CSV field key, display label)
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

# Fields that have a Pro counterpart shown inline as "value (Pro: value)"
PRO_FIELDS: dict[str, str] = {
    "ALSA":   "ALSA Pro",
    "ATA":    "ATA Pro",
    "OH WR":  "OH WR Pro",
    "GIH WR": "GIH WR Pro",
}


def load_cards(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class App(tk.Tk):
    def __init__(self, csv_path: Path):
        super().__init__()
        self.title("Limited Tool — Card Browser")
        self.geometry("950x600")
        self.minsize(700, 400)
        self.csv_path = csv_path
        self.all_cards: list[dict] = []
        self.filtered: list[dict] = []
        self._build()
        self._load()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self):
        # ---- Left panel: search + list ----
        left = ttk.Frame(self, padding=(8, 8, 4, 8))
        left.pack(side=tk.LEFT, fill=tk.Y)

        ttk.Label(left, text="Search card name:").pack(anchor="w")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_search)
        ttk.Entry(left, textvariable=self._search_var, width=32).pack(fill=tk.X, pady=(2, 4))

        ttk.Button(
            left, text="Open Draft Advisor",
            command=self._open_draft_advisor,
        ).pack(fill=tk.X, pady=(0, 6))

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

            # Insert a small visual gap before the Best columns
            if field == "Best1":
                ttk.Separator(right, orient=tk.HORIZONTAL).grid(
                    row=row, column=0, columnspan=2, sticky="ew", pady=(6, 4)
                )
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

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _load(self):
        self.all_cards = load_cards(self.csv_path)
        self._refresh_list(self.all_cards)

    def _refresh_list(self, cards: list[dict]):
        self.filtered = cards
        self._listbox.delete(0, tk.END)
        for c in cards:
            self._listbox.insert(tk.END, c["Name"])
        self._count_label.config(text=f"{len(cards)} card(s)")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _open_draft_advisor(self):
        advisor = Path(__file__).parent / "draft_advisor.py"
        subprocess.Popen([sys.executable, str(advisor), str(self.csv_path)])

    def _on_search(self, *_):
        q = self._search_var.get().strip().lower()
        if not q:
            self._refresh_list(self.all_cards)
        else:
            self._refresh_list([c for c in self.all_cards if q in c["Name"].lower()])

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


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def main():
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    if not csv_path.exists():
        print(f"Error: file not found: '{csv_path}'")
        sys.exit(1)
    App(csv_path).mainloop()


if __name__ == "__main__":
    main()
