"""PEP 517 wrapper that makes setuptools sdists byte reproducible."""
from __future__ import annotations

import gzip
import io
import os
from pathlib import Path
import tarfile

from setuptools.build_meta import build_editable
from setuptools.build_meta import build_sdist as _setuptools_build_sdist
from setuptools.build_meta import build_wheel
from setuptools.build_meta import get_requires_for_build_editable
from setuptools.build_meta import get_requires_for_build_sdist
from setuptools.build_meta import get_requires_for_build_wheel
from setuptools.build_meta import prepare_metadata_for_build_editable
from setuptools.build_meta import prepare_metadata_for_build_wheel


DEFAULT_SOURCE_DATE_EPOCH = 1704067200


def _source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH", str(DEFAULT_SOURCE_DATE_EPOCH))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("SOURCE_DATE_EPOCH must be an integer") from exc
    if value < 0:
        raise RuntimeError("SOURCE_DATE_EPOCH must be non-negative")
    return value


def _canonicalize_sdist(path: Path, epoch: int) -> None:
    entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(path, "r:gz") as source:
        for member in source.getmembers():
            extracted = source.extractfile(member) if member.isfile() else None
            entries.append((member, extracted.read() if extracted else None))

    temporary = path.with_name(path.name + ".canonical.tmp")
    with temporary.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw_output,
            mtime=epoch,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as target:
                for member, data in sorted(entries, key=lambda item: item[0].name):
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    member.mtime = epoch
                    member.pax_headers = {}
                    member.mode = 0o755 if member.isdir() else 0o644
                    target.addfile(
                        member,
                        io.BytesIO(data) if data is not None else None,
                    )
    os.replace(temporary, path)


def build_sdist(sdist_directory, config_settings=None):
    """Build with setuptools, then canonicalize the returned tar.gz in place."""
    filename = _setuptools_build_sdist(sdist_directory, config_settings)
    _canonicalize_sdist(Path(sdist_directory) / filename, _source_date_epoch())
    return filename
