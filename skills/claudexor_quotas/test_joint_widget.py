"""0.10.0 in the real widget.js: the Together row, resets near every row,
"Other limits", the card where it was opened, and the one timeline as the
limits together.

The world is synthetic (invented accounts and readings) and built through the
skill's own route code — plugin.build_view and plugin.reserve_view over a real
history store — so the widget draws exactly what the route would send. Node
runs widget.js in the fake DOM of the other widget tests; where Playwright has
a browser, the same widget runs in a real opaque-origin frame.
"""
import html
import json
import os
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

import plugin
import quota_history
import quota_summary as qs
import test_quotas
from test_reserve import _node

H = 3600.0
DAY = 86400
WEEK = 604800
FIVE = 18000
FABLE = ["claude-fable-5", "fable"]
WIDGET = Path(__file__).with_name("widget.js").resolve()


def _cons(cid, used, window, reset, models=None, label=None):
    row = {"id": cid, "label": label or cid, "used_ratio": used, "window_seconds": window,
           "resets_at": qs.iso(reset) if reset else None}
    if models is not None:
        row["applies_to_models"] = models
    return row


def _snap(harness, sid, constraints, observed):
    return {"subject": {"harness": harness, "subject_id": sid, "plan_label": "max"}, "constraints": constraints,
            "availability": {"state": "available"}, "observed_at": qs.iso(observed), "freshness": "fresh",
            "source": "app"}


def _profile(harness, pid, enabled=True):
    return {"profile": {"harness_id": harness, "profile_id": pid, "enabled": enabled, "display_name": pid},
            "status": {"verification": "passed", "availability": "available", "verification_source": "vendor"},
            "identity": {"email": "", "plan": "max"}}


def world(folder, now, roster_drop=(), fable=FABLE):
    """Claude: six accounts, three held by their spent weekly limit while their
    5-hour limit shows plenty, a Fable-scoped weekly limit (``fable``: its
    model names). Codex: four accounts, one switched off, and an extra bucket
    of two accounts seen 29 h ago and not since. Every value invented."""
    claude = [  # sid, 5h used now, 5h use/h, week used now, week use/h, fable used, 5h reset, week reset
        ("c0", .30, .08, .50, .03, .40, now + 1.5 * H, now + 2 * DAY),
        ("c1", .20, .06, .60, .02, .50, now + 2.0 * H, now + 3 * DAY),
        ("c2", .10, 0.0, 1.0, 0.0, 1.0, now + 2.5 * H, now + 10 * H),
        ("c3", .15, 0.0, 1.0, 0.0, 1.0, now + 3.0 * H, now + 30 * H),
        ("c4", .25, 0.0, 1.0, 0.0, .9, now + 3.5 * H, now + 50 * H),
        ("c5", .40, .05, .70, .02, 1.0, now + 4.0 * H, now + 4 * DAY),
    ]
    codex = [("x0", .40, .02, now + 2 * DAY), ("x1", .60, .03, now + 3 * DAY),
             ("x2", .20, .01, now + 4 * DAY), ("x3", .50, .01, now + 5 * DAY)]
    store = quota_history.HistoryStore(Path(folder))

    def status(t, extra, quota_read="ok"):
        back = (now - t) / H
        snaps = []
        for sid, u5, r5, uw, rw, uf, t5, tw in claude:
            snaps.append(_snap("claude", sid, [
                _cons("five_hour", round(max(0, u5 - r5 * back), 2), FIVE, t5),
                _cons("seven_day", 1.0 if uw >= 1 else round(max(0, uw - rw * back), 2), WEEK, tw),
                # c0 also uses Fable; c1's Fable limit does not move (its share of
                # c1's mix is unknown: no account-hours for c1).
                _cons("seven_day_fable", 1.0 if uf >= 1 else round(max(0, uf - (.03 if sid == "c0" else 0) * back), 2),
                      WEEK, tw + H, models=fable, label="7 day (Fable)"),
            ], t - 30))
        for sid, uw, rw, tw in codex:
            constraints = [_cons("codex:primary", round(max(0, uw - rw * back), 2), WEEK, tw, label="codex primary")]
            if extra and sid in ("x0", "x1"):
                constraints.append(_cons("base_model_inference:primary", 0.0, WEEK, now + 5 * DAY,
                                         label="base_model_inference primary"))
            snaps.append(_snap("codex", sid, constraints, t - 40))
        snaps[-2]["constraints"].append({"id": "codex:reset_credits", "label": "1 reset credits", "used_ratio": None,
                                         "window_seconds": None, "resets_at": None})
        profiles = [_profile("claude", c[0]) for c in claude if c[0] not in roster_drop]
        profiles += [_profile("codex", x[0], enabled=x[0] != "x3") for x in codex]
        return {"reads": {"catalog": "not_read", "accounts": "ok", "quota": quota_read}, "view": "quota",
                "daemon": {}, "unified_accounts": True, "harnesses": [],
                "profiles": {"profiles": profiles, "harnessAccounts": []},
                "quota": snaps if quota_read == "ok" else [], "quota_absences": []}

    t = now - 30 * H
    while t <= now - 29 * H:
        plugin.persist_sweep(store, status(t, True), "", t)
        t += 120
    t = now - 5400
    while t <= now:
        plugin.persist_sweep(store, status(t, False), "", t)
        t += 120

    def view(harness, data):
        out = plugin.build_view(data, "", now)
        out["prefs"] = {}
        horizon = "7d"
        out["reserve"] = plugin.reserve_view(store, data, now, now, harness=harness, horizon=horizon,
                                             name_accounts=True, reads=plugin.facet_states(data))
        return out

    live = status(now, False)
    cold = status(now, False, quota_read="not_read")
    return {"claude": view("claude", live), "codex": view("codex", live), "cold_codex": view("codex", cold),
            "now": now}


@pytest.fixture(scope="module")
def views(tmp_path_factory):
    now = float(int(time.time()))
    base = world(tmp_path_factory.mktemp("world"), now)
    gone = world(tmp_path_factory.mktemp("gone"), now, roster_drop=("c0",))
    base["claude_without_c0"] = gone["claude"]
    return base


def test_the_world_says_what_the_widget_needs(views):
    claude = next(f for f in views["claude"]["reserve"]["summary"]["together"] if f["harness"] == "claude")
    other = next(o for o in claude["options"] if o["key"] == "other")
    assert other["counts"]["no_known_block"] == 3 and other["counts"]["blocked"] == 3
    assert claude["workload"]["accounts_calibrated"] == 2
    assert claude["workload"]["not_calibrated"] == {"no_change_observed": 1, "shared_limit_spent": 3}
    codex = views["codex"]["reserve"]["summary"]
    extra = next(g for g in codex["groups"] if "base_model_inference" in g["key"])
    assert extra["restored_from_history"] is True and codex["reads"]["quota"] == "ok"
    cold = views["cold_codex"]["reserve"]["summary"]
    assert cold["reads"]["quota"] == "not_read"
    assert all(g["restored_from_history"] for g in cold["groups"] if g["harness"] == "codex")


NODE_JOINT = r"""
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'no node with data-focus ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
const has = (n, c) => String(n.className || '').split(/\s+/).includes(c);
const rowOfKey = (env, key) => byFocus(env.root, 'limit:' + key).parentNode;
const siblingsAfter = (node) => node.parentNode.childNodes.slice(node.parentNode.childNodes.indexOf(node) + 1);
async function poll(env) { env.interval()(); await settle(); }

(async () => {
  const W = JSON.parse(process.env.JOINT_WORLD);
  const route = (key) => (url) => (/harness=codex/.test(url) ? W.codex : W[key]);
  const claude = W.claude.reserve.summary;
  const groups = claude.groups.filter((g) => g.harness === 'claude');
  const fam = claude.together.find((f) => f.harness === 'claude');
  const weekly = groups.find((g) => g.key === 'claude|seven_day|604800|-');
  const five = groups.find((g) => g.key === 'claude|five_hour|18000|-');

  // 1. The Together row: above the limits, the shared limits by default,
  // one square per account, the counts and the account-hours — and the one
  // timeline open under it on every mount, as the limits together: no chart
  // of one limit is asked for.
  let env = await boot(route('claude'));
  const jrow = classes(env.root, 'jrow')[0];
  assert.ok(jrow, 'a Together row');
  assert.ok(has(siblingsAfter(jrow)[0], 'tl-block'), 'the limits-together timeline opens under Together');
  assert.equal(byFocus(env.root, 'limit:joint').textContent, 'Hide chart');
  assert.equal(byFocus(env.root, 'limit:joint').getAttribute('aria-pressed'), 'true');
  assert.equal(classes(env.root, 'l-chart').filter((b) => has(b, 'on')).length, 0, 'no limit charted by default');
  assert.equal(classes(env.root, 'tl-block').length, 1);
  assert.ok(!has(classes(env.root, 'tl-block')[0], 'reveal'), 'a mount never animates');
  assert.match(env.calls[0].url, /chart=0/);
  const rows = jrow.parentNode.childNodes;
  assert.ok(rows.indexOf(jrow) < rows.indexOf(rowOfKey(env, five.key)), 'Together stands above the limits');
  assert.equal(classes(env.root, 'lrow').length, groups.length, 'one .lrow per limit, as before');
  const pick = byFocus(env.root, 'joint-option');
  assert.equal(pick.value, 'other');
  assert.match(jrow.textContent, /Together/);
  assert.match(classes(jrow, 'l-fig')[0].textContent, /^3 of 6 accounts$/);
  assert.match(classes(jrow, 'l-sub')[0].textContent, /^≈ [0-9.]+ account-h$/);
  assert.match(classes(jrow, 'l-tail')[0].textContent, /3 held — Weekly 3/);
  assert.match(classes(jrow, 'l-tail')[0].textContent, /earliest unlock in \d+h · \w{3} \d+ \w{3} \d\d:\d\d/);
  const cells = classes(jrow, 'jcell');
  assert.equal(cells.length, 6);
  assert.deepEqual(cells.map((c) => c.className.replace('jcell ', '').split(' ')[0]),
                   ['free', 'free', 'free', 'held', 'held', 'held']);
  assert.match(cells[3].getAttribute('aria-label'), /Held for Shared limits: Weekly at the limit until/);
  // The model choice: every full name, each its own; the choice survives a poll.
  const named = fam.options.filter((o) => o.key !== 'other');
  assert.deepEqual(named.map((o) => o.models[0]), ['claude-fable-5', 'fable']);
  pick.value = named[0].key;
  pick.listeners.change[0]({ stopPropagation() {} });
  assert.equal(byFocus(env.root, 'joint-option').value, named[0].key);
  assert.match(classes(classes(env.root, 'jrow')[0], 'l-fig')[0].textContent,
               new RegExp('^' + named[0].counts.no_known_block + ' of 6 accounts$'));
  await poll(env);
  assert.equal(byFocus(env.root, 'joint-option').value, named[0].key, 'the choice survives the poll');

  // 2. A reset near every row: how far off and the local date, opening its
  // whole schedule in place under the row, without the chart.
  env = await boot(route('claude'));
  const resetBtn = byFocus(env.root, 'sched:' + five.key);
  assert.match(resetBtn.textContent, /^next reset in \d+h · \w{3} \d+ \w{3} \d\d:\d\d.* ▸$/);
  assert.equal(resetBtn.getAttribute('aria-expanded'), 'false');
  click(env, 'limit:joint');                   // fold the timeline: the schedule does not need it
  assert.equal(classes(env.root, 'tl-block').length, 0);
  click(env, 'sched:' + five.key);
  let sched = classes(env.root, 'sched-block');
  assert.equal(sched.length, 1);
  assert.equal(siblingsAfter(rowOfKey(env, five.key))[0], sched[0], 'the schedule opens right under its row');
  assert.match(sched[0].textContent, /Reported resets · 5-hour/);
  assert.equal(classes(sched[0], 'sw').length, 6);
  assert.match(sched[0].textContent, /refills this limit alone/);
  await poll(env);
  assert.equal(classes(env.root, 'sched-block').length, 1, 'still open after a poll');

  // 3. One card, right under the row whose bar selected it, before that
  // row's timeline; from Together under Together; from the list under its row.
  env = await boot(route('claude'));
  click(env, 'limit:' + weekly.key);            // the weekly limit's own record, under its row
  await settle();
  assert.ok(has(siblingsAfter(rowOfKey(env, weekly.key))[0], 'tl-block'));
  assert.ok(has(siblingsAfter(rowOfKey(env, weekly.key))[0], 'reveal'), 'a timeline a click opens eases in');
  await poll(env);
  assert.ok(!has(siblingsAfter(rowOfKey(env, weekly.key))[0], 'reveal'), 'a poll never animates');
  const weeklyBars = walk(rowOfKey(env, weekly.key)).filter((n) => has(n, 'bar') && n.getAttribute('data-focus'));
  const pickBar = weeklyBars[weeklyBars.length - 1];
  const barKey = pickBar.getAttribute('data-focus');
  pickBar.listeners.click[0]({ stopPropagation() {} });
  let card = classes(env.root, 'inspector');
  assert.equal(card.length, 1);
  let after = siblingsAfter(rowOfKey(env, weekly.key));
  assert.equal(after[0], card[0], 'the card stands right under the row that selected it');
  assert.ok(has(after[1], 'tl-block'), 'and before that row\'s timeline');
  assert.ok(has(card[0], 'reveal'), 'a click eases it in');
  assert.match(classes(card[0], 'j-verdict')[0].textContent, /Held for Shared limits: Weekly at the limit until/);
  assert.match(classes(card[0], 'j-verdict')[0].textContent, /May run again .*once every hold above has ended/);
  await poll(env);
  card = classes(env.root, 'inspector');
  assert.equal(card.length, 1, 'the poll keeps the selection');
  assert.ok(!has(card[0], 'reveal'), 'a poll never animates');
  assert.equal(siblingsAfter(rowOfKey(env, weekly.key))[0], card[0], 'nor moves it');
  // Escape gives the keyboard back to the bar.
  env.document.listeners.keydown.forEach((fn) => fn({ key: 'Escape' }));
  assert.equal(classes(env.root, 'inspector').length, 0);
  assert.equal(env.document.activeElement.getAttribute('data-focus'), barKey);
  // From Together.
  const cell = classes(classes(env.root, 'jrow')[0], 'jcell')[0];
  cell.listeners.click[0]({ stopPropagation() {} });
  card = classes(env.root, 'inspector');
  assert.equal(siblingsAfter(classes(env.root, 'jrow')[0])[0], card[0]);
  assert.match(classes(card[0], 'j-verdict')[0].textContent, /No known restriction for Shared limits/);
  assert.match(classes(card[0], 'j-verdict')[0].textContent, /Its recent mix could run about [0-9.]+ h more before/);
  click(env, 'inspector-clear');
  // From the account list, under its row.
  click(env, 'accounts');
  click(env, 'acct:claude:c3');
  card = classes(env.root, 'inspector');
  assert.equal(card.length, 1);
  assert.equal(siblingsAfter(byFocus(env.root, 'acct:claude:c3'))[0], card[0], 'under the list row');
  assert.ok(has(card[0].parentNode, 'acc-table'));

  // 4. The account leaves the account list: its card closes with a note in
  // place, the keyboard moves to it, and Escape dismisses it.
  let calls = 0;
  env = await boot((url) => (++calls > 1 ? W.claude_without_c0 : W.claude));
  click(env, 'jcell:claude:c0');
  assert.equal(classes(env.root, 'inspector').length, 1);
  byFocus(env.root, 'inspector-clear').focus();
  await poll(env);
  assert.equal(classes(env.root, 'inspector').length, 0);
  const gone = classes(env.root, 'vanished');
  assert.equal(gone.length, 1);
  assert.match(gone[0].textContent, /c0 is no longer in the account list/);
  assert.equal(siblingsAfter(classes(env.root, 'jrow')[0])[0], gone[0], 'where its card stood');
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'vanished-ok');
  env.document.listeners.keydown.forEach((fn) => fn({ key: 'Escape' }));
  assert.equal(classes(env.root, 'vanished').length, 0);

  // 5. The one timeline as the limits together, under Together, open on a
  // mount; account-hours only where measured, the unit and its coverage in
  // one line; what changes when; no-effect refills said apart, in one line;
  // every assumption under Details.
  env = await boot(route('claude'));
  const jblock = siblingsAfter(classes(env.root, 'jrow')[0])[0];
  assert.ok(has(jblock, 'tl-block'));
  assert.equal(classes(env.root, 'tl-block').length, 1, 'one timeline');
  assert.equal(walk(env.root).filter((n) => n.getAttribute('class') === 'chart-svg').length, 1);
  assert.equal(classes(jblock, 'tl-scope')[0].textContent,
               'Account-hours · each account repeating its own recent mix, not hours of one model · '
               + fam.workload.accounts_calibrated + ' of 6 accounts measured');
  assert.equal(classes(jblock, 'chart-legend')[0].textContent,
               'no new use · limits refill and holds end at their reported times, once');
  assert.match(classes(jblock, 'chart-table')[0].textContent, /Futures: only each limit’s next reported reset/);
  assert.match(classes(jblock, 'chart-table')[0].textContent, /Free accounts in “What changes when” are counted for Shared limits/);
  const idleNote = classes(jblock, 'schedule-notes');
  assert.equal(idleNote.length, 1, 'refills that change nothing together are counted in one line');
  assert.match(idleNote[0].textContent,
               /^\+ (1 reported reset or hold end changes|\d+ reported resets or hold ends change) nothing together in this span · each row lists its resets$/);
  assert.match(jblock.textContent, /What changes when · no new use/);
  assert.match(jblock.textContent, /Weekly resets/);
  assert.equal(byFocus(env.root, 'limit:joint').textContent, 'Hide chart');
  assert.equal(classes(env.root, 'l-chart').filter((b) => has(b, 'on')).length, 0, 'no limit is charted meanwhile');
  assert.ok(env.calls.at(-1).url.includes('chart=0') || env.calls.length === 1);
  click(env, 'scenario:recent_pace');
  assert.match(classes(env.root, 'tl-block')[0].textContent, /What changes when · recent load/);
  assert.match(classes(env.root, 'tl-block')[0].textContent, /runs out/);
  // No account measured: no line and never a flat zero; the events stay.
  const flat = JSON.parse(JSON.stringify(W.claude));
  const ff = flat.reserve.summary.together.find((f) => f.harness === 'claude');
  ff.workload.hours = null; ff.workload.accounts_calibrated = 0;
  Object.values(ff.scenarios).forEach((sc) => { sc.hours_line = null; sc.events.forEach((e) => { e.hours_after = null; e.hours_before = null; }); });
  env = await boot(flat);
  assert.equal(walk(env.root).filter((n) => n.getAttribute('class') === 'chart-svg').length, 0);
  assert.match(classes(env.root, 'tl-block')[0].textContent, /No account-hours: the recent mix of none of 6 accounts could be measured/);
  assert.match(classes(classes(env.root, 'jrow')[0], 'l-sub')[0].textContent, /^hours not measured$/);
  assert.doesNotMatch(classes(env.root, 'jrow')[0].textContent, /≈ 0/);

  // 6. Codex: the extra bucket known only from the history folds into one
  // line with its own name, last read and last reported reset; it opens in
  // place and stays open on the poll.
  env = await boot(route('codex'));
  click(env, 'harness:codex');
  await settle();
  const toggle = byFocus(env.root, 'other-limits');
  assert.ok(toggle);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  const head = toggle.parentNode;
  assert.match(head.textContent, /Other limits · 1Weekly · base model inference · 2 accounts · not in this answer · last read \d+ h ago · last reported reset in \d+d · /);
  assert.match(head.textContent, /what it applies to is unknown/);
  const codexGroups = W.codex.reserve.summary.groups.filter((g) => g.harness === 'codex');
  const extra = codexGroups.find((g) => g.key.includes('base_model_inference'));
  assert.equal(byFocus(env.root, 'limit:' + extra.key), undefined, 'folded: no full row');
  assert.match(classes(env.root, 'jrow')[0].textContent, /2 incomplete · 1 not running · 1 limit of unknown scope/);
  click(env, 'other-limits');
  assert.ok(byFocus(env.root, 'limit:' + extra.key), 'opened: the full row');
  assert.match(rowOfKey(env, extra.key).textContent, /last reported reset in \d+d/);
  await poll(env);
  assert.ok(byFocus(env.root, 'limit:' + extra.key), 'still open after a poll');

  // ...but never when the quota facet was not read: every limit would be
  // "known only from the history" then, and the primary rows must stay.
  env = await boot(W.cold_codex);
  click(env, 'harness:codex');
  await settle();
  assert.equal(byFocus(env.root, 'other-limits'), undefined);
  W.cold_codex.reserve.summary.groups.filter((g) => g.harness === 'codex').forEach((g) => {
    assert.ok(byFocus(env.root, 'limit:' + g.key), 'row of ' + g.key);
  });

  // 7. A screen kept after a failed read claims nothing is free now.
  env = await boot(route('claude'));
  const kept = env.testHooks.keptView(W.claude);
  assert.equal(kept.reserve.summary.together.length, 0);
  env = await boot(kept);
  assert.equal(classes(env.root, 'jrow').length, 0);

  // 8. The span the limits-together timeline opens on: a day when every
  // reported event of the future on screen falls within one — the shape of
  // what changes, not a week of flat line after it — else a week. A limit's
  // own timeline keeps its own span; the reader's pick is kept.
  const nowS = Math.round(Date.parse(claude.generated_at) / 1000);
  assert.ok(!fam.scenarios.no_new_use.events.every((e) => e.at <= nowS + 86400), 'weekly resets days ahead here');
  env = await boot(route('claude'));
  assert.equal(byFocus(env.root, 'horizon:7d').getAttribute('aria-pressed'), 'true');
  const day = JSON.parse(JSON.stringify(W.claude));
  const dfam = day.reserve.summary.together.find((f) => f.harness === 'claude');
  Object.values(dfam.scenarios).forEach((sc) => {
    sc.events = sc.events.filter((e) => e.at <= nowS + 86400);
    sc.events_omitted = 0;
  });
  assert.ok(dfam.scenarios.no_new_use.events.length >= 2);
  env = await boot(day);
  assert.equal(byFocus(env.root, 'horizon:24h').getAttribute('aria-pressed'), 'true');
  const ticks = walk(classes(env.root, 'tl-block')[0]).filter((n) => /\baxis-text\b/.test(String(n.getAttribute('class') || '')));
  assert.ok(ticks.length >= 2, 'a drawn time axis');
  click(env, 'scenario:recent_pace');
  assert.equal(byFocus(env.root, 'horizon:24h').getAttribute('aria-pressed'), 'true');
  click(env, 'scenario:no_new_use');
  click(env, 'limit:' + weekly.key);            // a limit's own record: its own span
  await settle();
  assert.equal(byFocus(env.root, 'horizon:7d').getAttribute('aria-pressed'), 'true');
  click(env, 'limit:' + five.key);
  await settle();
  assert.equal(byFocus(env.root, 'horizon:24h').getAttribute('aria-pressed'), 'true');
  click(env, 'limit:joint');
  assert.equal(byFocus(env.root, 'horizon:24h').getAttribute('aria-pressed'), 'true');
  click(env, 'horizon:7d');                     // the reader's pick, kept through a poll
  await poll(env);
  assert.equal(byFocus(env.root, 'horizon:7d').getAttribute('aria-pressed'), 'true');
  // An event past the day, or events left out of the answer: a week.
  const later = JSON.parse(JSON.stringify(day));
  later.reserve.summary.together.find((f) => f.harness === 'claude').scenarios.no_new_use.events_omitted = 1;
  env = await boot(later);
  assert.equal(byFocus(env.root, 'horizon:7d').getAttribute('aria-pressed'), 'true');
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def _run_node(script, env):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    return subprocess.run([str(node), "-e", textwrap.dedent(harness + script)], cwd=WIDGET.parent,
                          env={**os.environ, "WIDGET_PATH": str(WIDGET), **env},
                          capture_output=True, text=True, timeout=90, check=False)


def test_real_widget_together_resets_other_limits_and_the_card_in_place(views):
    result = _run_node(NODE_JOINT, {"JOINT_WORLD": json.dumps(views)})
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# In a real browser: where things stand, at the owner's ordinary and narrow
# card widths, light and dark; motion only on a click, none under reduced
# motion; a poll that keeps the selection and the scroll.


GEOMETRY = """(barKey) => {
  const r = (e) => e.getBoundingClientRect();
  const bar = document.querySelector('[data-focus="' + barKey + '"]');
  const row = bar.closest('.lrow');
  const card = document.querySelector('.inspector');
  const jrow = document.querySelector('.jrow');
  const after = (n) => { let x = n && n.nextElementSibling; while (x && x.classList.contains('inspector')) x = x.nextElementSibling; return x; };
  const joint = jrow.nextElementSibling && jrow.nextElementSibling.classList.contains('tl-block') ? jrow.nextElementSibling : null;
  const own = after(row) && after(row).classList.contains('tl-block') ? after(row) : null;
  const cells = [...jrow.querySelectorAll('.jcell')].map(r);
  return {width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
          rowTop: r(row).top, rowBottom: r(row).bottom,
          cardTop: card ? r(card).top : null, cardBottom: card ? r(card).bottom : null,
          jointBottom: joint ? r(joint).bottom : null, ownTop: own ? r(own).top : null,
          ownAnimation: own ? getComputedStyle(own).animationName : null,
          blocks: document.querySelectorAll('.tl-block').length, jrow: r(jrow), cells,
          animation: card ? getComputedStyle(card).animationName : null, scrollY: scrollY};
}"""


def _launch(playwright, engine):
    from test_order_focus import _launch_engine
    return _launch_engine(playwright, engine)


def _mount(page, views, width, theme, motion="no-preference"):
    stub = ("const W=" + json.dumps({"claude": views["claude"], "codex": views["codex"]}) + ";"
            "window.fetch = async (url) => ({ok:true,status:200,text:async () => JSON.stringify("
            "/harness=codex/.test(String(url)) ? W.codex : W.claude)});"
            "window.OuroborosWidget = {onTheme(fn){ fn('" + theme + "'); return () => {}; }};")
    doc = ('<!doctype html><html data-theme="' + theme + '"><head></head><body><div id="root"></div>'
           + '<script>' + stub + '</script><script>' + WIDGET.read_text().replace('</script', '<\\/script')
           + '</script></body></html>')
    page.emulate_media(reduced_motion=motion)
    page.set_content('<iframe sandbox="allow-scripts" style="border:0;width:' + str(width)
                     + 'px;height:3800px" srcdoc="' + html.escape(doc, quote=True) + '"></iframe>')
    frame = page.frames[1]
    frame.wait_for_selector('.jrow')
    frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
    return frame


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_places_the_card_under_its_row_at_every_width(views, engine):
    sync_api = pytest.importorskip("playwright.sync_api")
    weekly = next(g for g in views["claude"]["reserve"]["summary"]["groups"] if g["key"] == "claude|seven_day|604800|-")
    bar_key = "bar:" + weekly["key"] + ":" + weekly["bars"][-1]["account"]
    with sync_api.sync_playwright() as playwright:
        browser = _launch(playwright, engine)
        try:
            for theme in ("light", "dark"):
                for width in (860, 360):
                    for motion in ("no-preference", "reduce"):
                        page = browser.new_page(viewport={"width": width + 20, "height": 4000})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        frame = _mount(page, views, width, theme, motion)
                        # A mount: the limits-together timeline under Together, still.
                        g = frame.evaluate(GEOMETRY, bar_key)
                        case = (theme, width, motion, g)
                        assert g["blocks"] == 1 and g["jointBottom"] is not None, case
                        assert g["animation"] is None and g["ownTop"] is None, case
                        frame.click('[data-focus="%s"]' % bar_key)
                        frame.wait_for_selector('.inspector')
                        g = frame.evaluate(GEOMETRY, bar_key)
                        case = (theme, width, motion, g)
                        assert g["scrollWidth"] <= g["width"] + 1, case
                        # Right under the row that selected it; the joint
                        # timeline stays where it was, above that row.
                        assert 0 <= g["cardTop"] - g["rowBottom"] <= 24, case
                        assert g["jointBottom"] <= g["rowTop"] + 1, case
                        assert all(c["left"] >= g["jrow"]["left"] and c["right"] <= g["jrow"]["right"] + 1
                                   for c in g["cells"]), case
                        assert g["animation"] == ("none" if motion == "reduce" else "q-reveal"), case
                        # The row's own record, on its control: under the
                        # card, the card still right under the row; the
                        # timeline the click opened eases in, never under
                        # reduced motion.
                        frame.click('[data-focus="limit:%s"]' % weekly["key"])
                        frame.wait_for_function("() => document.querySelectorAll('.tl-block').length === 1"
                                                " && !document.querySelector('.jrow + .tl-block')")
                        g = frame.evaluate(GEOMETRY, bar_key)
                        case = (theme, width, motion, g)
                        assert g["scrollWidth"] <= g["width"] + 1, case
                        assert 0 <= g["cardTop"] - g["rowBottom"] <= 24, case
                        assert g["ownTop"] is not None and g["ownTop"] >= g["cardBottom"] - 1, case
                        assert g["ownAnimation"] == ("none" if motion == "reduce" else "q-reveal"), case
                        assert not errors, errors
                        page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_poll_keeps_selection_scroll_and_never_animates(views, engine):
    sync_api = pytest.importorskip("playwright.sync_api")
    weekly = next(g for g in views["claude"]["reserve"]["summary"]["groups"] if g["key"] == "claude|seven_day|604800|-")
    bar_key = "bar:" + weekly["key"] + ":" + weekly["bars"][-1]["account"]
    with sync_api.sync_playwright() as playwright:
        browser = _launch(playwright, engine)
        try:
            page = browser.new_page(viewport={"width": 880, "height": 4000})
            frame = _mount(page, views, 860, "light")
            # The frame's own document scrolls here, as a card the host has
            # capped would: a short frame, scrolled down to the card.
            page.locator('iframe').evaluate('(e) => { e.style.height = "520px"; }')
            frame.click('[data-focus="%s"]' % bar_key)
            frame.wait_for_selector('.inspector')
            frame.evaluate("document.querySelector('.inspector').scrollIntoView()")
            frame.wait_for_timeout(300)  # the click's 160 ms entrance has ended
            before = frame.evaluate("({y: scrollY, top: document.querySelector('.inspector').getBoundingClientRect().top})")
            assert before["y"] > 0
            frame.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            frame.wait_for_selector('[data-focus="refresh"]:not([disabled])')
            frame.wait_for_timeout(300)
            after = frame.evaluate("({y: scrollY, top: document.querySelector('.inspector').getBoundingClientRect().top,"
                                   " anim: getComputedStyle(document.querySelector('.inspector')).animationName,"
                                   " cls: document.querySelector('.inspector').className})")
            assert abs(after["y"] - before["y"]) <= 1 and abs(after["top"] - before["top"]) <= 1, (before, after)
            assert "reveal" not in after["cls"], after
            # Escape gives the keyboard back to the bar without scrolling.
            frame.focus('[data-focus="inspector-clear"]')
            y = frame.evaluate("scrollY")
            page.keyboard.press("Escape")
            state = frame.evaluate("({key: document.activeElement.getAttribute('data-focus'), y: scrollY,"
                                   " card: !!document.querySelector('.inspector')})")
            assert state["key"] == bar_key and state["card"] is False, state
            assert abs(state["y"] - y) <= 400, (y, state)
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Release-review repairs, in the real widget. Each fails on the reviewed
# candidate: the native select over the chart control at 320 px, events left
# out of an answer said nowhere, a card closed silently when its whole family
# leaves the account list, and a family of only model-scoped limits opened on
# its first model.

LONG_MODEL = "claude-sonnet-4-5-20250929"


def _route_view(folder, now, data, harness):
    store = quota_history.HistoryStore(Path(folder))
    out = plugin.build_view(data, "", now)
    out["prefs"] = {}
    out["reserve"] = plugin.reserve_view(store, data, now, now, harness=harness, horizon="7d",
                                         name_accounts=True, reads=plugin.facet_states(data))
    return out


def _status(snaps, profiles):
    return {"reads": {"catalog": "not_read", "accounts": "ok", "quota": "ok"}, "view": "quota", "daemon": {},
            "unified_accounts": True, "harnesses": [], "profiles": {"profiles": profiles, "harnessAccounts": []},
            "quota": snaps, "quota_absences": []}


@pytest.fixture(scope="module")
def repair_views(tmp_path_factory):
    now = float(int(time.time()))
    long_names = world(tmp_path_factory.mktemp("long"), now, fable=[LONG_MODEL, "fable"])
    no_claude = world(tmp_path_factory.mktemp("no_claude"), now, roster_drop=tuple(f"c{i}" for i in range(6)))
    without_c0 = world(tmp_path_factory.mktemp("without_c0"), now, roster_drop=("c0",), fable=[LONG_MODEL, "fable"])
    assert not any(g["harness_id"] == "claude" for g in no_claude["claude"]["groups"])
    # 60 accounts, each 5-hour limit spent and reset ten minutes apart: 120
    # reported events in the future with no new use, the first 48 published.
    many = _route_view(tmp_path_factory.mktemp("many"), now, _status(
        [_snap("claude", f"m{i:02d}", [_cons("five_hour", 1.0, FIVE, now + H + i * 600),
                                       _cons("seven_day", .5, WEEK, now + 3 * DAY + i * 60)], now - 30)
         for i in range(60)],
        [_profile("claude", f"m{i:02d}") for i in range(60)]), "claude")
    scoped = _route_view(tmp_path_factory.mktemp("scoped"), now, _status(
        [_snap("codex", "s0", [_cons("weekly_m", 1.0, WEEK, now + 2 * DAY, models=["model-m"])], now - 30)],
        [_profile("codex", "s0")]), "codex")
    return {"long": long_names, "no_claude": no_claude["claude"], "without_c0": without_c0["claude"],
            "many": many, "scoped_only": scoped}


NODE_REPAIR_PRELUDE = r"""
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'no node with data-focus ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
const has = (n, c) => String(n.className || '').split(/\s+/).includes(c);
const siblingsAfter = (node) => node.parentNode.childNodes.slice(node.parentNode.childNodes.indexOf(node) + 1);
async function poll(env) { env.interval()(); await settle(); }

"""

NODE_REPAIR = {
    "events_left_out": r"""
(async () => {
  const R = JSON.parse(process.env.REPAIR_WORLD);
  const W = R.long;
  let env;
  // Events a future's answer left out are said under its table, with
  // where every reported reset is.
  env = await boot(R.many);
  const block = siblingsAfter(classes(env.root, 'jrow')[0])[0];
  assert.ok(has(block, 'tl-block'));
  const sc = R.many.reserve.summary.together[0].scenarios.no_new_use;
  assert.equal(sc.events.length, 48);
  assert.ok(sc.events_omitted > 0);
  const notes = classes(block, 'schedule-notes').map((n) => n.textContent);
  assert.ok(notes.includes('+ ' + sc.events_omitted + ' later events not listed: the answer carries the first 48'
                           + ' · each row’s reset list has every reported reset'), notes.join(' | '));
  const said = () => classes(siblingsAfter(classes(env.root, 'jrow')[0])[0], 'schedule-notes')
    .map((n) => n.textContent).filter((t) => /later event/.test(t));
  // The 48 published end within hours: a day's span is cut too.
  click(env, 'horizon:24h');
  assert.equal(said().length, 1);
  // Published events that run past the day: the cut is not this span's.
  const past = JSON.parse(JSON.stringify(W.claude));
  const pastSc = past.reserve.summary.together.find((f) => f.harness === 'claude').scenarios.no_new_use;
  assert.ok(pastSc.events[pastSc.events.length - 1].at > Date.now() / 1000 + 86400);
  pastSc.events_omitted = 2;
  env = await boot(past);
  assert.deepEqual(said(), ['+ 2 later events not listed: the answer carries the first ' + pastSc.events.length
                            + ' · each row’s reset list has every reported reset']);
  click(env, 'horizon:24h');
  assert.deepEqual(said(), []);

})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
""",
    "whole_family_left": r"""
(async () => {
  const R = JSON.parse(process.env.REPAIR_WORLD);
  const W = R.long;
  let env;
  // Every account of the selected family leaves the account list: the
  // card closes with a note under the Together row of the family left on
  // screen, the keyboard on it; Escape dismisses it.
  let calls = 0;
  const leaving = (url) => (++calls > 1 ? R.no_claude : (/harness=codex/.test(url) ? W.codex : W.claude));
  env = await boot(leaving);
  click(env, 'jcell:claude:c0');
  assert.equal(classes(env.root, 'inspector').length, 1);
  byFocus(env.root, 'inspector-clear').focus();
  await poll(env);
  assert.equal(classes(env.root, 'inspector').length, 0);
  let gone = classes(env.root, 'vanished');
  assert.equal(gone.length, 1);
  assert.match(gone[0].textContent,
               /^c0 is no longer in the account list \(read \d\d:\d\d\), nor is any other Claude Code account, so its card is closed\./);
  assert.equal(siblingsAfter(classes(env.root, 'jrow')[0])[0], gone[0], 'under the family on screen');
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'vanished-ok');
  env.document.listeners.keydown.forEach((fn) => fn({ key: 'Escape' }));
  assert.equal(classes(env.root, 'vanished').length, 0);
  // Opened from the account list: the note stands in the list on screen.
  calls = 0;
  env = await boot(leaving);
  click(env, 'accounts');
  click(env, 'acct:claude:c3');
  byFocus(env.root, 'inspector-clear').focus();
  await poll(env);
  gone = classes(env.root, 'vanished');
  assert.equal(gone.length, 1);
  assert.ok(has(gone[0].parentNode, 'accounts'), 'in the account list on screen');
  assert.equal(env.document.activeElement.getAttribute('data-focus'), 'vanished-ok');
  // One account leaving a family that stays keeps the words it had.
  calls = 0;
  env = await boot((url) => (++calls > 1 ? R.without_c0 : W.claude));
  click(env, 'jcell:claude:c0');
  await poll(env);
  assert.match(classes(env.root, 'vanished')[0].textContent,
               /^c0 is no longer in the account list \(read \d\d:\d\d\), so its card is closed\./);
  // The reader switching family closes the card without a note.
  env = await boot((url) => (/harness=codex/.test(url) ? W.codex : W.claude));
  click(env, 'jcell:claude:c0');
  click(env, 'harness:codex');
  await settle();
  assert.equal(classes(env.root, 'vanished').length, 0);
  assert.equal(classes(env.root, 'inspector').length, 0);

})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
""",
    "only_scoped_family": r"""
(async () => {
  const R = JSON.parse(process.env.REPAIR_WORLD);
  const W = R.long;
  let env;
  // A family whose only limits are model-scoped opens on the shared
  // limits, never on its first model, and never counts them free.
  env = await boot(R.scoped_only);
  const jrow = classes(env.root, 'jrow')[0];
  assert.equal(byFocus(env.root, 'joint-option').value, 'other');
  assert.match(classes(jrow, 'l-fig')[0].textContent, /^0 of 1 account$/);
  assert.match(classes(jrow, 'l-tail')[0].textContent, /1 incomplete/);
  const cell = classes(jrow, 'jcell')[0];
  assert.match(cell.getAttribute('aria-label'),
               /Incomplete for Shared limits: no limit read is known to apply to it, so nothing shows it has quota/);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
""",
}


@pytest.mark.parametrize("case", sorted(NODE_REPAIR))
def test_real_widget_review_repairs(repair_views, case):
    result = _run_node(NODE_REPAIR_PRELUDE + NODE_REPAIR[case], {"REPAIR_WORLD": json.dumps(repair_views)})
    assert result.returncode == 0, result.stdout + result.stderr


SELECT_GEOMETRY = """() => {
  const r = (e) => e.getBoundingClientRect();
  const select = document.querySelector('[data-focus="joint-option"]');
  const chart = document.querySelector('.jrow .j-chart');
  const word = document.querySelector('.j-pick > span');
  const a = r(select), b = r(chart), row = r(document.querySelector('.jrow'));
  return {select: {left: a.left, right: a.right, top: a.top, bottom: a.bottom},
          chart: {left: b.left, right: b.right, top: b.top, bottom: b.bottom},
          row: {left: row.left, right: row.right}, wordHeight: r(word).height,
          lineHeight: parseFloat(getComputedStyle(word).lineHeight) || 20,
          shown: select.options[select.selectedIndex].text, title: select.title,
          width: innerWidth, scrollWidth: document.documentElement.scrollWidth};
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_keeps_a_full_model_name_off_the_chart_control(repair_views, engine):
    """At 320 px the native select with claude-sonnet-4-5-20250929 ran
    under the chart control (x29-237 over x208-304) and the word "for"
    broke into letters. The picker now narrows within its track."""
    sync_api = pytest.importorskip("playwright.sync_api")
    views = repair_views["long"]
    with sync_api.sync_playwright() as playwright:
        browser = _launch(playwright, engine)
        try:
            for theme in ("light", "dark"):
                for width in (320, 360, 860):
                    page = browser.new_page(viewport={"width": width + 20, "height": 4000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    frame = _mount(page, views, width, theme)
                    frame.select_option('[data-focus="joint-option"]', label=LONG_MODEL)
                    frame.wait_for_function(
                        "() => document.querySelector('[data-focus=joint-option]').value !== 'other'")
                    g = frame.evaluate(SELECT_GEOMETRY)
                    case = (engine, theme, width, g)
                    assert g["shown"] == LONG_MODEL, case
                    apart = g["select"]["right"] <= g["chart"]["left"] or g["select"]["bottom"] <= g["chart"]["top"]
                    assert apart, case
                    assert g["row"]["left"] - .5 <= g["select"]["left"] and g["select"]["right"] <= g["row"]["right"] + .5, case
                    assert g["wordHeight"] <= g["lineHeight"] * 1.5, case  # "for" on one line
                    assert g["scrollWidth"] <= g["width"] + 1, case
                    assert g["title"] == LONG_MODEL, case  # clipped on screen, whole on hover and in the list
                    assert not errors, errors
                    page.close()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_real_browser_says_a_whole_family_left_and_keeps_the_keyboard(repair_views, engine):
    sync_api = pytest.importorskip("playwright.sync_api")
    views = repair_views["long"]
    weekly = next(g for g in views["claude"]["reserve"]["summary"]["groups"] if g["key"] == "claude|seven_day|604800|-")
    account = weekly["bars"][-1]["account"]
    with sync_api.sync_playwright() as playwright:
        browser = _launch(playwright, engine)
        try:
            for origin in ("bar:" + weekly["key"] + ":" + account, "acct:" + account):
                page = browser.new_page(viewport={"width": 880, "height": 4000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                frame = _mount(page, views, 860, "light")
                if origin.startswith("acct:"):
                    frame.click('[data-focus="accounts"]')
                frame.click('[data-focus="%s"]' % origin)
                frame.wait_for_selector(".inspector")
                frame.focus('[data-focus="inspector-clear"]')
                y = frame.evaluate("scrollY")
                frame.evaluate("(v) => { W.claude = v; W.codex = v; }", repair_views["no_claude"])
                frame.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                frame.wait_for_selector(".vanished")
                state = frame.evaluate("""() => ({
                    focus: document.activeElement.getAttribute('data-focus'),
                    card: !!document.querySelector('.inspector'),
                    note: document.querySelector('.vanished').textContent,
                    family: document.querySelector('.harness-btn.active').textContent, y: scrollY})""")
                case = (engine, origin, state)
                assert state["focus"] == "vanished-ok" and not state["card"], case
                assert "nor is any other Claude Code account" in state["note"], case
                assert state["family"].startswith("Codex"), case
                assert abs(state["y"] - y) <= 1, case
                page.keyboard.press("Escape")
                assert frame.evaluate("!document.querySelector('.vanished')"), case
                assert not errors, errors
                page.close()
        finally:
            browser.close()
