# Day 30 — When the Anomaly Detector Becomes the Anomaly

## TL;DR
Day 28's Isolation Forest scores solar wind windows against a fixed reference distribution — but space weather isn't stationary. Solar cycle phase shifts and instrument recalibration mean "normal" drifts out from under the model, silently. Added a drift-monitoring layer: PSI + KS-test per feature on a rolling window, severity-gated the same way alerting is (Day 29), and routed through a HITL checkpoint instead of auto-retraining.

## The Problem

An unsupervised anomaly detector has no concept of its own staleness:

- The model keeps scoring live data against a training-time reference distribution with no expiry — precision degrades continuously and nothing signals it.
- Drift and genuine anomalies produce the same symptom (elevated scores), so without a separate check, drift just looks like a wave of false positives hitting the Day-29 alerting pipeline.
- Auto-retraining on whatever the model last saw is its own failure mode — a sustained real event (a geomagnetic storm) can get absorbed into "normal" if the retrain window isn't filtered first.

Left alone, drift shows up as an unexplained spike in alert volume — which looks like a Day-29 tuning problem when it's actually a Day-28 model going stale underneath.

## Architecture: Detect, Gate, Checkpoint

**Stage 1 — Per-feature drift detection.** PSI catches distribution shift, KS-test catches shape changes PSI can miss, computed per core feature (Bz, solar wind speed, proton density) on a rolling window against the training reference:

```python
import numpy as np
from scipy.stats import ks_2samp

def population_stability_index(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    breakpoints = np.linspace(0, 100, bins + 1)
    cut_points = np.percentile(reference, breakpoints)
    cut_points[0], cut_points[-1] = -np.inf, np.inf

    ref_counts, _ = np.histogram(reference, bins=cut_points)
    cur_counts, _ = np.histogram(current, bins=cut_points)

    ref_pct = np.where(ref_counts == 0, 1e-4, ref_counts / len(reference))
    cur_pct = np.where(cur_counts == 0, 1e-4, cur_counts / len(current))

    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def check_drift(reference_window: dict, current_window: dict,
                 ks_alpha: float = 0.05) -> dict:
    results = {}
    for feature, ref_values in reference_window.items():
        cur_values = current_window[feature]
        psi = population_stability_index(ref_values, cur_values)
        ks_stat, ks_pvalue = ks_2samp(ref_values, cur_values)
        results[feature] = {
            "psi": round(psi, 4),
            "ks_stat": round(float(ks_stat), 4),
            "ks_significant": bool(ks_pvalue < ks_alpha),
        }
    return results
```

**Stage 2 — Severity gating.** PSI thresholds fit against known solar-cycle transitions, not the generic credit-risk 0.1/0.25 cutoffs — same principle as tiering alert severity off the historical score distribution in Day 29 rather than a fixed number:

```python
def severity_for_psi(psi: float, thresholds: dict) -> str:
    if psi >= thresholds["severe"]:
        return "severe"
    if psi >= thresholds["moderate"]:
        return "moderate"
    return "none"

# calibrated by backtesting against labeled solar-cycle phase transitions
DRIFT_THRESHOLDS = {"moderate": 0.2, "severe": 0.25}
```

**Stage 3 — HITL checkpoint, not auto-retrain.** Drift crossing threshold raises a retrain-approval checkpoint (reusing the Day-25 HITL pattern) naming the drifted features, rather than retraining unattended:

```python
def drift_node(state: dict) -> dict:
    drift_report = check_drift(state["reference_window"], state["current_window"])

    drifted = [
        f for f, r in drift_report.items()
        if severity_for_psi(r["psi"], DRIFT_THRESHOLDS) != "none" and r["ks_significant"]
    ]

    if drifted:
        state["hitl_checkpoint"] = {
            "type": "retrain_approval",
            "drifted_features": drifted,
            "drift_report": drift_report,
            "message": f"Drift detected in {drifted}. Approve retrain scope before proceeding.",
        }
        state["route"] = "human_checkpoint"
    else:
        state["route"] = "continue"

    return state
```

## Failure Modes (the reason this took longer than expected)

- **PSI thresholds don't transfer across domains.** The commonly cited 0.1/0.25 cutoffs come from credit-risk modeling. Applied directly, they either fired constantly on normal seasonal variation or missed real solar-cycle transitions — had to backtest against known phase-transition dates to calibrate values that actually mean something here.

- **A naive retrain window absorbs the anomaly it should be flagging.** If the "most recent data" becomes the new reference distribution, a sustained real event — the exact thing the detector exists to catch — gets learned in as normal. The reference window has to be anomaly-filtered before it's trusted as ground truth, which means the drift check and the anomaly detector aren't fully independent — they have to coordinate.

- **Retraining silently invalidates score calibration.** Anyone (human or downstream agent) who's learned to trust a specific anomaly-score threshold is now wrong the moment a retrain happens. Every retrain gets a version-tagged model card, same provenance pattern as Day 26, so the score history stays interpretable across model versions.

- **Drift and alert-volume spikes look identical from the outside.** Without this layer, a drifted model and a genuine wave of anomalies both show up as "alerts are firing more." Splitting drift detection out as its own signal, separate from the Day-29 alerting pipeline, was necessary just to be able to tell the two apart.

## What's Next

Calibrating confidence on top of this — not just "is this drifted" but "how sure is the model," so escalation to the HITL checkpoint scales with actual uncertainty instead of a flat threshold.

---
*Building Orbital Watch, a space situational awareness agent, in public.*
