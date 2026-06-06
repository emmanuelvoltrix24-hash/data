"""Per-source market schemas — features vs bettable targets."""
import json

# Every source gets these features FOR FREE from scores
SCORE_FEATURES = [
    "parity",          # total goals odd/even
    "cs",              # clean sheet (either side)
    "both_score",      # GG
    "total",           # goal count
    "margin",          # goal difference
    "outcome",         # win/loss/draw per match
    "ht_parity",       # half-time total odd/even
    "ht_outcome",      # half-time win/loss/draw
    "any_late",        # any goal after 60'
    "both_late",       # both teams scored after 60'
    "h_late_goals",    # home late goal count
    "a_late_goals",    # away late goal count
    "gg_scored",       # both teams scored (boolean)
    "tg25_scored",     # over 2.5 goals (boolean)
    "tg45_scored",     # over 4.5 goals (boolean)
    "cs_home",         # away kept 0
    "cs_away",         # home kept 0
    "dc_home",         # home wins or draws
    "dc_away",         # away wins or draws
    "margin_group",    # big/small/draw
    "score_band",      # 0-5+
]

# Odds-derived features (when available)
ODDS_FEATURES = [
    "h_prob",          # home win probability bucket
    "a_prob",          # away win probability bucket
    "odds_fav",        # odds favorite
    "gg_yes",          # GG odds value
    "tg25_o",          # O/U 2.5 odds value
]

# Standings-derived features
STANDINGS_FEATURES = [
    "pos_diff",        # position difference
    "pts_diff",        # points difference
    "form_diff",       # form difference
    "h_trend",         # home trend
    "a_trend",         # away trend
]

SOURCES = {
    "bangbet": {
        "name": "BangBet",
        "features": SCORE_FEATURES + STANDINGS_FEATURES,
        "targets": {
            "cs":       {"type": "categorical", "bettable": True,  "derived": False},
            "gg":       {"type": "categorical", "bettable": True,  "derived": True},
            "tg25":     {"type": "categorical", "bettable": True,  "derived": True},
            "dc_home":  {"type": "categorical", "bettable": True,  "derived": True},
            "dc_away":  {"type": "categorical", "bettable": True,  "derived": True},
            "margin":   {"type": "categorical", "bettable": False, "derived": True},
        },
        "features_only": ["parity", "outcome", "ht_parity", "ht_outcome",
                          "both_late", "any_late", "h_late_goals", "a_late_goals",
                          "pos_diff", "pts_diff", "form_diff", "h_trend", "a_trend"],
    },
    "betkraft": {
        "name": "BetKraft",
        "features": SCORE_FEATURES + ODDS_FEATURES + STANDINGS_FEATURES,
        "targets": {
            "cs":       {"type": "categorical", "bettable": True,  "derived": False},
            "gg":       {"type": "categorical", "bettable": True,  "derived": True},
            "tg25":     {"type": "categorical", "bettable": True,  "derived": True},
            "tg45":     {"type": "categorical", "bettable": False, "derived": True},
            "oe":       {"type": "categorical", "bettable": True,  "derived": True},
            "dc_home":  {"type": "categorical", "bettable": True,  "derived": True},
            "dc_away":  {"type": "categorical", "bettable": True,  "derived": True},
            "margin":   {"type": "categorical", "bettable": False, "derived": True},
        },
        "features_only": ["parity", "outcome", "ht_parity", "ht_outcome",
                          "both_late", "any_late", "h_late_goals", "a_late_goals",
                          "pos_diff", "pts_diff", "form_diff", "h_trend", "a_trend"],
    },
    "bongobongo": {
        "name": "BongoBongo",
        "features": SCORE_FEATURES,
        "targets": {
            "cs":       {"type": "categorical", "bettable": True,  "derived": False},
            "gg":       {"type": "categorical", "bettable": True,  "derived": True},
            "tg25":     {"type": "categorical", "bettable": True,  "derived": True},
            "margin":   {"type": "categorical", "bettable": False, "derived": True},
        },
        "features_only": ["parity", "outcome", "ht_parity", "ht_outcome",
                          "both_late", "any_late"],
    },
    "betpawa": {
        "name": "BetPawa",
        "features": SCORE_FEATURES + STANDINGS_FEATURES,
        "targets": {
            "cs":       {"type": "categorical", "bettable": True,  "derived": False},
            "gg":       {"type": "categorical", "bettable": True,  "derived": True},
            "tg25":     {"type": "categorical", "bettable": True,  "derived": True},
            "oe":       {"type": "categorical", "bettable": True,  "derived": True},
            "dc_home":  {"type": "categorical", "bettable": True,  "derived": True},
            "dc_away":  {"type": "categorical", "bettable": True,  "derived": True},
            "margin":   {"type": "categorical", "bettable": False, "derived": True},
        },
        "features_only": ["parity", "outcome", "ht_parity", "ht_outcome",
                          "both_late", "any_late",
                          "pos_diff", "pts_diff", "form_diff", "h_trend", "a_trend"],
    },
}

def get_source_config(source):
    """Get the market config for a source."""
    return SOURCES.get(source, {})

def get_bettable_targets(source):
    """Get only bettable targets for a source."""
    config = SOURCES.get(source, {})
    return {k: v for k, v in config.get("targets", {}).items() if v.get("bettable")}

def get_all_targets(source):
    """Get all targets (bettable + derived non-bettable)."""
    config = SOURCES.get(source, {})
    return dict(config.get("targets", {}))

def get_features(source):
    """Get all features available for a source."""
    config = SOURCES.get(source, {})
    return list(config.get("features", []))

def get_features_only(source):
    """Get features that are NEVER targets (always inputs only)."""
    config = SOURCES.get(source, {})
    return list(config.get("features_only", []))

def get_dimension_for_target(target_key):
    """Map a target key to its dimension group."""
    DIMENSION_MAP = {
        "cs":      "clean_sheet",
        "gg":      "both_scored",
        "tg25":    "goal_volume",
        "tg45":    "goal_volume",
        "oe":      "odd_even",
        "dc_home": "match_outcome",
        "dc_away": "match_outcome",
        "margin":  "goal_margin",
    }
    return DIMENSION_MAP.get(target_key, "unknown")
