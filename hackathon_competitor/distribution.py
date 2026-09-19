from __future__ import annotations

import hashlib
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

REQUIRED_FILES = {
    "Dockerfile",
    "LICENSE",
    "README.md",
    "compose.yml",
    "pyproject.toml",
}
FORBIDDEN_PARTS = {".git", ".knightwatch", "__pycache__", "plow-credentials"}
FORBIDDEN_SUFFIXES = {".db", ".db-shm", ".db-wal", ".pyc", ".pyo"}


def _relative_archive_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe archive path: {name}")
    return PurePosixPath(*path.parts[1:]) if len(path.parts) > 1 else path


def validate_public_bundle(bundle: Path) -> dict[str, object]:
    with zipfile.ZipFile(bundle) as archive:
        files = [name for name in archive.namelist() if not name.endswith("/")]
        relative = {_relative_archive_name(name): name for name in files}
        unsafe = [
            str(path)
            for path in relative
            if FORBIDDEN_PARTS.intersection(path.parts)
            or path.name == ".env"
            or any(path.name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)
        ]
        if unsafe:
            raise ValueError(f"public bundle contains forbidden paths: {unsafe}")
        missing = sorted(REQUIRED_FILES - {str(path) for path in relative})
        if missing:
            raise ValueError(f"public bundle is missing required files: {missing}")
        readme = archive.read(relative[PurePosixPath("README.md")]).decode("utf-8")
        for marker in ("Docker Compose", "plow-credentials", "AGENT_ID"):
            if marker not in readme:
                raise ValueError(f"public README is missing install marker: {marker}")
        # A README that shows either a literal `docker build` or the compose
        # equivalent (`compose up ... --build`) has told the reader how the
        # image actually gets built; requiring one exact phrasing would
        # reject a correct README for using the other.
        if "docker build" not in readme and "compose up" not in readme:
            raise ValueError(
                "public README is missing install marker: a docker build or compose up command"
            )
        license_text = archive.read(relative[PurePosixPath("LICENSE")]).decode("utf-8")
        if not license_text.startswith("MIT License"):
            raise ValueError("public bundle does not contain the MIT license")
        linux_control_files = [
            path
            for path in relative
            if path.suffix == ".pin" or path.parts[:2] == ("image", "s6-overlay")
        ]
        crlf_files = [
            str(path) for path in linux_control_files if b"\r" in archive.read(relative[path])
        ]
        if crlf_files:
            raise ValueError(f"Linux control files contain carriage returns: {crlf_files}")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    return {"path": str(bundle.resolve()), "sha256": digest, "files": len(files)}


def build_public_bundle(repo_root: Path, output: Path) -> dict[str, object]:
    repo_root = repo_root.resolve()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="joust-public-", suffix=".zip", dir=output.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "archive",
                "--format=zip",
                "--prefix=joust/",
                f"--output={temporary}",
                "HEAD",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git archive failed: {result.stderr.strip()}")
        summary = validate_public_bundle(temporary)
        temporary.replace(output)
        return {
            **summary,
            "path": str(output),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        }
    finally:
        temporary.unlink(missing_ok=True)
