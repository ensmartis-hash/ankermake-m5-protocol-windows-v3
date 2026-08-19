# MQTT commandType 1001 field units

Print job status is published as MQTT notices with `commandType: 1001`. The raw
field units are easy to misread in the UI.

## Remaining time (`time`)

| | |
|--|--|
| **Unit** | **Milliseconds** remaining (not seconds) |
| **Behaviour** | Live **re-estimate** — value can rise as well as fall |
| **Bug if treated as seconds** | ~2.6 h left (`9472157` ms) renders as **`2631:07:34`** (~110 days) |

**Fix in this fork (dashboard units):** `static/ankersrv.js` converts with
`Math.floor(time / 1000)` before formatting (`remainingSecondsFrom1001`).

That only fixes *display units*. If the **printer itself** was seeded with a
bad estimate (classic Orca → M5 symptom: 30 min job shows **+1000 h** on the
panel / eufyMake), MQTT `time` is still huge after `/1000`. Seed the firmware
with a Cura-style `;TIME:<seconds>` comment — see below.

Upstream analysis and capture evidence:

- Commit: [bigminer/ankermake-m5-protocol@0595a83](https://github.com/bigminer/ankermake-m5-protocol/commit/0595a83b4a0004db5642ac9d6f8e0e000f421c83)
- That tree applies the same division in `web/service/state.py` (`remaining = time // 1000`).
  Our fork still feeds the dashboard from raw `/ws/mqtt`, so the conversion lives in JS.

## Printer / eufyMake ETA seed (`;TIME:`)

AnkerMake firmware looks for `;TIME:<seconds>` near `G28`. Orca usually only
writes a footer such as `; estimated printing time (normal mode) = 12m 58s`
(and `M73 … R<minutes>`). Without `;TIME:`, the panel invents multi-day ETAs.

**Fix in this fork (upload path):** `cli/gcode_meta.py` →
`inject_ankermake_print_meta()` runs before PPPP upload (web + CLI). It:

1. Parses Orca’s estimated printing time (fallback: `M73 P0 R<minutes>`)
2. Inserts `;TIME:<seconds>` immediately before the first `G28` if missing
3. Inserts `;LAYER_COUNT:` from `; total layer number:` when that is also missing

**Optional Orca tip** (Machine Start G-code) if you print from USB without ankerctl:

```
;LAYER_COUNT:{total_layer_count}
;LAYER_HEIGHT:{layer_height}
```

`;TIME:` still needs seconds — either let ankerctl inject it, or use a
post-processing script (community gist based on the Orca footer).

## Other 1001 fields (for reference)

| Field | Unit / notes |
|-------|----------------|
| `totalTime` | Overloaded: pre-print **estimate** (often looks like minutes), then **elapsed seconds** once printing (increments ~1/s) |
| `progress` | Hundredths of a percent (`10000` = 100%); UI uses `progress / 100` |
| `startLeftTime` | Often stuck at `1` — not a usable countdown |
| `name` | Job filename |

## Code map

| Piece | Location |
|-------|----------|
| Remaining display | `static/ankersrv.js` → commandType `1001` handler |
| Elapsed display | same (`totalTime` passed to `getTime` as seconds during print) |
| Progress bar | `getPercentage(progress)` |
