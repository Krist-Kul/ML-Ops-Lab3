# Lab 2 — seed variance for the chosen configuration

`{'n_estimators': 200, 'max_depth': 4, 'min_samples_leaf': 5, 'max_features': 'sqrt', 'class_weight': None}`, data fingerprint `422cccb9136e8140`.

## A. Forest seed only (split held at 20260101)

| forest seed | val ROC AUC | val PR AUC | test ROC AUC | test PR AUC |
|---|---|---|---|---|
| 1 | 0.8429 | 0.3989 | 0.8542 | 0.5096 |
| 2 | 0.8399 | 0.3902 | 0.8530 | 0.4965 |
| 3 | 0.8408 | 0.3921 | 0.8536 | 0.5034 |
| 4 | 0.8412 | 0.3925 | 0.8534 | 0.5047 |
| 5 | 0.8408 | 0.3931 | 0.8546 | 0.5019 |
| **val_roc_auc spread** | 0.0030 | (min 0.8399, max 0.8429, sd 0.0010) | | |
| **test_roc_auc spread** | 0.0017 | (min 0.8530, max 0.8546, sd 0.0006) | | |

## B. Seed changes the split as well — what `--seed N` does

| seed | val ROC AUC | val PR AUC | test ROC AUC | test PR AUC |
|---|---|---|---|---|
| 20260101 | 0.8405 | 0.3871 | 0.8543 | 0.4995 |
| 7 | 0.8570 | 0.4423 | 0.8532 | 0.4929 |
| 1234 | 0.8678 | 0.4906 | 0.8623 | 0.4868 |
| 99 | 0.8602 | 0.5385 | 0.8468 | 0.4664 |
| 4242 | 0.8527 | 0.3928 | 0.8532 | 0.4593 |
| **val_roc_auc spread** | 0.0273 | (min 0.8405, max 0.8678, sd 0.0090) | | |
| **test_roc_auc spread** | 0.0155 | (min 0.8468, max 0.8623, sd 0.0049) | | |

The split-grouped design means a new seed deals different machines into test, so B is dominated by which machines land where, not by the model. B is the number to compare trial-to-trial differences against.
