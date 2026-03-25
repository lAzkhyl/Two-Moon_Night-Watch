# =====
# MODULE: config/defaults.py
# =====
# Architecture Overview:
# Contains the hardcoded baseline JSON strings and dictionary payloads
# for the bot's default presets (Balanced, Casual, Competitive).
#
# These are used to swiftly bootstrap a blank server during the
# initial setup wizard.
# =====

import json

_DURATION_TIERS_BALANCED = json.dumps([
    {"max": 60,   "mult": 1.0},
    {"max": 90,   "mult": 1.2},
    {"max": 120,  "mult": 1.4},
    {"max": 9999, "mult": 1.5},
])

_DURATION_TIERS_CASUAL = json.dumps([
    {"max": 60,   "mult": 1.0},
    {"max": 90,   "mult": 1.3},
    {"max": 120,  "mult": 1.6},
    {"max": 9999, "mult": 2.0},
])

_DURATION_TIERS_COMPETITIVE = json.dumps([
    {"max": 60,   "mult": 1.0},
    {"max": 90,   "mult": 1.1},
    {"max": 120,  "mult": 1.3},
    {"max": 9999, "mult": 1.4},
])

_TIER_DEFINITIONS = json.dumps([
    {"label": "Bronze", "min": 0,    "max": 399,    "vote_weight": 1.0},
    {"label": "Silver", "min": 400,  "max": 999,    "vote_weight": 1.5},
    {"label": "Gold",   "min": 1000, "max": 9999999, "vote_weight": 2.0},
])

_HOST_TIER_DEFINITIONS = json.dumps([
    {"label": "Newcomer", "min_avg": 0.0},
    {"label": "Regular",  "min_avg": 3.5},
    {"label": "Elite",    "min_avg": 4.2},
])

PRESET_BALANCED: dict[str, str] = {
    "system.state":                      "UNCONFIGURED",
    "system.admin_role_id":               "[]",
    "system.mod_role_id":                 "[]",
    "system.admin_user_id":               "[]",
    "system.mod_user_id":                 "[]",
    "ec.join_bonus":                     "15",
    "ec.completion_bonus":               "10",
    "ec.base_max_bonus":                 "50",
    "ec.t_cap":                          "120",
    "ec.duration_tiers":                 _DURATION_TIERS_BALANCED,
    "tier.definitions":                  _TIER_DEFINITIONS,
    "decay.grace_days":                  "7",
    "decay.zone1_days":                  "7",
    "decay.zone2_days":                  "7",
    "decay.rate_zone1":                  "5.0",
    "decay.rate_zone2":                  "15.0",
    "decay.rate_zone3":                  "30.0",
    "afk.budget_minutes":                "15",
    "afk.thread_check_interval_minutes": "45",
    "host.income_multiplier":            "2.0",
    "host.rolling_window":               "10",
    "host.min_duration_minutes":         "45",
    "host.cooldown_hours":               "12",
    "host.min_voters":                   "5",
    "host.outlier_trim_threshold":       "8",
    "host.voter_min_age_days":           "7",
    "host.elite_rank_count":             "3",
    "host.apex_ping":                    "@here",
    "host.tier_definitions":             _HOST_TIER_DEFINITIONS,
    "vote.score_positive":               "5",
    "vote.score_neutral":                "3",
    "vote.score_negative":               "1",
    "vote.window_minutes":               "10",
    "shop.blackmarket_slot_count":       "3",
    "shop.blackmarket_refresh_day":      "0",
    "economy.bounty_min_amount":         "50",
    "economy.bounty_max_amount":         "null",
    "economy.betting_tax_percent":       "10.0",
    "owner.backup_interval_hours":       "24",
    "owner.backup_auto_enabled":         "true",
    "owner.backup_full":                 "false",
}

PRESET_CASUAL: dict[str, str] = {
    **PRESET_BALANCED,
    "ec.join_bonus":                     "20",
    "ec.completion_bonus":               "15",
    "ec.base_max_bonus":                 "70",
    "ec.t_cap":                          "150",
    "ec.duration_tiers":                 _DURATION_TIERS_CASUAL,
    "decay.grace_days":                  "14",
    "decay.zone1_days":                  "14",
    "decay.zone2_days":                  "14",
    "decay.rate_zone1":                  "2.0",
    "decay.rate_zone2":                  "7.0",
    "decay.rate_zone3":                  "15.0",
    "host.min_duration_minutes":         "30",
    "host.cooldown_hours":               "6",
    "host.min_voters":                   "3",
    "vote.window_minutes":               "15",
}

PRESET_COMPETITIVE: dict[str, str] = {
    **PRESET_BALANCED,
    "ec.join_bonus":                     "10",
    "ec.completion_bonus":               "8",
    "ec.base_max_bonus":                 "40",
    "ec.t_cap":                          "90",
    "ec.duration_tiers":                 _DURATION_TIERS_COMPETITIVE,
    "decay.grace_days":                  "3",
    "decay.zone1_days":                  "4",
    "decay.zone2_days":                  "4",
    "decay.rate_zone1":                  "10.0",
    "decay.rate_zone2":                  "25.0",
    "decay.rate_zone3":                  "50.0",
    "host.min_duration_minutes":         "60",
    "host.cooldown_hours":               "24",
    "host.min_voters":                   "7",
    "vote.window_minutes":               "7",
}

PRESETS: dict[str, dict[str, str]] = {
    "balanced":    PRESET_BALANCED,
    "casual":      PRESET_CASUAL,
    "competitive": PRESET_COMPETITIVE,
}