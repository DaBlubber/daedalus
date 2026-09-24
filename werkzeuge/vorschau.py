# -*- coding: utf-8 -*-
"""Eine statische Daedalus-Vorschau aus einer Stand-JSON-Datei bauen."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


HTML_PFAD = Path(__file__).parents[1] / "daedalus" / "web" / "index.html"
_STAND_RE = re.compile(
    r'(<script\s+id="stand"\s+type="application/json">).*?(</script>)',
    re.DOTALL,
)


def stand_einbetten(html: str, stand: dict) -> str:
    """Dieselbe sichere Einbettung wie der Server, aber ohne Web-Abhaengigkeit."""
    daten = json.dumps(stand, ensure_ascii=False, separators=(",", ":"))
    daten = daten.replace("</", "<\\/")
    html, anzahl = _STAND_RE.subn(
        lambda treffer: treffer.group(1) + daten + treffer.group(2), html, count=1
    )
    if anzahl != 1:
        raise RuntimeError("index.html enthaelt keinen eindeutigen Stand-Platzhalter")
    return html


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe-JSON in index.html einbetten, ganz ohne Datenbank",
    )
    parser.add_argument("probe", type=Path, help="Pfad zu stand-probe.json")
    parser.add_argument(
        "ausgabe",
        nargs="?",
        type=Path,
        default=Path("vorschau.html"),
        help="Zieldatei (Vorgabe: ./vorschau.html)",
    )
    argumente = parser.parse_args()

    stand = json.loads(argumente.probe.read_text(encoding="utf-8"))
    html = stand_einbetten(HTML_PFAD.read_text(encoding="utf-8"), stand)
    # Elternordner werden angelegt, weil eine explizite Zieldatei auch in einem
    # noch nicht vorhandenen Vorschauordner liegen darf.
    argumente.ausgabe.parent.mkdir(parents=True, exist_ok=True)
    argumente.ausgabe.write_text(html, encoding="utf-8")
    print(argumente.ausgabe.resolve())


if __name__ == "__main__":
    main()
