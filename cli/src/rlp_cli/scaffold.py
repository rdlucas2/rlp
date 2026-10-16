"""Render and update repos from rlp Copier templates."""

from __future__ import annotations

from pathlib import Path


class ScaffoldError(Exception):
    """Copier failed."""


def parse_data(pairs: tuple[str, ...]) -> dict[str, str]:
    data = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ScaffoldError(f"--data expects KEY=VALUE, got {pair!r}")
        data[key] = value
    return data


def new(template: str, dest: Path, data: dict[str, str], defaults: bool, vcs_ref: str | None, trust: bool = False) -> None:
    import copier  # imported lazily: it is most of rlp's startup time

    try:
        copier.run_copy(
            template,
            dest,
            data=data,
            defaults=defaults,
            vcs_ref=vcs_ref,
            unsafe=trust,
            quiet=True,
        )
    except Exception as err:  # copier raises many types; report them uniformly
        raise ScaffoldError(f"copier copy failed: {err}") from err


def update(dest: Path, data: dict[str, str], defaults: bool, vcs_ref: str | None, trust: bool = False) -> None:
    import copier  # imported lazily: it is most of rlp's startup time

    try:
        copier.run_update(
            dest,
            data=data,
            defaults=defaults,
            vcs_ref=vcs_ref,
            overwrite=True,
            unsafe=trust,
            quiet=True,
        )
    except Exception as err:  # copier raises many types; report them uniformly
        raise ScaffoldError(f"copier update failed: {err}") from err
