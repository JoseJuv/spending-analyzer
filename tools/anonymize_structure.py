"""Print the layout of a bank statement PDF without its content.

Letters are replaced with X/x and digits with 9. Punctuation, word positions
on the page and font sizes are kept, so a parser can be designed from the
output without seeing any real data. Common statement header words (Finnish,
e.g. "Saldo", "Viesti") are left visible.

Usage:
    python tools/anonymize_structure.py statement.pdf
    python tools/anonymize_structure.py statement.pdf -o layout.txt --pages 1-2,last

Review the output file yourself before sharing it with anyone.
"""

import argparse
import re
import sys
from pathlib import Path

import pdfplumber

# Finnish words that describe layout and carry no personal information.
ALLOWED_WORDS = {
    "tiliote", "tili", "tilinumero", "iban", "bic", "sivu", "pvm", "päivä",
    "päivämäärä", "kirjauspäivä", "arvopäivä", "maksupäivä", "tapahtumapäivä",
    "saaja", "maksaja", "saaja/maksaja", "maksaja/saaja", "viesti", "viite",
    "viitenumero", "selite", "selitys", "laji", "tapahtuma", "tapahtumat",
    "tapahtumalaji", "määrä", "summa", "eur", "euro", "euroa", "saldo",
    "alkusaldo", "loppusaldo", "panot", "otot", "pano", "otto", "yhteensä",
    "arkistointitunnus", "kausi", "ajalta", "jakso", "korttiosto", "tilisiirto",
    "e-lasku", "palvelumaksu", "korko", "verot", "palkka", "pankkikortti",
    "kortti", "korttinumero", "mobilepay", "siirto", "oma", "hyvitys",
    "veloitus", "kpl", "nro", "numero",
}

LINE_TOLERANCE = 3  # points: words this close vertically are on the same line


def mask(word: str) -> str:
    if word.strip(".,:;()[]").lower() in ALLOWED_WORDS:
        return word
    word = re.sub(r"[^\W\d_]", lambda m: "X" if m.group().isupper() else "x", word)
    return re.sub(r"\d", "9", word)


def group_lines(words):
    lines = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0]["top"] - word["top"]) <= LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def describe_page(page, number: int) -> list[str]:
    out = [f"=== Page {number} (width {page.width:.0f}, height {page.height:.0f}) ==="]
    words = page.extract_words(extra_attrs=["size"])
    if not words:
        out.append("No text: the page is probably a scanned image.")
        return out

    out.append(f"Lines {len(page.lines)}, rectangles {len(page.rects)}")
    out.append("Format:  y=<top>  [x=<left edge> size=<font size>] text")
    for line in group_lines(words):
        parts = [f"[x={w['x0']:.0f} size={w['size']:.1f}] {mask(w['text'])}" for w in line]
        out.append(f"y={line[0]['top']:6.1f}  " + "  ".join(parts))

    tables = page.extract_tables()
    out.append(f"pdfplumber detected {len(tables)} tables")
    for i, table in enumerate(tables, 1):
        out.append(f"--- Table {i}: {len(table)} rows ---")
        for row in table[:15]:
            out.append(" | ".join(mask(cell or "").replace("\n", "⏎") for cell in row))
    return out


def parse_pages(spec: str, total: int) -> list[int]:
    """Turn "1-2,last" into 1-based page numbers."""
    numbers = []
    for part in spec.replace("last", str(total)).split(","):
        start, _, end = part.strip().partition("-")
        numbers += range(int(start), int(end or start) + 1)
    return sorted({n for n in numbers if 1 <= n <= total})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("layout_anonymized.txt"))
    parser.add_argument("--pages", default="1-2,last", help='pages to process, e.g. "1-2,last"')
    args = parser.parse_args()

    with pdfplumber.open(args.pdf) as pdf:
        out = [f"Total pages: {len(pdf.pages)}"]
        for number in parse_pages(args.pages, len(pdf.pages)):
            out += describe_page(pdf.pages[number - 1], number) + [""]

    args.output.write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {args.output}. Review it before sharing.", file=sys.stderr)


if __name__ == "__main__":
    main()
