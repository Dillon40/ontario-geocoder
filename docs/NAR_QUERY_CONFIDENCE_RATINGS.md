# Geocoding confidence policy

## Meaning and scope

Scores are **heuristic match-strength indicators, not calibrated probabilities of positional
accuracy**. A high score does not guarantee rooftop precision. Consult `source`, `match_tier`,
`match_method`, fuzzy-match metadata and the matched address as well as the score.

NAR can identify an address unambiguously while its coordinates are less precise than municipal
address points. It therefore has a separate, lower score scale and a **hard final ceiling of 0.90**.
This applies to every NAR layer, including exact, fuzzy, cached and batch results—not just to
Middlesex. Tier ordering is unchanged. Unit disambiguation uses `properties.unit_no` and
`nar_addresses.apt_no_label`, separately from civic-number suffixes.

The single source of runtime scoring policy is `engine/confidence.py`. Earlier versions of this
note mixed duplicate-resolution/uniqueness estimates from a partial NAR sample with examples from
an older matching implementation. Those estimates are not coordinate-accuracy probabilities and
must not be used as the current scoring contract. The policy below supersedes those examples.

## Strategy scores

`engine/layered_matcher.py` selects a candidate and assigns the appropriate tier's strategy score:

| Layer | Match strategy | Municipal base | NAR base |
|---|---|---:|---:|
| 0 | Exact match on supplied fields | 0.99 | 0.90 |
| 1 | Postal code + civic/street core | 0.95 | 0.85 |
| 2 | FSA + civic/street core + city | 0.92 | 0.80 |
| 3 | Postal zone + civic/street core | 0.88 | 0.75 |
| 4 | City + civic/street core | 0.85 | 0.70 |
| 5 | Fuzzy street within postal zone | 0.70 × similarity | 0.60 × similarity |
| 6 | Fuzzy street within city | 0.65 × similarity | 0.55 × similarity |

Fuzzy scores are rounded to three decimal places before geographic adjustment. For example,
NAR layer 5 with similarity 0.80 produces a matcher score of 0.48. Similarity thresholds and
ambiguity handling still determine whether a candidate is eligible; a confidence cap does not
make an otherwise ambiguous or ineligible candidate valid.

## Geographic adjustment and final bounds

`engine/geographic_validator.py` can supply a geographic adjustment:

| Evidence | Adjustment |
|---|---:|
| Requested FSA and municipality align | +0.05 |
| Missing geographic field inferred from FSA | +0.03 |
| Missing geographic field inferred from postal zone | +0.015 |
| Requested FSA and municipality conflict | −0.10 |
| No usable reference evidence / no adjustment | 0.00 |

The validator supplies one adjustment. These are not bonuses to sum independently.
Missing FSA reference coverage is unknown, not evidence of a conflict.

After selection and any fuzzy scaling, `finalize_confidence()` applies the adjustment **once**:

```text
base_confidence = matcher score (already similarity-scaled for fuzzy results)
unclamped_confidence = round(base_confidence + geography_adjustment, 6)
confidence = round(max(0, min(tier_ceiling, unclamped_confidence)), 6)
```

Final ceilings:

- Tier 1, PIN: **1.00** (parcel identity; a parcel can have multiple addresses).
- Tier 2, municipal: **0.99**.
- Tier 3, NAR: **0.90**.

There is no fixed 0.36 lower bound: weaker fuzzy-city matches and geographic penalties can
produce lower scores. All final scores remain within `[0, tier_ceiling]`.

## Public confidence breakdown

`confidence_factors` is identical in single, batch and cached results:

```json
{
  "confidence": 0.90,
  "confidence_factors": {
    "base_confidence": 0.85,
    "geography_adjustment": 0.05,
    "unclamped_confidence": 0.90,
    "confidence_cap": 0.90,
    "final_confidence": 0.90,
    "clamped": false,
    "tier": 3,
    "match_method": "nar_postal_core"
  }
}
```

For an exact NAR match with FSA/city alignment, the values are instead base `0.90`, adjustment
`+0.05`, unclamped `0.95`, final `0.90`, and `clamped=true`. For a city/core NAR match with no
geographic adjustment, base and final are both `0.70`.

`base_confidence` is the pre-geography matcher score, **not** a duplicate of the final score.
Consumers that want the final decision score should read top-level `confidence` (also exposed as
`confidence_factors.final_confidence`). On fuzzy results, `street_similarity` describes the
similarity scaling that has already been applied to `base_confidence`.

## Upgrades and verification

Response-cache namespace v5 invalidates older labels/scores/factors and unit results without flushing API-key or
rate-limit state. Rebuild/recreate the API; no database migration or source reimport is required
for scoring changes. Existing NAR coordinates and municipal source exclusions are unchanged.

The live gate in `api/scripts/verify_deployment.py` checks a postal/core NAR match in Middlesex,
city-only and exact-postal matches in Huntsville, their cached responses, and batch parity.
Regression tests cover all seven NAR layers, both fuzzy selection branches, positive/negative
geographic adjustments, tier bounds, and preservation of the true base score through API
serialization. Future changes to these heuristic weights require consistent updates to this
policy and its tests; claims about actual accuracy require independent evaluation.
