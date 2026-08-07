"""自适应频率策略 — MON01-07: restart→ALERT, INV-007: P0不被稀释。"""
import time
from beidou_observability.monitoring.contracts import CheckSeverity, CheckStatus, FrequencyLevel, FrequencyState
LEVEL_INTERVALS={FrequencyLevel.ALERT:600,FrequencyLevel.NORMAL:1800,FrequencyLevel.STABLE:3600}
PROMOTION_STREAK_REQUIRED=3
def init_frequency_state(reason="init"):
    now=time.monotonic(); return FrequencyState(level=FrequencyLevel.ALERT,interval_seconds=600,last_check_at=now,next_due_at=now+600,last_level_change_at=now,last_reason=reason)
def restart_frequency_state(): return init_frequency_state("restart_or_recovery")
def is_promotion_clean(results, allowlisted_p2_warns=None):
    allowed=allowlisted_p2_warns or set()
    for s,sev in results:
        if sev in (CheckSeverity.P0,CheckSeverity.P1) and s!=CheckStatus.PASS: return False,f"{sev.value} {s.value}"
        if sev==CheckSeverity.P2 and s==CheckStatus.WARN and "P2_WARN" not in allowed: return False,"non-allowlisted P2"
    if any(s==CheckStatus.UNKNOWN and sev in (CheckSeverity.P0,CheckSeverity.P1) for s,sev in results): return False,"critical UNKNOWN"
    return True,""
def update_frequency(cur,results,*,self_heal_active=False,restart_detected=False,open_p0_incident=False,open_p1_incident=False,clock_reversal=False,allowlisted_p2_warns=None):
    now=time.monotonic(); reason=""
    if restart_detected: reason="restart"
    elif clock_reversal: reason="clock_reversal"
    elif self_heal_active: reason="self_heal"
    elif open_p0_incident: reason="open_p0"
    elif open_p1_incident: reason="open_p1"
    else:
        for s,sev in results:
            if sev in (CheckSeverity.P0,CheckSeverity.P1) and s in (CheckStatus.FAIL,CheckStatus.UNKNOWN): reason=f"{sev.value}_{s.value}"; break
    if reason: return FrequencyState(level=FrequencyLevel.ALERT,interval_seconds=600,last_check_at=now,next_due_at=now+600,last_level_change_at=now,last_reason=reason,policy_version=cur.policy_version)
    clean,cr=is_promotion_clean(results,allowlisted_p2_warns)
    if not clean:
        changed=cur.level!=FrequencyLevel.ALERT
        return FrequencyState(level=FrequencyLevel.ALERT,interval_seconds=600,last_check_at=now,next_due_at=now+600,last_level_change_at=now if changed else cur.last_level_change_at,last_reason=cr,policy_version=cur.policy_version)
    ns=cur.promotion_clean_streak+1
    if cur.level==FrequencyLevel.ALERT and ns>=PROMOTION_STREAK_REQUIRED:
        return FrequencyState(level=FrequencyLevel.NORMAL,interval_seconds=1800,promotion_clean_streak=0,last_check_at=now,next_due_at=now+1800,last_level_change_at=now,last_reason=f"promoted:{ns}",policy_version=cur.policy_version)
    if cur.level==FrequencyLevel.NORMAL and ns>=PROMOTION_STREAK_REQUIRED:
        return FrequencyState(level=FrequencyLevel.STABLE,interval_seconds=3600,promotion_clean_streak=0,stable_since=now,last_check_at=now,next_due_at=now+3600,last_level_change_at=now,last_reason=f"promoted:{ns}",policy_version=cur.policy_version)
    return FrequencyState(level=cur.level,interval_seconds=cur.interval_seconds,promotion_clean_streak=ns,stable_since=cur.stable_since,last_check_at=now,next_due_at=now+cur.interval_seconds,last_level_change_at=cur.last_level_change_at,last_reason=f"streak:{ns}",policy_version=cur.policy_version)
def is_due(state,tolerance=5.0): return time.monotonic()>=(state.next_due_at-tolerance)
