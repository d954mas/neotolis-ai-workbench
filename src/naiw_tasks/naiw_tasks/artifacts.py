"""Artifact-bundle helpers for finish/recover.

The whole capture flow lives here (not just the hard-link mirror): lifecycle.py
is deliberately forbidden from calling shutil.rmtree
(test_lifecycle_module_does_not_call_rmdir / test_finish_never_uses_rm_rf
guard against accidental raw recursive-delete of git worktrees), and the
rmtree-on-retry of meta/artifacts/output/ keeps that primitive contained to
this module.
"""

import errno
import logging
import os
import shutil
import stat
import subprocess
from contextlib import suppress
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
            # follow_symlinks=False: Python's os.link defaults to True and
            # calls linkat(AT_SYMLINK_FOLLOW), so a Pi swap of src to a
            # symlink between lstat and link would hardlink the symlink's
            # target (e.g. /etc/shadow). With False, the link goes to the
            # symlink inode itself — still wrong for an artifact bundle,
            # so re-check dst with lstat and fall through to copy if it
            # came out as a symlink.
            linked = False
            try:
                os.link(str(src), str(dst), follow_symlinks=False)
                linked = True
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
            if linked:
                try:
                    dst_st = dst.lstat()
                except OSError:
                    continue
                if not stat.S_ISLNK(dst_st.st_mode):
                    continue
                # Pi raced; rip the symlink out of the bundle and copy
                # via the O_NOFOLLOW path below, which will refuse the
                # swap with ELOOP and leave dst absent.
                with suppress(OSError):
                    dst.unlink()
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


def copy_io_file(src: Path, dst: Path) -> None:
    """Copy a single io/ regular file to dst with O_NOFOLLOW defense.

    Used for terminal.log and summary.md captures where hard-link is not
    desired — terminal.log is treated as append-only by pipe-pane even
    after teardown by image conventions, and a copy decouples the artifact
    from any post-finish Pi behaviour.
    """
    logger = logging.getLogger("naiw_tasks")
    try:
        st = src.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        logger.warning(
            "artifact capture: %s not a regular file "
            "(mode=%o); skipping",
            src, st.st_mode,
        )
        return
    try:
        fd = os.open(str(src), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        logger.warning(
            "artifact capture: cannot open %s: %s; skipping",
            src, exc,
        )
        return
    try:
        with os.fdopen(fd, "rb") as fsrc:
            dst.write_bytes(fsrc.read())
        shutil.copystat(str(src), str(dst), follow_symlinks=False)
    except OSError as exc:
        logger.warning(
            "artifact capture: write/copystat failed for %s: %s",
            src, exc,
        )


def capture_bundle(
    task_dir: Path, task_data: dict, work_path: Path | None,
) -> None:
    """Bundle artifacts into meta/artifacts/. Best-effort: all failures
    are WARN-logged and swallowed so terminal-status write still happens.
    Repeated calls overwrite (no artifact versioning).
    """
    logger = logging.getLogger("naiw_tasks")
    artifacts = task_dir / "meta" / "artifacts"
    try:
        artifacts.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "artifact capture: cannot create %s: %s; aborting capture",
            artifacts, exc,
        )
        return

    io_dir = task_dir / "io"
    copy_io_file(io_dir / "terminal.log", artifacts / "terminal.log")
    copy_io_file(io_dir / "summary.md", artifacts / "summary.md")
    replicate_output_tree(io_dir / "output", artifacts / "output")

    # Git captures — project tasks only, gated by task.json.kind (NOT by
    # .git/ existence). A Pi-corrupted .git on a project task surfaces the
    # git error rather than being silently mistaken for a generic task.
    if task_data.get("kind") != "project":
        return
    if work_path is None or not work_path.exists():
        logger.warning(
            "artifact capture: project task missing work_path %r; "
            "skipping git captures",
            work_path,
        )
        return

    def _run_git(*args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            ["git", "-C", str(work_path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def _run_git_bytes(*args: str) -> "subprocess.CompletedProcess[bytes]":
        """Like _run_git but keeps stdout as raw bytes.

        `git diff --binary` may emit non-UTF-8 bytes inside the binary-patch
        section (literal/delta blobs). Decoding with errors='replace' corrupts
        them to U+FFFD and the resulting diff.patch no longer applies via
        `git apply --binary`. Use this for any captured output that must
        round-trip unchanged.
        """
        return subprocess.run(
            ["git", "-C", str(work_path), *args],
            capture_output=True,
            check=False,
        )

    base = task_data.get("base_commit")

    try:
        r = _run_git("status", "--porcelain=v2", "--branch")
        (artifacts / "git-status.txt").write_text(
            r.stdout, encoding="utf-8"
        )
        if r.returncode != 0:
            logger.warning(
                "artifact capture: git status non-zero (%d): %s",
                r.returncode, r.stderr.strip(),
            )
    except OSError as exc:
        logger.warning("artifact capture: git status write: %s", exc)

    if not base:
        logger.warning(
            "artifact capture: no base_commit on project task; "
            "skipping diff captures",
        )
        return

    # `git diff <base>` (no `..HEAD`) captures committed + uncommitted
    # changes against the working tree — Pi commonly edits without
    # committing. Untracked files added to changed-files.txt with `??` tag.
    try:
        r = _run_git("diff", "--name-status", base)
        changed = r.stdout
        if r.returncode != 0:
            logger.warning(
                "artifact capture: git diff --name-status non-zero "
                "(%d): %s", r.returncode, r.stderr.strip(),
            )
        r_unt = _run_git("ls-files", "--others", "--exclude-standard")
        if r_unt.returncode == 0 and r_unt.stdout:
            untracked = "".join(
                f"??\t{line}\n"
                for line in r_unt.stdout.splitlines()
                if line
            )
            changed = changed + untracked
        (artifacts / "changed-files.txt").write_text(
            changed, encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(
            "artifact capture: changed-files.txt write: %s", exc,
        )

    try:
        rb = _run_git_bytes("diff", "--binary", base)
        (artifacts / "diff.patch").write_bytes(rb.stdout)
        if rb.returncode != 0:
            logger.warning(
                "artifact capture: git diff non-zero (%d): %s",
                rb.returncode,
                rb.stderr.decode("utf-8", errors="replace").strip(),
            )
    except OSError as exc:
        logger.warning("artifact capture: diff.patch write: %s", exc)
