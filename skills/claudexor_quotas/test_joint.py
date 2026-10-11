"""0.10.0: what the limits allow together (quota_summary.build_together).

Every fixture here is synthetic: invented accounts, limits and readings at the
fixed synthetic NOW of the other reserve tests. Two layers are checked apart:

- eligibility — per account and model choice, every limit that applies at
  once (a 5-hour, a weekly and a model's own limit), unknown scope or reading
  never read as positive;
- the quantity — account-hours of each account repeating its own recent mix,
  every rate over one common stretch, and its conditional futures with only
  reported events.

The A/B numbers are the agreed oracle (load 3,1,0,1,10,12 at t0,1,2,3,4,6;
no new use 3,3,4,13,15 at t0,2,3,4,6).
"""
import json

import pytest

import plugin
import quota_history
import quota_summary as qs
from test_reserve import FIVE_H, NOW, WEEK, constraint, payload, profile, snap

H = 3600.0
DAY = 86400


def _state(data, now=NOW):
    return qs.compute(data, qs.HistoryView(state="empty"), now)


def _pace(state, rates, now=NOW, span=H):
    """Give measured accounts the recent use ``rates[(subject, meaning)]`` per
    hour over one stretch ending now — the shape _rate_and_points produces
    (time, used ratio). A missing entry leaves the account's pace unknown."""
    for calc in state.groups:
        for member in calc.members:
            if member.status != "measured":
                continue
            rate = rates.get((member.subject_id, calc.meaning))
            if rate is None:
                continue
            used = 1.0 - member.remaining
            before = max(0.0, used - rate * span / H)
            member.rate = {"state": "ok", "windows_per_hour": rate, "span_seconds": round(span),
                           "observations": 2, "zero_growth": used - before <= qs.RATIO_EPS,
                           "change": round(used - before, 6), "from": now - span, "to": now}
            member.rate_points = [(now - span, before), (now, used)]
    return state


def _family(state, harness="claude", kind="text", named=True):
    found = [f for f in qs.build_together(state, kind, named) if f["harness"] == harness]
    assert len(found) == 1, found
    return found[0]


def _option(family, key="other"):
    return next(o for o in family["options"] if o["key"] == key)


def _account(family, sid, harness="claude"):
    return next(a for a in family["account_states"] if a["account"] == f"{harness}:{sid}")


def ab_payload():
    """The agreed A/B example: every limit shared and applicable."""
    snaps = [
        snap("claude", "A", [constraint("five_hour", .80, FIVE_H, reset=2 * H),
                             constraint("seven_day", .95, WEEK, reset=4 * H)], observed=0),
        snap("claude", "B", [constraint("five_hour", .60, FIVE_H, reset=3 * H),
                             constraint("seven_day", .70, WEEK, reset=6 * H)], observed=0),
    ]
    return payload(snaps, [profile("claude", "A"), profile("claude", "B")], harnesses=("claude",))


AB_RATES = {("A", "five_hour"): .1, ("A", "seven_day"): .05,
            ("B", "five_hour"): .2, ("B", "seven_day"): .1}


def _line_at(line, t, right=True):
    """A published hours line at NOW + t hours: the last vertex at or before
    it (right), or the one before a jump there (left)."""
    value = None
    for stamp, v in line:
        if stamp < NOW + t * H - 1e-3 or (right and abs(stamp - (NOW + t * H)) <= 1e-3):
            value = v
        elif not right and abs(stamp - (NOW + t * H)) <= 1e-3 and value is None:
            value = v
    return value


def _interp(line, t):
    """The hours line read as drawn: straight between vertices."""
    at = NOW + t * H
    for (t0, v0), (t1, v1) in zip(line, line[1:]):
        if t0 <= at <= t1 and t1 > t0:
            return v0 + (v1 - v0) * (at - t0) / (t1 - t0)
    return line[-1][1]


def test_ab_recent_load_matches_the_agreed_oracle():
    family = _family(_pace(_state(ab_payload()), AB_RATES))
    assert _account(family, "A")["workload"]["hours"] == pytest.approx(1.0)
    assert _account(family, "B")["workload"]["hours"] == pytest.approx(2.0)
    assert family["workload"]["hours"] == pytest.approx(3.0)
    sc = family["scenarios"]["recent_load"]
    after = {round((e["at"] - NOW) / H, 6): e["hours_after"] for e in sc["events"]}
    # H_A = 1,0,0,0,10,8 and H_B = 2,1,0,1,0,4 at t0,1,2,3,4,6
    assert after[1.0] == pytest.approx(1.0)   # A spent its week; B 1 h left
    assert after[2.0] == pytest.approx(0.0)   # A's 5-hour reset unlocks nothing; B spent its 5-hour
    assert after[3.0] == pytest.approx(1.0)   # B's 5-hour reset: one hour before its week binds
    assert after[4.0] == pytest.approx(10.0)  # A's weekly reset: 10 h; B spent its week
    assert after[6.0] == pytest.approx(12.0)  # A 8 h + B 4 h
    assert sc["hours_line"][0] == [round(NOW), 3.0]
    assert _interp(sc["hours_line"], 0.5) == pytest.approx(2.0)  # both burn an hour per hour
    # What changed when, said by kind and limit, never by account.
    first = sc["events"][0]
    assert first["changes"] == [{"kind": "exhausted", "limit": "claude|seven_day|604800|-", "accounts": 1}]
    assert first["counts_after"] == {"other": 1}
    at2 = next(e for e in sc["events"] if abs(e["at"] - (NOW + 2 * H)) < 1e-3)
    kinds = {(c["kind"], c.get("limit")) for c in at2["changes"]}
    assert kinds == {("exhausted", "claude|five_hour|18000|-"), ("reset", "claude|five_hour|18000|-")}
    assert at2["counts_after"] == {"other": 0}
    # Just before and after each event: A's 5-hour refill at t2 frees nothing.
    assert at2["hours_before"] == pytest.approx(0.0) and at2["hours_after"] == pytest.approx(0.0)
    at4 = next(e for e in sc["events"] if abs(e["at"] - (NOW + 4 * H)) < 1e-3)
    assert (at4["hours_before"], at4["hours_after"]) == (pytest.approx(0.0), pytest.approx(10.0))


def test_ab_without_new_use_matches_the_agreed_oracle():
    sc = _family(_pace(_state(ab_payload()), AB_RATES))["scenarios"]["no_new_use"]
    after = {round((e["at"] - NOW) / H, 6): e["hours_after"] for e in sc["events"]}
    assert sc["hours_line"][0][1] == pytest.approx(3.0)
    assert [after[t] for t in (2.0, 3.0, 4.0, 6.0)] == pytest.approx([3.0, 4.0, 13.0, 15.0])
    assert all(e["counts_after"] == {"other": 2} for e in sc["events"])
    # Only reported resets: each limit refills once; nothing after the last.
    assert [c["kind"] for e in sc["events"] for c in e["changes"]] == ["reset"] * 4
    assert sc["hours_line"][-1] == [round(NOW + 7 * DAY), 15.0]


def test_crossed_limits_leave_no_account_free_despite_one_full_window_per_limit():
    data = payload([
        snap("claude", "A", [constraint("five_hour", 0.0, FIVE_H, reset=H),
                             constraint("seven_day", 1.0, WEEK, reset=3 * DAY)]),
        snap("claude", "B", [constraint("five_hour", 1.0, FIVE_H, reset=2 * H),
                             constraint("seven_day", 0.0, WEEK, reset=4 * DAY)]),
    ], [profile("claude", "A"), profile("claude", "B")], harnesses=("claude",))
    state = _state(data)
    summary = qs.build_summary(data, state.view, NOW, state=state)
    # Each limit alone shows one full account-window: min of the pool sums is 1.
    assert sorted(g["measured"]["windows"] for g in summary["groups"]) == [1.0, 1.0]
    option = _option(_family(state))
    assert option["counts"]["no_known_block"] == 0
    assert option["counts"]["blocked"] == 2
    assert option["blocked_by"] == {"claude|five_hour|18000|-": 1, "claude|seven_day|604800|-": 1}
    # Each unlocks only when its own spent limit resets.
    assert option["next_unlock"] == {"at": qs.iso(NOW + 2 * H), "accounts": 1}


def test_a_refill_of_one_spent_limit_does_not_unlock_while_another_is_spent():
    data = payload([snap("claude", "A", [constraint("five_hour", 1.0, DAY, reset=12 * H),
                                         constraint("seven_day", 1.0, WEEK, reset=18 * H)])],
                   [profile("claude", "A")], harnesses=("claude",))
    family = _family(_state(data))
    option = _option(family)
    assert option["counts"]["blocked"] == 1
    assert option["next_unlock"] == {"at": qs.iso(NOW + 18 * H), "accounts": 1}
    verdict = _account(family, "A")["options"]["other"]
    assert verdict["unlock_at"] == qs.iso(NOW + 18 * H)
    assert {b["limit"] for b in verdict["blockers"]} == {"claude|five_hour|86400|-", "claude|seven_day|604800|-"}
    counts = family["scenarios"]["no_new_use"]["counts"]["other"]
    assert counts == [[round(NOW), 0], [round(NOW + 18 * H), 1], [round(NOW + 7 * DAY), 1]]
    events = family["scenarios"]["no_new_use"]["events"]
    assert [round((e["at"] - NOW) / H) for e in events] == [12, 18]
    assert events[0]["counts_after"] == {"other": 0}  # 12 h: one refill, still held


def _scoped_payload(fable_used=1.0, exhaustion=None, cooldown_models=None):
    fable = ["claude-fable-5", "fable"]
    row = snap("claude", "A", [
        constraint("five_hour", .30, FIVE_H, reset=2 * H),
        constraint("seven_day", .50, WEEK, reset=3 * DAY),
        constraint("seven_day_fable", fable_used, WEEK, reset=2 * DAY, models=fable, label="7 day (Fable)"),
    ])
    if exhaustion is not None:
        row["availability"]["model_scoped_exhaustions"] = [exhaustion]
    if cooldown_models:
        row["constraints"].append({"id": "opus_cooldown", "used_ratio": None, "window_seconds": None,
                                   "cooldown_until": qs.iso(NOW + 900), "applies_to_models": cooldown_models})
    return payload([row], [profile("claude", "A")], harnesses=("claude",))


def test_a_spent_model_limit_holds_that_model_only():
    family = _family(_state(_scoped_payload(cooldown_models=["claude-opus-5"])))
    # The shared limits come first — the widget's default — then every full
    # name the scoped limits and holds carry, each its own choice.
    assert [o["models"] for o in family["options"]] == [[], ["claude-fable-5"], ["fable"], ["claude-opus-5"]]
    fable_key = "claude|seven_day_fable|604800|" + qs._scope_hash(("claude-fable-5", "fable"))
    for name in ("claude-fable-5", "fable"):
        fable = next(o for o in family["options"] if o["models"] == [name])
        assert fable["counts"]["blocked"] == 1 and fable["blocked_by"] == {fable_key: 1}
        assert fable_key in fable["windows"]
    opus = next(o for o in family["options"] if o["models"] == ["claude-opus-5"])
    assert opus["counts"]["blocked"] == 1 and opus["blocked_by"] == {"model_hold": 1}
    assert fable_key in opus["not_applicable"]
    other = _option(family)
    assert other["counts"]["no_known_block"] == 1
    assert other["named_elsewhere"] is True
    assert other["not_applicable"] == [fable_key]


def test_a_live_reported_model_limit_holds_its_models_until_its_reset():
    scope = ["claude-fable-5", "fable"]
    family = _family(_state(_scoped_payload(
        fable_used=.4, exhaustion={"constraint_id": "seven_day_fable", "applies_to_models": scope,
                                   "resets_at": qs.iso(NOW + 5 * H)})))
    fable = next(o for o in family["options"] if o["models"] == ["fable"])
    assert fable["counts"]["blocked"] == 1
    assert fable["next_unlock"]["at"] == qs.iso(NOW + 5 * H)
    assert _option(family)["counts"]["no_known_block"] == 1
    events = family["scenarios"]["no_new_use"]["events"]
    assert [round((e["at"] - NOW) / H) for e in events[:2]] == [2, 5]  # the 5-hour reset comes first
    assert events[0]["counts_after"][fable["key"]] == 0  # a refill elsewhere does not lift the hold
    assert events[1]["changes"] == [{"kind": "model_hold_end", "scope": qs._scope_hash(tuple(scope)), "accounts": 1}]
    assert events[1]["counts_after"][fable["key"]] == 1


def test_selected_model_free_while_the_recent_mix_also_used_a_spent_model_limit():
    """Parent counterexample: the prior load used M and N; N's own limit is
    spent. Choosing M, the account has no known restriction — but its recent
    mix cannot be repeated: 0 account-hours, bound by N, never M-hours."""
    m_models, n_models = ["model-m"], ["model-n"]
    data = payload([snap("codex", "A", [
        constraint("primary", .50, FIVE_H, reset=3 * H),
        constraint("weekly_m", .40, WEEK, reset=3 * DAY, models=m_models),
        constraint("weekly_n", 1.0, WEEK, reset=2 * DAY, models=n_models),
    ])], [profile("codex", "A")], harnesses=("codex",))
    state = _pace(_state(data), {("A", "primary"): .10, ("A", "weekly_m"): .02, ("A", "weekly_n"): .05})
    family = _family(state, "codex")
    m = next(o for o in family["options"] if o["models"] == m_models)
    n = next(o for o in family["options"] if o["models"] == n_models)
    assert m["counts"]["no_known_block"] == 1
    assert n["counts"]["blocked"] == 1
    workload = _account(family, "A", "codex")["workload"]
    assert workload["state"] == "blocked" and workload["hours"] == 0.0
    assert workload["binding"].startswith("codex|weekly_n|") and workload["stopped_by"] == workload["binding"]
    assert family["workload"]["hours"] == 0.0 and family["workload"]["accounts_blocked"] == 1
    # The quantity does not depend on the model chosen.
    assert "options" not in family["workload"]


def test_an_extra_bucket_of_unknown_scope_is_never_positive_proof():
    extra = lambda used: constraint("base_model_inference:primary", used, WEEK, reset=2 * DAY,
                                    label="reserve primary")
    data = payload([
        snap("codex", "held", [constraint("codex:primary", 1.0, WEEK, reset=DAY), extra(0.0)]),
        snap("codex", "spent_extra", [constraint("codex:primary", .2, WEEK, reset=DAY), extra(1.0)]),
        snap("codex", "stale_extra", [constraint("codex:primary", .2, WEEK, reset=DAY)]),
        snap("codex", "stale_extra", [extra(.5)], fresh=False, source="other"),
        snap("codex", "positive_extra", [constraint("codex:primary", .2, WEEK, reset=DAY), extra(.3)]),
        snap("codex", "plain", [constraint("codex:primary", .2, WEEK, reset=DAY)]),
    ], [profile("codex", sid) for sid in ("held", "spent_extra", "stale_extra", "positive_extra", "plain")],
        harnesses=("codex",))
    state = _state(data)
    summary = qs.build_summary(data, state.view, NOW, state=state)
    bases = {g["meaning"]: g["applicability_basis"] for g in summary["groups"]}
    assert bases == {"primary": "own", "base_model_inference:primary": "foreign_namespace"}
    family = _family(state, "codex")
    option = _option(family)
    assert option["unknown_windows"] == ["codex|base_model_inference:primary|604800|-"]
    words = {a["account"].split(":")[1]: a["options"]["other"] for a in family["account_states"]}
    extra_key = "codex|base_model_inference:primary|604800|-"
    assert words["held"]["state"] == "blocked"  # a positive extra bucket frees nothing
    assert words["held"]["reasons"] == [{"why": "unknown_scope", "limit": extra_key, "reading": "has_quota"}]
    assert words["spent_extra"]["state"] == "incomplete"
    assert words["spent_extra"]["reasons"] == [{"why": "unknown_scope", "limit": extra_key, "reading": "spent"}]
    assert words["stale_extra"]["state"] == "incomplete"
    assert words["stale_extra"]["reasons"] == [{"why": "unknown_scope", "limit": extra_key,
                                                "reading": "not_current"}]
    assert words["positive_extra"]["state"] == "incomplete"  # whatever it reads
    assert words["plain"]["state"] == "no_known_block"  # carries no such limit
    assert option["counts"] == {"not_running": 0, "blocked": 1, "incomplete": 3, "no_known_block": 1}


@pytest.mark.parametrize("kinds, scope, text_state, image_state", [
    (None, "universal", "blocked", "blocked"),           # absent: legacy, every kind
    (["image"], "listed", "no_known_block", "blocked"),  # image-only never gates text
    (["text", "image"], "listed", "blocked", "blocked"),
    ("image", "unknown", "incomplete", "incomplete"),     # malformed: unknown, never universal
    ([], "unknown", "incomplete", "incomplete"),
    ([1], "unknown", "incomplete", "incomplete"),
])
def test_request_kinds_gate_only_the_kinds_they_name(kinds, scope, text_state, image_state):
    images = constraint("images_daily", 1.0, DAY, reset=6 * H)
    if kinds is not None:
        images["applies_to_request_kinds"] = kinds
    data = payload([snap("codex", "A", [constraint("codex:primary", .2, WEEK, reset=DAY), images])],
                   [profile("codex", "A")], harnesses=("codex",))
    state = _state(data)
    group = next(g for g in qs.build_summary(data, state.view, NOW, state=state)["groups"]
                 if g["meaning"] == "images_daily")
    # The field never moves a limit's key: its history stays its own.
    assert group["key"] == "codex|images_daily|86400|-"
    assert group["request_scope"] == scope
    assert _account(_family(state, "codex"), "A", "codex")["options"]["other"]["state"] == text_state
    assert _account(_family(state, "codex", kind="image"), "A", "codex")["options"]["other"]["state"] == image_state


def test_stale_missing_reset_and_missing_limit_readings_are_incomplete_or_open_ended():
    data = payload([
        snap("claude", "stale", [constraint("five_hour", .2, FIVE_H, reset=H)]),
        snap("claude", "stale", [constraint("seven_day", .5, WEEK, reset=DAY)], fresh=False, source="old"),
        snap("claude", "no_reset", [constraint("five_hour", 1.0, FIVE_H, reset=None),
                                    constraint("seven_day", .5, WEEK, reset=DAY)]),
        snap("claude", "no_week", [constraint("five_hour", .2, FIVE_H, reset=H)]),
    ], [profile("claude", sid) for sid in ("stale", "no_reset", "no_week")], harnesses=("claude",))
    family = _family(_state(data))
    words = {a["account"].split(":")[1]: a["options"]["other"] for a in family["account_states"]}
    assert words["stale"]["state"] == "incomplete"
    assert words["stale"]["reasons"] == [{"why": "no_current_reading", "limit": "claude|seven_day|604800|-"}]
    assert words["no_reset"]["state"] == "blocked" and words["no_reset"]["unlock_at"] is None
    assert words["no_reset"]["blockers"] == [{"kind": "limit", "until": None, "limit": "claude|five_hour|18000|-"}]
    assert words["no_week"]["state"] == "incomplete"
    assert words["no_week"]["reasons"] == [{"why": "no_reading_of_limit", "limit": "claude|seven_day|604800|-"}]
    option = _option(family)
    assert option["unlock_unknown"] == 1 and option["next_unlock"] is None
    # A held account with no reported end stays held in every future.
    assert family["scenarios"]["no_new_use"]["counts"]["other"][-1][1] == 0


def test_accounts_that_cannot_run_come_first_and_a_cached_roster_is_incomplete():
    rows = [snap("claude", sid, [constraint("five_hour", used, FIVE_H, reset=H)])
            for sid, used in (("off", 1.0), ("out", .2), ("failed", .2), ("ok", .2))]
    data = payload(rows, [profile("claude", "off", enabled=False), profile("claude", "out", verification=""),
                          profile("claude", "failed", verification="failed"), profile("claude", "ok")],
                   harnesses=("claude",))
    data["profiles"]["profiles"][1]["status"]["availability"] = "unavailable"
    data["profiles"]["profiles"][2]["status"]["availability"] = "unavailable"
    family = _family(_state(data))
    words = {a["account"].split(":")[1]: a["options"]["other"] for a in family["account_states"]}
    # Switched off and spent: not running wins; a quota reset repairs no login.
    assert words["off"]["state"] == "not_running" and words["off"]["unlock_at"] is None
    assert words["off"]["blockers"]  # the spent limit is still said
    assert words["out"]["state"] == "not_running" and words["failed"]["state"] == "not_running"
    assert words["ok"]["state"] == "no_known_block"
    assert family["workload"]["accounts_considered"] == 1
    cached = qs.compute(data, qs.HistoryView(state="empty"), NOW)
    cached.norm.cached["accounts"] = NOW - 300
    words = {a["account"].split(":")[1]: a["options"]["other"]
             for a in _family(cached)["account_states"]}
    assert all(w["state"] == "incomplete" for k, w in words.items() if k != "off")
    assert words["ok"]["reasons"] == [{"why": "account_state_unknown"}]


def test_zero_missing_and_unaligned_rates_leave_the_account_out_of_the_hours():
    data = payload([
        snap("claude", sid, [constraint("five_hour", .5, FIVE_H, reset=2 * H),
                             constraint("seven_day", .5, WEEK, reset=DAY)])
        for sid in ("ok", "zero", "partial", "apart")
    ], [profile("claude", sid) for sid in ("ok", "zero", "partial", "apart")], harnesses=("claude",))
    state = _pace(_state(data), {("ok", "five_hour"): .1, ("ok", "seven_day"): .05,
                                 ("zero", "five_hour"): .1, ("zero", "seven_day"): 0.0,
                                 ("partial", "five_hour"): .1,
                                 ("apart", "five_hour"): .1, ("apart", "seven_day"): .05})
    apart = next(m for c in state.groups if c.meaning == "seven_day" for m in c.members if m.subject_id == "apart")
    apart.rate_points = [(NOW - 3 * H, .3), (NOW - 2.9 * H, .5)]  # no common stretch with its 5-hour
    family = _family(state)
    work = {a["account"].split(":")[1]: a["workload"] for a in family["account_states"]}
    assert work["ok"]["state"] == "ok" and work["ok"]["hours"] == pytest.approx(5.0)
    assert work["zero"] == dict(work["zero"], state="not_calibrated", reason="no_change_observed",
                                limits=["claude|seven_day|604800|-"])
    assert work["partial"]["reason"] == "pace_unknown"
    assert work["apart"]["reason"] == "no_common_interval"
    assert family["workload"]["hours"] == pytest.approx(5.0)
    assert family["workload"]["accounts_calibrated"] == 1
    assert family["workload"]["accounts_considered"] == 4
    assert family["workload"]["not_calibrated"] == {"no_change_observed": 1, "no_common_interval": 1,
                                                    "pace_unknown": 1}


def test_with_no_account_measured_the_hours_are_unknown_never_zero():
    family = _family(_state(ab_payload()))
    assert family["workload"]["hours"] is None
    assert family["scenarios"]["recent_load"]["hours_line"] is None
    assert all(e["hours_after"] is None for e in family["scenarios"]["no_new_use"]["events"])
    # Eligibility is still there.
    assert _option(family)["counts"]["no_known_block"] == 2


def test_a_cooldown_end_is_an_event_and_refills_nothing():
    row = snap("claude", "A", [constraint("five_hour", .5, FIVE_H, reset=4 * H),
                               constraint("seven_day", .5, WEEK, reset=2 * DAY)])
    row["constraints"].append({"id": "cooldown", "used_ratio": None, "window_seconds": None,
                               "cooldown_until": qs.iso(NOW + H)})
    data = payload([row], [profile("claude", "A")], harnesses=("claude",))
    family = _family(_pace(_state(data), {("A", "five_hour"): .1, ("A", "seven_day"): .05}))
    verdict = _account(family, "A")["options"]["other"]
    assert verdict["state"] == "blocked" and verdict["blockers"] == [{"kind": "cooldown", "until": qs.iso(NOW + H)}]
    assert _account(family, "A")["workload"] == dict(_account(family, "A")["workload"], state="blocked", hours=0.0,
                                                    stopped_by="cooldown")
    sc = family["scenarios"]["no_new_use"]
    assert sc["events"][0]["changes"] == [{"kind": "cooldown_end", "accounts": 1}]
    assert sc["events"][0]["hours_after"] == pytest.approx(5.0)  # 5-hour .5 left at .1/h; nothing refilled
    load = family["scenarios"]["recent_load"]
    assert _interp(load["hours_line"], 0.5) == pytest.approx(0.0)  # nothing is used while it cools
    assert load["events"][0]["hours_after"] == pytest.approx(5.0)


def test_simultaneous_events_apply_together_and_then_everything_is_judged():
    data = payload([snap("claude", "A", [constraint("five_hour", 1.0, FIVE_H, reset=2 * H),
                                         constraint("seven_day", 1.0, WEEK, reset=2 * H)])],
                   [profile("claude", "A")], harnesses=("claude",))
    family = _family(_state(data))
    events = family["scenarios"]["no_new_use"]["events"]
    assert len(events) == 1
    assert sorted(c["limit"] for c in events[0]["changes"]) == ["claude|five_hour|18000|-",
                                                               "claude|seven_day|604800|-"]
    assert events[0]["counts_after"] == {"other": 1}


def test_the_reset_schedule_keeps_current_and_last_known_apart():
    data = payload([
        snap("claude", "a", [constraint("seven_day", .4, WEEK, reset=DAY)]),
        snap("claude", "b", [constraint("seven_day", .6, WEEK, reset=DAY + 60)]),
        snap("claude", "c", [constraint("seven_day", .1, WEEK, reset=2 * DAY)], fresh=False),
        snap("claude", "d", [constraint("seven_day", .2, WEEK, reset=None)]),
    ], [profile("claude", sid) for sid in "abcd"], harnesses=("claude",))
    state = _state(data)
    group = qs.build_summary(data, state.view, NOW, state=state)["groups"][0]
    assert group["reset_schedule"] == {
        "current": [{"at": qs.iso(NOW + DAY), "last": qs.iso(NOW + DAY + 60), "accounts": 2}],
        "last_known": [{"at": qs.iso(NOW + 2 * DAY), "last": qs.iso(NOW + 2 * DAY), "accounts": 1, "passed": False}],
        "current_without_reset": 1,
    }
    # The legacy field keeps its meaning: current readings only.
    assert group["next_reset"] == {"at": qs.iso(NOW + DAY), "accounts": 2}


def _sweep_history(store, rows, start=-5400, step=120):
    """Collector sweeps of the host's passive answer. ``rows``: subject ->
    [(meaning, window, reset offset, [(obs offset, used)...])]."""
    t = NOW + start
    while t <= NOW + 1e-6:
        snaps = []
        for sid, limits in rows.items():
            constraints = []
            observed = None
            for meaning, window, reset, points in limits:
                seen = [p for p in points if NOW + p[0] <= t + 1e-9]
                if not seen:
                    continue
                observed = max(observed or -1e18, NOW + seen[-1][0])
                constraints.append(constraint(meaning, seen[-1][1], window, reset=reset))
            if constraints:
                row = snap("claude", sid, constraints, observed_abs=observed)
                snaps.append(row)
        data = payload(snaps, [profile("claude", sid) for sid in rows], harnesses=("claude",))
        plugin.persist_sweep(store, data, "", t)
        t += step
    return data


def _ramp(end_used, per_hour, start=-5400, step=None):
    """Whole-percent readings rising ``per_hour`` and reaching ``end_used`` at
    NOW, observed each time they change (one percentage point)."""
    every = 3600.0 * 0.01 / per_hour
    points = []
    k = 0
    while True:
        offset = -k * every
        if offset < start:
            break
        points.append((offset, round(end_used - k * 0.01, 2)))
        k += 1
    return sorted(points)


def test_the_ab_example_from_recorded_history_and_the_registered_route(tmp_path):
    store = quota_history.HistoryStore(tmp_path)
    data = _sweep_history(store, {
        "A": [("five_hour", FIVE_H, 2 * H, _ramp(.80, .1)), ("seven_day", WEEK, 4 * H, _ramp(.95, .05))],
        "B": [("five_hour", FIVE_H, 3 * H, _ramp(.60, .2)), ("seven_day", WEEK, 6 * H, _ramp(.70, .1))],
    })
    out = plugin.reserve_view(store, data, NOW, NOW, chart=False, name_accounts=True)
    family = out["summary"]["together"][0]
    work = {a["account"]: a["workload"] for a in family["account_states"]}
    assert work["claude:A"]["state"] == "ok" and work["claude:A"]["hours"] == pytest.approx(1.0, abs=.02)
    assert work["claude:B"]["hours"] == pytest.approx(2.0, abs=.02)
    assert work["claude:A"]["interval"]["span_seconds"] >= qs.MIN_RATE_SPAN_SEC
    after = {round((e["at"] - NOW) / H): e["hours_after"]
             for e in family["scenarios"]["recent_load"]["events"]}
    assert [after[t] for t in (1, 2, 3, 4, 6)] == pytest.approx([1, 0, 1, 10, 12], abs=.15)
    # The tool reads the same calculation, without any account's identity.
    tool = qs.compact(out["summary"])
    text = json.dumps(tool)
    assert "account_states" not in text and "claude:A" not in text
    assert tool["together"][0]["workload"]["hours"] == family["workload"]["hours"]


def test_the_tool_answers_together_per_model_and_request_kind(tmp_path, monkeypatch):
    data = _scoped_payload()
    monkeypatch.setattr(plugin, "history_store", lambda api: quota_history.HistoryStore(tmp_path))
    host = type("Api", (), {"get_state_dir": lambda self: str(tmp_path),
                            "get_runtime_info": lambda self: {"server_port": 1}})()
    latest = plugin.LatestRead()
    answer = plugin.tool_answer(host, latest, fetch=lambda: (data, ""), now=NOW, model="fable")
    together = answer["together"][0]
    assert [o["models"] for o in together["options"]] == [["fable"]]
    assert together["options"][0]["counts"]["blocked"] == 1
    assert "held_by" in together["options"][0] and "headline" in together["options"][0]
    assert {row["model"] for row in together["named_models"]} == {"claude-fable-5"}
    latest = plugin.LatestRead()
    shared = plugin.tool_answer(host, latest, fetch=lambda: (data, ""), now=NOW)
    assert [o["key"] for o in shared["together"][0]["options"]] == ["other"]
    assert [row["model"] for row in shared["together"][0]["named_models"]] == ["claude-fable-5", "fable"]
    latest = plugin.LatestRead()
    plain = plugin.tool_answer(host, latest, fetch=lambda: (data, ""), now=NOW, model="claude-opus-9")
    assert [o["key"] for o in plain["together"][0]["options"]] == ["other"]
    assert "named by no model-scoped limit" in plain["together"][0]["model_note"]
    latest = plugin.LatestRead()
    image = plugin.tool_answer(host, latest, fetch=lambda: (data, ""), now=NOW, request_kind="image")
    assert image["together"][0]["request_kind"] == "image"
    assert "account_states" not in json.dumps(answer)


def test_the_tool_schema_keeps_its_old_parameters_and_adds_two_optional_ones():
    props = plugin.TOOL_SCHEMA["properties"]
    assert set(props) == {"harness", "detail", "model", "request_kind"}
    assert plugin.TOOL_SCHEMA.get("required") in (None, [])


# ---------------------------------------------------------------------------
# The quantity needs the whole set of limits that may bind the mix, as the
# account's eligibility knows it — never only the limits it happens to read.


def test_a_family_limit_the_account_has_no_reading_of_leaves_it_out_of_the_hours():
    """Parent negative (a): A reads only its 5-hour limit; B's weekly limit
    makes the weekly limit the family's. A's eligibility is incomplete, and so
    are its hours: its 5-hour pace alone is not what its mix can run."""
    data = payload([
        snap("claude", "a", [constraint("five_hour", .5, FIVE_H, reset=3 * H)]),
        snap("claude", "b", [constraint("five_hour", .5, FIVE_H, reset=3 * H),
                             constraint("seven_day", .5, WEEK, reset=DAY)]),
    ], [profile("claude", "a"), profile("claude", "b")], harnesses=("claude",))
    state = _pace(_state(data), {("a", "five_hour"): .1, ("b", "five_hour"): .1, ("b", "seven_day"): .05})
    family = _family(state)
    a = _account(family, "a")
    assert a["options"]["other"]["state"] == "incomplete"
    assert a["workload"] == {"state": "incomplete", "reason": "no_reading_of_limit",
                             "limits": ["claude|seven_day|604800|-"]}
    assert _account(family, "b")["workload"]["hours"] == pytest.approx(5.0)
    assert family["workload"]["hours"] == pytest.approx(5.0)  # B alone, never 10
    assert (family["workload"]["accounts_calibrated"], family["workload"]["accounts_considered"]) == (1, 2)
    assert family["workload"]["not_calibrated"] == {"no_reading_of_limit": 1}
    # Neither future spends or counts A's hours.
    load = family["scenarios"]["recent_load"]
    assert load["hours_line"][0][1] == pytest.approx(5.0) and load["held"] == 1


def test_a_model_limit_the_account_has_no_reading_of_leaves_it_out_of_the_hours():
    """A model's own limit applies to the models it names: the account's
    unattributed mix may have used them, so with no reading of it the hours
    are unknown — while the shared limits alone still judge other models."""
    m_limit = constraint("weekly_m", .4, WEEK, reset=3 * DAY, models=["model-m"])
    data = payload([
        snap("codex", "a", [constraint("primary", .5, FIVE_H, reset=3 * H)]),
        snap("codex", "b", [constraint("primary", .5, FIVE_H, reset=3 * H), m_limit]),
    ], [profile("codex", "a"), profile("codex", "b")], harnesses=("codex",))
    state = _pace(_state(data), {("a", "primary"): .1, ("b", "primary"): .1, ("b", "weekly_m"): .05})
    family = _family(state, "codex")
    a = _account(family, "a", "codex")
    assert a["options"]["other"]["state"] == "no_known_block"
    assert a["workload"]["state"] == "incomplete" and a["workload"]["reason"] == "no_reading_of_limit"
    assert a["workload"]["limits"][0].startswith("codex|weekly_m|")
    assert family["workload"]["accounts_calibrated"] == 1


def test_a_carried_limit_of_unknown_scope_leaves_the_account_out_of_the_hours():
    """Parent negative (b): primary and an extra bucket both at .5 left and .1
    an hour. What the bucket applies to is unknown, so its growth proves no
    share of the mix: incomplete, never 5 account-hours."""
    data = payload([snap("codex", "a", [constraint("codex:primary", .5, FIVE_H, reset=3 * H),
                                        constraint("base_model_inference:primary", .5, WEEK, reset=DAY)])],
                   [profile("codex", "a")], harnesses=("codex",))
    state = _pace(_state(data), {("a", "primary"): .1, ("a", "base_model_inference:primary"): .1})
    family = _family(state, "codex")
    a = _account(family, "a", "codex")
    assert a["options"]["other"]["state"] == "incomplete"
    assert a["workload"] == {"state": "incomplete", "reason": "unknown_scope",
                             "limits": ["codex|base_model_inference:primary|604800|-"]}
    assert family["workload"]["hours"] is None and family["workload"]["accounts_considered"] == 1
    assert family["scenarios"]["recent_load"]["hours_line"] is None


def test_limits_paced_from_different_sources_have_no_common_stretch():
    """Two sources round and observe apart: a 5-hour pace from one and a
    weekly pace from another are not one stretch of the account's mix."""
    data = payload([
        snap("claude", "a", [constraint("five_hour", .5, FIVE_H, reset=3 * H)], source="one"),
        snap("claude", "a", [constraint("seven_day", .5, WEEK, reset=DAY)], source="two"),
    ], [profile("claude", "a")], harnesses=("claude",))
    state = _pace(_state(data), {("a", "five_hour"): .1, ("a", "seven_day"): .05})
    sources = {calc.meaning: calc.members[0].reading.source for calc in state.groups}
    assert sources == {"five_hour": "one", "seven_day": "two"}
    family = _family(state)
    work = _account(family, "a")["workload"]
    assert work["state"] == "not_calibrated" and work["reason"] == "sources_differ"
    assert _account(family, "a")["options"]["other"]["state"] == "no_known_block"  # eligibility stays
    assert family["workload"]["hours"] is None


def test_different_sources_through_recorded_history_and_the_registered_route(tmp_path):
    """The same through the collector's own records: each limit's pace is
    real (one source each), but the two are not one stretch."""
    store = quota_history.HistoryStore(tmp_path)
    five, week = _ramp(.80, .1), _ramp(.95, .05)
    t = NOW - 5400
    while t <= NOW + 1e-6:
        rows = []
        for meaning, window, reset, points, source in (("five_hour", FIVE_H, 2 * H, five, "one"),
                                                       ("seven_day", WEEK, 4 * H, week, "two")):
            seen = [p for p in points if NOW + p[0] <= t + 1e-9]
            if seen:
                rows.append(snap("claude", "A", [constraint(meaning, seen[-1][1], window, reset=reset)],
                                 source=source, observed_abs=NOW + seen[-1][0]))
        data = payload(rows, [profile("claude", "A")], harnesses=("claude",))
        plugin.persist_sweep(store, data, "", t)
        t += 120
    out = plugin.reserve_view(store, data, NOW, NOW, chart=False, name_accounts=True)
    groups = {g["meaning"]: g for g in out["summary"]["groups"]}
    assert all(g["recent_pace"]["state"] == "ok" for g in groups.values())
    family = out["summary"]["together"][0]
    assert family["account_states"][0]["workload"]["reason"] == "sources_differ"
    assert family["workload"]["not_calibrated"] == {"sources_differ": 1}


def _held(models, until=2 * H):
    return {"id": "hold_" + "_".join(models), "used_ratio": None, "window_seconds": None,
            "cooldown_until": qs.iso(NOW + until), "applies_to_models": models}


def _hold_world(hold_models, mn_used=.4, rates=None):
    """A codex account: its own primary, a weekly limit of M alone and one of
    M and N together, and one model cooldown on ``hold_models``."""
    row = snap("codex", "a", [constraint("primary", .5, FIVE_H, reset=3 * H),
                              constraint("weekly_m", .4, WEEK, reset=3 * DAY, models=["model-m"]),
                              constraint("weekly_mn", mn_used, WEEK, reset=3 * DAY,
                                         models=["model-m", "model-n"])])
    if hold_models:
        row["constraints"].append(_held(hold_models))
    data = payload([row], [profile("codex", "a")], harnesses=("codex",))
    rates = rates or {("a", "primary"): .1, ("a", "weekly_m"): .05, ("a", "weekly_mn"): .05}
    family = _family(_pace(_state(data), rates), "codex")
    return family, _account(family, "a", "codex")["workload"]


@pytest.mark.parametrize("hold, mn_used, rates, want", [
    # No hold: the mix runs; 5-hour primary binds (.5 / .1 = 5 h).
    (None, .4, None, ("ok", 5.0)),
    # A hold naming exactly a consumed limit's models stops the mix.
    (["model-m"], .4, None, ("blocked", 0.0)),
    # A hold covering every model of a consumed limit, and more, stops it.
    (["model-m", "model-n", "model-x"], .4, None, ("blocked", 0.0)),
    # The mix used [M, N]'s limit; a hold on N alone may or may not hold
    # part of it: not attributed, never free and never zero.
    (["model-n"], .4, None, ("not_calibrated", None)),
    # [M, N]'s limit was spent all through (not consumed), M's own was
    # consumed: a hold naming exactly [M, N] still covers M, which the mix
    # used — the whole-list identity of the old match missed it.
    (["model-m", "model-n"], 1.0,
     {("a", "primary"): .1, ("a", "weekly_m"): .05, ("a", "weekly_mn"): 0.0}, ("blocked", 0.0)),
    # A hold on a model of no measured limit: the mix may have used it.
    (["model-z"], .4, None, ("not_calibrated", None)),
])
def test_a_model_hold_joins_the_mix_by_the_model_names_it_shares(hold, mn_used, rates, want):
    family, work = _hold_world(hold, mn_used, rates)
    assert work["state"] == want[0], work
    assert work.get("hours") == (None if want[1] is None else pytest.approx(want[1])), work
    if want[0] == "blocked":
        assert work["stopped_by"] == "model_hold"
        # The hold ends at 2 h; the mix runs again then, and is judged again.
        events = family["scenarios"]["no_new_use"]["events"]
        end = next(e for e in events if abs(e["at"] - (NOW + 2 * H)) < 1e-3)
        assert end["changes"][0]["kind"] == "model_hold_end"
        assert (end["hours_before"], end["hours_after"]) == (pytest.approx(0.0), pytest.approx(5.0))
    if want[0] == "not_calibrated":
        assert work["reason"] == "model_hold_unmeasured"
        assert family["workload"]["hours"] is None


def test_a_hold_only_on_models_the_mix_could_not_use_leaves_it_running():
    """[M, N]'s limit spent all through: requests of M or N could not run, so
    the mix did not use them; a hold on N alone takes nothing from it."""
    row = snap("codex", "a", [constraint("primary", .5, FIVE_H, reset=3 * H),
                              constraint("weekly_mn", 1.0, WEEK, reset=3 * DAY, models=["model-m", "model-n"])])
    row["constraints"].append(_held(["model-n"]))
    data = payload([row], [profile("codex", "a")], harnesses=("codex",))
    family = _family(_pace(_state(data), {("a", "primary"): .1, ("a", "weekly_mn"): 0.0}), "codex")
    work = _account(family, "a", "codex")["workload"]
    assert work["state"] == "ok" and work["hours"] == pytest.approx(5.0)


def test_calibrated_accounts_have_every_limit_their_eligibility_needs():
    """The invariant behind the four negatives: an account with hours has no
    model choice left incomplete for want of a limit's reading or scope."""
    extra = constraint("base_model_inference:primary", .5, WEEK, reset=DAY)
    data = payload([
        snap("codex", "full", [constraint("primary", .5, FIVE_H, reset=3 * H),
                               constraint("weekly_m", .4, WEEK, reset=3 * DAY, models=["model-m"])]),
        snap("codex", "no_m", [constraint("primary", .5, FIVE_H, reset=3 * H)]),
        snap("codex", "extra", [constraint("primary", .5, FIVE_H, reset=3 * H),
                                constraint("weekly_m", .4, WEEK, reset=3 * DAY, models=["model-m"]), extra]),
        snap("codex", "stale", [constraint("primary", .5, FIVE_H, reset=3 * H)]),
        snap("codex", "stale", [constraint("weekly_m", .4, WEEK, reset=3 * DAY, models=["model-m"])],
             fresh=False, source="old"),
    ], [profile("codex", sid) for sid in ("full", "no_m", "extra", "stale")], harnesses=("codex",))
    rates = {(sid, meaning): .1 for sid in ("full", "no_m", "extra", "stale")
             for meaning in ("primary", "weekly_m", "base_model_inference:primary")}
    family = _family(_pace(_state(data), rates), "codex")
    limit_reasons = {"no_reading_of_limit", "unknown_scope", "no_current_reading"}
    work = {}
    for acct in family["account_states"]:
        sid = acct["account"].split(":")[1]
        work[sid] = acct["workload"].get("reason") or acct["workload"]["state"]
        if acct["workload"]["state"] in ("ok", "blocked"):
            for verdict in acct["options"].values():
                assert not {r["why"] for r in verdict["reasons"]} & limit_reasons, (sid, verdict)
    assert work == {"full": "ok", "no_m": "no_reading_of_limit", "extra": "unknown_scope",
                    "stale": "no_current_reading"}


def test_the_reset_schedule_is_whole_past_two_dozen_distinct_resets():
    """Parent gap: the schedule kept only its first 24 moments, silently.
    Every reported reset is reachable: one row per distinct moment, current
    and last-known apart, never more rows than the limit has accounts."""
    current = [snap("claude", f"c{i:02d}", [constraint("seven_day", .5, WEEK, reset=H + i * 600)])
               for i in range(30)]
    dated = [snap("claude", f"k{i:02d}", [constraint("seven_day", .4, WEEK, reset=DAY + i * 600)], fresh=False)
             for i in range(27)]
    data = payload(current + dated, [profile("claude", s["subject"]["subject_id"]) for s in current + dated],
                   harnesses=("claude",))
    state = _state(data)
    group = qs.build_summary(data, state.view, NOW, state=state)["groups"][0]
    sched = group["reset_schedule"]
    assert [e["at"] for e in sched["current"]] == [qs.iso(NOW + H + i * 600) for i in range(30)]
    assert [e["at"] for e in sched["last_known"]] == [qs.iso(NOW + DAY + i * 600) for i in range(27)]
    assert sum(e["accounts"] for e in sched["current"]) == 30
    assert sum(e["accounts"] for e in sched["last_known"]) == 27
    assert group["next_reset"] == {"at": qs.iso(NOW + H), "accounts": 1}


# ---------------------------------------------------------------------------
# Release-review repairs. Each test fails on the reviewed candidate.


def _count_at(steps, at):
    """A published free-count step line read at ``at``: its last step there."""
    value = None
    for stamp, count in steps:
        if stamp <= at + 1e-6:
            value = count
    return value


def _assert_events_agree_with_the_lines(family):
    """Every event says what the lines it summarizes say at its moment: the
    free count after it, and the hours just before and after it."""
    for mode, sc in family["scenarios"].items():
        for event in sc["events"]:
            for key, count in event["counts_after"].items():
                assert count == _count_at(sc["counts"][key], event["at"]), (mode, event)
            if sc["hours_line"]:
                at = [v for stamp, v in sc["hours_line"] if abs(stamp - event["at"]) <= 1e-6]
                assert at and event["hours_after"] == at[-1], (mode, event, at)
                assert event["hours_before"] == at[0] or abs(event["at"] - NOW) <= 1e-6, (mode, event, at)


def test_an_exhaustion_at_a_fraction_of_a_second_is_judged_at_its_own_moment():
    """Review finding: events were grouped at round(t, 3) and judged there,
    before an exhaustion between: 0.20 left at 0.07 an hour runs out at
    +10285.714285… s, and the account was still counted free after it."""
    data = payload([snap("claude", "A", [constraint("five_hour", .80, FIVE_H, reset=5 * H),
                                         constraint("seven_day", .50, WEEK, reset=3 * DAY)])],
                   [profile("claude", "A")], harnesses=("claude",))
    family = _family(_pace(_state(data), {("A", "five_hour"): .07, ("A", "seven_day"): .01}))
    sc = family["scenarios"]["recent_load"]
    out = next(e for e in sc["events"] if e["changes"][0]["kind"] == "exhausted")
    assert out["at"] == pytest.approx(NOW + .2 / .07 * H, abs=1e-6)
    assert out["counts_after"] == {"other": 0} and out["hours_after"] == pytest.approx(0.0)
    _assert_events_agree_with_the_lines(family)
    tool = qs.compact({"groups": [], "together": [family]})["together"][0]["events_at_recent_load"]
    assert tool[0]["no_known_restriction_after"] == {"other": 0}


def test_a_fractional_reported_reset_keeps_its_refill_and_the_tool_lists_it():
    """A reset reported at a fraction of a second (…530569Z): A is freed, B's
    hours jump 5 -> 10 there; rounded, the jump was lost and the tool counted
    the refill as changing nothing."""
    reset = NOW + 2 * H + 0.530569
    rows = []
    for sid, used in (("A", 1.0), ("B", .5)):
        five = constraint("five_hour", used, FIVE_H)
        five["resets_at"] = qs.iso_exact(reset)
        rows.append(snap("claude", sid, [five, constraint("seven_day", .5, WEEK, reset=3 * DAY)]))
    data = payload(rows, [profile("claude", "A"), profile("claude", "B")], harnesses=("claude",))
    family = _family(_pace(_state(data), {("B", "five_hour"): .1, ("B", "seven_day"): .02}))
    first = family["scenarios"]["no_new_use"]["events"][0]
    assert first["at"] == pytest.approx(reset, abs=1e-6)
    assert (first["hours_before"], first["hours_after"]) == (pytest.approx(5.0), pytest.approx(10.0))
    assert first["counts_after"] == {"other": 2}
    _assert_events_agree_with_the_lines(family)
    # B alone: only the hours move; the tool still lists that refill.
    alone = payload([rows[1]], [profile("claude", "B")], harnesses=("claude",))
    family = _family(_pace(_state(alone), {("B", "five_hour"): .1, ("B", "seven_day"): .02}))
    rows_out = qs.compact({"groups": [], "together": [family]})["together"][0]["events_if_no_new_use"]
    assert rows_out[0]["at"] == qs.iso(reset) and rows_out[0]["account_hours_after"] == pytest.approx(10.0)


def test_nearby_distinct_events_stay_apart_each_judged_at_its_moment():
    """0.3 ms apart, two accounts' resets are two events (rounded, they
    merged and were judged before both: 0 free after)."""
    rows = []
    for sid, frac in (("A", 0.0001), ("B", 0.0004)):
        five = constraint("five_hour", 1.0, FIVE_H)
        five["resets_at"] = qs.iso_exact(NOW + 2 * H + frac)
        rows.append(snap("claude", sid, [five, constraint("seven_day", .5, WEEK, reset=3 * DAY)]))
    data = payload(rows, [profile("claude", "A"), profile("claude", "B")], harnesses=("claude",))
    family = _family(_state(data))
    events = family["scenarios"]["no_new_use"]["events"]
    assert [round(e["at"] - NOW - 2 * H, 4) for e in events[:2]] == [0.0001, 0.0004]
    assert [e["counts_after"] for e in events[:2]] == [{"other": 1}, {"other": 2}]
    assert [e["changes"] for e in events[:2]] == [[{"kind": "reset", "accounts": 1,
                                                    "limit": "claude|five_hour|18000|-"}]] * 2
    _assert_events_agree_with_the_lines(family)


def test_recorded_fractional_readings_through_the_registered_route(tmp_path):
    """Persisted by the collector: readings rising .90 -> .97 over the last
    hour. The route's own pace runs the limit out at +1542.857 s; the event
    there and the count line agree."""
    store = quota_history.HistoryStore(tmp_path)
    t = NOW - 3600
    while t <= NOW + 1e-6:
        used = .90 + .07 * (t - (NOW - 3600)) / 3600
        data = payload([snap("claude", "A", [constraint("five_hour", round(used, 6), FIVE_H, reset=4 * H)],
                             observed_abs=t)], [profile("claude", "A")], harnesses=("claude",))
        plugin.persist_sweep(store, data, "", t)
        t += 120
    family = plugin.reserve_view(store, data, NOW, NOW, chart=False, name_accounts=True)["summary"]["together"][0]
    sc = family["scenarios"]["recent_load"]
    out = [e for e in sc["events"] if e["changes"][0]["kind"] == "exhausted"]
    assert out and out[0]["at"] - NOW == pytest.approx(1542.857, abs=0.01)
    assert out[0]["counts_after"] == {"other": 0}
    _assert_events_agree_with_the_lines(family)


def test_the_events_of_the_agreed_worlds_agree_with_their_lines():
    for data, rates in ((ab_payload(), AB_RATES), (ab_payload(), {}),
                        (_scoped_payload(cooldown_models=["claude-opus-5"]), {})):
        _assert_events_agree_with_the_lines(_family(_pace(_state(data), rates)))


def _many_scoped_names(count=26):
    names = [f"model-{i:02d}" for i in range(count)]
    return payload([snap("codex", "A", [constraint("primary", .2, WEEK, reset=DAY),
                                        constraint("scoped", 1.0, WEEK, reset=2 * DAY, models=names)])],
                   [profile("codex", "A")], harnesses=("codex",))


def test_a_model_asked_for_past_the_bound_on_choices_is_its_own_choice(tmp_path, monkeypatch):
    """Review finding: the choices were cut at MAX_OPTIONS before the tool
    looked for the model asked for: model-25, named by a spent scoped limit,
    was answered as named by none and free on the shared limits."""
    data = _many_scoped_names()
    family = _family(_state(data), "codex", named=False)
    assert len(family["options"]) == qs.MAX_OPTIONS and family["options_omitted"] == 2  # the bound stays
    monkeypatch.setattr(plugin, "history_store", lambda api: quota_history.HistoryStore(tmp_path))
    host = type("Api", (), {"get_state_dir": lambda self: str(tmp_path),
                            "get_runtime_info": lambda self: {"server_port": 1}})()
    answer = plugin.tool_answer(host, plugin.LatestRead(), fetch=lambda: (data, ""), now=NOW, model="model-25")
    together = answer["together"][0]
    assert [o["models"] for o in together["options"]] == [["model-25"]]
    assert together["options"][0]["counts"]["blocked"] == 1
    assert "model_note" not in together and together["options_omitted"] == 1
    # One more choice computed, no more: the rest stay out of the answer.
    asked = qs.build_together(_state(data), "text", False, "model-25")[0]
    assert [o["models"] for o in asked["options"]][-1] == ["model-25"]
    assert len(asked["options"]) == qs.MAX_OPTIONS + 1 and asked["options_omitted"] == 1
    # A name no limit carries still falls back to the shared limits, said.
    plain = plugin.tool_answer(host, plugin.LatestRead(), fetch=lambda: (data, ""), now=NOW, model="model-99")
    assert [o["key"] for o in plain["together"][0]["options"]] == ["other"]
    assert "named by no model-scoped limit" in plain["together"][0]["model_note"]


def test_many_accounts_and_choices_stay_cheap_to_compute():
    """Review finding: every option's free counts were computed inside each
    option's loop (about 40 s at 100 accounts x 25 choices). Each moment's
    counts are now computed once; a generous bound catches the old shape."""
    import time
    names = [f"m{i:02d}" for i in range(24)]
    rows, rates = [], {}
    for i in range(100):
        sid = f"a{i:03d}"
        rows.append(snap("claude", sid, [
            constraint("five_hour", .3 + (i % 5) * .1, FIVE_H, reset=(1 + i % 4) * H + i * 7),
            constraint("seven_day", .4 + (i % 3) * .1, WEEK, reset=(2 + i % 6) * DAY + i * 11),
            constraint("seven_day_m", .2 + (i % 4) * .1, WEEK, reset=(1 + i % 5) * DAY + i * 13, models=names)]))
        rates.update({(sid, "five_hour"): .05 + (i % 7) * .01, (sid, "seven_day"): .01 + (i % 3) * .005,
                      (sid, "seven_day_m"): .01 + (i % 5) * .003})
    data = payload(rows, [profile("claude", r["subject"]["subject_id"]) for r in rows], harnesses=("claude",))
    state = _pace(_state(data), rates)
    started = time.perf_counter()
    family = _family(state, named=False)
    elapsed = time.perf_counter() - started
    assert len(family["options"]) == qs.MAX_OPTIONS
    assert all(len(sc["events"]) == qs.MAX_TOGETHER_EVENTS for sc in family["scenarios"].values())
    assert elapsed < 5.0, elapsed


def test_the_prior_mix_is_complete_or_not_whatever_request_kind_is_judged():
    """Review finding: B reports an image-only limit A has no reading of.
    Judging text gave A five account-hours; judging image left the same mix
    incomplete. The mix is what was observed: incomplete either way, while
    A's text eligibility stays free and B's fully measured mix is counted."""
    images = constraint("images_daily", .3, DAY, reset=6 * H)
    images["applies_to_request_kinds"] = ["image"]
    data = payload([
        snap("codex", "A", [constraint("primary", .5, FIVE_H, reset=3 * H)]),
        snap("codex", "B", [constraint("primary", .5, FIVE_H, reset=3 * H), images]),
    ], [profile("codex", "A"), profile("codex", "B")], harnesses=("codex",))
    rates = {("A", "primary"): .1, ("B", "primary"): .1, ("B", "images_daily"): .05}
    seen = {}
    for kind in ("text", "image"):
        family = _family(_pace(_state(data), rates), "codex", kind=kind)
        seen[kind] = (_account(family, "A", "codex")["workload"], _account(family, "B", "codex")["workload"],
                      family["workload"])
    assert seen["text"] == seen["image"]
    a, b, total = seen["text"]
    assert a == {"state": "incomplete", "reason": "no_reading_of_limit", "limits": ["codex|images_daily|86400|-"]}
    assert b["state"] == "ok" and b["hours"] == pytest.approx(5.0)
    assert total["hours"] == pytest.approx(5.0) and total["accounts_calibrated"] == 1
    text = _family(_pace(_state(data), rates), "codex", kind="text")
    assert _account(text, "A", "codex")["options"]["other"]["state"] == "no_known_block"


def test_events_past_the_answer_are_said_in_the_tool_with_every_reset_on_detail():
    """Review finding: past the 48 events a future publishes, the tool said
    nothing. It now says how many, and detail carries each limit's whole
    reset schedule."""
    rows = [snap("claude", f"a{i:02d}", [constraint("five_hour", 1.0, FIVE_H, reset=H + i * 600),
                                         constraint("seven_day", .5, WEEK, reset=3 * DAY)]) for i in range(60)]
    data = payload(rows, [profile("claude", r["subject"]["subject_id"]) for r in rows], harnesses=("claude",))
    state = _state(data)
    summary = qs.build_summary(data, state.view, NOW, state=state)
    sc = summary["together"][0]["scenarios"]["no_new_use"]
    assert len(sc["events"]) == qs.MAX_TOGETHER_EVENTS and sc["events_omitted"] == 13
    tool = qs.compact(summary)
    tail = tool["together"][0]["events_if_no_new_use"][-1]
    assert tail == {"later_events_not_listed": 13, "every_reported_reset": "groups[].reset_schedule, with detail"}
    assert "reset_schedule" not in tool["groups"][0]
    detail = qs.compact(summary, detail=True)
    five = next(g for g in detail["groups"] if g["key"] == "claude|five_hour|18000|-")
    assert len(five["reset_schedule"]["current"]) == 60


def test_only_model_scoped_limits_keep_the_shared_default_and_never_prove_it():
    """Review finding: with only a model's own limit reported, the shared
    choice was dropped and the first model chosen in its place. The shared
    limits stay the default; nothing read shows them, so never free."""
    data = payload([snap("codex", "A", [constraint("weekly_m", 1.0, WEEK, reset=2 * DAY, models=["model-m"])]),
                    snap("codex", "off", [constraint("weekly_m", .2, WEEK, reset=2 * DAY, models=["model-m"])])],
                   [profile("codex", "A"), profile("codex", "off", enabled=False)], harnesses=("codex",))
    family = _family(_state(data), "codex")
    assert [o["key"] for o in family["options"]][0] == "other"
    shared, model = family["options"]
    assert shared["named_elsewhere"] is True and shared["windows"] == []
    assert shared["counts"] == {"not_running": 1, "blocked": 0, "incomplete": 1, "no_known_block": 0}
    assert shared["incomplete_by"] == {"no_limit_known_to_apply": 1}
    assert _account(family, "A", "codex")["options"]["other"]["reasons"] == [{"why": "no_limit_known_to_apply"}]
    assert model["models"] == ["model-m"] and model["counts"]["blocked"] == 1
    tool = qs.compact({"groups": [], "together": [family]})["together"][0]
    assert [o["key"] for o in tool["options"]] == ["other"]
    assert "0 of 2 accounts with no known restriction" in tool["options"][0]["headline"]
    assert [row["model"] for row in tool["named_models"]] == ["model-m"]
    # The same for an account that carries none of a family's extra buckets.
    extra = payload([snap("codex", "x", [constraint("base_model_inference:primary", .2, WEEK, reset=DAY)]),
                     snap("codex", "y", [])],
                    [profile("codex", "x"), profile("codex", "y")], harnesses=("codex",))
    words = {a["account"]: a["options"]["other"]["state"] for a in _family(_state(extra), "codex")["account_states"]}
    assert words == {"codex:x": "incomplete", "codex:y": "incomplete"}


@pytest.mark.parametrize("kinds, text_state, image_state", [
    (["text"], "incomplete", "no_known_block"),    # never every model for text
    (["image"], "no_known_block", "incomplete"),   # spent or not, image is unknown; text not gated
    (["text", "image"], "incomplete", "incomplete"),
])
def test_a_foreign_bucket_listing_request_kinds_keeps_unknown_model_scope(kinds, text_state, image_state):
    """Review finding: listing request kinds made an extra bucket the
    family's own (every model). Kinds say which requests it may bind, never
    which models: a kind it names is unknown, one it omits is not gated."""
    extra = constraint("base_model_inference:primary", 1.0 if kinds == ["image"] else .5, WEEK, reset=DAY)
    extra["applies_to_request_kinds"] = kinds
    data = payload([snap("codex", "A", [constraint("codex:primary", .5, FIVE_H, reset=3 * H), extra])],
                   [profile("codex", "A")], harnesses=("codex",))
    state = _pace(_state(data), {("A", "primary"): .1, ("A", "base_model_inference:primary"): .1})
    bases = {g["meaning"]: g["applicability_basis"]
             for g in qs.build_summary(data, state.view, NOW, state=state)["groups"]}
    assert bases["base_model_inference:primary"] == "foreign_namespace"
    for kind, want in (("text", text_state), ("image", image_state)):
        acct = _account(_family(state, "codex", kind=kind), "A", "codex")
        assert acct["options"]["other"]["state"] == want, (kind, acct["options"]["other"])
        # Its share of the recent mix is unknown whatever kind is judged.
        assert acct["workload"] == {"state": "incomplete", "reason": "unknown_scope",
                                    "limits": ["codex|base_model_inference:primary|604800|-"]}
