"""Markdown link checker for release-scope documentation (release trust pack R0C).

Local relative links (files and heading anchors) are checked strictly for
release-scope documents. External links (http/https/mailto) are collected
report-only, without network access. Documents outside the release scope are
checked report-only as well, so unrelated historical-link failures never
block a release PR without review.

Usage:
    python scripts/check_links.py              # fail on release-scope breakage
    python scripts/check_links.py --report-all # also print out-of-scope issues
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$", re.MULTILINE)
IGNORED_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "tmp", "graphify-out", ".qwen"}

# Documents whose broken local links fail the check (release surface).
RELEASE_SCOPE_FILES = {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md"}
RELEASE_SCOPE_DIRS = {"docs/releases"}


def slugify(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = text.lower().strip()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def heading_slugs(markdown_text: str) -> set[str]:
    slugs: set[str] = set()
    counts: dict[str, int] = {}
    for match in HEADING_RE.finditer(markdown_text):
        slug = slugify(match.group("text"))
        seen = counts.get(slug, 0)
        counts[slug] = seen + 1
        slugs.add(slug if seen == 0 else f"{slug}-{seen}")
    return slugs


def iter_markdown_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return files


def is_release_scope(path: Path) -> bool:
    relative = path.relative_to(REPO_ROOT).as_posix()
    if relative in RELEASE_SCOPE_FILES:
        return True
    return any(
        relative.startswith(prefix + "/") for prefix in RELEASE_SCOPE_DIRS
    )


def check_file(path: Path) -> tuple[list[str], list[str]]:
    """Return (local_failures, external_reports) for one markdown file."""
    failures: list[str] = []
    externals: list[str] = []
    text = path.read_text(encoding="utf-8")
    for match in LINK_RE.finditer(text):
        target = match.group("target").strip()
        if target.startswith(("http://", "https://", "mailto:")):
            externals.append(target)
            continue
        if re.match(r"^[a-z][a-z0-9+.-]*://", target):
            continue
        fragment = ""
        link_path_text = target
        if "#" in target:
            link_path_text, fragment = target.split("#", 1)
        if link_path_text:
            resolved = (path.parent / link_path_text).resolve()
            if not resolved.exists():
                failures.append(f"broken local link: {target}")
                continue
            target_file = resolved
        else:
            target_file = path
        if fragment and target_file.suffix == ".md" and target_file.is_file():
            slugs = heading_slugs(target_file.read_text(encoding="utf-8"))
            if fragment.lower() not in slugs:
                failures.append(f"broken anchor: {target}")
    return failures, externals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-all", action="store_true", help="print out-of-scope failures too")
    args = parser.parse_args(argv)

    scope_failures: list[str] = []
    other_failures: list[str] = []
    external_count = 0
    for path in iter_markdown_files(REPO_ROOT):
        failures, externals = check_file(path)
        external_count += len(externals)
        if not failures:
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        if is_release_scope(path):
            scope_failures.extend(f"{relative}: {failure}" for failure in failures)
        else:
            other_failures.extend(f"{relative}: {failure}" for failure in failures)

    for failure in scope_failures:
        print(f"FAIL {failure}")
    if args.report_all:
        for failure in other_failures:
            print(f"report-only {failure}")
    print(
        f"check_links: release-scope failures={len(scope_failures)} "
        f"out-of-scope failures={len(other_failures)} "
        f"external links (report-only, not fetched)={external_count}"
    )
    return 1 if scope_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
