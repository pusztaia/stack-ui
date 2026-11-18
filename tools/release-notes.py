"""The release notes of a tag: its version's section of CHANGELOG.md, for the GitHub release (release.yml).

    python tools/release-notes.py v1.0.27 CHANGELOG.md notes.md

Fails (exit 1) when the changelog has no section, or an empty one, for the tag's version: a release without its
notes is a mistake to be seen, not published. Python 3.14+, standard library only."""
from __future__ import annotations

import re
import sys
from pathlib import Path

TAG = re.compile(r'^(?:refs/tags/)?v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$')


def version(tag: str) -> str:
    """1.0.27 for v1.0.27 (or refs/tags/v1.0.27)."""
    m = TAG.match(tag)
    if not m:
        raise ValueError(f'not a release tag: {tag!r} (expected v<major>.<minor>.<patch>)')
    return m.group(1)


def section(changelog: str, ver: str) -> str:
    """The text under '## <ver>' up to the next '## ' heading, without the blank lines around it."""
    lines = changelog.splitlines()
    try:
        start = lines.index(f'## {ver}') + 1 if ver else -1
    except ValueError:
        start = -1
    if start < 0:
        raise LookupError(f'CHANGELOG.md has no section "## {ver}"')
    end = next((i for i in range(start, len(lines)) if lines[i].startswith('## ')), len(lines))
    text = '\n'.join(lines[start:end]).strip('\n')
    if not text.strip():
        raise LookupError(f'the section "## {ver}" of CHANGELOG.md is empty')
    return text


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    tag, changelog, out = argv
    try:
        notes = section(Path(changelog).read_text(encoding='utf-8'), version(tag))
    except (ValueError, LookupError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    Path(out).write_text(notes + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
