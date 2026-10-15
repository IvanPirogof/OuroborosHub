"""Explicit resolution of one provider copy; network stays outside SQLite.

The token binds the observed provider revision and local family. Old operations
are resolved, never reported as successful provider writes. Linked copies are
not silently rewritten by choosing one physical calendar's version.
"""
import hashlib
import json
from datetime import timedelta

import ops
from model import event_public, iso_utc, new_id, now_utc, parse_stored


def _payload(intent):
    return json.loads(intent.get('payload_json') or '{}')


def _snapshot(c, event_id):
    row = c.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    root = row.get('master_id') or row['id']
    cal = c.execute('SELECT * FROM calendars WHERE id=?', (row['calendar_id'],)).fetchone()
    rows = c.execute('SELECT * FROM events WHERE id=? OR master_id=? ORDER BY id', (root, root)).fetchall()
    intents = c.execute('SELECT i.* FROM intents i JOIN events e ON e.id=i.event_id '
                        'WHERE e.id=? OR e.master_id=? ORDER BY i.created_at,i.rowid', (root, root)).fetchall()
    return {'event': row, 'calendar': dict(cal) if cal else None,
            'rows': [dict(r) for r in rows], 'intents': [dict(i) for i in intents]}


def _load(ctx, event_id):
    base, occurrence = ops._series_id(str(event_id))
    row = ctx.store.get_event(base)
    if row is None:
        return None, None, {'status': 'not_found'}
    if occurrence or row.get('master_id'):
        master, key = ops.resolve_occurrence(ctx.store, row, occurrence)
        row = next((e for e in ctx.store.exceptions_for(master['id'])
                    if e.get('recurrence_id') == key), row)
    with ctx.store._conn() as c:
        c.execute('BEGIN')
        state = _snapshot(c, row['id'])
    if state is None:
        return None, None, {'status': 'not_found'}
    row = state['event']
    selected = [i for i in state['intents'] if i['event_id'] == row['id']]
    if row.get('sync_state') != 'conflict' and not any(i['state'] == 'conflict' for i in selected):
        return state, None, {'status': 'not_conflicted'}
    body = {'status': 'conflict', 'event_id': row['id'], 'local': event_public(row, ctx.tz, compact=False),
            'operations': [{'id': i['id'], 'kind': i['kind'], 'state': i['state']}
                           for i in selected if i['state'] in ('pending', 'conflict')]}
    cal = state['calendar']
    adapter = ctx.providers.adapter_for(cal['account_id']) if cal else None
    if adapter is None:
        return state, None, {**body, 'status': 'unavailable', 'message': 'аккаунт не подключён; выбор не применён'}
    master = next((r for r in state['rows'] if r['id'] == row.get('master_id')), {})
    address = {**row, 'master_external_id': master.get('external_id') or '',
               'master_href': master.get('href') or '', 'master_etag': master.get('etag') or ''}
    latest = next((i for i in reversed(selected) if i['state'] != 'resolved'), None)
    try:
        current = adapter.read_current(cal, address, _payload(latest) if latest else {})
    except ops.ProviderError as exc:
        if exc.kind not in ('not_found', 'gone'):
            return state, None, {**body, 'status': 'unavailable', 'message': exc.message}
        current = {'event': None, 'etag': ''}
    remote = current.get('event')
    body['provider'] = event_public(remote, ctx.tz, compact=False) if remote else None
    if remote is None or not current.get('etag'):
        return state, current, {**body, 'message': 'у провайдера нет выбранного события или ETag; '
                               'локальное намерение сохранено, пересоздание требует отдельного выбора'}
    # Opaque token is bound to exactly these observations; no persisted token registry.
    encoded = json.dumps([state, current], sort_keys=True, ensure_ascii=False, default=str).encode()
    body['resolution_token'] = hashlib.sha256(encoded).hexdigest()
    return state, current, body


def inspect(ctx, event_id):
    return _load(ctx, event_id)[2]


def resolve(ctx, event_id, choice, token):
    if choice not in ('local', 'provider'):
        return {'status': 'error', 'message': 'resolution: local | provider'}
    state, current, body = _load(ctx, event_id)
    if not token or body.get('resolution_token') != token:
        return {**body, 'snapshot_changed': True, 'message': 'снимок изменился или недоступен; покажи обе версии заново'}
    row, remote = state['event'], current['event']
    selected = [i for i in state['intents'] if i['event_id'] == row['id']]
    latest = next((i for i in reversed(selected) if i['state'] != 'resolved'), None)
    op_id = _payload(latest).get('op_id') if latest else None
    templates = [i for i in selected if i['state'] in ('pending', 'conflict', 'failed')
                 and (i['id'] == latest['id'] or (op_id and _payload(i).get('op_id') == op_id))] if latest else []
    if choice == 'local' and (not templates or any(i['kind'] == 'create' for i in templates)
                              or remote.get('status') == 'cancelled'):
        return {**body, 'message': 'нет однозначной прежней операции для повтора; '
                'можно выбрать provider, затем задать отдельное изменение или создание'}
    if choice == 'local' and not state['calendar'].get('writable', True):
        return {**body, 'status': 'error', 'message': 'календарь только для чтения'}
    retired = {i['id'] for i in selected if i['state'] in ('pending', 'conflict')}
    retired.update(i['id'] for i in templates)
    new_op, clones = new_id('op'), []
    if choice == 'local':
        changes = {}
        for i in selected:
            if i['id'] in retired:
                changes.update(_payload(i).get('changes') or {})
        for n, original in enumerate(templates):
            payload = {**_payload(original), 'op_id': new_op}
            if original['kind'] == 'update':
                payload['changes'] = changes
            ts = (now_utc() + timedelta(microseconds=n)).isoformat(timespec='microseconds')
            clones.append({**original, 'id': new_id('int'), 'payload_json': json.dumps(payload),
                           'expected_etag': current['etag'] if n == 0 else '',
                           'state': 'pending', 'attempts': 0, 'lease_until': None, 'lease_owner': '',
                           'result_json': '{}', 'created_at': ts, 'updated_at': ts, 'next_attempt_at': ts})
        fields = {'sync_state': 'pending_delete' if templates[-1]['kind'] == 'delete' else 'pending'}
        if not row.get('master_id'):
            fields['raw_payload'] = remote.get('raw_payload') or ''
    else:
        keys = ('title', 'description', 'location', 'start_utc', 'end_utc', 'tz', 'all_day',
                'rrule', 'rdates', 'exdates', 'status', 'organizer', 'attendees_json',
                'my_response', 'availability', 'external_id', 'href', 'etag', 'raw_payload')
        fields = {k: remote[k] for k in keys if k in remote}
        fields.update(sync_state='synced', deleted_at=None)
    fields['updated_at'] = iso_utc(now_utc())
    with ctx.store._conn() as c:
        c.execute('BEGIN IMMEDIATE')
        changed = _snapshot(c, row['id']) != state
        if not changed:
            if any(parse_stored(i.get('lease_until')) and parse_stored(i['lease_until']) > now_utc()
                   for i in state['intents']):
                return {**body, 'status': 'busy', 'message': 'операция этой серии ещё выполняется; дождись её исхода'}
            for i in selected:
                if i['id'] in retired:
                    result = {'resolution': choice, 'superseded_by': new_op,
                              'previous': json.loads(i.get('result_json') or '{}')}
                    c.execute("UPDATE intents SET state='resolved', result_json=?, lease_until=NULL, "
                              'lease_owner=\'\', next_attempt_at=NULL, updated_at=? WHERE id=?',
                              (json.dumps(result), fields['updated_at'], i['id']))
            c.execute('UPDATE events SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                      (*fields.values(), row['id']))
            for clone in clones:
                columns = list(clone)
                c.execute('INSERT INTO intents (' + ','.join(columns) + ') VALUES ('
                          + ','.join('?' for _ in columns) + ')', tuple(clone[k] for k in columns))
    if changed:
        return {**inspect(ctx, event_id), 'snapshot_changed': True, 'message': 'локальный снимок изменился; выбор не применён'}
    return {'status': 'pending' if clones else 'resolved', 'event_id': row['id'],
            'resolution': choice, 'queued': [i['id'] for i in clones],
            'message': 'прежняя операция поставлена заново; запись у провайдера ещё не подтверждена'
                       if clones else 'принята версия провайдера только для выбранной копии'}
