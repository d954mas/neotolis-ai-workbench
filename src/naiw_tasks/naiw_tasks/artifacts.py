"""Artifact-bundle helpers for finish/recover.

Lives outside lifecycle.py because the recursive overwrite of
meta/artifacts/output/ on capture-retry needs shutil.rmtree — a primitive
that lifecycle.py is deliberately forbidden from using
(test_lifecycle_module_does_not_call_rmdir / test_finish_never_uses_rm_rf
guard against accidental raw recursive-delete of git worktrees).
"""

import errno
import logging
import os
import shutil
import stat
from pathlib import Path


def replicate_output_tree(src_root: Path, dst_root: Path) -> None:
    """Mirror src_root at dst_root using hard-links with copy-fallback.

    Defenses against a hostile Pi:
      - os.walk(followlinks=False): a planted directory symlink like
        io/output/loop -> .. cannot drive an unbounded walk.
      - lstat + S_ISREG + os.link / O_NOFOLLOW copy on
        EXDEV/EPERM/EMLINK: a planted file symlink like
        output/secret.txt -> /etc/passwd cannot resolve and exfiltrate
        the host file.

    Stale-state contract: dst_root is wiped before replication so a retry
    after a partial capture (operator-driven re-finish, or _capture
    bailed mid-walk) does not leave behind files that have since been
    deleted from io/output/. Caller runs inside teardown_and_mark's
    lifecycle scope, so the temporary non-atomicity is invisible to other
    operations.
    """
    logger = logging.getLogger("naiw_tasks")
    if dst_root.exists():
        shutil.rmtree(dst_root, ignore_errors=True)
    if not src_root.exists():
        return
    for dirpath, _dirnames, filenames in os.walk(
        src_root, topdown=True, followlinks=False
    ):
        rel = Path(dirpath).relative_to(src_root)
        target_dir = dst_root / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in filenames:
            src = Path(dirpath) / fname
            dst = target_dir / fname
            try:
                st = src.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode):
                logger.warning(
                    "artifact capture: %s not a regular file "
                    "(mode=%o); skipping",
                    src, st.st_mode,
                )
                continue
            try:
                os.link(str(src), str(dst))
                continue
            except OSError as exc:
                if exc.errno not in (
                    errno.EXDEV, errno.EPERM, errno.EMLINK,
                ):
                    logger.warning(
                        "artifact capture: os.link failed for %s: %s; "
                        "skipping",
                        src, exc,
                    )
                    continue
            # O_NOFOLLOW fd defends against a TOCTOU swap (Pi replacing
            # the regular file with a symlink between lstat and open) —
            # raises ELOOP instead of resolving to the symlink target.
            try:
                fd = os.open(str(src), os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as exc:
                logger.warning(
                    "artifact capture: O_NOFOLLOW open failed "
                    "for %s: %s; skipping",
                    src, exc,
                )
                continue
            try:
                with os.fdopen(fd, "rb") as fsrc:
                    dst.write_bytes(fsrc.read())
                shutil.copystat(
                    str(src), str(dst), follow_symlinks=False
                )
            except OSError as exc:
                logger.warning(
                    "artifact capture: copy fallback failed "
                    "for %s: %s",
                    src, exc,
                )
