"""The portability seam.

Eleven methods as issued. Every managed ML platform sells you the same operations under
different names; writing this once is the difference between knowing a product and
knowing the category. `generate` is the newest of them and the one the vendors are
currently busiest renaming.

Lab 2 added two more — `get_model` and `promote_model`. Reading a version back and moving
an alias are as provider-specific as writing one, and leaving them out would have pushed a
`gs://` path into scripts/, which is the leak this file exists to prevent.

You implement exactly ONE of aws.py, azure.py, or gcp.py. Lab 1 needs only `upload`,
`download`, and `push_image`. The rest raise NotImplementedError until the lab that
needs them, which is deliberate — do not implement ahead.

Nothing outside cloudlayer/ may import a provider SDK.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class CloudAdapter(ABC):
    """Provider-neutral interface. The grader only ever calls these."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    # --- Lab 1 ---------------------------------------------------------------
    @abstractmethod
    def upload(self, local_path: str, key: str) -> str:
        """Upload a file under BLOB_URI. Returns the full URI of the stored object."""

    @abstractmethod
    def download(self, uri: str, local_path: str) -> None:
        """Fetch an object to a local path. Creates parent directories."""

    @abstractmethod
    def push_image(self, local_tag: str) -> str:
        """Push a locally built image to CONTAINER_REGISTRY. Returns the remote reference,
        which must be digest-pinned (repo@sha256:...), not tag-pinned."""

    # --- Lab 2 ---------------------------------------------------------------
    def submit_training(self, image_uri: str, args: dict[str, Any]) -> str:
        raise NotImplementedError("Lab 2")

    def wait_training(self, job_id: str) -> dict[str, Any]:
        raise NotImplementedError("Lab 2")

    def register_model(self, model_uri: str, name: str,
                       lineage: dict[str, str] | None = None) -> str:
        """Register the model stored at `model_uri` under `name`. Returns the version.

        `lineage` is the eight fields Lab 2 requires, and they belong on the VERSION, not
        on the tracking run: a run records how a model was made, a version is what gets
        promoted, and at promotion time the run may not be the thing in front of you.
        """
        raise NotImplementedError("Lab 2")

    def get_model(self, name: str, version: str) -> dict[str, Any]:
        """The read side of register_model: resolve name + version to at least

            {"artifact_uri": str, "aliases": [str], "lineage": {...}}

        Without this the registry is write-only, and `scripts/reload_check.py` would have
        to fall back to a path someone remembered — which is the failure the lab is about.
        """
        raise NotImplementedError("Lab 2")

    def promote_model(self, name: str, version: str, alias: str) -> dict[str, Any]:
        """Move `alias` (staging, production, ...) to this version. Returns the version's
        aliases after the move. Promotion is a separate operation from registration on
        purpose: registering is a record, promoting is a decision."""
        raise NotImplementedError("Lab 2")

    # --- Lab 3 ---------------------------------------------------------------
    def deploy(self, model_ref: str, endpoint: str, instance: str) -> str:
        raise NotImplementedError("Lab 3")

    def invoke(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Lab 3")

    # --- Lab 4 ---------------------------------------------------------------
    def emit_metric(self, name: str, value: float, unit: str = "None") -> None:
        raise NotImplementedError("Lab 4")

    # --- Lab 5 ---------------------------------------------------------------
    def generate(self, prompt: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call a managed LLM endpoint once. Returns at least:

            {"response": str, "input_tokens": int,
             "output_tokens": int, "latency_ms": int}

        Token counts must be read from the provider's own usage fields. Estimating them
        by counting words produces a cost report built on a number you made up, and
        every provider tokenises differently.

        `params` carries the knobs worth exposing at the seam — `max_output_tokens`
        above all, since output tokens dominate the bill. Provider-specific options
        belong here in Layer 3, never in the caller.
        """
        raise NotImplementedError("Lab 5")

    def teardown(self, tags: dict[str, str]) -> list[str]:
        """Delete every resource carrying these tags. Returns what was deleted.

        Deletion is asynchronous on all three providers — returning successfully does
        not mean the resource is gone. Re-check, and check the bill.
        """
        raise NotImplementedError("Lab 5")


class LocalAdapter(CloudAdapter):
    """Filesystem stand-in so Lab 1 runs before your cloud account is ready.

    Acceptable for developing Lab 1. NOT acceptable for submission — your submitted
    Lab 1 must upload to real object storage and push to a real registry.
    """

    def upload(self, local_path: str, key: str) -> str:
        import shutil
        from pathlib import Path

        dest = Path(self.cfg.data_dir) / "_local_blob" / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        return f"local://{dest}"

    def download(self, uri: str, local_path: str) -> None:
        import shutil
        from pathlib import Path

        src = uri.removeprefix("local://")
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local_path)

    def push_image(self, local_tag: str) -> str:
        raise NotImplementedError(
            "LocalAdapter cannot push images. Implement your provider's adapter for Lab 1 submission."
        )
