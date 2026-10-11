"""Exact identities and counts remain separate from bounded presentation."""
import json

import plugin
import quota_history
import quota_summary as qs
from test_joint import _state, _family, H, DAY
from test_reserve import NOW, WEEK, constraint, payload, profile, snap
from test_joint_widget import world, _run_node


def test_compact_matches_full_model_identity_before_clipping(tmp_path, monkeypatch):
    first, second = 'm' * 80 + 'a', 'm' * 80 + 'b'
    data = payload([snap('codex', 'A', [
        constraint('primary', .2, WEEK, reset=DAY),
        constraint('first', 1.0, WEEK, reset=DAY, models=[first]),
        constraint('second', .2, WEEK, reset=DAY, models=[second]),
    ])], [profile('codex', 'A')], harnesses=('codex',))
    monkeypatch.setattr(plugin, 'history_store', lambda api: quota_history.HistoryStore(tmp_path))
    host = type('Api', (), {'get_state_dir': lambda self: str(tmp_path),
                           'get_runtime_info': lambda self: {'server_port': 1}})()
    for name, word in ((first, 'blocked'), (second, 'no_known_block')):
        result = plugin.tool_answer(host, plugin.LatestRead(), fetch=lambda: (data, ''), now=NOW, model=name)
        options = result['together'][0]['options']
        assert len(options) == 1
        assert options[0]['key'] == 'model:' + qs._scope_hash((name,))
        assert options[0]['counts'][word] == 1


def test_repeated_sources_of_one_hold_count_one_account():
    data = payload([snap('claude', 'A', [constraint('weekly', .2, WEEK, reset=DAY)])],
                   [profile('claude', 'A')], harnesses=('claude',))
    state = _state(data)
    hold = ('cooldown', qs._scope_hash(('model-a',)), ('model-a',), NOW + H)
    state.norm.model_holds[('claude', 'A')] = [hold, hold]
    family = _family(state)
    events = family['scenarios']['no_new_use']['events']
    ends = [change for event in events for change in event['changes'] if change['kind'] == 'model_hold_end']
    assert len(ends) == 1 and ends[0]['accounts'] == 1


def test_widget_discloses_model_choices_not_listed(tmp_path):
    views = world(tmp_path, NOW, fable=[f'model-{i}' for i in range(30)])
    omitted = views['claude']['reserve']['summary']['together'][0]['options_omitted']
    assert omitted > 0
    script = r'''
(async () => {
 const view = JSON.parse(process.env.OMITTED_WORLD);
 const env = await boot(view);
 const tail = classes(classes(env.root, 'jrow')[0], 'l-tail')[0].textContent;
 assert.ok(tail.includes(process.env.OMITTED_COUNT + ' more model choices'), tail);
 assert.ok(tail.includes('quota_summary'), tail);
})().catch(e => {console.error(e.stack || e); process.exitCode = 1;});
'''
    result = _run_node(script, {'OMITTED_WORLD': json.dumps(views['claude']), 'OMITTED_COUNT': str(omitted)})
    assert result.returncode == 0, result.stdout + result.stderr
