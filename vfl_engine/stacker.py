"""
VFL Stacker — Core prediction engine
Mines dimension rules, stacks them per slot, produces correct score probabilities.
"""
import os, json, math, time, itertools
from collections import defaultdict, Counter
from datetime import datetime

import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Dimension definitions
DIMENSIONS = {
    "odd_even": {
        "feature": "parity",
        "values": ["O", "E"],
        "weight": 1.0,
        "scores": {
            "O": [(1,0),(2,1),(3,0),(3,2),(4,1),(4,3),(0,1),(1,2),(0,3),(2,3),(1,4),(3,4)],
            "E": [(0,0),(2,0),(0,2),(1,1),(3,1),(1,3),(2,2),(4,0),(0,4),(4,2),(2,4)],
        }
    },
    "both_scored": {
        "feature": "gg_scored",
        "values": [True, False],
        "weight": 1.0,
        "scores": {
            True:  [(1,1),(2,1),(1,2),(2,2),(3,1),(1,3),(3,2),(2,3),(3,3)],
            False: [(0,0),(1,0),(2,0),(3,0),(4,0),(0,1),(0,2),(0,3),(0,4)],
        }
    },
    "goal_volume": {
        "feature": "tg25_scored",
        "values": [True, False],
        "weight": 1.0,
        "scores": {
            True:  [(2,1),(1,2),(2,2),(3,0),(3,1),(3,2),(4,0),(4,1),(2,3),(1,4)],
            False: [(0,0),(1,0),(0,1),(2,0),(0,2),(1,1)],
        }
    },
    "home_cs": {
        "feature": "cs_home",
        "values": [True, False],
        "weight": 0.8,
        "scores": {
            True:  [(1,0),(2,0),(3,0),(4,0)],
            False: [(0,0),(0,1),(1,1),(0,2),(2,1),(1,2),(0,3),(2,2),(3,1),(1,3)],
        }
    },
    "away_cs": {
        "feature": "cs_away",
        "values": [True, False],
        "weight": 0.8,
        "scores": {
            True:  [(0,1),(0,2),(0,3),(0,4)],
            False: [(0,0),(1,0),(1,1),(2,0),(2,1),(1,2),(3,0),(2,2),(3,1),(1,3)],
        }
    },
    "match_outcome": {
        "feature": "outcome",
        "values": ["W", "D", "L"],
        "weight": 0.9,
        "scores": {
            "W": [(1,0),(2,0),(2,1),(3,0),(3,1),(4,0),(4,1),(3,2)],
            "D": [(0,0),(1,1),(2,2),(3,3)],
            "L": [(0,1),(0,2),(1,2),(0,3),(1,3),(2,3),(0,4),(1,4)],
        }
    },
}

ALL_SCORES = [(h, a) for h in range(5) for a in range(5)]


def mine_dimension_rules(fvecs, source, min_hits=5, min_precision=0.75):
    """
    Mine rules per dimension from feature vectors.
    Returns { dimension: [(conditions, precision, hits, total), ...] }
    """
    n = len(fvecs)
    if n == 0:
        return {}
    
    # Temporal decay
    weights = [math.exp(-0.693 * (n - 1 - i) / 30) for i in range(n)]
    PRIOR_MASS = 3.6   # 36% prior × 10 strength
    PRIOR_STR = 10
    
    result = {}
    
    for dim_name, dim in DIMENSIONS.items():
        feat_key = dim["feature"]
        dim_rules = []
        
        # For each possible dimension value, find conditions that predict it
        for dim_val in dim["values"]:
            # 1-feature conditions from all available keys
            all_keys = list(fvecs[0].keys()) if fvecs else []
            # Focus on slot-level features from previous matches
            slot_keys = [k for k in all_keys if any(
                k.startswith(f"M{s}_") for s in range(1, 11)
            ) and not any(k.endswith(f"_{x}") for x in ["outcome", "parity", "cs", "gg_scored", "tg25_scored", "cs_home", "cs_away", "dc_home", "dc_away", "margin_group", "score_band", "tg45_scored", "both_score", "any_late", "both_late", "h_late_goals", "a_late_goals", "h_trend", "a_trend", "pos_diff", "pts_diff", "form_diff"])]
            # Add the dimension feature itself (for same-slot rules)
            dim_feat_keys = [k for k in all_keys if k.endswith(f"_{feat_key}")]
            condition_keys = slot_keys[:20] + dim_feat_keys[:5]
            
            for ck in condition_keys:
                # Count occurrences of each condition value paired with dimension value
                vc = defaultdict(lambda: {"hits": 0, "total": 0})
                for i in range(n - 1):  # lag 1
                    cv = fvecs[i].get(ck)
                    dv = fvecs[i + 1].get(f"M_{feat_key}") or fvecs[i + 1].get(f"M{i+1}_{feat_key}")
                    if cv is not None and dv == dim_val:
                        vc[cv]["hits"] += weights[i]
                    if cv is not None:
                        vc[cv]["total"] += weights[i]
                
                for cv, counts in vc.items():
                    total = counts["total"]
                    hits = counts["hits"]
                    if total >= min_hits:
                        raw_prec = hits / total
                        bayes_prec = (hits + PRIOR_MASS) / (total + PRIOR_STR)
                        if bayes_prec >= min_precision:
                            dim_rules.append({
                                "condition": {ck: cv},
                                "value": dim_val,
                                "precision": round(bayes_prec, 3),
                                "hits": int(hits),
                                "total": int(total),
                            })
        
        if dim_rules:
            # Sort by precision, take top 5 per value
            dim_rules.sort(key=lambda r: -r["precision"])
            result[dim_name] = dim_rules[:15]
    
    return result


def stack_slot(slot_features, source, dim_rules, market_config):
    """
    Given current slot features + mined dimension rules, produce:
    { "top_scores": [(2,1, 0.45), (1,0, 0.20), ...],
      "derived": {"gg": True, "tg25": False, ...},
      "edge": +0.15 }
    """
    # Phase 1: For each dimension, estimate probabilities
    dim_probs = {}
    for dim_name, dim in DIMENSIONS.items():
        feat_key = dim["feature"]
        # Get the current slot's value for this feature
        current_val = slot_features.get(f"M_{feat_key}", slot_features.get(f"M1_{feat_key}"))
        current_slot_num = None
        for k, v in slot_features.items():
            if k.endswith(f"_{feat_key}"):
                current_val = v
                current_slot_num = k.split("_")[0].replace("M", "")
                break
        
        matching_rules = dim_rules.get(dim_name, [])
        relevant = [r for r in matching_rules if current_val == r.get("value")]
        
        if relevant:
            avg_prec = sum(r["precision"] for r in relevant) / len(relevant)
            # Distribute probability across dimension values
            for dv in dim["values"]:
                if dv == current_val:
                    dim_probs[dv] = dim_probs.get(dv, 0) + avg_prec * dim["weight"]
                else:
                    dim_probs[dv] = dim_probs.get(dv, 0) + (1 - avg_prec) / (len(dim["values"]) - 1) * dim["weight"]
        else:
            # No rules — uniform distribution
            for dv in dim["values"]:
                dim_probs[dv] = dim_probs.get(dv, 0) + 1.0 / len(dim["values"])
    
    # Normalize dimension probabilities
    total_prob = sum(dim_probs.values())
    if total_prob > 0:
        for dv in dim_probs:
            dim_probs[dv] /= total_prob
    
    # Phase 2: Cross dimensions to score probabilities
    score_probs = {}
    for h in range(5):
        for a in range(5):
            prob = 1.0
            for dim_name, dim in DIMENSIONS.items():
                # Find which dimension values this score satisfies
                score_matches = []
                for dv, score_list in dim["scores"].items():
                    if (h, a) in score_list:
                        score_matches.append(dv)
                
                if score_matches:
                    # Use the matching dimension value's probability
                    dim_val_prob = max(dim_probs.get(dv, 0) for dv in score_matches)
                    prob *= max(dim_val_prob, 0.01)  # floor at 1%
                else:
                    # Score doesn't match any dimension value — low probability
                    prob *= 0.01
            
            score_probs[(h, a)] = prob
    
    # Normalize all score probabilities
    total_score_prob = sum(score_probs.values())
    if total_score_prob > 0:
        for score in score_probs:
            score_probs[score] /= total_score_prob
    
    # Phase 3: Top 3 scores
    sorted_scores = sorted(score_probs.items(), key=lambda x: -x[1])
    top3 = [(h, a, round(p, 3)) for (h, a), p in sorted_scores[:3]]
    
    # Phase 4: Derive markets from top score
    top = top3[0] if top3 else (0, 0, 0)
    derived = {
        "gg": top[0] > 0 and top[1] > 0,
        "tg25": top[0] + top[1] >= 3,
        "tg45": top[0] + top[1] >= 5,
        "oe": "O" if (top[0] + top[1]) % 2 == 1 else "E",
        "dc_home": top[0] >= top[1],
        "dc_away": top[1] >= top[0],
        "cs_home": top[1] == 0,
        "cs_away": top[0] == 0,
        "margin": abs(top[0] - top[1]),
        "margin_group": "big" if abs(top[0]-top[1]) >= 3 else ("draw" if top[0]==top[1] else "small"),
        "winner": "H" if top[0] > top[1] else ("A" if top[1] > top[0] else "D"),
    }
    
    # Filter to only what this source markets
    bettable = {k: v for k, v in derived.items() if k in market_config.get("targets", {})}
    
    return {
        "top_scores": top3,
        "top_score_str": f"{top[0]}-{top[1]}",
        "top_prob": top[2],
        "derived_all": derived,
        "derived_bettable": bettable,
    }


def stack_round(matches_features, source, dim_rules, market_config):
    """
    Stack predictions for an entire round.
    Returns { slot_num: stack_result }
    """
    results = {}
    for slot_num, features in matches_features.items():
        result = stack_slot(features, source, dim_rules, market_config)
        if result and result["top_prob"] > 0.1:
            results[slot_num] = result
    return results


def validate_audit(slot_result, actual_hg, actual_ag):
    """
    Check if a stacked prediction was correct.
    Returns: { exact, top3, winner_match, margin_match, gg_match, precision_score }
    """
    if not slot_result:
        return {"error": "no prediction"}
    
    top3 = slot_result.get("top_scores", [])
    exact = any(h == actual_hg and a == actual_ag for h, a, _ in top3)
    top3_match = exact  # exact is highest form
    
    # Proximity scoring
    actual_winner = "H" if actual_hg > actual_ag else ("A" if actual_ag > actual_hg else "D")
    predicted_winner = slot_result.get("derived_all", {}).get("winner", "")
    winner_match = actual_winner == predicted_winner
    
    actual_margin = abs(actual_hg - actual_ag)
    predicted_margin = slot_result.get("derived_all", {}).get("margin", -1)
    margin_match = actual_margin == predicted_margin
    
    actual_gg = actual_hg > 0 and actual_ag > 0
    predicted_gg = slot_result.get("derived_all", {}).get("gg", False)
    gg_match = actual_gg == predicted_gg
    
    # Precision: 1.0 if exact, 0.7 if winner+margin, 0.4 if only winner, 0 if nothing
    if exact:
        precision_score = 1.0
    elif winner_match and margin_match:
        precision_score = 0.7
    elif winner_match:
        precision_score = 0.4
    elif gg_match:
        precision_score = 0.2
    else:
        precision_score = 0.0
    
    return {
        "exact": exact,
        "top3": top3_match,
        "winner_match": winner_match,
        "margin_match": margin_match,
        "gg_match": gg_match,
        "precision_score": precision_score,
        "total_goals": actual_hg + actual_ag,
        "actual_score": f"{actual_hg}-{actual_ag}",
    }
