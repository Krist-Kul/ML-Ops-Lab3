**Registered: `trial-00` — `max_depth=4`, `max_features=sqrt`, `class_weight=None`, 200 trees.**

This is also the highest val ROC AUC (0.8405), so the margin has to be defended rather than
cited. On its own it is not defensible: the best five trials sit within 0.0040, while
re-seeding the forest alone moves this configuration by 0.0030 and re-seeding — which
re-deals the machine-grouped split — moves it by 0.0273 (`reports/lab2-seed-variance.md`).
Any single trial-to-trial gap here is noise.

What survives the noise is direction, not rank: `sqrt` beats `max_features=1.0` in all six
pairings, and depth 4 ≥ 8 ≥ unlimited in all four. I registered that family, then took
`class_weight=None` within it because weighting moves the metric by 0.0013 — nothing — while
distorting the probabilities Lab 3 has to serve.

Cost cannot break the tie: every trial costs about 0.011 THB. The study cost 0.23 THB, and
retraining this model monthly on spot costs roughly 0.08 THB, dominated by container startup
rather than fitting.

How this could be wrong: PR AUC ranks `trial-04` first. If the service is judged on
precision and recall at a threshold rather than on ranking, I have optimised the wrong
metric on a 12%-positive problem.
