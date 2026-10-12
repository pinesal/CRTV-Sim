"""Resolve physical assets independently of their programming placements.

External ownership is immutable: callers may read external files, never edit them.
Every modifying service must check this boundary, not just its UI or batch query.
"""
from pathlib import Path, PureWindowsPath
import os


def asset_root(connection, config, asset_id):
    row = connection.execute(
        "SELECT r.* FROM storage_roots r JOIN assets a ON a.source_root_id=r.id WHERE a.id=?",
        (asset_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown asset {asset_id}")
    # External roots were canonicalized at registration. Do not contact an
    # offline network root merely to resolve an unrelated managed asset.
    path = Path(row['absolute_path']) if row['source_kind'] == 'external' else config.storage_root(row['logical_name']).resolve()
    return path, row['source_kind']


def contained_path(root, relative):
    raw = str(relative).replace('\\', '/')
    if not raw or Path(raw).is_absolute() or PureWindowsPath(raw).drive or '..' in raw.split('/'):
        raise ValueError('Asset path must be relative and stay inside its storage root')
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Asset path escapes its storage root')
    return path


def resolve_asset_path(connection, config, asset_id):
    row = connection.execute('SELECT source_relative_path FROM assets WHERE id=?', (asset_id,)).fetchone()
    if row is None:
        raise ValueError(f'Unknown asset {asset_id}')
    root, _ = asset_root(connection, config, asset_id)
    return contained_path(root, row['source_relative_path'])


def is_external(connection, asset_id):
    row = connection.execute(
        "SELECT r.source_kind FROM assets a JOIN storage_roots r ON r.id=a.source_root_id WHERE a.id=?",
        (asset_id,),
    ).fetchone()
    return row is not None and row[0] == 'external'


def require_writable_path(connection, path):
    resolved = Path(path).resolve()
    for row in connection.execute("SELECT absolute_path FROM storage_roots WHERE source_kind='external'").fetchall():
        root = Path(row[0])
        if resolved.is_relative_to(root):
            raise ValueError('External media is read-only; this operation cannot modify its files')
    return resolved


def require_managed_asset(connection, config, asset_id):
    root, kind = asset_root(connection, config, asset_id)
    if kind != 'managed':
        raise ValueError('External media is read-only; normalization and source modification are unavailable')
    return require_writable_path(connection, resolve_asset_path(connection, config, asset_id))


def checked_replace(connection, source, target):
    require_writable_path(connection, source)
    require_writable_path(connection, target)
    os.replace(source, target)


def checked_unlink(connection, path, **kwargs):
    require_writable_path(connection, path)
    Path(path).unlink(**kwargs)
