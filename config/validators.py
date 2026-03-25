# =====
# MODULE: config/validators.py
# =====
# Architecture Overview:
# A registry of lambda boundary constraints. Before any value is written 
# to the 'bot_config' database table via the admin panels, it must pass 
# evaluating 'True' against its respective function here to prevent 
# mathematically impossible system states (e.g. negative point yields).
# =====

import json

def _validate_id_list(v: str) -> bool:
    try:
        ids = json.loads(v)
        if not isinstance(ids, list): return False
        return all(str(i).isdigit() for i in ids)
    except:
        return False

def _validate_duration_tiers(v: str) -> bool:
    try:
        tiers = json.loads(v)
        return (
            len(tiers) >= 1
            and all(0 < t.get("mult", 0) <= 10.0 for t in tiers)
            and all(t.get("max", 0) > 0 for t in tiers)
        )
    except Exception:
        return False

CONFIG_VALIDATORS: dict[str, callable] = {
    "system.state":                       lambda v: v in ("UNCONFIGURED", "ACTIVE", "PAUSED"),
    "system.guild_name":                  lambda v: len(v.strip()) > 0,
    "system.admin_role_id":               _validate_id_list,
    "system.mod_role_id":                 _validate_id_list,
    "system.admin_user_id":               _validate_id_list,
    "system.mod_user_id":                 _validate_id_list,
    "owner.backup_channel_id":            lambda v: v.isdigit(),
    "owner.backup_interval_hours":        lambda v: int(v) in (12, 24, 48, 168),
    "owner.backup_auto_enabled":          lambda v: v in ("true", "false"),
    "owner.backup_full":                  lambda v: v in ("true", "false"),
    "channel.gamenight_id":               lambda v: v.isdigit(),
    "channel.activity_id":                lambda v: v.isdigit(),
    "channel.vc_category_id":             lambda v: v.isdigit(),
    "ec.join_bonus":                      lambda v: int(v) >= 0,
    "ec.completion_bonus":                lambda v: int(v) >= 0,
    "ec.base_max_bonus":                  lambda v: int(v) >= 0,
    "ec.t_cap":                           lambda v: int(v) > 0,
    # CVE-2M-010: Added multiplier cap and positive max requirement
    "ec.duration_tiers":                  _validate_duration_tiers,
    "tier.definitions":                   lambda v: len(json.loads(v)) >= 2,
    "decay.grace_days":                   lambda v: int(v) >= 0,
    "decay.zone1_days":                   lambda v: int(v) >= 0,
    "decay.zone2_days":                   lambda v: int(v) >= 0,
    "decay.rate_zone1":                   lambda v: float(v) >= 0,
    "decay.rate_zone2":                   lambda v: float(v) >= 0,
    "decay.rate_zone3":                   lambda v: float(v) >= 0,
    "afk.budget_minutes":                 lambda v: int(v) >= 1,
    "afk.thread_check_interval_minutes":  lambda v: int(v) >= 1,
    "host.income_multiplier":             lambda v: float(v) > 0,
    "host.rolling_window":                lambda v: int(v) >= 1,
    "host.min_duration_minutes":          lambda v: int(v) >= 1,
    "host.cooldown_hours":                lambda v: int(v) >= 0,
    "host.min_voters":                    lambda v: int(v) >= 1,
    "host.outlier_trim_threshold":        lambda v: int(v) >= 1,
    "host.voter_min_age_days":            lambda v: int(v) >= 0,
    "host.elite_rank_count":              lambda v: int(v) >= 1,
    "host.apex_ping":                     lambda v: len(v.strip()) > 0,
    "host.tier_definitions":              lambda v: len(json.loads(v)) >= 1,
    "vote.score_positive":                lambda v: int(v) > 0,
    "vote.score_neutral":                 lambda v: int(v) >= 0,
    "vote.score_negative":                lambda v: int(v) >= 0,
    "vote.window_minutes":                lambda v: int(v) >= 1,
    "shop.blackmarket_slot_count":        lambda v: int(v) >= 1,
    "shop.blackmarket_refresh_day":       lambda v: 0 <= int(v) <= 6,
    "economy.bounty_min_amount":          lambda v: int(v) >= 0,
    "economy.bounty_max_amount":          lambda v: v == "null" or int(v) >= 0,
    "economy.betting_tax_percent":        lambda v: 0.0 <= float(v) <= 100.0,
}