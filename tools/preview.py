# -*- coding: utf-8 -*-
"""Build a static Daedalus preview from a state JSON file."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


HTML_PATH = Path(__file__).parents[1] / "daedalus" / "web" / "index.html"
_STATE_RE = re.compile(
    r'(<script\s+id="state"\s+type="application/json">).*?(</script>)',
    re.DOTALL,
)


def embed_state(html: str, state: dict) -> str:
    """The same safe embedding as the server, but without web dependencies."""
    data = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    html, count = _STATE_RE.subn(
        lambda m: m.group(1) + data + m.group(2), html, count=1
    )
    if count != 1:
        raise RuntimeError("index.html has no unique state placeholder")
    return html


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Embed a sample state JSON into index.html, without a database",
    )
    parser.add_argument("sample", type=Path, help="path to state-sample.json")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("preview.html"),
        help="target file (default: ./preview.html)",
    )
    args = parser.parse_args()

    state = json.loads(args.sample.read_text(encoding="utf-8"))
    html = embed_state(HTML_PATH.read_text(encoding="utf-8"), state)
    # Parent folders are created, because an explicit target file may live in a
    # preview folder that does not exist yet.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
