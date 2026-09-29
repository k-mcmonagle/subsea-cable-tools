# -*- coding: utf-8 -*-
"""Build the QGIS plugin zip from a git ref.

Usage:  python scripts/build_release.py [REF] [--out-dir dist] [--allow-version-mismatch]

REF defaults to HEAD; pass a tag (``v1.9.0``) to build a release. The zip is
made with ``git archive`` from the committed tree - uncommitted changes are
never included - so it is reproducible from the ref alone:

* top-level folder ``subsea_cable_tools/`` - the plugin's package name on
  plugins.qgis.org (QGIS imports the plugin by folder name, so it must be a
  valid identifier; the repository folder ``subsea-cable-tools`` is not);
* file name ``subsea_cable_tools.<version>.zip`` in ``dist/``;
* paths marked ``export-ignore`` in ``.gitattributes`` (tests, CI, tooling,
  developer docs, local reference data, ...) are left out. The working
  tree's .gitattributes is used, so older tags get the same exclusions.

The zip is then validated: metadata.txt present with the required fields
and (for a tag) a version equal to the tag; classFactory and the metadata
icon present; no tests/, ref/, CI or tooling paths, no reference data files
(.mdb/.pthmdb) and no byte-code. Standard library only; needs ``git``.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "subsea_cable_tools"
REQUIRED_METADATA = ("name", "qgisMinimumVersion", "description", "about", "version",
                     "author", "email", "repository")
FORBIDDEN_PREFIXES = ("tests/", "ref/", ".github/", "scripts/", "docs/", ".agents/", ".codex/")
FORBIDDEN_NAMES = (".gitignore", ".gitattributes", "pyproject.toml", ".pre-commit-config.yaml",
                   "requirements-dev.txt")
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".mdb", ".pthmdb", ".accdb")


class BuildError(RuntimeError):
    pass


def git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                            encoding="utf-8")
    if result.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def parse_metadata(text: str) -> dict:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # keep camelCase keys
    parser.read_string(text)
    if not parser.has_section("general"):
        raise BuildError("metadata.txt has no [general] section")
    return dict(parser.items("general"))


def tag_version(ref: str):
    """'1.9.0' for a tag ref like v1.9.0 / 1.9.0 / refs/tags/v1.9.0; else None."""
    name = ref[len("refs/tags/"):] if ref.startswith("refs/tags/") else ref
    is_tag = subprocess.run(["git", "-C", str(ROOT), "show-ref", "--verify", "--quiet",
                             f"refs/tags/{name}"]).returncode == 0
    if not is_tag:
        return None
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*(?:[-.+]?\w+)*)", name)
    return match.group(1) if match else name


def validate(zip_path: Path, metadata: dict) -> list:
    problems = []
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    prefix = f"{PACKAGE}/"
    outside = [n for n in names if not n.startswith(prefix)]
    if outside:
        problems.append(f"entries outside {prefix}: {outside[:5]}")
    rel = [n[len(prefix):] for n in names if n.startswith(prefix)]
    files = [r for r in rel if r and not r.endswith("/")]
    for required in ("metadata.txt", "__init__.py"):
        if required not in files:
            problems.append(f"missing {required}")
    icon = metadata.get("icon")
    if icon and icon not in files:
        problems.append(f"metadata icon {icon!r} not in the zip")
    for path in files:
        if path.startswith(FORBIDDEN_PREFIXES) or ("/" not in path and path in FORBIDDEN_NAMES):
            problems.append(f"development/private path shipped: {path}")
        elif path.endswith(FORBIDDEN_SUFFIXES) or "__pycache__/" in path:
            problems.append(f"byte-code or reference data shipped: {path}")
    return problems


def summarise(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
    groups = {}
    for info in infos:
        rel = info.filename.split("/", 1)[1]
        top = rel.split("/", 1)[0] + ("/" if "/" in rel else "")
        count, size = groups.get(top, (0, 0))
        groups[top] = (count + 1, size + info.file_size)
    print(f"\n{zip_path.name}: {zip_path.stat().st_size / 1024:.0f} KiB compressed, "
          f"{sum(i.file_size for i in infos) / 1024:.0f} KiB uncompressed, {len(infos)} files")
    print(f"Top-level contents of {PACKAGE}/:")
    for top, (count, size) in sorted(groups.items(), key=lambda kv: (not kv[0].endswith("/"), kv[0])):
        print(f"  {top:38} {count:5} files {size / 1024:9.0f} KiB")


def build(ref: str, out_dir: Path, allow_mismatch: bool) -> Path:
    commit = git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    metadata = parse_metadata(git("show", f"{commit}:metadata.txt"))
    missing = [key for key in REQUIRED_METADATA if not metadata.get(key, "").strip()]
    if missing:
        raise BuildError(f"metadata.txt at {ref} lacks: {', '.join(missing)}")
    version = metadata["version"].strip()
    tagged = tag_version(ref)
    if tagged is not None and tagged != version:
        message = f"tag {ref} says {tagged} but metadata.txt at that commit says version={version}"
        if not allow_mismatch:
            raise BuildError(message + " (use --allow-version-mismatch to build anyway)")
        print(f"WARNING: {message}")
    if ref == "HEAD" and git("status", "--porcelain", "--untracked-files=no").strip():
        print("Note: the working tree has uncommitted changes; they are NOT in the zip "
              "(it is built from the HEAD commit).")

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{PACKAGE}.{version}.zip"
    git("archive", "--format=zip", "-9", "--worktree-attributes", f"--prefix={PACKAGE}/",
        "-o", str(zip_path), commit)
    print(f"Built {zip_path} from {ref} ({commit[:10]}), version {version}")
    problems = validate(zip_path, metadata)
    summarise(zip_path)
    if problems:
        zip_path.unlink()
        raise BuildError("zip failed validation (deleted):\n  " + "\n  ".join(problems))
    print("Validation passed.")
    return zip_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the QGIS plugin zip from a git ref.")
    parser.add_argument("ref", nargs="?", default="HEAD", help="commit or tag to package (default HEAD)")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist", help="output folder (default dist/)")
    parser.add_argument("--allow-version-mismatch", action="store_true",
                        help="build a tag whose name differs from metadata.txt's version")
    args = parser.parse_args(argv)
    try:
        path = build(args.ref, args.out_dir, args.allow_version_mismatch)
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if os.environ.get("GITHUB_OUTPUT"):  # consumed by .github/workflows/release.yml
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(f"zip={path.as_posix()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
