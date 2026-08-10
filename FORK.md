# ankerctl — Windows / eufyMake / Firmware V3 fork

This fork is based on [anselor/ankermake-m5-protocol](https://github.com/anselor/ankermake-m5-protocol)
(itself a maintained fork of the original Ankermgmt project).

It focuses on **Windows + eufyMake Studio + AnkerMake M5 firmware V3** issues that break
Orca Slicer / web uploads and PPPP connectivity on modern printers.

## Why this fork?

On current eufyMake / M5 V3 setups we observed:

1. **No `login.json`** from eufyMake Studio — use email/password login (from anselor).
2. **PPPP unicast LAN connect fails** — printer only answers **broadcast** discovery and
   continues on an ephemeral UDP port.
3. **Web/Orca uploads hang** — the browser video websocket reconnects mid-transfer and
   steals the PPPP session from file transfer.
4. **“Loading please wait” / PPPP restart loops** — aggressive websocket timeouts and
   shared-service teardown dropped the link continuously.

## What changed

| Area | Change |
|------|--------|
| PPPP LAN | Broadcast discovery + bind to local interface (Windows) |
| Uploads | Exclusive CLI-style PPPP transfer path (pause video, stop shared PPPP, upload, restore) |
| Video | Suspended during uploads; softer reconnect; no thrashing restart loop |
| Websockets | Correct `ws://` URLs; longer stream timeouts |
| Channel I/O | Write ACK timeouts; non-blocking frame parse (avoid deadlocks) |
| Windows helpers | `start-ankerctl.bat`, `start-ankerctl.ps1`, `login-ankerctl.ps1` |

## Windows quick start

```powershell
git clone --recurse-submodules https://github.com/<YOU>/ankermake-m5-protocol.git
cd ankermake-m5-protocol
python -m pip install -r requirements.txt
python ankerctl.py webserver run --host 0.0.0.0 --port 4470
```

Open http://localhost:4470 and log in with your eufyMake / AnkerMake account
(country code e.g. `SE`, email, password).

Or double-click `start-ankerctl.bat`.

### Orca Slicer

- Host type: **OctoPrint**
- Hostname: `localhost:4470`
- API key: empty
- Use **Upload and Print** only

### CLI print (most reliable)

```powershell
python ankerctl.py pppp print-file path\to\file.gcode
```

## Tips

- Close **eufyMake Studio** while using ankerctl (one PPPP client at a time).
- Printer must be **idle** before a new upload.
- Keep the ankerctl window open while printing from Orca.

## Credits

- Original project: [Ankermgmt/ankermake-m5-protocol](https://github.com/Ankermgmt/ankermake-m5-protocol)
- Maintained fork / login: [anselor/ankermake-m5-protocol](https://github.com/anselor/ankermake-m5-protocol)
- Additional upstream work cited in anselor’s history (exiles, sondregronas, and others)

Not affiliated with Anker / eufyMake.
