"""Run outbox delivery and saved-profile projection as independent local loops."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from filelock import Timeout

from nanobot.memory_sync.forget import ForgetWorker, SystemdWorkerControl
from nanobot.memory_sync.outbox import MemoryOutbox
from nanobot.memory_sync.profiles import ProfileExporter
from nanobot.memory_sync.worker import HindsightWorker


async def run_delivery(worker: HindsightWorker, *, interval: float = 5) -> None:
    """Delivery failure must not disable the last valid profile files."""
    previous: str | None = None
    while True:
        try:
            status = await worker.run_once()
        except Timeout:
            status = "busy"
        if status != previous:
            print(json.dumps({"component": "outbox", "status": status}), flush=True)
            previous = status
        await asyncio.sleep(interval)


async def run_profiles(exporter: ProfileExporter, *, interval: float = 300) -> None:
    """Poll saved content only; never issue an LLM refresh from this loop."""
    previous: dict[str, str] | None = None
    while True:
        try:
            status = await exporter.sync_once()
        except httpx.HTTPError:
            status = {"status": "api_unavailable"}
        except (OSError, ValueError):
            # Do not emit exception bodies containing file content, paths, or
            # HTTP payloads. The exporter keeps the last good files intact.
            status = {"status": "needs_attention"}
        if status != previous:
            print(json.dumps({"component": "profiles", **status}), flush=True)
            previous = status
        await asyncio.sleep(interval)


async def run_forget(forgetter: ForgetWorker, *, interval: float = 30) -> None:
    previous: str | None = None
    while True:
        try:
            status = await forgetter.run_once()
        except Timeout:
            status = "busy"
        except (httpx.HTTPError, OSError, ValueError, RuntimeError):
            status = "retry_pending"
        if status != previous:
            print(json.dumps({"component": "forget", "status": status}), flush=True)
            previous = status
        await asyncio.sleep(interval)


async def serve(
    outbox_path: Path, api_url: str, bank_id: str, workspace: Path, profile_state: Path,
    *, delivery_interval: float = 5, profile_interval: float = 300, pause_path: Path | None = None,
    hold_path: Path | None = None,
) -> None:
    if delivery_interval <= 0 or profile_interval <= 0:
        raise ValueError("service intervals must be positive")
    if not profile_state.is_file():
        raise ValueError("profile seeding and baseline registration must finish before service start")
    worker = HindsightWorker(MemoryOutbox(outbox_path, bank_id=bank_id, pause_path=pause_path),
                             api_url=api_url, bank_id=bank_id, hold_path=hold_path)
    exporter = ProfileExporter(worker, workspace, profile_state)
    async with asyncio.TaskGroup() as group:
        group.create_task(run_forget(ForgetWorker(worker, exporter, SystemdWorkerControl(worker.outbox.pause_path))))
        group.create_task(run_delivery(worker, interval=delivery_interval))
        group.create_task(run_profiles(exporter, interval=profile_interval))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Deliver memory and project saved Hindsight profiles")
    parser.add_argument("--outbox", required=True, type=Path)
    parser.add_argument("--api-url", default="http://127.0.0.1:8888")
    parser.add_argument("--bank-id", default="personal")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--profile-state", required=True, type=Path)
    parser.add_argument("--pause-file", type=Path)
    parser.add_argument("--hold-file", type=Path)
    parser.add_argument("--delivery-interval", type=float, default=5)
    parser.add_argument("--profile-interval", type=float, default=300)
    args = parser.parse_args()
    asyncio.run(serve(args.outbox, args.api_url, args.bank_id, args.workspace, args.profile_state,
                      delivery_interval=args.delivery_interval, profile_interval=args.profile_interval,
                      pause_path=args.pause_file, hold_path=args.hold_file))


if __name__ == "__main__":
    main()
