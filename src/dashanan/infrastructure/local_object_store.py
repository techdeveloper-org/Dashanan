"""LocalObjectStore: a filesystem-backed `ObjectStorePort` adapter (ADR-009).

Zone 8's object-store half (`dashanan.application.zone8_consolidation_
store.ObjectStorePort`), backed by ordinary files under one root
directory rather than a cloud object-store SDK -- this codebase adds no
new third-party dependency for Sprint 2 (mirrors `SqlProvenanceRepository`
et al.'s own "no database driver dependency added by this story"
convention, applied here to the object-store side instead of the
manifest side). A production deployment swaps this adapter for an S3/GCS/
Azure Blob client behind the same `ObjectStorePort` Protocol without
touching `Zone8ConsolidationStore`.

Content-addressing (must-not-deviate item 3, "immutable and content-
addressed -- never overwritten") is enforced here structurally: each
blob is written with `os.O_CREAT | os.O_EXCL`, the OS-level "create only
if absent, atomically" flag combination, so two concurrent writers racing
to create the same `blob_id` can never both succeed in overwriting one
another -- the loser's `FileExistsError` is caught and treated as the
no-op `put_if_absent` already promises, never surfaced as a failure.

PII NOTE: this adapter writes and reads exactly the bytes it is given --
it never decodes, parses, or logs blob content (dev_prompt's PII
constraint).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import BinaryIO

from dashanan.application.zone8_consolidation_store import (
    Zone8ObjectStoreUnavailableError,
    Zone8StorePortError,
)
from dashanan.domain.consolidated_blob import ByteRange

logger = logging.getLogger(__name__)


class LocalObjectStore:
    """Implements `ObjectStorePort` against `root_dir`, one file per blob.

    Each blob is stored as `root_dir / blob_id` with no extension --
    `blob_id` is always a 64-character lowercase hex SHA-256 digest
    (`dashanan.domain.consolidated_blob.compute_blob_id`), which is
    already a filesystem-safe filename on every platform this codebase
    targets (ASCII hex only, fixed length, no path separators).
    """

    def __init__(self, root_dir: Path) -> None:
        """Bind the adapter to `root_dir`, creating it if it does not yet exist.

        Args:
            root_dir: The directory every blob is stored directly under.
                Created (including parents) if missing.

        Raises:
            Zone8ObjectStoreUnavailableError: If `root_dir` cannot be
                created (e.g. a permissions failure) -- construction
                itself is treated as an availability check, since every
                subsequent `put_if_absent`/`get_range` call would fail
                the same way.
        """
        self._root_dir = root_dir
        try:
            self._root_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Zone8ObjectStoreUnavailableError(
                f"LocalObjectStore could not create root_dir '{root_dir}': {exc}"
            ) from exc

    def put_if_absent(self, blob_id: str, payload: bytes) -> None:
        """Write `payload` to `root_dir / blob_id`, atomically, only if absent.

        `FileExistsError` (the blob is already present) is caught and
        treated as success -- `ObjectStorePort.put_if_absent`'s own
        contract: a repeat write of content this adapter already holds
        under the same content-addressed `blob_id` is a no-op, never an
        error, and never touches the existing file's bytes.

        Raises:
            Zone8ObjectStoreUnavailableError: If the write fails for any
                reason OTHER than the blob already existing (e.g. disk
                full, permission denied, `root_dir` removed after
                construction).
        """
        path = self._root_dir / blob_id
        try:
            fd = _os_open_exclusive_create(path)
        except FileExistsError:
            logger.debug(
                "zone8 local object store: blob already present, no-op",
                extra={"blob_id": blob_id},
            )
            return
        except OSError as exc:
            raise Zone8ObjectStoreUnavailableError(
                f"LocalObjectStore could not create blob '{blob_id}': {exc}"
            ) from exc

        try:
            with _fdopen_write_binary(fd) as handle:
                handle.write(payload)
        except OSError as exc:
            raise Zone8ObjectStoreUnavailableError(
                f"LocalObjectStore could not write blob '{blob_id}': {exc}"
            ) from exc

    def get_range(self, blob_id: str, byte_range: ByteRange) -> bytes:
        """Read exactly `byte_range`'s slice of `root_dir / blob_id`.

        One `seek` plus one bounded `read` -- AC-008-1's "at most one
        ranged GET," never a full-file read followed by local slicing.

        Raises:
            Zone8StorePortError: If `blob_id` does not exist under
                `root_dir` -- a manifest/object-store consistency fault
                this adapter detected, not a store-availability issue.
            Zone8ObjectStoreUnavailableError: If the file exists but
                cannot be read (e.g. a permissions or I/O failure).
        """
        path = self._root_dir / blob_id
        try:
            with path.open("rb") as handle:
                handle.seek(byte_range.start)
                return handle.read(byte_range.length())
        except FileNotFoundError as exc:
            raise Zone8StorePortError(
                f"LocalObjectStore has no blob '{blob_id}' -- manifest/object-store "
                "inconsistency"
            ) from exc
        except OSError as exc:
            raise Zone8ObjectStoreUnavailableError(
                f"LocalObjectStore could not read blob '{blob_id}': {exc}"
            ) from exc


def _os_open_exclusive_create(path: Path) -> int:
    """Open `path` for exclusive, atomic creation (`O_CREAT | O_EXCL | O_WRONLY`).

    Isolated into its own function so `put_if_absent`'s two failure
    branches (already-exists vs. genuinely unavailable) stay easy to
    read side by side; `os.open` is used directly rather than
    `Path.open("xb")` only because it is the documented, portable way to
    combine the three flags explicitly.
    """
    return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)


def _fdopen_write_binary(fd: int) -> BinaryIO:
    """Wrap a raw file descriptor from `_os_open_exclusive_create` as a binary writer."""
    return os.fdopen(fd, "wb")
