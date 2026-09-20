"""GCP adapter. Implements upload/download/push_image for Lab 1 and
submit_training/wait_training (Vertex AI custom jobs) for Lab 2.

SDK:  pip install google-cloud-storage google-cloud-aiplatform
Docs: storage.Client for GCS; Artifact Registry push goes through `docker push` after
      `gcloud auth configure-docker <region>-docker.pkg.dev`.

Layer 2 rules this file obeys:
  * BLOB_URI looks like gs://bucket/prefix — parsed here, never in src/.
  * Artifact Registry paths are region-scoped:
        <region>-docker.pkg.dev/<project>/<repo>/<image>
    A common first failure is pushing to gcr.io out of habit; it is a different service.
  * push_image returns the digest reference, not the tag.
  * GCP calls them labels, not tags, and they must be lowercase with no spaces.
    cfg.tags(1) already satisfies that constraint — the values are used as they stand.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from cloudlayer.base import CloudAdapter


def _split_gs_uri(uri: str) -> tuple[str, str]:
    """gs://bucket/some/prefix -> ('bucket', 'some/prefix'). Prefix may be empty."""
    if not uri.startswith("gs://"):
        raise ValueError(f"Not a GCS URI: {uri!r}. Expected gs://bucket[/prefix].")
    bucket, _, prefix = uri[len("gs://"):].partition("/")
    if not bucket:
        raise ValueError(f"No bucket in {uri!r}.")
    return bucket, prefix.strip("/")


def _run(cmd: list[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{out.stderr.strip()}")
    return out.stdout.strip()


class GcpAdapter(CloudAdapter):
    def _bucket(self):
        from google.cloud import storage

        bucket_name, prefix = _split_gs_uri(self.cfg.blob_uri)
        client = storage.Client(project=self.cfg.project_id)
        return client.bucket(bucket_name), prefix

    def upload(self, local_path: str, key: str) -> str:
        bucket, prefix = self._bucket()
        blob_name = f"{prefix}/{key.lstrip('/')}" if prefix else key.lstrip("/")
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(local_path)
        return f"gs://{bucket.name}/{blob_name}"

    def download(self, uri: str, local_path: str) -> None:
        from google.cloud import storage

        bucket_name, blob_name = _split_gs_uri(uri)
        if not blob_name:
            raise ValueError(f"No object path in {uri!r}.")
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        client = storage.Client(project=self.cfg.project_id)
        client.bucket(bucket_name).blob(blob_name).download_to_filename(str(target))

    def push_image(self, local_tag: str) -> str:
        registry = self.cfg.container_registry.rstrip("/")
        host = registry.split("/", 1)[0]
        remote = f"{registry}/{local_tag.rsplit('/', 1)[-1]}"

        # Idempotent, and cheap if the helper is already configured.
        _run(["gcloud", "auth", "configure-docker", host, "--quiet"])
        _run(["docker", "tag", local_tag, remote])
        _run(["docker", "push", remote])

        # The tag we just pushed can be moved later; the digest cannot. Read it back
        # from the registry rather than trusting the local daemon's view.
        repo = remote.rsplit(":", 1)[0]
        digest = _run([
            "gcloud", "artifacts", "docker", "images", "describe", remote,
            "--project", self.cfg.project_id, "--format", "value(image_summary.digest)",
        ])
        return f"{repo}@{digest}"

    # --- Lab 2 ---------------------------------------------------------------
    # Vertex custom jobs through the low-level v1 client. The high-level
    # aiplatform.CustomContainerTrainingJob wrapper wants a staging bucket and uploads a
    # model on success; neither is wanted here.

    def _jobs(self):
        from google.cloud import aiplatform_v1

        return aiplatform_v1.JobServiceClient(
            client_options={"api_endpoint": f"{self.cfg.region}-aiplatform.googleapis.com"})

    def _mounted(self, key: str) -> str:
        """Where BLOB_URI/<key> appears inside a Vertex job. Vertex mounts buckets at
        /gcs/<bucket> with Cloud Storage FUSE, so the image reads and writes object storage
        as plain files and needs no storage SDK."""
        bucket, prefix = _split_gs_uri(self.cfg.blob_uri)
        return "/".join(p for p in ("/gcs", bucket, prefix, key.strip("/")) if p)

    def submit_training(self, image_uri: str, args: dict[str, Any]) -> str:
        """args: job_name, data_key, output_key (both relative to BLOB_URI), and optionally
        command (replaces the image entrypoint), args (extra container args), instance,
        spot, env, timeout_s, lab. Returns the job resource name."""
        from google.cloud import aiplatform_v1 as v
        from google.protobuf import duration_pb2

        container = v.ContainerSpec(
            image_uri=image_uri,
            command=args.get("command", []),
            args=[*args.get("args", []),
                  "--data-path", self._mounted(args["data_key"]),
                  "--artifact-dir", self._mounted(args["output_key"])],
            env=[v.EnvVar(name=k, value=str(val)) for k, val in args.get("env", {}).items()],
        )
        pool = v.WorkerPoolSpec(
            replica_count=1, container_spec=container,
            machine_spec=v.MachineSpec(machine_type=args.get("instance", "n1-standard-4")),
        )
        # The run-time identity. Without one Vertex falls back to its own service agent,
        # which is not the identity you granted bucket access to.
        identity = self.cfg.identity_ref.removeprefix("serviceAccount:")
        # The timeout is a spend cap: a hung job is killed instead of billed until noticed.
        # On spot, a reclaimed VM restarts the container, and the job resumes from its
        # checkpoint rather than failing.
        spot = bool(args.get("spot"))
        scheduling = v.Scheduling(
            timeout=duration_pb2.Duration(seconds=int(args.get("timeout_s", 3600))),
            strategy=v.Scheduling.Strategy.SPOT if spot else v.Scheduling.Strategy.STRATEGY_UNSPECIFIED,
            restart_job_on_worker_restart=spot,
        )
        spec = v.CustomJobSpec(
            worker_pool_specs=[pool], scheduling=scheduling,
            service_account=identity if identity.endswith(".gserviceaccount.com") else "",
        )
        job = v.CustomJob(display_name=args["job_name"], job_spec=spec,
                          labels=self.cfg.tags(args.get("lab", 2)))
        parent = f"projects/{self.cfg.project_id}/locations/{self.cfg.region}"
        return self._jobs().create_custom_job(parent=parent, custom_job=job).name

    def wait_training(self, job_id: str, poll_s: float = 20.0) -> dict[str, Any]:
        import time

        from google.cloud import aiplatform_v1 as v

        S = v.JobState
        terminal = {S.JOB_STATE_SUCCEEDED, S.JOB_STATE_FAILED, S.JOB_STATE_CANCELLED,
                    S.JOB_STATE_EXPIRED, S.JOB_STATE_PARTIALLY_SUCCEEDED}
        client, last = self._jobs(), None
        while True:
            job = client.get_custom_job(name=job_id)
            if job.state != last:
                print(f"  {time.strftime('%H:%M:%S')}  {job.state.name}", flush=True)
                last = job.state
            if job.state in terminal:
                break
            time.sleep(poll_s)

        raw = v.CustomJob.pb(job)
        ran = raw.HasField("start_time") and raw.HasField("end_time")
        pool = job.job_spec.worker_pool_specs[0]
        return {
            "job_id": job.name,
            "state": job.state.name.removeprefix("JOB_STATE_").lower(),
            "succeeded": job.state == S.JOB_STATE_SUCCEEDED,
            "error": job.error.message,
            # RUNNING to end. Provisioning before start_time is excluded, so treat cost
            # derived from this as a floor and reconcile it against the bill.
            "duration_s": (job.end_time - job.start_time).total_seconds() if ran else 0.0,
            "instance": pool.machine_spec.machine_type,
            "spot": job.job_spec.scheduling.strategy == v.Scheduling.Strategy.SPOT,
            "logs": f"gcloud ai custom-jobs stream-logs {job.name.rsplit('/', 1)[-1]} "
                    f"--region {self.cfg.region} --project {self.cfg.project_id}",
        }

    # --- Lab 2: Vertex Model Registry ---------------------------------------
    # A "registered model" in Vertex is one Model resource with many versions; each
    # version is addressed as <model resource>@<version_id>. Lineage goes on the version:
    # labels for the fields that survive GCP's label rules (lowercase, [a-z0-9_-], 63
    # chars, no ':') and version_description for the full values, because a sha256 digest
    # is 71 characters with its prefix and no label will hold it.

    LINEAGE_FIELDS = ("git_commit", "data_version", "mlflow_run_id", "training_job_id",
                      "image_digest", "seed", "metric_val", "metric_test")

    def _models(self):
        from google.cloud import aiplatform_v1

        return aiplatform_v1.ModelServiceClient(
            client_options={"api_endpoint": f"{self.cfg.region}-aiplatform.googleapis.com"})

    def _registry_parent(self) -> str:
        return f"projects/{self.cfg.project_id}/locations/{self.cfg.region}"

    def _find_model(self, name: str) -> str | None:
        """Resource name of the registered model called `name`, or None."""
        from google.cloud import aiplatform_v1 as v

        models = list(self._models().list_models(v.ListModelsRequest(
            parent=self._registry_parent(), filter=f'display_name="{name}"')))
        if not models:
            return None
        # The list returns the default version of each model; strip any @version suffix.
        return models[0].name.split("@", 1)[0]

    @staticmethod
    def _label_value(value: str) -> str:
        """GCP labels: lowercase letters, digits, dash, underscore; 63 characters.
        Anything else is dropped rather than mangled into something that looks real."""
        import re

        out = re.sub(r"[^a-z0-9_-]", "-", str(value).lower())
        return out[:63]

    def register_model(self, model_uri: str, name: str,
                       lineage: dict[str, str] | None = None,
                       serving_image: str | None = None) -> str:
        """Upload the model directory at `model_uri` (gs://...) as a new version of the
        registered model `name`. Returns the version id, e.g. "1".

        Vertex refuses a model with no container_spec ("container_spec must be set for
        custom model"), so a version always names an environment. The default is the
        image that trained it — measured reason: the newest prebuilt sklearn serving
        container is sklearn-cpu.1-6 and this model is pickled by scikit-learn 1.8, so a
        prebuilt container would record an environment that cannot load the artifact.
        Lab 3 replaces this with a serving image that answers /health and /predict."""
        import json

        from google.cloud import aiplatform_v1 as v

        lineage = {k: str(lineage.get(k, "")) for k in self.LINEAGE_FIELDS} if lineage else {}
        missing = [k for k, val in lineage.items() if not val]
        if lineage and missing:
            raise ValueError(f"lineage is incomplete: {missing}. A half-traced model is "
                             "worse than an untraced one, because it looks traced.")

        image = serving_image or lineage.get("image_digest", "")
        if not image:
            raise ValueError("no serving_image and no image_digest in lineage: Vertex "
                             "requires a container for every registered version.")
        model = v.Model(
            display_name=name,
            artifact_uri=model_uri,
            container_spec=v.ModelContainerSpec(image_uri=image),
            # Lineage twice, on purpose: labels are queryable in the console and in
            # `gcloud ai models list --filter`, the description holds the untruncated
            # values a rebuild actually needs.
            labels={**self.cfg.tags(2),
                    **{k: self._label_value(val) for k, val in lineage.items()}},
            version_description=json.dumps(lineage, sort_keys=True) if lineage else "",
        )
        parent_model = self._find_model(name)
        request = v.UploadModelRequest(parent=self._registry_parent(), model=model,
                                       parent_model=parent_model or None)
        result = self._models().upload_model(request=request).result()
        return result.model_version_id

    def get_model(self, name: str, version: str) -> dict[str, Any]:
        """Resolve name + version to the artifact and the lineage recorded with it.
        `version` may be a version id ("1") or an alias ("production")."""
        import json

        resource = self._find_model(name)
        if resource is None:
            raise LookupError(f"No registered model named {name!r} in "
                              f"{self._registry_parent()}. Run `make register` first.")
        model = self._models().get_model(name=f"{resource}@{version}")
        try:
            recorded = json.loads(model.version_description or "{}")
        except json.JSONDecodeError:  # written by hand, or by an earlier version of this code
            recorded = {"version_description": model.version_description}
        return {
            "name": name,
            "resource": model.name,
            "version": model.version_id,
            "artifact_uri": model.artifact_uri,
            "aliases": list(model.version_aliases),
            "lineage": recorded,
            "labels": dict(model.labels),
            "created": model.version_create_time.rfc3339(),
        }

    def promote_model(self, name: str, version: str, alias: str) -> dict[str, Any]:
        """Point `alias` at this version, taking it off whichever version holds it now.
        Vertex aliases are unique per model, so the move is the API's own behaviour."""
        resource = self._find_model(name)
        if resource is None:
            raise LookupError(f"No registered model named {name!r} to promote.")
        model = self._models().get_model(name=f"{resource}@{version}")
        self._models().merge_version_aliases(
            name=f"{resource}@{model.version_id}", version_aliases=[alias])
        return self.get_model(name, model.version_id)

    # deploy / invoke                   -> Lab 3 (Vertex Endpoint)
    # emit_metric                       -> Lab 4 (Cloud Monitoring time series)
    # generate                          -> Lab 5 (managed LLM endpoint; read usageMetadata for tokens)
    # teardown                          -> Lab 5 (filter resources by label)
