"""Fix UTF-8 mojibake and normalize fancy punctuation to plain ASCII."""
from pathlib import Path

# Literal mojibake: UTF-8 curly quotes were decoded as Windows-1252 and re-saved
MOJIBAKE = {
    "â€™": "'",
    "â€˜": "'",
    "â€œ": '"',
    "â€\x9d": '"',
    "â€\x9c": '"',
    "â€\x99": "'",
    "â€\x98": "'",
    "â€“": "-",
    "â€”": "-",
    "â€\x9c": '"',
    "Â ": " ",
    "Â": "",
    "Ã¢â‚¬â„¢": "'",  # double-encoded forms if any
}

# Proper Unicode punctuation -> ASCII
UNICODE_NORM = {
    "\u2019": "'",  # ’
    "\u2018": "'",  # ‘
    "\u201c": '"',  # “
    "\u201d": '"',  # ”
    "\u2013": "-",  # –
    "\u2014": "-",  # —
    "\u2026": "...",
    "\u00a0": " ",
}

TARGETS = [
    Path("README.md"),
    Path("FORK.md"),
    Path("documentation/transfer-integrity.md"),
    Path("web/util.py"),
    Path("web/__init__.py"),
    Path("web/lib/service.py"),
    Path("web/service/filetransfer.py"),
    Path("web/service/pppp.py"),
    Path("web/service/video.py"),
]


def fix_text(text: str) -> str:
    for a, b in MOJIBAKE.items():
        text = text.replace(a, b)
    # Common broken right-double after partial replace
    text = text.replace("â€\x9d", '"')
    text = text.replace("â€\x9c", '"')
    for a, b in UNICODE_NORM.items():
        text = text.replace(a, b)
    return text


def main() -> None:
    for p in TARGETS:
        if not p.exists():
            print(f"skip missing {p}")
            continue
        raw = p.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
        fixed = fix_text(text)
        if fixed != text or bom:
            p.write_bytes(fixed.encode("utf-8"))  # no BOM
            print(f"fixed {p} (had_bom={bom})")
        else:
            print(f"unchanged {p}")

    # Verify README
    t = Path("README.md").read_text(encoding="utf-8")
    for bad in ("â€™", "â€œ", "â€", "Â", "\u2019", "\u201c"):
        if bad in t:
            print(f"WARNING still in README: {bad!r} x{t.count(bad)}")
    for line in t.splitlines():
        if "there" in line and "'" in line:
            print("SAMPLE:", line[:100])
            break


if __name__ == "__main__":
    main()
