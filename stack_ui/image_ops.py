"""Images: what is on this host, pulling, tagging, and removing the unused ones."""
from __future__ import annotations

import re
from datetime import datetime

from . import podman_api as pa
from . import rollback
from . import stacks as st
from .core import operation, require_podman

ISO_TIME = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$')


def unix_time(value) -> int:
    """Seconds since 1970 from what Podman sends: a number (images) or an ISO date with nanoseconds and a zone
    (containers, e.g. 2026-10-08T06:52:56.251284638+02:00); 0 when there is nothing usable."""
    if isinstance(value, (int, float)):
        return int(value)
    m = ISO_TIME.match(str(value or '').strip())
    if not m:
        return 0
    zone = m.group(3) or 'Z'
    stamp = datetime.fromisoformat(m.group(1) + ('+00:00' if zone == 'Z' else zone))
    return int(stamp.timestamp())


def short_id(image_id: str) -> str:
    return str(image_id or '').split(':', 1)[-1]


def image_table(containers: list[dict], imgs: list[dict], endpoint: int | None) -> list[dict]:
    """The images with their tags, size and the stacks (or containers) using them. removable: untagged (the old
    image a pull replaced) and used by no container."""
    mods = [(m.name, st.container_names_of(m, c)) for m, c, _ in st.discover()]
    used: dict[str, set[str]] = {}
    for c in containers:
        owner = next((n for n, names in mods if st.belongs(c, n, names)), None)
        labels = c.get('Labels') or {}
        owner = owner or next((labels[x] for x in st.COMPOSE_PROJECT_LABELS if labels.get(x)), None) or st.cname(c)
        used.setdefault(short_id(c.get('ImageID')), set()).add(owner)
    kept = rollback.kept_images()
    rows = []
    for img in imgs:
        iid = short_id(img.get('Id'))
        tags = [t for t in img.get('RepoTags') or [] if t and t != '<none>:<none>']
        users = sorted(used.get(iid, set()))
        rows.append({'id': iid, 'endpoint': endpoint, 'tags': tags, 'size': int(img.get('Size') or 0),
                     'created': unix_time(img.get('Created')), 'usedBy': users, 'keptFor': sorted(kept.get(iid, [])),
                     'removable': not tags and not users and not kept.get(iid)})
    return rows


def all_images() -> list[dict]:
    rows = image_table(pa.containers(), pa.images(), None)
    rows.sort(key=lambda r: (not r['usedBy'], r['tags'][0] if r['tags'] else '~', -r['created']))
    return rows


def images() -> dict:
    rows = all_images()
    return {'images': rows, 'reclaimable': sum(r['size'] for r in rows if r['removable'])}


def prune(data: dict) -> dict:
    """Remove the chosen images, but only those that are still removable (untagged and unused) right now."""
    wanted = {str(x) for x in data.get('ids') or []}
    removed, errors, freed = [], [], 0
    with operation('images-prune', f'{len(wanted)} images'):
        for r in all_images():
            if r['id'] not in wanted or not r['removable']:
                continue
            try:
                pa.remove_image(r['id'])
                removed.append(r['id'])
                freed += r['size']
            except pa.PodmanError as exc:
                errors.append(f'{r["id"][:12]}: HTTP {exc.code}')
    return {'removed': removed, 'freed': freed, 'errors': errors}


IMAGE_REF = re.compile(r'^[a-z0-9][a-z0-9._/:@-]{0,254}$')
IMAGE_TAG = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')


def find_image(iid: str) -> dict:
    row = next((r for r in all_images() if r['id'] == short_id(iid)), None)
    if row is None:
        raise ValueError(f'no image "{iid}"')
    return row


def image_pull(data: dict) -> dict:
    """Pull an image by reference (anonymous; up to 15 minutes)."""
    require_podman()
    ref = str(data.get('reference') or '').strip()
    if not IMAGE_REF.match(ref):
        raise ValueError('the image: e.g. docker.io/library/nginx:1.27 (lowercase, no spaces)')
    with operation('image-pull', ref):
        pa.pull_image(ref)
    return {'pulled': ref}


def image_tag(data: dict) -> dict:
    """{id, repo, tag}: one more name for an image."""
    require_podman()
    repo, tag = str(data.get('repo') or '').strip(), str(data.get('tag') or '').strip()
    if not IMAGE_REF.match(repo) or '@' in repo:
        raise ValueError('the repository: e.g. localhost/nginx (lowercase, no tag)')
    if not IMAGE_TAG.match(tag):
        raise ValueError('the tag: letters, digits, _ . - (e.g. 1.27 or keep)')
    with operation('image-tag', str(data.get('id') or '')):
        row = find_image(str(data.get('id') or ''))
        pa.tag_image(row['id'], repo, tag)
    return {'tagged': f'{repo}:{tag}'}


def remove_image_row(row: dict) -> None:
    """Remove the image name by name, without force: Podman itself refuses if a container started using it
    meanwhile (force would remove that container too)."""
    for tag in row['tags'][1:]:
        pa.remove_image(tag)
    pa.remove_image(row['tags'][0] if row['tags'] else row['id'])


def image_label(row: dict) -> str:
    return row['tags'][0] if row['tags'] else row['id'][:12]


def images_remove(ids) -> dict:
    """Remove several images: {removed: [ids], freed, errors: [...]}. One that is unknown or used by a container
    (checked again here) is reported and skipped; the others are still removed."""
    if not isinstance(ids, list) or not ids or not all(isinstance(x, str) for x in ids):
        raise ValueError('ids: a list of image ids')
    removed, errors, freed = [], [], 0
    with operation('images-remove', f'{len(ids)} images'):
        rows = {r['id']: r for r in all_images()}
        for iid in dict.fromkeys(short_id(x) for x in ids):
            row = rows.get(iid)
            if row is None:
                errors.append(f'{iid[:12]}: no such image')
            elif row['usedBy']:
                errors.append(f'{image_label(row)}: used by {", ".join(row["usedBy"])}')
            elif row['keptFor']:
                errors.append(f'{image_label(row)}: kept for the rollback of {", ".join(row["keptFor"])} '
                              "(discard the rollback point on the stack's page first)")
            else:
                try:
                    remove_image_row(row)
                except pa.PodmanError as exc:
                    errors.append(f'{image_label(row)}: {exc}')
                    continue
                removed.append(row['id'])
                freed += row['size']
    return {'removed': removed, 'freed': freed, 'errors': errors}


def image_remove(data: dict) -> dict:
    """{id}: remove one image, only if no container (running or stopped) uses it, checked again here.
    {ids: [...]}: remove several (see images_remove)."""
    require_podman()
    if 'ids' in data:
        return images_remove(data['ids'])
    with operation('image-remove', str(data.get('id') or '')):
        row = find_image(str(data.get('id') or ''))
        if row['usedBy']:
            raise ValueError(f'the image is used by {", ".join(row["usedBy"])}')
        if row['keptFor']:
            raise ValueError(f'the image is kept for the rollback of {", ".join(row["keptFor"])} '
                             "(discard the rollback point on the stack's page first)")
        remove_image_row(row)
    return {'removed': row['id'], 'freed': row['size']}


def image_details(iid: str) -> dict:
    require_podman()
    row = find_image(iid)
    info = pa.image(row['id'])
    return {'id': row['id'], 'tags': row['tags'], 'digests': info.get('RepoDigests') or [], 'size': row['size'],
            'created': row['created'], 'architecture': info.get('Architecture') or '', 'os': info.get('Os') or '',
            'labels': info.get('Labels') or {}, 'layers': len((info.get('RootFS') or {}).get('Layers') or []),
            'usedBy': row['usedBy']}
