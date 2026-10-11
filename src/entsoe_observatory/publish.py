"""Publication of the gold layer to Cloudflare R2 (S3 API) and Hugging Face.

Both targets work on a plan first (`dry_run=True` lists what would change). Credentials come
from the environment through `Settings` and never appear in logs, manifests or errors.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .bronze import code_version, new_run_id, write_run_manifest
from .dataset_card import build_card
from .settings import Settings
from .storage import sha256_file

MANIFEST_KEY = "_publish/manifest.json"
CONTENT_TYPES = {".parquet": "application/vnd.apache.parquet", ".json": "application/json",
                 ".md": "text/markdown"}


class ObjectStore(Protocol):
    def get_text(self, key: str) -> str | None: ...
    def put_file(self, key: str, path: Path, content_type: str) -> None: ...
    def put_text(self, key: str, text: str, content_type: str) -> None: ...
    def delete(self, keys: list[str]) -> None: ...


@dataclass
class Plan:
    upload: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    bytes_to_upload: int = 0


def collect_files(settings: Settings) -> dict[str, Path]:
    """Object key -> local file: gold tables, contracts and the dataset card."""
    files: dict[str, Path] = {}
    gold = settings.layer_dir("gold")
    for path in sorted(gold.rglob("*.parquet")):
        files[f"data/{path.relative_to(gold).as_posix()}"] = path
    contracts = Path(__file__).resolve().parents[2] / "contracts" / "gold"
    for path in sorted(contracts.glob("*.json")):
        files[f"contracts/{path.name}"] = path
    return files


def plan_sync(
    local: dict[str, tuple[Path, str]], remote: dict[str, dict[str, Any]], *, prune: bool
) -> Plan:
    plan = Plan()
    for key, (path, digest) in local.items():
        if remote.get(key, {}).get("sha256") == digest:
            plan.unchanged.append(key)
        else:
            plan.upload.append(key)
            plan.bytes_to_upload += path.stat().st_size
    if prune:
        plan.delete = sorted(set(remote) - set(local))
    return plan


def publish_r2(
    settings: Settings,
    store: ObjectStore,
    *,
    dry_run: bool = False,
    prune: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Sync gold, contracts and the card to an object store. Idempotent per file checksum."""
    run_id = run_id or new_run_id()
    card = build_card(settings)
    files = collect_files(settings)
    card_path = settings.layer_dir("publish") / "README.md"
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(card, encoding="utf-8", newline="\n")
    files["README.md"] = card_path
    local = {key: (path, sha256_file(path)) for key, path in files.items()}
    raw = store.get_text(MANIFEST_KEY)
    remote = json.loads(raw)["files"] if raw else {}
    plan = plan_sync(local, remote, prune=prune)
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "upload": len(plan.upload),
        "unchanged": len(plan.unchanged),
        "delete": len(plan.delete),
        "mb_to_upload": round(plan.bytes_to_upload / 2**20, 1),
    }
    if dry_run:
        return summary
    for key in plan.upload:
        path = local[key][0]
        store.put_file(key, path, CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
    if plan.delete:
        store.delete(plan.delete)
    kept = {k: v for k, v in remote.items() if k not in plan.delete}
    kept.update({k: {"sha256": d, "bytes": p.stat().st_size} for k, (p, d) in local.items()})
    manifest = {
        "run_id": run_id,
        "code_version": code_version(),
        "published_at_utc": datetime.now(UTC).isoformat(),
        "files": kept,
    }
    # The manifest goes last: readers that see it see every file it lists.
    store.put_text(MANIFEST_KEY, json.dumps(manifest, indent=1), "application/json")
    write_run_manifest(settings, run_id, "publish_r2", summary)
    return summary


class S3Store:
    """Cloudflare R2 through its S3-compatible API (needs the `publish` extra)."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        account = settings.r2_account_id
        key = settings.r2_access_key_id
        secret = settings.r2_secret_access_key
        if not (account and settings.r2_bucket and key and secret):
            raise ValueError(
                "R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY are required"
            )
        self.bucket = settings.r2_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            aws_access_key_id=key.get_secret_value(),
            aws_secret_access_key=secret.get_secret_value(),
            region_name="auto",
        )

    def get_text(self, key: str) -> str | None:
        try:
            body = self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self._client.exceptions.NoSuchKey:
            return None
        return str(body.decode("utf-8"))

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        self._client.upload_file(
            str(path), self.bucket, key, ExtraArgs={"ContentType": content_type}
        )

    def put_text(self, key: str, text: str, content_type: str) -> None:
        self._client.put_object(
            Bucket=self.bucket, Key=key, Body=text.encode("utf-8"), ContentType=content_type
        )

    def delete(self, keys: list[str]) -> None:
        for start in range(0, len(keys), 1000):
            batch = [{"Key": k} for k in keys[start : start + 1000]]
            self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})


def stage_huggingface(settings: Settings) -> Path:
    """Build the repository tree for Hugging Face (hard links when possible)."""
    stage = settings.layer_dir("publish") / "hf"
    if stage.exists():
        shutil.rmtree(stage)
    for key, path in collect_files(settings).items():
        target = stage / key
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)
    (stage / "README.md").write_text(build_card(settings), encoding="utf-8", newline="\n")
    return stage


def publish_huggingface(
    settings: Settings, api: Any | None = None, *, dry_run: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    run_id = run_id or new_run_id()
    stage = stage_huggingface(settings)
    files = [p for p in stage.rglob("*") if p.is_file()]
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "repo_id": settings.hf_repo_id,
        "files": len(files),
        "mb": round(sum(p.stat().st_size for p in files) / 2**20, 1),
    }
    if dry_run:
        return summary
    if api is None:
        from huggingface_hub import HfApi

        if settings.hf_token is None:
            raise ValueError("HF_TOKEN is required")
        api = HfApi(token=settings.hf_token.get_secret_value())
    api.create_repo(settings.hf_repo_id, repo_type="dataset", exist_ok=True)
    commit = api.upload_folder(
        folder_path=str(stage),
        repo_id=settings.hf_repo_id,
        repo_type="dataset",
        commit_message=f"Gold tables, run {run_id} ({code_version()})",
    )
    summary["commit"] = str(getattr(commit, "oid", commit))
    write_run_manifest(settings, run_id, "publish_hf", summary)
    return summary
