# Lab 2 — registered model

`itcs355-6688053` version **1** · aliases `default`, `staging` · registered 2026-09-20T18:46:59.429169Z

Resource: `projects/916002887960/locations/asia-southeast1/models/1022699745459568640@1`  
Artifact: `gs://itcs355-6688053/registry/itcs355-6688053/itcs355-register-20260921-014231/model`

## Lineage, on the version

Vertex keeps labels and the version description per version, not per model — checked by registering a second version with a different commit and reading the first one back. The full values are in the version description; labels carry the same fields for console filtering, truncated to the 63 characters a GCP label allows, which is why `image_digest` is short there and complete here.

| field | value |
|---|---|
| `git_commit` | `6173dd677264976917a708c4742f970727986392` |
| `data_version` | `422cccb9136e8140` |
| `mlflow_run_id` | `63f64ecfb35841deac2c22e41710e293` |
| `training_job_id` | `4570562909159030784` |
| `image_digest` | `asia-southeast1-docker.pkg.dev/itcs355-6688053/itcs355/itcs355-lab1@sha256:3e520841ebb7264256b2829cecfe3236a7152f32ff0a920f34c2c1d4924692b5` |
| `seed` | `20260101` |
| `metric_val` | `0.840455` |
| `metric_test` | `0.854311` |

## How this version was produced

Job `4570562909159030784` ran the digest-pinned image above on n1-standard-4 spot for 61 s, costing 0.0518 THB. It read the data at `422cccb9136e8140` from object storage and wrote the model back to it; nothing on the laptop took part.

Retrained from commit `6173dd677264`, the study's `trial-00` reproduces exactly: val ROC AUC 0.8405, test 0.8543, identical to the trial's.

## Reload

```bash
make reload-check VERSION=1
```

`scripts/reload_check.py` resolves name and version through the registry, reads the artifact URI it gets back, and loads that — no local path, no remembered bucket key.

## Promotion

`staging` now points at version 1. Vertex aliases are unique within a model, so promoting moves the alias off whatever held it — the alias, not the version number, is what a service should load. Note that the `default` alias stays on version 1 unless it is moved explicitly; registering a newer version does not promote it, which is the correct default and a surprise to anyone expecting latest-wins.

Who may do this, and on what evidence: see README, *Who may promote a model*.

## Evidence

Reload, by version, from the registry — `make reload-check VERSION=1`, on a laptop that
did not train this model (the job did):

```
registry projects/916002887960/locations/asia-southeast1/models/1022699745459568640@1
version  1  aliases: default, staging
  git_commit       6173dd677264976917a708c4742f970727986392
  data_version     422cccb9136e8140
  mlflow_run_id    63f64ecfb35841deac2c22e41710e293
  training_job_id  4570562909159030784
  image_digest     ...itcs355-lab1@sha256:3e520841ebb7264256b2829cecfe3236a7152f32ff0a920f34c2c1d4924692b5
  seed             20260101
  metric_val       0.840455
  metric_test      0.854311

loading gs://itcs355-6688053/registry/itcs355-6688053/itcs355-register-20260921-014231/model
  reading 125: p(failure)=0.0203
  reading 126: p(failure)=0.0658
  reading 127: p(failure)=0.0293
  reading 128: p(failure)=0.0262
  reading 129: p(failure)=0.0137

PASS  model reloaded from the registry and scored rows
```

The check fails rather than warns if any of the eight fields is empty on the version: a
model you cannot trace is not one you can promote, whatever it scores.

Promotion, staging then production — `make promote VERSION=1 ALIAS=production`:

```
itcs355-6688053 version 1 aliases: default, production, staging
```

Rolling back is the same call naming the previous version, which is why the promotion
evidence list in the README asks for that version number before the alias moves.

## What it cost, and what is still running

The registration job cost 0.0518 THB on spot, against 0.23 THB for the 12-trial study
at the same verified rates (`reports/lab2-study.md` reports 0.1716 THB for it, the
figure its ledger logged before the rates in `src/costs.py` were corrected). Lab 2 has
spent 0.51 THB of its 150 THB budget in total, the largest single item being the first
managed run, which was submitted on demand rather than spot.
Registering a model and holding versions costs nothing beyond the ~8 MB of object storage
the artifact occupies; the meter only restarts when Lab 3 attaches an endpoint.

No compute is left running: `gcloud ai custom-jobs list` shows three jobs, all terminal.
`make teardown` is not implemented until Lab 5, and would be wrong to run here anyway —
the handout says the registered model and its artifacts stay, because Lab 3 loads this
version.

## Known gap: the container recorded on this version

Vertex refuses a version with no container (`container_spec must be set for custom
model`), so every version names an environment. This one names the training image,
because the newest prebuilt Vertex serving container for scikit-learn is
`sklearn-cpu.1-6` and this model is pickled by scikit-learn 1.8 — checked with
`docker manifest inspect` against `us-docker.pkg.dev/vertex-ai/prediction/`, where 1-7
and 1-8 do not exist. Naming a prebuilt container would have recorded an environment
that cannot load the artifact, which is the exact serialization failure this task is
about, hidden one layer deeper. The training image can load it; it cannot yet serve
HTTP. Lab 3 registers a serving image that answers `/health` and `/predict`, and that
version is the one an endpoint should deploy.
