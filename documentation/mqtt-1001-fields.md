# MQTT commandType 1001 field units

Print job status is published as MQTT notices with `commandType: 1001`. The raw
field units are easy to misread in the UI.

## Remaining time (`time`)

| | |
|--|--|
| **Unit** | **Milliseconds** remaining (not seconds) |
| **Behaviour** | Live **re-estimate** — value can rise as well as fall |
| **Bug if treated as seconds** | ~2.6 h left (`9472157` ms) renders as **`2631:07:34`** (~110 days) |

**Fix in this fork:** `static/ankersrv.js` converts with `Math.floor(time / 1000)`
before formatting (`remainingSecondsFrom1001`).

Upstream analysis and capture evidence:

- Commit: [bigminer/ankermake-m5-protocol@0595a83](https://github.com/bigminer/ankermake-m5-protocol/commit/0595a83b4a0004db5642ac9d6f8e0e000f421c83)
- That tree applies the same division in `web/service/state.py` (`remaining = time // 1000`).
  Our fork still feeds the dashboard from raw `/ws/mqtt`, so the conversion lives in JS.

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
