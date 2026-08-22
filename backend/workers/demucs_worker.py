"""Run Demucs in its isolated Python environment and publish fixed stem paths."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DEMUCS_CACHE_DIR_ENV = "DEMUCS_CACHE_DIR"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_demucs_cache_dir(project_root: Path) -> Path:
    configured = os.getenv(DEMUCS_CACHE_DIR_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = project_root / path
        return path.resolve()
    return project_root / ".tools" / "demucs-py311" / "cache"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-audio", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()

    source_audio = Path(args.source_audio).resolve()
    work_dir = Path(args.work_dir).resolve()
    raw_dir = work_dir / "raw"
    manifest_path = work_dir / "separation_manifest.json"
    vocals_path = work_dir / "vocals.wav"
    no_vocals_path = work_dir / "no_vocals.wav"
    work_dir.mkdir(parents=True, exist_ok=True)
    project_root = Path(__file__).resolve().parents[2]
    cache_dir = resolve_demucs_cache_dir(project_root)
    cache_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "demucs",
        "--two-stems=vocals",
        "-n",
        "htdemucs",
        "--device",
        "cpu",
        "-o",
        str(raw_dir),
        str(source_audio),
    ]
    started_at = _timestamp()
    env = os.environ.copy()
    env["TORCH_HOME"] = str(cache_dir)
    env["HF_HOME"] = str(cache_dir / "huggingface")
    payload: dict[str, object] = {
        "task_id": args.task_id,
        "status": "failed",
        "model": "htdemucs",
        "device": "cpu",
        "two_stems": "vocals",
        "source_audio": str(source_audio),
        "started_at": started_at,
        "command": command,
    }
    try:
        if not source_audio.is_file() or source_audio.stat().st_size <= 0:
            raise RuntimeError("The source audio file is missing or empty.")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        payload.update({
            "return_code": result.returncode,
            "stdout": result.stdout[-12000:],
            "stderr": result.stderr[-12000:],
        })
        if result.returncode != 0:
            raise RuntimeError(f"Demucs exited with return code {result.returncode}.")
        demucs_dir = raw_dir / "htdemucs" / source_audio.stem
        raw_vocals = demucs_dir / "vocals.wav"
        raw_no_vocals = demucs_dir / "no_vocals.wav"
        if not raw_vocals.is_file() or not raw_no_vocals.is_file():
            raise RuntimeError("Demucs did not produce both required stems.")
        shutil.copy2(raw_vocals, vocals_path)
        shutil.copy2(raw_no_vocals, no_vocals_path)
        if vocals_path.stat().st_size <= 0 or no_vocals_path.stat().st_size <= 0:
            raise RuntimeError("One or more Demucs stems are empty.")
        payload.update({
            "status": "completed",
            "vocals_path": str(vocals_path),
            "no_vocals_path": str(no_vocals_path),
            "vocals_size": vocals_path.stat().st_size,
            "no_vocals_size": no_vocals_path.stat().st_size,
        })
        return_code = 0
    except Exception as error:
        payload["error"] = str(error)
        return_code = 1
    finally:
        payload["finished_at"] = _timestamp()
        _write_manifest(manifest_path, payload)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
