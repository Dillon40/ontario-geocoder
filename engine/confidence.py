"""Heuristic match scores, not calibrated probabilities of positional accuracy.

Municipal and NAR scores intentionally differ. Apply geography once, then clamp
by tier; preserve the pre-geography score in the public confidence breakdown.
"""
MUNICIPAL_LAYER_CONFIDENCE = {
    0: 0.99, 1: 0.95, 2: 0.92, 3: 0.88, 4: 0.85, 5: 0.70, 6: 0.65,
}
NAR_LAYER_CONFIDENCE = {
    0: 0.90, 1: 0.85, 2: 0.80, 3: 0.75, 4: 0.70, 5: 0.60, 6: 0.55,
}
TIER_CONFIDENCE_CEILINGS = {1: 1.0, 2: 0.99, 3: 0.90}


def finalize_confidence(result: dict, geography_adjustment: float = 0.0) -> dict:
    """Finalize a raw matcher result (after any fuzzy similarity scaling).

    This is called once by the municipal/NAR matching wrappers, or for a PIN
    result by the common response finalizer. It must not be reapplied to an
    already adjusted result.
    """
    base = float(result.get('confidence', 0.5))
    adjustment = float(geography_adjustment)
    ceiling = TIER_CONFIDENCE_CEILINGS[result['match_tier']]
    unclamped = round(base + adjustment, 6)
    final = round(max(0.0, min(ceiling, unclamped)), 6)
    result['confidence'] = final
    result['confidence_factors'] = {
        'base_confidence': base,
        'geography_adjustment': adjustment,
        'unclamped_confidence': unclamped,
        'confidence_cap': ceiling,
        'final_confidence': final,
        'clamped': final != unclamped,
        'tier': result['match_tier'],
        'match_method': result['match_method'],
    }
    return result
