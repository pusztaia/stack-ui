"""The version bump of a release, in one step (step 1 and 2 of CLAUDE.md's Release):

    python tools/bump.py 1.0.41

CHANGELOG.md's "## Unreleased" becomes "## 1.0.41" and stack_ui/__init__.py says __version__ = '1.0.41'. Refused,
with nothing written, when the version is not X.Y.Z or not higher than the current one, or when the changelog has
no "## Unreleased" section (or more than one, or an empty one): a release needs its notes. It does not commit, tag
or run the tests. Python 3.9+, standard library only."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')
INIT_LINE = re.compile(r"^__version__ = '(\d+\.\d+\.\d+)'$", re.MULTILINE)
UNRELEASED = '## Unreleased'


def current_version(root: Path) -> str:
    text = (root / 'stack_ui' / '__init__.py').read_bytes().decode('utf-8')
    found = INIT_LINE.findall(text)
    if len(found) != 1:
        raise ValueError("stack_ui/__init__.py has no single line __version__ = 'X.Y.Z'")
    return found[0]


def parts(version: str) -> tuple[int, int, int]:
    m = VERSION.match(version)
    if not m:
        raise ValueError(f'the version must be X.Y.Z (like 1.0.41), not {version!r}')
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bumped_changelog(text: str, version: str) -> str:
    lines = text.split('\n')
    at = [i for i, line in enumerate(lines) if line.rstrip() == UNRELEASED]
    if len(at) != 1:
        raise ValueError(f'CHANGELOG.md needs exactly one "{UNRELEASED}" section with the changes to release '
                         f'(found {len(at)})')
    end = next((i for i in range(at[0] + 1, len(lines)) if lines[i].startswith('## ')), len(lines))
    if not '\n'.join(lines[at[0] + 1:end]).strip():
        raise ValueError(f'the "{UNRELEASED}" section of CHANGELOG.md is empty: nothing to release')
    lines[at[0]] = f'## {version}'
    return '\n'.join(lines)


def main(argv: list[str], root: Path = ROOT, say=None) -> int:
    say = say or (lambda line: print(line, file=sys.stderr))
    if len(argv) != 1:
        say('usage: python tools/bump.py X.Y.Z')
        return 2
    version = argv[0]
    changelog_path, init_path = root / 'CHANGELOG.md', root / 'stack_ui' / '__init__.py'
    try:
        now = current_version(root)
        if parts(version) <= parts(now):
            raise ValueError(f'{version} is not higher than {now}')
        changelog = bumped_changelog(changelog_path.read_bytes().decode('utf-8'), version)
        init = INIT_LINE.sub(f"__version__ = '{version}'", init_path.read_bytes().decode('utf-8'))
    except (ValueError, OSError) as exc:
        say(f'ERROR: {exc}')
        return 1
    changelog_path.write_bytes(changelog.encode('utf-8'))       # bytes: LF on Windows too
    init_path.write_bytes(init.encode('utf-8'))
    print(f'{now} -> {version}: CHANGELOG.md and stack_ui/__init__.py. Next: the whole test suite (Windows and Linux).')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
