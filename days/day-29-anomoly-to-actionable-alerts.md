# Day 29 — From Anomaly Scores to Actionable Alerts

## TL;DR
Day 28's Isolation Forest gave Orbital Watch a way to flag anomalous solar wind windows — but a flagged row sitting in a dataframe doesn't notify anyone. Added an alerting layer on top: a persistence gate to filter point noise, severity tiering off the historical score distribution, and rate-limited dispatch to Slack with an escalation override and a durable fallback if delivery fails.

## The Problem

Raw Isolation Forest output fires per-window, independently, with no memory of what came before it:

- A single noisy sample gets flagged with the same urgency as a sustained deviation — no notion of persistence, so point noise and real drift look identical.
- Every flagged window is treated the same — no severity tiering, so a borderline anomaly score and an extreme one produce the same (non-existent) response.
- Nothing was actually listening. Flags landed in a dataframe. No channel, no human or downstream agent could act on them.

Wiring raw model output straight into a notification channel would mean alert fatigue within the first hour of live traffic.

## Architecture: Gate, Tier, Dispatch

**Stage 1 — Persistence gate.** An anomaly only becomes alert-eligible after N consecutive windows are flagged, not a single blip:

```python
def check_persistence(flags: list[bool], min_consecutive: int = 3) -> bool:
    if len(flags) < min_consecutive:
        return False
    return all(flags[-min_consecutive:])
```

**Stage 2 — Severity tiering.** Anomaly score maps to watch / warning / critical using percentile thresholds fit on the historical score distribution, not a fixed cutoff, so tiers stay meaningful as the model retrains:

```python
def tier_for_score(score: float, thresholds: dict) -> str:
    if score >= thresholds["critical_p"]:
        return "critical"
    if score >= thresholds["warning_p"]:
        return "warning"
    if score >= thresholds["watch_p"]:
        return "watch"
    return "none"

# thresholds refit alongside each Isolation Forest retrain
def fit_thresholds(historical_scores: np.ndarray) -> dict:
    return {
        "watch_p": np.percentile(historical_scores, 90),
        "warning_p": np.percentile(historical_scores, 97),
        "critical_p": np.percentile(historical_scores, 99.5),
    }
```

**Stage 3 — Dispatch with cooldown + escalation override.** Each severity tier gets its own rate-limit window per entity/event. A tier that already fired won't re-fire until cooldown expires — *unless* severity escalates, which bypasses the limiter:

```python
def should_dispatch(event_id: str, new_tier: str, cooldowns: dict, tier_rank: dict) -> bool:
    last = cooldowns.get(event_id)

    if last is None:
        return True

    if tier_rank[new_tier] > tier_rank[last["tier"]]:
        return True  # escalation always bypasses cooldown

    return (time.time() - last["fired_at"]) > COOLDOWN_SECONDS[new_tier]


def dispatch_alert(event_id: str, tier: str, payload: dict, cooldowns: dict):
    if not should_dispatch(event_id, tier, cooldowns, TIER_RANK):
        return

    try:
        send_slack_webhook(payload)
    except (Timeout, HTTPError) as e:
        log_alert_fallback(event_id, tier, payload, error=str(e))
    finally:
        cooldowns[event_id] = {"tier": tier, "fired_at": time.time()}
```

## Failure Modes (the reason this took longer than expected)

- **Cooldown swallowing genuine escalation.** A naive cooldown suppresses a worsening event just because a lower-severity alert already fired recently — the exact case alerting exists for. The `tier_rank` comparison in `should_dispatch` exists specifically to bypass cooldown on escalation; tested this explicitly by simulating watch → warning → critical within a single cooldown window.

- **Silent delivery failure.** A caught webhook exception that just gets swallowed is worse than no alerting — it creates false confidence that someone was notified. `log_alert_fallback` writes the full payload and error to a structured local log, so a failed Slack call is still an auditable event:

```python
def log_alert_fallback(event_id: str, tier: str, payload: dict, error: str):
    logger.error(json.dumps({
        "event": "alert_dispatch_failed",
        "event_id": event_id,
        "tier": tier,
        "payload": payload,
        "error": error,
        "timestamp": datetime.utcnow().isoformat(),
    }))
```

- **Threshold drift across retrains.** Since severity buckets are relative to the historical score distribution, retraining the Isolation Forest shifts what counts as "critical." Logging the fitted threshold values alongside each retrain so tier definitions stay auditable over time rather than silently moving underfoot.

- **Cooldown state scope.** Cooldowns are keyed per `event_id`, not globally — a naive global rate limit would suppress alerts for a second, unrelated anomaly just because a different event recently fired. Worth calling out since it's an easy default to get wrong.

## What's Next

Extending the same gate → tier → dispatch contract to the NeoWs and CelesTrak agents, so all three data sources report through one alerting system instead of three bespoke ones.

---
*Building Orbital Watch, a space situational awareness agent, in public.*
