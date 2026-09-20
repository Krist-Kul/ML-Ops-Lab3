# Lab 2 — study `lab2-20260921-010551`

State: `gs://itcs355-6688053/studies/lab2-20260921-010551` · budget 150 THB · spent 0.1716 THB on jobs, of which 0.1174 THB was trial compute

## Jobs

| attempt | job | state | running s | instance | spot | THB | trials finished |
|---|---|---|---|---|---|---|---|
| 1 | `3460425601012203520` | succeeded | 271 | n1-standard-4 | True | 0.1716 | trial-00, trial-01, trial-02, trial-03, trial-04, trial-05, trial-06, trial-07, trial-08, trial-09, trial-10, trial-11 |

Interruption drill: `trial-05` was killed mid-trial, before its checkpoint was written. Work resumed from the checkpoint — either the platform restarted the container or the next attempt picked the study up — skipping every finished trial, so the interruption cost one trial, not the run.

## Trials

| trial | max_depth | max_features | class_weight | val ROC AUC | test ROC AUC | s | THB |
|---|---|---|---|---|---|---|---|
| trial-00 | 4 | sqrt | None | 0.8405 | 0.8543 | 26.6 | 0.016821 |
| trial-01 | 4 | sqrt | balanced_subsample | 0.8392 | 0.8515 | 13.4 | 0.008514 |
| trial-02 | 4 | 1.0 | None | 0.8317 | 0.8470 | 13.7 | 0.008678 |
| trial-03 | 4 | 1.0 | balanced_subsample | 0.8311 | 0.8530 | 14.0 | 0.008871 |
| trial-04 | 8 | sqrt | None | 0.8364 | 0.8482 | 13.4 | 0.008513 |
| trial-05 | 8 | sqrt | balanced_subsample | 0.8388 | 0.8471 | 20.0 | 0.012647 |
| trial-06 | 8 | 1.0 | None | 0.8239 | 0.8438 | 14.0 | 0.008847 |
| trial-07 | 8 | 1.0 | balanced_subsample | 0.8221 | 0.8443 | 14.0 | 0.008875 |
| trial-08 | None | sqrt | None | 0.8365 | 0.8418 | 13.4 | 0.008487 |
| trial-09 | None | sqrt | balanced_subsample | 0.8271 | 0.8443 | 14.1 | 0.008907 |
| trial-10 | None | 1.0 | None | 0.8190 | 0.8352 | 14.3 | 0.009068 |
| trial-11 | None | 1.0 | balanced_subsample | 0.8126 | 0.8380 | 14.5 | 0.009167 |
