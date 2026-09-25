"""ランキング関連操作のスコープ適用と適格性判定。

ユーザー要件:
- バンカラ（オープン）とイベントは本人参加回だけランキング関連情報を保存。
- Xランキングは終了シーズン最終のみ。
- オープン本人試合・bankaraPower・参加表彰記録は保持。
- 現行カタログにはオープン全体ランキング独立操作はないため、
  BankaraBattleHistories*/VsHistoryDetail*/HistoryRecord*は対象制限しない。
- オープンをイベントと同一視しない。
"""

import json
import sqlite3
from datetime import datetime, timezone

RULES = ('Ar', 'Cl', 'Gl', 'Lf')
X_DETAIL_OPS = {
    'XRankingDetailQuery',
    'XRankingDetailRefetchQuery',
    *(f'DetailTabViewXRanking{r}RefetchQuery' for r in RULES),
    *(f'DetailTabViewWeaponTops{r}RefetchQuery' for r in RULES),
}
SCOPED_OPS = set(X_DETAIL_OPS) | {'EventMatchRankingPeriodQuery'}


def parse_iso_datetime(dt_str: str) -> datetime:
    """ISO 8601 文字列を timezone-aware な datetime に変換する。"""
    if not isinstance(dt_str, str):
        raise TypeError('Datetime string must be str')
    s = dt_str.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def decision(
    db: sqlite3.Connection,
    account: str,
    operation: str,
    variables: dict | str | None,
    at: datetime | None = None,
    _cache: dict | None = None,
) -> dict:
    """特定のアカウント・操作・変数についてのスコープ適格性を判定する。

    戻り値:
        dict: {'state': 'eligible' | 'out_of_scope' | 'awaiting_scope' | 'superseded', 'reason': str, 'final': bool}
    """
    if at is None:
        at = datetime.now(timezone.utc)
    elif at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    cache = _cache if _cache is not None else {}

    if operation not in SCOPED_OPS:
        return {'state': 'eligible', 'reason': 'unrestricted_operation', 'final': False}

    if operation == 'XRankingDetailRefetchQuery':
        return {
            'state': 'superseded',
            'reason': 'avoid_cartesian_refetch',
            'final': False,
        }

    if isinstance(variables, str):
        try:
            variables = json.loads(variables)
        except Exception:
            variables = {}
    elif not isinstance(variables, dict):
        variables = {}

    if operation in X_DETAIL_OPS:
        season_id = variables.get('id')
        if not season_id:
            return {'state': 'awaiting_scope', 'reason': 'missing_season_id', 'final': False}

        row = db.execute(
            'SELECT json_text FROM entities WHERE account=? AND typename=? AND entity_id=?',
            (account, 'XRankingSeason', str(season_id)),
        ).fetchone()
        try:
            season_data = json.loads(row['json_text'] if isinstance(row, sqlite3.Row) else row[0]) if row else {}
            if isinstance(season_data, dict) and isinstance(season_data.get('data'), dict):
                season_data = season_data['data']
            if not isinstance(season_data, dict):
                return {'state': 'awaiting_scope', 'reason': 'invalid_season_json', 'final': False}
        except Exception:
            return {'state': 'awaiting_scope', 'reason': 'invalid_season_json', 'final': False}

        # Pagination objects omit season dates. Recover metadata from preserved
        # catalog/detail observations instead of treating omission as erasure.
        if not season_data.get('endTime'):
            if 'seasons' not in cache:
                from .planner import walk
                cache['seasons'] = {}
                for saved in db.execute('''SELECT json_text FROM responses WHERE account=?
                    AND operation IN ('XRankingQuery','XRankingRefetchQuery','XRanking_PastRankings_PaginationQuery','XRankingDetailQuery')
                    AND http_status=200 AND json_text IS NOT NULL ORDER BY fetched_at,id''', (account,)):
                    payload=json.loads(saved['json_text'])
                    if not isinstance(payload,dict) or payload.get('errors'):continue
                    for _, obj in walk(payload.get('data')):
                        if isinstance(obj,dict) and obj.get('id') and obj.get('endTime'):
                            cache['seasons'][obj['id']] = obj
            season_data=cache['seasons'].get(season_id,season_data)

        is_current = season_data.get('isCurrent')
        end_time_raw = season_data.get('endTime')
        if not end_time_raw:
            return {'state': 'awaiting_scope', 'reason': 'missing_end_time', 'final': False}

        try:
            end_time = parse_iso_datetime(end_time_raw)
        except Exception:
            return {'state': 'awaiting_scope', 'reason': 'invalid_end_time', 'final': False}

        if is_current is True or end_time > at:
            return {'state': 'out_of_scope', 'reason': 'season_active_or_future', 'final': False}

        return {
            'state': 'eligible',
            'reason': 'season_ended',
            'final': True,
            'final_after': end_time.isoformat(),
        }

    if operation == 'EventMatchRankingPeriodQuery':
        period_id = variables.get('eventMatchRankingPeriodId')
        if not period_id:
            return {'state': 'awaiting_scope', 'reason': 'missing_period_id', 'final': False}

        # The catalog's small period descriptors carry the same dates and event
        # identity. Avoid loading multi-megabyte ranking trees for every job.
        if 'periods' not in cache:
            cache['periods'] = {}
            for grow in db.execute('SELECT json_text FROM entities WHERE account=? AND typename=?',
                                   (account, 'LeagueMatchRankingTimePeriodGroup')):
                group = json.loads(grow['json_text'])
                if not isinstance(group, dict):continue
                for period in group.get('timePeriods') or []:
                    if isinstance(period, dict) and period.get('id'):
                        cache['periods'][period['id']] = {**period, 'leagueMatchSetting': group.get('leagueMatchSetting')}
        descriptor = cache['periods'].get(period_id)
        row = (json.dumps(descriptor),) if descriptor and descriptor.get('startTime') and descriptor.get('endTime') else db.execute(
            'SELECT json_text FROM entities WHERE account=? AND typename=? AND entity_id=?',
            (account, 'LeagueMatchRankingTimePeriod', str(period_id)),
        ).fetchone()
        if not row:
            return {'state': 'awaiting_scope', 'reason': 'period_entity_not_found', 'final': False}

        try:
            period_data = json.loads(row['json_text'] if isinstance(row, sqlite3.Row) else row[0])
            if isinstance(period_data, dict) and isinstance(period_data.get('data'), dict):
                period_data = period_data['data']
            if not isinstance(period_data, dict):
                return {'state': 'awaiting_scope', 'reason': 'invalid_period_json', 'final': False}
        except Exception:
            return {'state': 'awaiting_scope', 'reason': 'invalid_period_json', 'final': False}

        start_time_raw = period_data.get('startTime')
        end_time_raw = period_data.get('endTime')
        if not start_time_raw or not end_time_raw:
            return {'state': 'awaiting_scope', 'reason': 'missing_period_time_range', 'final': False}

        try:
            start_time = parse_iso_datetime(start_time_raw)
            end_time = parse_iso_datetime(end_time_raw)
        except Exception:
            return {'state': 'awaiting_scope', 'reason': 'invalid_period_time_format', 'final': False}

        if not (start_time < end_time):
            return {'state': 'awaiting_scope', 'reason': 'invalid_period_time_range', 'final': False}

        event_id = None
        setting = period_data.get('leagueMatchSetting')
        if isinstance(setting, dict):
            event = setting.get('leagueMatchEvent')
            if isinstance(event, dict):
                event_id = event.get('id')

        if not event_id:
            group_rows = db.execute(
                'SELECT json_text FROM entities WHERE account=? AND typename=?',
                (account, 'LeagueMatchRankingTimePeriodGroup'),
            ).fetchall()
            for grow in group_rows:
                try:
                    gdata = json.loads(grow['json_text'] if isinstance(grow, sqlite3.Row) else grow[0])
                except Exception:
                    continue
                if isinstance(gdata, dict) and isinstance(gdata.get('data'), dict):
                    gdata = gdata['data']
                if not isinstance(gdata, dict):
                    continue
                tps = gdata.get('timePeriods') or []
                if any(isinstance(tp, dict) and tp.get('id') == period_id for tp in tps):
                    gsetting = gdata.get('leagueMatchSetting')
                    if isinstance(gsetting, dict):
                        gevent = gsetting.get('leagueMatchEvent')
                        if isinstance(gevent, dict) and gevent.get('id'):
                            event_id = gevent.get('id')
                            break

        if not event_id:
            return {'state': 'awaiting_scope', 'reason': 'event_id_unconfirmed', 'final': False}

        if 'evidence' not in cache:
            cache['evidence'] = db.execute('''
            SELECT json_extract(d.json_text,'$.playedTime') played_time,
                   json_extract(d.json_text,'$.leagueMatch.leagueMatchEvent.id') event_id
            FROM match_classification c
            JOIN documents d ON d.response_id = c.detail_response_id
                            AND d.account = c.account
                            AND d.kind = c.kind
                            AND d.match_key = c.match_key
            WHERE c.account = ? AND c.genre = 'event'
        ''', (account,)).fetchall()

        has_participated = False
        for erow in cache['evidence']:
            pt_raw = erow['played_time']
            if not pt_raw:
                continue
            try:
                played_time = parse_iso_datetime(pt_raw)
            except Exception:
                continue

            if not (start_time <= played_time < end_time):
                continue

            if erow['event_id'] == event_id:
                has_participated = True
                break

        if not has_participated:
            return {'state': 'out_of_scope', 'reason': 'no_participation_evidence', 'final': False}

        is_final = end_time <= at
        result = {'state': 'eligible', 'reason': 'participated_event', 'final': is_final}
        if is_final:
            result['final_after'] = end_time.isoformat()
        return result

    return {'state': 'eligible', 'reason': 'unrestricted_operation', 'final': False}


def apply_scope(store, account: str, at: datetime | None = None) -> dict:
    """対象操作 job だけ走査し decision で状態を更新する。

    戻り値:
        dict: 更新後の対象操作 job の state 別件数
    """
    if at is None:
        at = datetime.now(timezone.utc)
    elif at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)

    placeholders = ','.join('?' for _ in SCOPED_OPS)
    sql = f'''
        SELECT operation, variables_json, state, attempts, next_attempt, last_response_id
        FROM jobs
        WHERE account = ? AND operation IN ({placeholders})
    '''

    counts = {}
    cache = {}
    with store.db:
        rows = store.db.execute(sql, (account, *SCOPED_OPS)).fetchall()
        for row in rows:
            op = row['operation']
            v_json = row['variables_json']
            old_state = row['state']
            old_next_attempt = row['next_attempt']

            last_response_id = row['last_response_id']

            try:
                variables = json.loads(v_json) if v_json else {}
            except Exception:
                variables = {}

            dec = decision(store.db, account, op, variables, at=at, _cache=cache)
            d_state = dec['state']
            d_final = dec['final']

            new_state = old_state
            new_next_attempt = old_next_attempt

            if d_state != 'eligible':
                new_state = d_state
            else:
                if old_state in ('out_of_scope', 'awaiting_scope'):
                    new_state = 'pending'
                    new_next_attempt = 0.0
                elif old_state in ('done', 'done_final'):
                    if not d_final:
                        new_state = 'done'
                    else:
                        final_after_str = dec.get('final_after')
                        final_after_dt = None
                        if final_after_str:
                            try:
                                final_after_dt = parse_iso_datetime(final_after_str)
                            except Exception:
                                final_after_dt = None

                        has_final_receipt = False
                        if last_response_id and final_after_dt:
                            resp_row = store.db.execute(
                                'SELECT account, operation, variables_json, fetched_at FROM responses WHERE id = ?',
                                (last_response_id,),
                            ).fetchone()
                            if resp_row:
                                resp_acc = resp_row['account']
                                resp_op = resp_row['operation']
                                resp_v = resp_row['variables_json']
                                v_match = (resp_v == v_json)
                                if not v_match:
                                    try:
                                        v_match = (json.loads(resp_v or '{}') == json.loads(v_json or '{}'))
                                    except Exception:
                                        v_match = False

                                if resp_acc == account and resp_op == op and v_match:
                                    candidates = []
                                    raw_fetched_at = resp_row['fetched_at']
                                    if raw_fetched_at:
                                        try:
                                            candidates.append(parse_iso_datetime(raw_fetched_at))
                                        except Exception:
                                            pass

                                    fetch_rows = store.db.execute(
                                        'SELECT fetched_at FROM response_fetches WHERE response_id = ? AND acknowledged = 1',
                                        (last_response_id,),
                                    ).fetchall()
                                    for frow in fetch_rows:
                                        raw_fa = frow['fetched_at']
                                        if raw_fa:
                                            try:
                                                candidates.append(parse_iso_datetime(raw_fa))
                                            except Exception:
                                                pass

                                    if candidates and max(candidates) >= final_after_dt:
                                        has_final_receipt = True

                        if has_final_receipt:
                            new_state = 'done_final'
                        else:
                            new_state = 'pending'
                            new_next_attempt = 0.0
                else:
                    pass

            if new_state != old_state or new_next_attempt != old_next_attempt:
                store.db.execute(
                    '''
                    UPDATE jobs
                    SET state = ?, next_attempt = ?
                    WHERE account = ? AND operation = ? AND variables_json = ?
                    ''',
                    (new_state, new_next_attempt, account, op, v_json),
                )

            counts[new_state] = counts.get(new_state, 0) + 1

    return counts
