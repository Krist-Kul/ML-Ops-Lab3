# Lab 2 — registered model

`itcs355-6688053` version **4** · aliases — · registered 2026-09-30T19:09:56.92056Z

Resource: `projects/916002887960/locations/asia-southeast1/models/1022699745459568640@4`  
Artifact: `gs://itcs355-6688053/studies/lab2-20260921-010551/trials/trial-01/model`

## Lineage, on the version

Vertex keeps labels and the version description per version, not per model — checked by registering a second version with a different commit and reading the first one back. The full values are in the version description; labels carry the same fields for console filtering, truncated to the 63 characters a GCP label allows, which is why `image_digest` is short there and complete here.

| field | value |
|---|---|
| `git_commit` | `ef3748af7e4337c4d845a708479667a3f372a7c6-dirty` |
| `data_version` | `422cccb9136e8140` |
| `mlflow_run_id` | `429a96b858d646379d363a034b211fd6` |
| `training_job_id` | `3460425601012203520` |
| `image_digest` | `asia-southeast1-docker.pkg.dev/itcs355-6688053/itcs355/itcs355-lab1@sha256:355bcf8f42f24f13af13d32792f708a725fc920852e0ff54055a094cf534346b` |
| `seed` | `20260101` |
| `metric_val` | `0.839153` |
| `metric_test` | `0.851493` |

## How this version was produced

Job `3460425601012203520` ran the digest-pinned image above on n1-standard-4 spot for 13 s, costing 0.0085 THB. It read the data at `422cccb9136e8140` from object storage and wrote the model back to it; nothing on the laptop took part.

Registered straight from the study artifact, so its metrics are the study's own.

## Reload

```bash
make reload-check VERSION=4
```

`scripts/reload_check.py` resolves name and version through the registry, reads the artifact URI it gets back, and loads that — no local path, no remembered bucket key.

## Promotion

`none` now points at version 4. Vertex aliases are unique within a model, so promoting moves the alias off whatever held it — the alias, not the version number, is what a service should load. Note that the `default` alias stays on version 1 unless it is moved explicitly; registering a newer version does not promote it, which is the correct default and a surprise to anyone expecting latest-wins.

Who may do this, and on what evidence: see README, *Who may promote a model*.
