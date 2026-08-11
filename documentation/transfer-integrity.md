# File transfer integrity (PPPP over UDP)

ankerctl sends print jobs to the AnkerMake M5 over **PPPP**, which uses **UDP** on the
local network. UDP by itself does **not** guarantee delivery or integrity - packets can
be lost, reordered, or corrupted.

That does **not** mean transfers are "fire and forget." The Anker protocol (as implemented
in `libflagship`) stacks several reliability and integrity mechanisms on top of UDP.
A successful "print job sent" means those layers completed; a failure (timeout / 503)
means the transfer was **aborted** rather than silently accepted as complete.

## Overview

```
Orca / web UI / CLI
        │
        ▼
   ankerctl (HTTP or CLI)
        │
        ▼
   PPPP session (UDP)
        │
        ├─ DRW packets + DRW_ACK  → delivery / retransmission
        ├─ AABB frames + CRC-16   → per-chunk integrity
        ├─ File header + MD5      → whole-file fingerprint
        └─ FileTransferReply OK   → printer accepted each step
        │
        ▼
   AnkerMake M5
```

## Layer 1 - Reliable delivery (DRW + ACK)

File payload is split into small **DRW** (data read/write) packets on a logical channel
(channel 1 for file transfer).

| Mechanism | Role |
|-----------|------|
| Packet sequence indices | Detect missing / out-of-order pieces |
| **DRW_ACK** from printer | Confirms which indices were received |
| Retransmission queue | Unacked packets are sent again |
| In-flight window | Limits how many packets are outstanding |
| Write timeout (this fork) | If ACKs stall too long → **fail the upload** (no fake success) |

**Typical failure you may see:**

```text
Channel 1 write ACK timeout (acked 35, need 52)
```

Meaning: the PC sent packets that needed ACKs through index 52, but the printer only
confirmed through 35 within the timeout (Wi‑Fi loss, congestion, or printer busy).
ankerctl **stops** and reports an error so Orca/web can retry - it does not claim success.

## Layer 2 - Per-frame CRC (AABB)

Each file-transfer control/data block is wrapped in an **AABB** frame:

```text
[AABB header][payload bytes][CRC-16]
```

- CRC is computed with Anker's PPPP CRC-16 (`ppcs_crc16` in `libflagship/util.py`).
- Pack: `Aabb.pack_with_crc(data)`
- Unpack: `Aabb.parse_with_crc(...)` - CRC mismatch is rejected.

So random bit-flips inside a transfer frame are detectable by the receiver of that frame.
This is **not** "UDP with no checksum at the application layer."

Relevant code: `libflagship/pppp.py` (`Aabb.pack_with_crc` / `parse_with_crc`).

## Layer 3 - Whole-file MD5 in the transfer header

Before bulk data, ankerctl sends a **BEGIN** metadata record (`FileUploadInfo`), including:

| Field | Purpose |
|-------|---------|
| `name` | Sanitized filename |
| `size` | Byte length of the gcode |
| `md5` | **MD5 hex digest of the entire file** |
| user / machine fields | Account / client identification |

Built in `libflagship/ppppapi.py`:

```text
type,name,size,md5,user_name,user_id,machine_id
```

The MD5 is computed locally with `hashlib.md5(data).hexdigest()` over the full gcode
bytes before any chunk is sent. The printer is given a fingerprint of the intended file.

> **Note:** Whether every firmware build always re-hashes the file after reassembly and
> refuses to print on mismatch is **inside closed Anker firmware**. Open reverse-engineering
> confirms the MD5 is **sent**; ankerctl does not currently log a separate
> "printer verified MD5: OK" message. In practice, a completed transfer with all step
> replies OK is the best open-source signal you get.

## Layer 4 - Step replies (FileTransferReply)

Transfer is a small state machine:

1. **P2P_SEND_FILE** - open a file send session  
2. **BEGIN** - metadata (name, size, MD5, ...)  
3. **DATA** - file chunks with offset  
4. **END** - finish and request print start (when printing)

After BEGIN / DATA / END, the printer returns a **FileTransferReply** (e.g. OK).
ankerctl treats non-OK replies as failure (`PPPPError`).

So the printer is actively acknowledging protocol steps, not only ACKing raw UDP packets.

## What "success" and "failure" mean

| Outcome | Interpretation |
|---------|----------------|
| Log: `Successfully sent print job` + HTTP 200 | DRW ACKs completed, AABB steps returned OK, END accepted. File is very likely intact on the printer. |
| ACK timeout / CRC error / non-OK reply / HTTP 503 | Transfer **did not** complete safely. Retry; do not assume a partial file will print correctly. |
| Connection test OK but upload fails | Test only hits HTTP (`/api/version`). Upload uses PPPP; Wi‑Fi/printer load can still fail mid-file. |

## What this stack does *not* guarantee

- It is **not** TCP. There is no single OS-level stream checksum.
- ankerctl does **not** re-download the file from the printer to re-verify MD5.
- There is **no** public proof that every M5 firmware build enforces MD5 before every print.
- Camera/video traffic shares the same PPPP world; this fork pauses video during uploads
  so a second stream does not corrupt or starve the file channel.

## Practical advice

1. Prefer a **stable 2.4 GHz** link (M5 is not 5 GHz).  
2. Keep **eufyMake Studio** closed during uploads (one PPPP client).  
3. Printer should be **idle** before a new job.  
4. Large files (~5-10+ MB) are more sensitive to Wi‑Fi blips; this fork retries chunks and
   full transfers - if you still see ACK timeouts, retry once or use CLI:
   `python ankerctl.py pppp print-file path\to\file.gcode`  
5. A print that **starts after a reported success** is the best real-world confirmation.

## Code map

| Piece | Location |
|-------|----------|
| MD5 + file header | `libflagship/ppppapi.py` → `FileUploadInfo` |
| AABB + CRC-16 | `libflagship/pppp.py` → `Aabb.pack_with_crc` / `parse_with_crc` |
| CRC function | `libflagship/util.py` → `ppcs_crc16` |
| DRW / ACK / retransmit | `libflagship/ppppapi.py` → `Channel` |
| Exclusive upload path (this fork) | `web/util.py` → `upload_file_to_printer` |
| CLI upload | `cli/pppp.py` → `pppp_send_file`, `ankerctl.py pppp print-file` |

## Summary

**UDP is the transport. Reliability and integrity are application-level:**

- **ACKs + retransmit** → don't finish with missing pieces  
- **CRC-16 per AABB frame** → detect corrupted chunks  
- **MD5 of full file in BEGIN** → fingerprint of the intended gcode  
- **FileTransferReply OK** → printer accepted each transfer step  

Failures are designed to surface as timeouts/errors so you can retry, rather than
silently treating a broken UDP stream as a good print job.
