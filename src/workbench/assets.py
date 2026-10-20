"""Canonical product inputs in a source tree or an unpacked Python installation.

Installed assets are package resources, never searched in the current project.
Logical source names stay stable for compatibility policy and detached verification.
"""
from importlib.resources import files
from pathlib import Path


def package_root() -> Path:
    resource = files('workbench')
    if not isinstance(resource, Path):
        raise RuntimeError('Workbench requires an unpacked package installation')
    return resource


def runtime_root() -> Path:
    """Resource root; canonical_path maps logical Python names to installed modules."""
    package = package_root()
    installed = package / '_assets'
    if installed.is_dir():
        return installed
    # Explicit source layout fallback only. Installed defaults never guess parents.
    if package.parent.name == 'src':
        source = package.parent.parent
        if (source / 'pyproject.toml').is_file() and (source / 'omp_bridge/g3/entry.ts').is_file():
            return source
    raise FileNotFoundError('Workbench runtime assets are unavailable')


def canonical_path(relative: str, root: Path | None = None) -> Path:
    """Resolve a policy-relative product path, optionally in a detached source tree."""
    logical = Path(relative)
    if logical.is_absolute() or '..' in logical.parts or not logical.parts:
        raise ValueError('unsafe canonical asset path')
    selected = runtime_root() if root is None else Path(root)
    package = package_root()
    if selected == package / '_assets' and logical.parts[:2] == ('src', 'workbench'):
        return package.joinpath(*logical.parts[2:])
    return selected / logical
