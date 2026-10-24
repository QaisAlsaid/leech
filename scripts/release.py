#!/usr/bin/env python3
"""
                    - leech release tooling -
the version rule:
    canary  ->  X.Y.Z-beta   (the living branch, supervisor auto-deploys)
    master  ->  X.Y.Z        (stable. promotion strips the -beta)
    tags    ->  vX.Y.Z       (one per release, created by finalize)

commands:
  read                       print the pyproject version
  check-version --target T   gate: master -> clean X.Y.Z, canary/dev
                             -> X.Y.Z-beta.  exit 1 + hint on violation.
  promote                    on canary: cut release/vX.Y.Z (stripped) +
                             changelog since origin/master.  no push - the
                             workflow pushes and opens the PR.
  finalize                   on master, idempotent: strip -beta if it slipped
                             through (best-effort push), tag vX.Y.Z, push the
                             tag, write release notes.  no-op when tagged.
  bump-canary --to V         on canary: merge origin/master (-X ours), set
                             version to V, commit, push canary.
  next-version               print the next canary version for this release
                             (patch bump + -beta).

outputs: every command prints key=value lines to stdout; when
$GITHUB_OUTPUT is present (github actions) the same pairs are appended
there for ${{ steps.<id>.outputs.<key> }}.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

VERSION_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(-(?:beta|b)(?:\.(\d+))?)?$", re.IGNORECASE
)
BETA_SUFFIX_RE = re.compile(r"-(?:beta|b)(?:\.\d+)?$", re.IGNORECASE)
VERSION_LINE_RE = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.MULTILINE)


class ReleaseError(Exception):
    pass


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and proc.returncode != 0:
        raise ReleaseError(
            f"git {' '.join(args)} failed:\n{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def _output(key: str, value: str) -> None:
    print(f"{key}={value}")
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


def _summary(line: str) -> None:
    print(f"note: {line}")
    gh = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh:
        with open(gh, "a", encoding="utf-8") as fh:
            fh.write(f"{line}\n")


def parse_version(raw: str) -> tuple[str, bool]:
    """-> (core 'X.Y.Z', is_beta).  raises ReleaseError on junk."""
    m = VERSION_RE.fullmatch((raw or "").strip())
    if not m:
        raise ReleaseError(
            f"pyproject version {raw!r} doesn't match X.Y.Z or X.Y.Z-beta "
            f"(or X.Y.Z-b / X.Y.Z-beta.N)."
        )
    core = f"{int(m.group(1))}.{int(m.group(2))}.{int(m.group(3))}"
    beta = BETA_SUFFIX_RE.search(raw.strip()) is not None
    return core, beta


def next_beta(core: str) -> str:
    major, minor, patch = (int(x) for x in core.split("."))
    return f"{major}.{minor}.{patch + 1}-beta"


def read_version(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        raise ReleaseError(f"no pyproject.toml under {root}")
    with open(pyproject, "rb") as fh:
        data = tomllib.load(fh)
    version = (data.get("project") or {}).get("version")
    if not version:
        raise ReleaseError("pyproject.toml has no [project] version")
    return str(version).strip()


def write_version(root: Path, version: str) -> None:
    pyproject = root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    new_text, count = VERSION_LINE_RE.subn(
        lambda m: m.group(1) + version + m.group(3), text, count=1
    )
    if count != 1:
        raise ReleaseError('could not find a version = "..." line in pyproject.toml')
    if new_text != text:
        pyproject.write_text(new_text, encoding="utf-8")


def _require_clean(root: Path) -> None:
    if (
        _git(root, "diff", "--quiet", check=False).returncode != 0
        or _git(root, "diff", "--cached", "--quiet", check=False).returncode != 0
    ):
        raise ReleaseError("working tree is dirty - commit or stash first")


def _fetch(root: Path) -> None:
    _git(root, "fetch", "origin", "--tags", "--prune")


def _ensure_identity(root: Path) -> None:
    """runners ship no git identity, so the first commit/tag dies with
    'Author identity unknown'.  fill repo-local gaps with a bot ident -
    an identity that already resolves (local or global) is never
    touched."""
    for key, value in (
        ("user.name", "release-bot"),
        ("user.email", "release-bot@users.noreply.github.com"),
    ):
        proc = _git(root, "config", key, check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            _git(root, "config", key, value)
            _summary(
                f"git {key} did not resolve - set {value!r} for the " f"release commits"
            )


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _tag_commit(root: Path, tag: str) -> str | None:
    proc = _git(
        root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}", check=False
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def _temp_file(text: str, prefix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".md", prefix=prefix)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def cmd_read(root: Path) -> int:
    print(read_version(root))
    return 0


def cmd_check_version(root: Path, target: str) -> int:
    version = read_version(root)
    core, beta = parse_version(version)
    target = (target or "").strip().lower().removeprefix("origin/")
    if target == "master":
        if beta:
            print(
                f"FAIL: this lands on master, but the version is {version!r}: "
                f"master releases are clean (no -beta).  use the promote "
                f"workflow (Actions -> promote -> Run) or strip the suffix "
                f"yourself before merging."
            )
            return 1
        print(f"ok: master version {version!r} is clean")
        return 0
    if target in ("canary", "dev"):
        if not beta:
            print(
                f"FAIL: {target} carries beta versions (X.Y.Z-beta), found "
                f"{version!r}.  if this is the start of a new cycle, bump it "
                f"like {next_beta(core)}."
            )
            return 1
        print(f"ok: {target} version {version!r} is beta")
        return 0
    print(f"note: no version rule for target {target!r}: passing through")
    return 0


def cmd_promote(root: Path) -> int:
    _require_clean(root)
    _fetch(root)
    _ensure_identity(root)
    head = _head(root)

    master = _git(
        root, "rev-parse", "-q", "--verify", "origin/master", check=False
    ).stdout.strip()
    if master and master == head:
        raise ReleaseError(
            "HEAD is origin/master - run promote from canary "
            "(the workflow checks out canary)."
        )

    version = read_version(root)
    core, beta = parse_version(version)
    if not beta:
        raise ReleaseError(
            f"canary must carry a -beta version, found {version!r}. "
            f"next cycle would be {next_beta(core)}. set that in "
            f"pyproject.toml on canary first."
        )
    tag = f"v{core}"
    if _tag_commit(root, tag) is not None:
        raise ReleaseError(
            f"tag {tag} already exists: bump the version on canary "
            f"(e.g. {next_beta(core)}) before promoting again."
        )
    branch = f"release/{tag}"
    if (
        _git(
            root, "rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False
        ).returncode
        == 0
    ):
        raise ReleaseError(
            f"branch {branch} already exists - delete the stale one "
            f"(git push origin --delete {branch}) and re-run."
        )

    if master:
        log = _git(root, "log", "--oneline", f"origin/master..{head}").stdout.strip()
    else:
        log = _git(root, "log", "--oneline", "-30").stdout.strip()
    if not log:
        raise ReleaseError("nothing to release - canary has no commits past master")
    changelog = _temp_file(
        f"release {tag}\n\npromotes canary -> master and strips the -beta "
        f"suffix ({version} -> {core}).\n\ncommits:\n{log}\n",
        "changelog-",
    )

    _git(root, "checkout", "-b", branch)
    write_version(root, core)
    _git(root, "add", "pyproject.toml")
    _git(
        root, "commit", "-m", f"release: {tag} (strip -beta for master)", "--no-verify"
    )

    _output("version", core)
    _output("tag", tag)
    _output("branch", branch)
    _output("changelog", changelog)
    _output("next_version", next_beta(core))
    return 0


def cmd_finalize(root: Path) -> int:
    _require_clean(root)
    _fetch(root)
    _ensure_identity(root)

    version = read_version(root)
    core, beta = parse_version(version)
    if beta:
        write_version(root, core)
        _git(root, "add", "pyproject.toml")
        _git(root, "commit", "-m", f"release: v{core} (strip -beta)", "--no-verify")
        push = _git(root, "push", "origin", "HEAD:master", check=False)
        if push.returncode != 0:
            _summary(
                f"could not push the -beta strip commit to master "
                f"(protected?) - tagged anyway; the next promote merge "
                f"will fix pyproject. push said: "
                f"{(push.stderr or push.stdout).strip()[:400]}"
            )
        else:
            _output("stripped", "true")

    tag = f"v{core}"
    existing = _tag_commit(root, tag)
    head = _head(root)
    if existing is not None:
        if existing == head:
            print(f"{tag} already tags this commit - nothing to do")
            _output("tagged", "false")
            _output("version", core)
            _output("tag", tag)
            _output("next_version", next_beta(core))
            return 0
        if (
            _git(
                root, "merge-base", "--is-ancestor", existing, head, check=False
            ).returncode
            == 0
        ):
            print(
                f"{tag} tags an ancestor of HEAD - no version bump since, "
                f"nothing to release"
            )
            _output("tagged", "false")
            _output("version", core)
            _output("tag", tag)
            _output("next_version", next_beta(core))
            return 0
        raise ReleaseError(
            f"tag {tag} exists at {existing[:12]} but master is at "
            f"{head[:12]} - looks like a version reuse; bump canary first."
        )

    prev = _git(root, "describe", "--tags", "--abbrev=0", "HEAD", check=False)
    if prev.returncode == 0:
        rng = f"{prev.stdout.strip()}..HEAD"
    else:
        rng = "HEAD"
    log = _git(root, "log", "--oneline", rng).stdout.strip() or "no changes"
    notes = _temp_file(f"release {tag}\n\ncommits:\n{log}\n", "notes-")

    _git(root, "tag", "-a", tag, "-m", f"release {tag}")
    _git(root, "push", "origin", f"refs/tags/{tag}")

    _output("tagged", "true")
    _output("version", core)
    _output("tag", tag)
    _output("notes", notes)
    _output("next_version", next_beta(core))
    return 0


def cmd_bump_canary(root: Path, to: str) -> int:
    _require_clean(root)
    core, beta = parse_version(to)
    if not beta:
        raise ReleaseError(f"--to must be a beta version (X.Y.Z-beta), got {to!r}")
    _fetch(root)
    _ensure_identity(root)

    master = _git(
        root, "rev-parse", "-q", "--verify", "origin/master", check=False
    ).stdout.strip()
    if master:
        contains = _git(
            root, "merge-base", "--is-ancestor", master, "HEAD", check=False
        )
        if contains.returncode != 0:
            merge = _git(root, "merge", "--no-edit", "-X", "ours", master, check=False)
            if merge.returncode != 0:
                _git(root, "merge", "--abort", check=False)
                raise ReleaseError(
                    "back-merge of master into canary conflicted beyond "
                    "-X ours - merge manually and re-run bump-canary."
                )
            print("merged master back into canary")

    write_version(root, to)
    _git(root, "add", "pyproject.toml")
    if _git(root, "diff", "--cached", "--quiet", check=False).returncode != 0:
        _git(root, "commit", "-m", f"canary: bump {to} (next cycle)", "--no-verify")

    push = _git(root, "push", "origin", "HEAD:canary", check=False)
    if push.returncode != 0:
        raise ReleaseError(
            "could not push canary (moved during release?) - pull and "
            f"re-run bump-canary. push said: "
            f"{(push.stderr or push.stdout).strip()[:400]}"
        )
    _output("version", to)
    return 0


def cmd_next_version(root: Path) -> int:
    version = read_version(root)
    core, _ = parse_version(version)
    print(next_beta(core))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=".", help="git checkout root")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("read")
    chk = sub.add_parser("check-version")
    chk.add_argument("--target", required=True)
    sub.add_parser("promote")
    sub.add_parser("finalize")
    bump = sub.add_parser("bump-canary")
    bump.add_argument("--to", required=True)
    sub.add_parser("next-version")

    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    if not (root / ".git").exists():
        print(f"error: {root} is not a git checkout", file=sys.stderr)
        return 2

    try:
        if args.command == "read":
            return cmd_read(root)
        if args.command == "check-version":
            return cmd_check_version(root, args.target)
        if args.command == "promote":
            return cmd_promote(root)
        if args.command == "finalize":
            return cmd_finalize(root)
        if args.command == "bump-canary":
            return cmd_bump_canary(root, args.to)
        if args.command == "next-version":
            return cmd_next_version(root)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
