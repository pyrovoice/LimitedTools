# LimitedTool — Agent Instructions

## Project purpose
MTG Limited draft tool with a Card Browser, real-time Draft Advisor, and 8-player
draft simulator. Card data lives under `sets/<SET>/` and is built by an enrichment
pipeline before the GUI can use it.

---

## Adding a new set — full runbook

### Prerequisites (supplied externally)
Place these files in `sets/<SET>/` before running anything:
| File | Source | Required? |
|---|---|---|
| `carddata_raw.csv` | Scryfall bulk export (filtered to the set) | Yes |
| `carddata_medium.csv` | 17Lands card-ratings CSV, overall players | Yes |
| `carddata_pro.csv` | 17Lands card-ratings CSV, top players | Optional |
| `set_data.txt` | 17Lands "deck color" page, tab-separated | Yes |

`set_data.txt` format (tab-separated, header row then archetype rows):
```
Color	Wins	# Games	Win Rate
Two-color	...	...	...
Azorius (WU)	13334	23067	57.8%
...
```

### Step 1 — Download spells data
```powershell
cd "c:\Users\maxim\My Stuff\LimitedTool"
spells add <SET>
```
Downloads ~150-300 MB of 17Lands draft+game parquet files to
`C:\Users\maxim\AppData\Local\Spells\External\<SET>\`.

**If you get a parquet corruption error** ("File out of specification"):
```powershell
Remove-Item "C:\Users\maxim\AppData\Local\Spells\External\<SET>\<SET>_PremierDraft_draft.parquet"
spells add <SET>
```

### Step 2 — Generate per-archetype pair data
```powershell
python spells_explore.py <SET>
```
Reads `sets/<SET>/set_data.txt` to determine if the set is bicolor or tricolor,
then queries spells for GIH WR per card per color pair.
Output: `sets/<SET>/spells_<SET>_by_pair.csv`

### Step 3 — Build the enriched CSV
Run from the **set directory**:
```powershell
cd "c:\Users\maxim\My Stuff\LimitedTool\sets\<SET>"
python "c:\Users\maxim\My Stuff\LimitedTool\enrich_cards.py" `
  -m carddata_medium.csv `
  [-s carddata_pro.csv] `
  -r carddata_raw.csv `
  -o carddata_enriched.csv `
  -e <SET>
```
`enrich_cards.py` auto-detects `spells_<SET>_by_pair.csv` in the same directory
and uses it to populate the Best1–Best4 columns. The 17Lands color-filter API is
used as a fallback only (it has no archetype data for older sets).

**Verify the output** — all of these should be populated:
- `Rarity` column (if missing, pack building will only produce 10 cards — see below)
- `Best1`–`Best4` columns (should have data for most cards)

### Step 4 — Register the set in the simulator
Add a tuple to `SETS` in `draft_sim.py`:
```python
SETS: list[tuple[str, str]] = [
    ...
    ("<SET>", "Full Set Name"),
    ...
]
```

---

## Common fixes

### Enriched CSV is missing the Rarity column
This happens when the CSV was generated before Rarity was added to OUTPUT_FIELDS.
Fix: read Rarity from `carddata_medium.csv` and patch into the enriched CSV:
```python
import csv
from pathlib import Path
from collections import defaultdict
# load name→rarity from medium CSV (utf-8-sig for BOM)
# open enriched, insert Rarity after Color column, write back
```
See the session history for a complete one-shot script.

### Best1–Best4 are all empty
Usually means `spells_<SET>_by_pair.csv` was not present when `enrich_cards.py`
ran, or the set name didn't match. Re-run Steps 2 and 3. If you already have an
enriched CSV you don't want to regenerate, use the `_patch_bests.py` pattern from
the session history to update only those columns in-place.

### Pack sizes are 10 instead of 14
The `Rarity` column is missing or blank → fix per the section above.

### `spells add` says "file already exists"
The parquet file exists but may be empty/corrupted from a prior interrupted run.
Delete it and re-run `spells add <SET>`.

---

## Architecture notes

### Enriched CSV columns
`Name, Type, CMC, Color, Rarity, ALSA, OH WR, GIH WR,`
`ALSA Pro, OH WR Pro, GIH WR Pro, Best1, Best2, Best3, Best4, ArenaId`

`ATA` (Average Taken At) was intentionally removed — not used in scoring.

### Best column format
`"WU:GIH=63.2%:OH=61.5%"` — pair label (may have trailing `+` for splash data from
the 17Lands API, stripped by `_parse_best_entry()`), then GIH%, then OH%.

### Scoring uses
`GIH WR`, `OH WR`, `Best1`–`Best4`, `CMC`, `Type`, `Color`, `Rarity`
The `GIH WR Pro` / `OH WR Pro` columns are displayed in the UI as secondary figures.

### UTF-8 BOM
Both `carddata_medium.csv` and `carddata_pro.csv` typically have a UTF-8 BOM.
Always open them with `encoding="utf-8-sig"`, not `"utf-8"`.

### set_data.txt and set type detection
`spells_explore.py` reads `sets/<SET>/set_data.txt` and counts archetypes with
win rate > 55% per bucket (2-color, 3-color, 4+/5-color) to determine:
- **bicolor**: fetch 10 pairs + MULTI (3+ color games)
- **tricolor**: fetch 10 pairs + 10 triples + MULTI (4+ color games)
- **has_5color**: also fetch a dedicated 5C bucket

### Lane pairs (SOS-specific)
`LANE_PAIRS = ["WB", "BG", "UG", "UR", "WR"]` in `limitedtool.py`.
These drive the open-lane signal display and should be updated when the default
set changes to something with different primary archetypes.
