# OctoPrint-Prusa-Preflight

Blocks a serial-streamed print when the gcode disagrees with the printer.

Prusa Buddy printers (CORE One, MK4 family, XL...) validate filament type and
nozzle size themselves — but only for **file-based** prints (USB stick,
PrusaLink, Connect), where the firmware can read the file's metadata. A print
streamed from OctoPrint over serial arrives one naked command at a time and
bypasses every one of those checks: the firmware's `M862.1 P` handler even
documents that the parameter "is ignored when printing". Slice with the wrong
preset and the printer lays PETG on a 60°C bed without a word.

OctoPrint sits at the perfect chokepoint — it has the whole file *and* the
serial port. This plugin restores the missing gate:

1. While the printer idles, it polls the firmware's own state:
   `M865 I<tool>` (loaded filament type — the same state the printer's
   file-print preview trusts) and `M862.1 Q` (fitted nozzle diameter /
   hardened / high-flow, from EEPROM).
2. When a print starts, it holds the first job command in OctoPrint's
   gcode-queuing phase, reads `; filament_type` and `; nozzle_diameter`
   from the selected file, and compares.
3. On mismatch it cancels the print and pops an error explaining both sides.
   Set `warn_only` to get the popup without the cancel.

No spool database, no bookkeeping, no companion plugins: the printer is the
single source of truth. Anything that updates the printer's loaded filament —
its own load/change UI, or an external `M865 S"PETG" L0` — feeds the check
automatically.

## Requirements

- A Prusa Buddy-firmware printer connected to OctoPrint over serial, with
  `M865` support (check by sending `M865 I0` in the OctoPrint terminal — you
  should get `name:<type>` back).
- Gcode sliced by PrusaSlicer or anything else that writes
  `; filament_type = ...` / `; nozzle_diameter = ...` comments.

## Setup

Install manually using this URL:

    https://github.com/BackSlasher/OctoPrint-Prusa-Preflight/archive/main.zip

## Settings

Via `config.yaml` under `plugins.prusa_preflight` (no UI page yet):

| key | default | meaning |
|---|---|---|
| `check_filament` | `true` | compare `; filament_type` against `M865` |
| `check_nozzle` | `true` | compare `; nozzle_diameter` against `M862.1 Q` |
| `warn_only` | `false` | pop the mismatch but let the print run |
| `refresh_interval` | `30` | seconds between idle polls of the firmware |
| `tool_count` | `1` | tools to poll (raise for toolchangers) |

## API

- `GET /api/plugin/prusa_preflight` — current cached firmware state.
- `POST /api/plugin/prusa_preflight` with `{"command": "refresh"}` — poll now.

## Design notes

- **Why a cache, not a live query at print start:** OctoPrint calls the
  queuing hook from its serial send loop — the same loop that would have to
  transmit the query. Waiting there would deadlock, so the gate only reads
  state gathered while the printer idled. Worst case is one refresh interval
  of staleness, which produces a spurious prompt, never a bad print.
- **How serial "RPC" works here:** OctoPrint has no request/response
  primitive. The plugin tags its query, the `gcode.sent` hook spots the tag
  and opens a capture window, and the protocol's strict lockstep means every
  received line until the closing `ok` belongs to that query.
- Unknown states fail open with an explanatory popup: no firmware answer, no
  metadata in the file, or nothing loaded all let the print through — the
  plugin only blocks on a positive contradiction.
- SD-card prints (from OctoPrint's perspective) are not validated; prints
  started on the printer itself from a file get the firmware's own preview
  checks anyway.
