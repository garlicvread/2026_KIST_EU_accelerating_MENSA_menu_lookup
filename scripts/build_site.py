"""Build Pages with content-versioned assets and unchanged menu snapshots."""

import argparse
import hashlib
from pathlib import Path
import re
import shutil


ROOT = Path(__file__).resolve().parents[1]


def build_site(source: Path, output: Path) -> None:
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents:
        raise ValueError("Build output cannot be inside the source directory")
    if output.exists():
        raise ValueError(f"Build output already exists: {output}; choose a fresh directory")

    html = (source / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "styles.css"):
        digest = hashlib.sha256((source / asset).read_bytes()).hexdigest()
        pattern = rf'(?P<prefix>\b(?:src|href)\s*=\s*)(?P<quote>["\']){re.escape(asset)}(?P=quote)'
        html, count = re.subn(
            pattern,
            lambda match: f'{match["prefix"]}{match["quote"]}{asset}?v={digest}{match["quote"]}',
            html,
        )
        if count != 1:
            raise ValueError(f"Expected one unversioned {asset} reference in index.html")

    # Keep plain filenames available to previously cached HTML documents.
    shutil.copytree(source, output)
    (output / "index.html").write_text(html, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "site")
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp" / "pages")
    args = parser.parse_args()
    try:
        build_site(args.source, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Site build failed: {error}\n")
    print(f"Built Pages at {args.output}")


if __name__ == "__main__":
    main()
