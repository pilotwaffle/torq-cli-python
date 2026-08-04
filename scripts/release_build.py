"""Fail-closed v0.2.0 release artifact builder (release trust pack R0/R0B).

Builds the wheel and sdist exactly once from an exact candidate commit,
verifies artifact metadata, hashes artifacts, verifies the hashes against the
artifact bytes, installs each artifact into a clean environment for smoke
verification, and records release evidence, an SBOM, and a CI-provenance
record.

This tool NEVER publishes, tags, signs, or attests anything. During R0 the
outputs remain workflow artifacts only. Signing and attestation are separate
trust levels that are designed in docs/releases/release-trust-model.md and
must not be conflated with CI provenance.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "release" / "v0.2.0" / "release-contract.json"
RELEASE_EVIDENCE_SCHEMA_VERSION = "1.0.0"
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleaseContractError(RuntimeError):
    """Fail-closed contract violation with a stable machine reason."""


def _fail(reason: str, detail: str = "") -> None:
    suffix = f": {detail}" if detail else ""
    raise ReleaseContractError(f"{reason}{suffix}")


def load_contract(path: Path = CONTRACT_PATH) -> dict:
    if not path.is_file():
        _fail("release_contract_missing", str(path))
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail("release_contract_unparseable", str(exc))
    return contract


def contract_digest(contract: dict) -> str:
    payload = json.dumps(contract, indent=2, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def validate_expected_sha(expected_sha: str) -> str:
    candidate = expected_sha or ""
    if not _FULL_SHA_RE.fullmatch(candidate):
        _fail("candidate_sha_invalid_format", repr(expected_sha))
    return candidate


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        _fail("git_command_failed", f"git {' '.join(args)}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def verify_source_state(
    contract: dict,
    expected_sha: str,
    *,
    head_sha: str | None = None,
    porcelain: str | None = None,
    on_main: bool | None = None,
) -> dict:
    """Fail closed on dirty tree, wrong head, or non-main candidate.

    "Dirty" means any change to tracked content (``git status --porcelain
    --untracked-files=no``). Untracked local-tooling files cannot enter the
    wheel or sdist (both package tracked content only); the release workflow
    additionally runs on a fresh checkout, which is clean by construction.
    """
    expected = validate_expected_sha(expected_sha)
    head = head_sha if head_sha is not None else _git("rev-parse", "HEAD")
    if head != expected:
        _fail("candidate_sha_not_head", f"head={head} expected={expected}")
    dirty = (
        porcelain
        if porcelain is not None
        else _git("status", "--porcelain", "--untracked-files=no")
    )
    if dirty and not contract["policies"].get("dirty_tree_permitted", False):
        _fail("working_tree_dirty", dirty.splitlines()[0])
    pinned = contract["candidate"].get("sha")
    if pinned and pinned != expected:
        _fail("candidate_sha_contract_mismatch", f"contract={pinned} supplied={expected}")
    if not contract["policies"].get("non_main_release_permitted", False):
        is_on_main = on_main
        if is_on_main is None:
            merged = subprocess.run(
                ["git", "merge-base", "--is-ancestor", expected, "origin/main"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            is_on_main = merged.returncode == 0
        if not is_on_main:
            _fail("candidate_sha_not_on_protected_main", expected)
    return {"head_sha": head, "candidate_sha": expected}


def build_once(dist_dir: Path) -> tuple[Path, Path]:
    if dist_dir.exists() and any(dist_dir.iterdir()):
        _fail("dist_dir_not_empty", str(dist_dir))
    dist_dir.mkdir(parents=True)
    completed = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        _fail("build_failed", completed.stderr.strip()[-800:])
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        _fail("build_output_unexpected", f"wheels={wheels} sdists={sdists}")
    return wheels[0], sdists[0]


def verify_artifact_names(
    wheel: Path, sdist: Path, contract: dict
) -> None:
    required = contract["required_artifacts"]
    if wheel.name != required["wheel"]:
        _fail("wheel_name_mismatch", f"{wheel.name} != {required['wheel']}")
    if sdist.name != required["sdist"]:
        _fail("sdist_name_mismatch", f"{sdist.name} != {required['sdist']}")


def _metadata_field(text: str, field: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return ""


def wheel_metadata(wheel: Path) -> dict:
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            (n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), ""
        )
        if not metadata_name:
            _fail("wheel_metadata_missing", wheel.name)
        text = archive.read(metadata_name).decode("utf-8")
    return {"name": _metadata_field(text, "Name"), "version": _metadata_field(text, "Version")}


def sdist_metadata(sdist: Path) -> dict:
    with tarfile.open(sdist, "r:gz") as archive:
        pkg_info = next(
            (m for m in archive.getmembers() if m.name.endswith("/PKG-INFO")), None
        )
        if pkg_info is None:
            _fail("sdist_metadata_missing", sdist.name)
        handle = archive.extractfile(pkg_info)
        if handle is None:
            _fail("sdist_metadata_unreadable", sdist.name)
        text = handle.read().decode("utf-8")
    return {"name": _metadata_field(text, "Name"), "version": _metadata_field(text, "Version")}


def verify_metadata_versions(wheel: Path, sdist: Path, contract: dict) -> None:
    expected_version = contract["package"]["version"]
    for metadata, label in (
        (wheel_metadata(wheel), "wheel"),
        (sdist_metadata(sdist), "sdist"),
    ):
        if metadata["version"] != expected_version:
            _fail("artifact_version_mismatch", f"{label}: {metadata['version']}")
        if metadata["name"] != contract["package"]["distribution_name"]:
            _fail("artifact_name_mismatch", f"{label}: {metadata['name']}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256sums(dist_dir: Path, artifacts: list[Path]) -> Path:
    sums_path = dist_dir / "SHA256SUMS"
    lines = [f"{sha256_file(artifact)}  {artifact.name}" for artifact in artifacts]
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sums_path


def verify_sha256sums(dist_dir: Path) -> dict:
    sums_path = dist_dir / "SHA256SUMS"
    if not sums_path.is_file():
        _fail("artifact_hash_missing", str(sums_path))
    recorded: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        name = name.strip().lstrip("*")
        target = dist_dir / name
        if not target.is_file():
            _fail("artifact_missing_for_hash", name)
        actual = sha256_file(target)
        if actual != digest:
            _fail("artifact_hash_mismatch", name)
        recorded[name] = digest
    return recorded


def clean_install_wheel(wheel: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "wheel_smoke.py"), str(wheel.parent)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        _fail("wheel_clean_install_failed", (completed.stderr or completed.stdout)[-800:])


def clean_install_sdist(sdist: Path, contract: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="torq-sdist-check-") as directory:
        venv_dir = Path(directory) / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install = subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(sdist)],
            capture_output=True,
            text=True,
            check=False,
        )
        if install.returncode != 0:
            _fail("sdist_clean_install_failed", install.stderr[-800:])
        check = subprocess.run(
            [
                str(python),
                "-c",
                "import importlib.metadata as md;"
                f"v = md.version('{contract['package']['distribution_name']}');"
                f"assert v == '{contract['package']['version']}', v;"
                "print(v)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode != 0:
            _fail("sdist_installed_version_mismatch", check.stderr[-400:])


def write_sbom(dist_dir: Path, wheel: Path, contract: dict) -> Path:
    metadata = wheel_metadata(wheel)
    components = [
        {
            "type": "application",
            "name": metadata["name"],
            "version": metadata["version"],
            "purl": f"pkg:pypi/{metadata['name']}@{metadata['version']}",
        }
    ]
    wheel_zip = zipfile.ZipFile(wheel)
    try:
        metadata_name = next(
            n for n in wheel_zip.namelist() if n.endswith(".dist-info/METADATA")
        )
        text = wheel_zip.read(metadata_name).decode("utf-8")
    finally:
        wheel_zip.close()
    for line in text.splitlines():
        if line.startswith("Requires-Dist:"):
            requirement = line.split(":", 1)[1].strip()
            if "extra ==" in requirement:
                continue
            components.append(
                {"type": "library", "name": re.split(r"[<>=!~;(\[ ]", requirement)[0], "declared_requirement": requirement}
            )
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
            "component": components[0],
            "properties": [
                {"name": "torq:sbom_scope", "value": "declared-runtime-dependencies"},
                {"name": "torq:sbom_note", "value": "generated by scripts/release_build.py; not a signing artifact"},
            ],
        },
        "components": components[1:],
    }
    sbom_path = dist_dir / contract["required_artifacts"]["sbom"]
    sbom_path.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return sbom_path


def write_release_evidence(
    dist_dir: Path,
    contract: dict,
    contract_path: Path,
    source_state: dict,
    hashes: dict,
) -> Path:
    def _blob_sha(relpath: str) -> str:
        completed = subprocess.run(
            ["git", "rev-parse", f"HEAD:{relpath}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""

    evidence = {
        "evidence_schema_version": RELEASE_EVIDENCE_SCHEMA_VERSION,
        "kind": "release_build_evidence",
        "generated_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "source": {
            "candidate_sha": source_state["candidate_sha"],
            "branch_policy": contract["candidate"]["branch"],
            "builder_script_blob_sha": _blob_sha("scripts/release_build.py"),
            "contract_path": str(contract_path.relative_to(REPO_ROOT)),
            "contract_sha256": contract_digest(contract),
        },
        "ci": {
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
            "workflow": os.environ.get("GITHUB_WORKFLOW", "local"),
            "repository": os.environ.get("GITHUB_REPOSITORY", "local"),
            "server_url": os.environ.get("GITHUB_SERVER_URL", "local"),
        },
        "artifacts": hashes,
        "policies": contract["policies"],
        "publication": {
            "published_anywhere": False,
            "pypi_publication_permitted": False,
            "production_trust_claimed": False,
        },
    }
    evidence_path = dist_dir / contract["required_artifacts"]["release_evidence"]
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence_path


def write_provenance(dist_dir: Path, contract: dict, hashes: dict) -> Path:
    provenance = {
        "provenance_schema_version": "1.0.0",
        "provenance_level": "ci_provenance_only",
        "trust_classification": {
            "ci_provenance": "recorded here: where/when/from-what-source the artifacts were built",
            "package_signing": "NOT performed in R0; distinct trust level, see release-trust-model.md",
            "git_tag_signing": "NOT performed in R0; distinct trust level, see release-trust-model.md",
            "torq_production_receipt_signing": "product trust domain; never equivalent to release signatures",
        },
        "builder": {
            "ci": {
                "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
                "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
                "workflow": os.environ.get("GITHUB_WORKFLOW", "local"),
                "job": os.environ.get("GITHUB_JOB", "local"),
                "repository": os.environ.get("GITHUB_REPOSITORY", "local"),
                "sha": os.environ.get("GITHUB_SHA", "local"),
            },
            "python_version": sys.version.split()[0],
        },
        "subject": hashes,
    }
    provenance_path = dist_dir / contract["required_artifacts"]["provenance"]
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return provenance_path


def build_release(expected_sha: str, dist_dir: Path) -> dict:
    contract_path = CONTRACT_PATH
    contract = load_contract(contract_path)
    source_state = verify_source_state(contract, expected_sha)
    wheel, sdist = build_once(dist_dir)
    verify_artifact_names(wheel, sdist, contract)
    verify_metadata_versions(wheel, sdist, contract)
    sums_path = write_sha256sums(dist_dir, [wheel, sdist])
    clean_install_wheel(wheel)
    clean_install_sdist(sdist, contract)
    hashes = verify_sha256sums(dist_dir)
    sbom_path = write_sbom(dist_dir, wheel, contract)
    evidence_path = write_release_evidence(dist_dir, contract, contract_path, source_state, hashes)
    provenance_path = write_provenance(dist_dir, contract, hashes)
    return {
        "candidate_sha": source_state["candidate_sha"],
        "wheel": wheel.name,
        "sdist": sdist.name,
        "sha256sums": sums_path.name,
        "sbom": sbom_path.name,
        "release_evidence": evidence_path.name,
        "provenance": provenance_path.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True, help="exact candidate commit SHA (40-hex)")
    parser.add_argument("--dist-dir", default="dist-release", help="output directory (must be empty)")
    args = parser.parse_args(argv)
    try:
        summary = build_release(args.expected_sha, REPO_ROOT / args.dist_dir)
    except ReleaseContractError as exc:
        print(f"release_build failed closed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
