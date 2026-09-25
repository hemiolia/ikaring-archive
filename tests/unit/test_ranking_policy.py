"""ranking_policy のユニットテスト。

人工 Store / manifest を使用して以下を検証する:
- X現行除外、終了許可、欠落保留、4ルール複合superseded
- イベント本人参加だけ、異アカウント/異イベント/同時刻openは不許可、時刻境界
- PeriodGroupからの親イベントID解決
- 後から参加詳細追加で再有効化（out_of_scope/awaiting_scope -> pending, next_attempt=0）
- done_final化（eligible final trueで元doneならdone_final）
- 他操作不変（特にBankaraBattleHistoriesQueryとHistoryRecordQueryがeligible）
"""

import base64
import json
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/python'))
from ikarchive.store import Store, js, now, digest
from ikarchive.ranking_policy import (
    X_DETAIL_OPS,
    SCOPED_OPS,
    decision,
    apply_scope,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / 'config/query-catalog.snapshot.json'
MANIFEST = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}


class RankingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'test.sqlite'
        self.store = Store(self.db_path)
        manifest_text = js(MANIFEST)
        self.store.db.execute(
            'INSERT OR IGNORE INTO manifests VALUES(?,?,?)',
            (digest(manifest_text.encode()), now(), manifest_text),
        )
        self.store.db.execute(
            "INSERT OR IGNORE INTO runs(id, started_at, status) VALUES(1, ?, 'completed')",
            (now(),),
        )
        self.account = 'acc-test-1'
        self.other_account = 'acc-test-2'

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _insert_entity(self, account, typename, entity_id, data):
        self.store.db.execute(
            'INSERT INTO bodies VALUES(?,?,?)',
            (f'sha-{entity_id}', b'{}', 2),
        )
        self.store.db.execute(
            '''INSERT INTO responses(event_id,run_id,account,fetched_at,operation,variables_json,headers_json,body_sha256,json_text)
               VALUES(?,1,?,?,'test_op','{}','{}',?,?)''',
            (str(uuid.uuid4()), account, now(), f'sha-{entity_id}', js({'data': data})),
        )
        rid = self.store.db.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.store.db.execute(
            '''INSERT INTO entities(account, typename, entity_id, response_id, json_text)
               VALUES(?,?,?,?,?)''',
            (account, typename, entity_id, rid, js(data)),
        )
        self.store.db.commit()
        return rid

    def _insert_canonical_detail(self, account, kind, match_key, genre, detail_obj):
        sha = f'sha-detail-{account}-{match_key}'
        self.store.db.execute('INSERT OR IGNORE INTO bodies VALUES(?,?,?)', (sha, b'{}', 2))
        self.store.db.execute(
            '''INSERT INTO responses(event_id,run_id,account,fetched_at,operation,variables_json,headers_json,body_sha256,json_text)
               VALUES(?,1,?,?,'VsHistoryDetailQuery','{}','{}',?,?)''',
            (str(uuid.uuid4()), account, now(), sha, js({'data': detail_obj})),
        )
        rid = self.store.db.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.store.db.execute(
            '''INSERT INTO matches(account, kind, match_key, first_seen, last_seen, detail_response_id)
               VALUES(?,?,?,?,?,?)''',
            (account, kind, match_key, now(), now(), rid),
        )
        self.store.db.execute(
            '''INSERT INTO documents(response_id, account, kind, match_key, json_text)
               VALUES(?,?,?,?,?)''',
            (rid, account, kind, match_key, js(detail_obj)),
        )
        self.store.db.execute(
            '''INSERT INTO match_classification(
                   account, kind, match_key, genre, roster_class, analysis_set,
                   detail_response_id, classified_at
               ) VALUES(?,?,?,?,'normal','regular',?,?)''',
            (account, kind, match_key, genre, rid, now()),
        )
        self.store.db.commit()
        return rid

    def _insert_response(self, account, operation, variables_json, fetched_at, body_sha=None, json_data=None):
        sha = body_sha or f'sha-{uuid.uuid4().hex[:8]}'
        self.store.db.execute('INSERT OR IGNORE INTO bodies VALUES(?,?,?)', (sha, b'{}', 2))
        data_text = js({'data': json_data or {}})
        self.store.db.execute(
            '''INSERT INTO responses(event_id,run_id,account,fetched_at,operation,variables_json,headers_json,body_sha256,json_text)
               VALUES(?,1,?,?,?,?,'{}',?,?)''',
            (str(uuid.uuid4()), account, fetched_at, operation, variables_json, sha, data_text),
        )
        self.store.db.commit()
        return self.store.db.execute('SELECT last_insert_rowid()').fetchone()[0]

    def _insert_fetch(self, response_id, fetched_at, acknowledged=1):
        self.store.db.execute(
            '''INSERT INTO response_fetches(event_id,response_id,run_id,fetched_at,headers_json,acknowledged)
               VALUES(?,?,1,?,'{}',?)''',
            (str(uuid.uuid4()), response_id, fetched_at, acknowledged),
        )
        self.store.db.commit()

    def test_pagination_does_not_erase_season_dates(self):
        season={'id':'season-closed','endTime':'2026-06-01T00:00:00Z','isCurrent':False}
        rid=self._insert_entity(self.account,'XRankingSeason',season['id'],season)
        self.store.db.execute("UPDATE responses SET operation='XRankingDetailQuery',http_status=200,json_text=? WHERE id=?",
                              (js({'data':{'xRanking':season}}),rid))
        # A later per-rule page has no endTime. It replaces the general entity
        # projection but must not turn completed season metadata into unknown.
        self.store.db.execute("UPDATE entities SET json_text=? WHERE entity_id=?",
                              (js({'id':season['id'],'xRankingAr':{'edges':[]}}),season['id']))
        self.store.db.commit()
        result=decision(self.store.db,self.account,'DetailTabViewXRankingArRefetchQuery',
                        {'id':season['id']},at=datetime(2026,9,22,tzinfo=timezone.utc))
        self.assertEqual(result['state'],'eligible')
        self.assertTrue(result['final'])
        other=decision(self.store.db,self.other_account,'DetailTabViewXRankingArRefetchQuery',
                       {'id':season['id']},at=datetime(2026,9,22,tzinfo=timezone.utc))
        self.assertEqual(other['state'],'awaiting_scope')

    def test_unrestricted_operations(self):
        """BankaraBattleHistoriesQuery, HistoryRecordQuery など非対象操作は eligible / final=False。"""
        at = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
        ops = [
            'BankaraBattleHistoriesQuery',
            'HistoryRecordQuery',
            'XRankingQuery',
            'VsHistoryDetailQuery',
            'LatestBattleHistoriesQuery',
        ]
        for op in ops:
            dec = decision(self.store.db, self.account, op, {}, at=at)
            self.assertEqual(dec['state'], 'eligible')
            self.assertFalse(dec['final'])
            self.assertEqual(dec['reason'], 'unrestricted_operation')

    def test_x_ranking_refetch_superseded(self):
        """XRankingDetailRefetchQuery は 4ルール同時ページング直積回避のため superseded。"""
        at = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
        dec = decision(self.store.db, self.account, 'XRankingDetailRefetchQuery', {'idecd': 'dummy'}, at=at)
        self.assertEqual(dec['state'], 'superseded')
        self.assertFalse(dec['final'])

    def test_x_ranking_season_decision(self):
        """Xランキングの判定: 終了シーズン許可(final=True)、現行・未来除外(out_of_scope)、欠落保留(awaiting_scope)。"""
        at = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)

        # 1. 終了シーズン: endTime <= at かつ isCurrent is False -> eligible, final=True
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-ended',
            {'id': 'season-ended', 'isCurrent': False, 'endTime': '2024-06-01T00:00:00Z'},
        )
        for op in [
            'XRankingDetailQuery',
            'DetailTabViewXRankingArRefetchQuery',
            'DetailTabViewWeaponTopsGlRefetchQuery',
        ]:
            dec = decision(self.store.db, self.account, op, {'id': 'season-ended'}, at=at)
            self.assertEqual(dec['state'], 'eligible')
            self.assertTrue(dec['final'])

        # 2. 現行シーズン: isCurrent is True -> out_of_scope
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-current',
            {'id': 'season-current', 'isCurrent': True, 'endTime': '2024-05-31T00:00:00Z'},
        )
        dec = decision(self.store.db, self.account, 'XRankingDetailQuery', {'id': 'season-current'}, at=at)
        self.assertEqual(dec['state'], 'out_of_scope')
        self.assertFalse(dec['final'])

        # 3. 未来/進行中シーズン: endTime > at -> out_of_scope
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-future',
            {'id': 'season-future', 'isCurrent': False, 'endTime': '2024-06-02T00:00:00Z'},
        )
        dec = decision(self.store.db, self.account, 'XRankingDetailQuery', {'id': 'season-future'}, at=at)
        self.assertEqual(dec['state'], 'out_of_scope')
        self.assertFalse(dec['final'])

        # 4. 欠落/無効: awaiting_scope
        # (a) 変数 id 欠落
        dec = decision(self.store.db, self.account, 'XRankingDetailQuery', {}, at=at)
        self.assertEqual(dec['state'], 'awaiting_scope')
        # (b) entity なし
        dec = decision(self.store.db, self.account, 'XRankingDetailQuery', {'id': 'non-existent'}, at=at)
        self.assertEqual(dec['state'], 'awaiting_scope')
        # (c) endTime なし / 不正
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-invalid',
            {'id': 'season-invalid', 'isCurrent': False, 'endTime': None},
        )
        dec = decision(self.store.db, self.account, 'XRankingDetailQuery', {'id': 'season-invalid'}, at=at)
        self.assertEqual(dec['state'], 'awaiting_scope')

    def test_event_ranking_participated_vs_unparticipated(self):
        """イベントの判定: 本人参加回だけ保存、未参加はout_of_scope、開催中はfinal=False。"""
        at = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)

        # イベント期間メタデータ
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriod',
            'period-1',
            {
                'id': 'period-1',
                'startTime': '2024-06-10T10:00:00Z',
                'endTime': '2024-06-10T12:00:00Z',
                'leagueMatchSetting': {'leagueMatchEvent': {'id': 'event-100'}},
            },
        )

        # 1. 参加証拠なし -> out_of_scope
        dec = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-1'},
            at=at,
        )
        self.assertEqual(dec['state'], 'out_of_scope')
        self.assertFalse(dec['final'])

        # 2. 参加証拠あり (終了後: endTime <= at) -> eligible, final=True
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-ev-1',
            'event',
            {
                'playedTime': '2024-06-10T10:30:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'event-100'}},
            },
        )
        dec = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-1'},
            at=at,
        )
        self.assertEqual(dec['state'], 'eligible')
        self.assertTrue(dec['final'])

        # 3. 参加証拠あり (開催中: endTime > at) -> eligible, final=False
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriod',
            'period-ongoing',
            {
                'id': 'period-ongoing',
                'startTime': '2024-06-15T10:00:00Z',
                'endTime': '2024-06-15T14:00:00Z',
                'leagueMatchSetting': {'leagueMatchEvent': {'id': 'event-200'}},
            },
        )
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-ev-ongoing',
            'event',
            {
                'playedTime': '2024-06-15T11:00:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'event-200'}},
            },
        )
        dec_ongoing = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-ongoing'},
            at=at,
        )
        self.assertEqual(dec_ongoing['state'], 'eligible')
        self.assertFalse(dec_ongoing['final'])

    def test_event_rejections_and_boundaries(self):
        """異アカウント、異イベント、同時刻オープン、時刻境界の厳密検査。"""
        at = datetime(2024, 6, 20, 12, 0, tzinfo=timezone.utc)
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriod',
            'period-target',
            {
                'id': 'period-target',
                'startTime': '2024-06-10T10:00:00Z',
                'endTime': '2024-06-10T12:00:00Z',
                'leagueMatchSetting': {'leagueMatchEvent': {'id': 'event-target'}},
            },
        )

        # 1. 異アカウントの参加証拠があっても自アカウントは out_of_scope
        self._insert_canonical_detail(
            self.other_account,
            'vs',
            'match-other-acc',
            'event',
            {
                'playedTime': '2024-06-10T10:30:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'event-target'}},
            },
        )
        dec = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-target'},
            at=at,
        )
        self.assertEqual(dec['state'], 'out_of_scope')

        # 2. 異イベントIDの参加証拠
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-diff-ev',
            'event',
            {
                'playedTime': '2024-06-10T10:30:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'event-different'}},
            },
        )
        dec = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-target'},
            at=at,
        )
        self.assertEqual(dec['state'], 'out_of_scope')

        # 3. 同時刻オープン（genre='bankara_open'）はイベント証拠にならない
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-bankara-open',
            'bankara_open',
            {
                'playedTime': '2024-06-10T10:30:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'event-target'}},
            },
        )
        dec = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-target'},
            at=at,
        )
        self.assertEqual(dec['state'], 'out_of_scope')

        # 4. 時刻境界: [startTime, endTime)
        # (a) 終了境界 playedTime == endTime は除外 -> out_of_scope
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-end-boundary',
            'event',
            {
                'playedTime': '2024-06-10T12:00:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'event-target'}},
            },
        )
        dec = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-target'},
            at=at,
        )
        self.assertEqual(dec['state'], 'out_of_scope')

        # (b) 開始境界 playedTime == startTime は含む -> eligible
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-start-boundary',
            'event',
            {
                'playedTime': '2024-06-10T10:00:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'event-target'}},
            },
        )
        dec = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-target'},
            at=at,
        )
        self.assertEqual(dec['state'], 'eligible')

        # 5. startTime >= endTime（無効な期間定義）は awaiting_scope
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriod',
            'period-invalid-range',
            {
                'id': 'period-invalid-range',
                'startTime': '2024-06-10T12:00:00Z',
                'endTime': '2024-06-10T10:00:00Z',
                'leagueMatchSetting': {'leagueMatchEvent': {'id': 'event-target'}},
            },
        )
        dec = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-invalid-range'},
            at=at,
        )
        self.assertEqual(dec['state'], 'awaiting_scope')

        # 6. イベントID未確認は awaiting_scope
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriod',
            'period-no-event-id',
            {
                'id': 'period-no-event-id',
                'startTime': '2024-06-10T10:00:00Z',
                'endTime': '2024-06-10T12:00:00Z',
            },
        )
        dec = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-no-event-id'},
            at=at,
        )
        self.assertEqual(dec['state'], 'awaiting_scope')

    def test_event_id_resolved_via_period_group(self):
        """LeagueMatchRankingTimePeriodGroup の timePeriods 配列から親 event_id を解決。"""
        at = datetime(2024, 6, 20, 12, 0, tzinfo=timezone.utc)
        # Period には leagueMatchSetting が直接ない
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriod',
            'period-sub',
            {
                'id': 'period-sub',
                'startTime': '2024-06-10T10:00:00Z',
                'endTime': '2024-06-10T12:00:00Z',
            },
        )
        # Group に timePeriods 配列と親 leagueMatchSetting がある
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriodGroup',
            'group-1',
            {
                'id': 'group-1',
                'leagueMatchSetting': {'leagueMatchEvent': {'id': 'event-group-resolved'}},
                'timePeriods': [{'id': 'period-sub'}, {'id': 'period-other'}],
            },
        )
        # 参加証拠
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-group-ev',
            'event',
            {
                'playedTime': '2024-06-10T10:30:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'event-group-resolved'}},
            },
        )
        dec = decision(
            self.store.db,
            self.account,
            'EventMatchRankingPeriodQuery',
            {'eventMatchRankingPeriodId': 'period-sub'},
            at=at,
        )
        self.assertEqual(dec['state'], 'eligible')
        self.assertTrue(dec['final'])

    def test_apply_scope_lifecycle_and_reenabling(self):
        """apply_scope による状態更新、done_final化、後からの証拠追加での再有効化を検証。"""
        at = datetime(2024, 6, 20, 12, 0, tzinfo=timezone.utc)

        # 1. 初期エンティティとジョブのセットアップ
        self._insert_entity(
            self.account,
            'XRankingSeason',
            's-ended',
            {'id': 's-ended', 'isCurrent': False, 'endTime': '2024-06-01T00:00:00Z'},
        )
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriod',
            'p-ev',
            {
                'id': 'p-ev',
                'startTime': '2024-06-10T10:00:00Z',
                'endTime': '2024-06-10T12:00:00Z',
                'leagueMatchSetting': {'leagueMatchEvent': {'id': 'ev-1'}},
            },
        )

        rid_s_ended = self._insert_response(
            self.account,
            'XRankingDetailQuery',
            '{"id":"s-ended"}',
            '2024-06-05T00:00:00Z',
        )

        # jobs を投入
        self.store.db.execute(
            '''INSERT INTO jobs(account, operation, variables_json, state, attempts, next_attempt, last_response_id)
               VALUES
               (?, 'XRankingDetailQuery', '{"id":"s-ended"}', 'done', 1, 100.0, ?),
               (?, 'XRankingDetailQuery', '{"id":"s-ended-pending"}', 'pending', 0, 0.0, NULL),
               (?, 'XRankingDetailRefetchQuery', '{"id":"s-ended"}', 'pending', 0, 0.0, NULL),
               (?, 'EventMatchRankingPeriodQuery', '{"eventMatchRankingPeriodId":"p-ev"}', 'pending', 0, 50.0, NULL),
               (?, 'BankaraBattleHistoriesQuery', '{}', 'pending', 0, 0.0, NULL),
               (?, 'HistoryRecordQuery', '{}', 'pending', 0, 0.0, NULL)''',
            (self.account, rid_s_ended, self.account, self.account, self.account, self.account, self.account),
        )
        # 他アカウントのジョブ（不変であることを確認）
        self.store.db.execute(
            '''INSERT INTO jobs(account, operation, variables_json, state, attempts, next_attempt)
               VALUES (?, 'EventMatchRankingPeriodQuery', '{"eventMatchRankingPeriodId":"p-ev"}', 'pending', 0, 99.0)''',
            (self.other_account,),
        )
        self.store.db.commit()

        # 2. 第1回 apply_scope
        counts = apply_scope(self.store, self.account, at=at)

        # 検証:
        # - s-ended (元done) -> done_final (next_attempt保持 100.0)
        # - s-ended-pending (entity未登録) -> awaiting_scope (attempts, last_response_id 不変)
        # - XRankingDetailRefetchQuery -> superseded
        # - p-ev (参加証拠なし) -> out_of_scope
        # - BankaraBattleHistoriesQuery, HistoryRecordQuery -> 対象外のため走査・変更されない
        # - other_account -> 不変

        j_x_done = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"s-ended\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_x_done['state'], 'done_final')
        self.assertEqual(j_x_done['next_attempt'], 100.0)

        j_x_missing = self.store.db.execute(
            "SELECT state FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"s-ended-pending\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_x_missing['state'], 'awaiting_scope')

        j_x_refetch = self.store.db.execute(
            "SELECT state FROM jobs WHERE account=? AND operation='XRankingDetailRefetchQuery'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_x_refetch['state'], 'superseded')

        j_ev = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='EventMatchRankingPeriodQuery'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_ev['state'], 'out_of_scope')
        self.assertEqual(j_ev['next_attempt'], 50.0)

        # 非対象操作が不変
        j_bankara = self.store.db.execute(
            "SELECT state FROM jobs WHERE account=? AND operation='BankaraBattleHistoriesQuery'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_bankara['state'], 'pending')

        # 他アカウントが不変
        j_other = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='EventMatchRankingPeriodQuery'",
            (self.other_account,),
        ).fetchone()
        self.assertEqual(j_other['state'], 'pending')
        self.assertEqual(j_other['next_attempt'], 99.0)

        # counts dict の集計確認
        self.assertEqual(
            counts,
            {
                'done_final': 1,
                'awaiting_scope': 1,
                'superseded': 1,
                'out_of_scope': 1,
            },
        )

        # 3. 後から参加証拠とメタデータを追加
        # p-ev の参加証拠を追加
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-late-ev',
            'event',
            {
                'playedTime': '2024-06-10T10:15:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'ev-1'}},
            },
        )
        # s-ended-pending のメタデータを追加
        self._insert_entity(
            self.account,
            'XRankingSeason',
            's-ended-pending',
            {'id': 's-ended-pending', 'isCurrent': False, 'endTime': '2024-06-01T00:00:00Z'},
        )

        # 4. 第2回 apply_scope -> out_of_scope / awaiting_scope から pending, next_attempt=0 へ再有効化
        counts2 = apply_scope(self.store, self.account, at=at)

        j_ev2 = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='EventMatchRankingPeriodQuery'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_ev2['state'], 'pending')
        self.assertEqual(j_ev2['next_attempt'], 0.0)

        j_x_missing2 = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"s-ended-pending\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_x_missing2['state'], 'pending')
        self.assertEqual(j_x_missing2['next_attempt'], 0.0)

        # s-ended (done_final) は done_final を維持
        j_x_done2 = self.store.db.execute(
            "SELECT state FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"s-ended\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_x_done2['state'], 'done_final')

    def test_decision_final_after_presence(self):
        """decision の返り値で eligible かつ final=True のときに final_after が追加され、final=False では付与されないこと。"""
        at = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)

        # 1. XRanking 終了シーズン -> eligible, final=True, final_after あり
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-decision-ended',
            {'id': 'season-decision-ended', 'isCurrent': False, 'endTime': '2024-06-01T00:00:00Z'},
        )
        dec_x_ended = decision(self.store.db, self.account, 'XRankingDetailQuery', {'id': 'season-decision-ended'}, at=at)
        self.assertEqual(dec_x_ended['state'], 'eligible')
        self.assertTrue(dec_x_ended['final'])
        self.assertEqual(dec_x_ended['final_after'], '2024-06-01T00:00:00+00:00')

        # 2. XRanking 進行中シーズン -> out_of_scope, final=False, final_after なし
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-decision-active',
            {'id': 'season-decision-active', 'isCurrent': True, 'endTime': '2024-06-20T00:00:00Z'},
        )
        dec_x_active = decision(self.store.db, self.account, 'XRankingDetailQuery', {'id': 'season-decision-active'}, at=at)
        self.assertFalse(dec_x_active['final'])
        self.assertNotIn('final_after', dec_x_active)

        # 3. Event 終了・参加済 -> eligible, final=True, final_after あり
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriod',
            'period-ev-decision-ended',
            {
                'id': 'period-ev-decision-ended',
                'startTime': '2024-06-10T10:00:00Z',
                'endTime': '2024-06-10T12:00:00Z',
                'leagueMatchSetting': {'leagueMatchEvent': {'id': 'ev-decision-1'}},
            },
        )
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-ev-decision-1',
            'event',
            {
                'playedTime': '2024-06-10T10:30:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'ev-decision-1'}},
            },
        )
        dec_ev_ended = decision(self.store.db, self.account, 'EventMatchRankingPeriodQuery', {'eventMatchRankingPeriodId': 'period-ev-decision-ended'}, at=at)
        self.assertEqual(dec_ev_ended['state'], 'eligible')
        self.assertTrue(dec_ev_ended['final'])
        self.assertEqual(dec_ev_ended['final_after'], '2024-06-10T12:00:00+00:00')

        # 4. Event 開催中・参加済 -> eligible, final=False, final_after なし
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriod',
            'period-ev-decision-ongoing',
            {
                'id': 'period-ev-decision-ongoing',
                'startTime': '2024-06-15T10:00:00Z',
                'endTime': '2024-06-15T14:00:00Z',
                'leagueMatchSetting': {'leagueMatchEvent': {'id': 'ev-decision-2'}},
            },
        )
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-ev-decision-2',
            'event',
            {
                'playedTime': '2024-06-15T10:30:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'ev-decision-2'}},
            },
        )
        dec_ev_ongoing = decision(self.store.db, self.account, 'EventMatchRankingPeriodQuery', {'eventMatchRankingPeriodId': 'period-ev-decision-ongoing'}, at=at)
        self.assertEqual(dec_ev_ongoing['state'], 'eligible')
        self.assertFalse(dec_ev_ongoing['final'])
        self.assertNotIn('final_after', dec_ev_ongoing)

    def test_apply_scope_response_timing_and_final_receipt(self):
        """終了前応答は pending(next_attempt=0.0)、終了後応答は done_final(next_attempt維持)。"""
        at = datetime(2024, 6, 20, 12, 0, tzinfo=timezone.utc)
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-timing',
            {'id': 'season-timing', 'isCurrent': False, 'endTime': '2024-06-01T00:00:00Z'},
        )
        # 1. 終了前応答 (2024-05-31T23:59:59Z)
        rid_before = self._insert_response(
            self.account,
            'XRankingDetailQuery',
            '{"id":"season-timing"}',
            '2024-05-31T23:59:59Z',
        )
        self.store.db.execute(
            '''INSERT INTO jobs(account, operation, variables_json, state, attempts, next_attempt, last_response_id)
               VALUES (?, 'XRankingDetailQuery', '{"id":"season-timing"}', 'done', 1, 100.0, ?)''',
            (self.account, rid_before),
        )
        self.store.db.commit()

        apply_scope(self.store, self.account, at=at)
        j_before = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"season-timing\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_before['state'], 'pending')
        self.assertEqual(j_before['next_attempt'], 0.0)

        # 2. 終了後応答 (2024-06-01T00:00:00Z)
        rid_after = self._insert_response(
            self.account,
            'XRankingDetailQuery',
            '{"id":"season-timing"}',
            '2024-06-01T00:00:00Z',
        )
        self.store.db.execute(
            '''UPDATE jobs SET state='done', next_attempt=200.0, last_response_id=?
               WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{"id":"season-timing"}' ''',
            (rid_after, self.account),
        )
        self.store.db.commit()

        apply_scope(self.store, self.account, at=at)
        j_after = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"season-timing\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_after['state'], 'done_final')
        self.assertEqual(j_after['next_attempt'], 200.0)

    def test_apply_scope_same_body_acknowledged_fetch(self):
        """同一本文の後日再取得 (response_fetches で acknowledged=1 かつ fetched_at >= final_after) で done_final。"""
        at = datetime(2024, 6, 20, 12, 0, tzinfo=timezone.utc)
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-refetch',
            {'id': 'season-refetch', 'isCurrent': False, 'endTime': '2024-06-01T00:00:00Z'},
        )
        # 初回レスポンスは終了前
        rid = self._insert_response(
            self.account,
            'XRankingDetailQuery',
            '{"id":"season-refetch"}',
            '2024-05-15T00:00:00Z',
        )
        # 後日同一本文が取得され response_fetches に acknowledged=1 で登録
        self._insert_fetch(rid, '2024-06-02T10:00:00Z', acknowledged=1)

        self.store.db.execute(
            '''INSERT INTO jobs(account, operation, variables_json, state, attempts, next_attempt, last_response_id)
               VALUES (?, 'XRankingDetailQuery', '{"id":"season-refetch"}', 'done', 1, 300.0, ?)''',
            (self.account, rid),
        )
        self.store.db.commit()

        apply_scope(self.store, self.account, at=at)
        j = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"season-refetch\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j['state'], 'done_final')
        self.assertEqual(j['next_attempt'], 300.0)

    def test_apply_scope_unacknowledged_fetch_rejected(self):
        """未ackフェッチ (acknowledged=0) は根拠とせず pending(next_attempt=0.0) とする。"""
        at = datetime(2024, 6, 20, 12, 0, tzinfo=timezone.utc)
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-unack',
            {'id': 'season-unack', 'isCurrent': False, 'endTime': '2024-06-01T00:00:00Z'},
        )
        rid = self._insert_response(
            self.account,
            'XRankingDetailQuery',
            '{"id":"season-unack"}',
            '2024-05-15T00:00:00Z',
        )
        # 終了後のフェッチだが acknowledged=0 (未ack)
        self._insert_fetch(rid, '2024-06-02T10:00:00Z', acknowledged=0)

        self.store.db.execute(
            '''INSERT INTO jobs(account, operation, variables_json, state, attempts, next_attempt, last_response_id)
               VALUES (?, 'XRankingDetailQuery', '{"id":"season-unack"}', 'done', 1, 100.0, ?)''',
            (self.account, rid),
        )
        self.store.db.commit()

        apply_scope(self.store, self.account, at=at)
        j = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"season-unack\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j['state'], 'pending')
        self.assertEqual(j['next_attempt'], 0.0)

    def test_apply_scope_different_account_or_operation_rejected(self):
        """別アカウントや別操作・別引数のレスポンスは終了後であっても根拠にしない。"""
        at = datetime(2024, 6, 20, 12, 0, tzinfo=timezone.utc)
        # 1. 別アカウントのレスポンス
        rid_other_acc = self._insert_response(
            self.other_account,
            'XRankingDetailQuery',
            '{"id":"season-diff-0"}',
            '2024-06-05T00:00:00Z',
        )
        # 2. 別操作のレスポンス
        rid_other_op = self._insert_response(
            self.account,
            'BankaraBattleHistoriesQuery',
            '{"id":"season-diff-1"}',
            '2024-06-05T00:00:00Z',
        )
        # 3. 別引数のレスポンス
        rid_other_var = self._insert_response(
            self.account,
            'XRankingDetailQuery',
            '{"id":"season-other"}',
            '2024-06-05T00:00:00Z',
        )

        bad_rids = [rid_other_acc, rid_other_op, rid_other_var]
        for i, bad_rid in enumerate(bad_rids):
            self._insert_entity(
                self.account,
                'XRankingSeason',
                f'season-diff-{i}',
                {'id': f'season-diff-{i}', 'isCurrent': False, 'endTime': '2024-06-01T00:00:00Z'},
            )
            self.store.db.execute(
                '''INSERT INTO jobs(account, operation, variables_json, state, attempts, next_attempt, last_response_id)
                   VALUES (?, 'XRankingDetailQuery', ?, 'done', 1, 100.0, ?)''',
                (self.account, f'{{"id":"season-diff-{i}"}}', bad_rid),
            )
        self.store.db.commit()

        apply_scope(self.store, self.account, at=at)

        for i in range(3):
            j = self.store.db.execute(
                "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json=?",
                (self.account, f'{{"id":"season-diff-{i}"}}'),
            ).fetchone()
            self.assertEqual(j['state'], 'pending')
            self.assertEqual(j['next_attempt'], 0.0)

    def test_apply_scope_existing_done_final_misclassification_repaired(self):
        """終了前に誤って done_final に移行していたジョブを pending(next_attempt=0.0) に修復する。"""
        at = datetime(2024, 6, 20, 12, 0, tzinfo=timezone.utc)
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-misclassified',
            {'id': 'season-misclassified', 'isCurrent': False, 'endTime': '2024-06-01T00:00:00Z'},
        )
        rid = self._insert_response(
            self.account,
            'XRankingDetailQuery',
            '{"id":"season-misclassified"}',
            '2024-05-20T00:00:00Z',  # 終了前
        )
        # 既に done_final に誤移行していた
        self.store.db.execute(
            '''INSERT INTO jobs(account, operation, variables_json, state, attempts, next_attempt, last_response_id)
               VALUES (?, 'XRankingDetailQuery', '{"id":"season-misclassified"}', 'done_final', 1, 100.0, ?)''',
            (self.account, rid),
        )
        self.store.db.commit()

        apply_scope(self.store, self.account, at=at)
        j = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"season-misclassified\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j['state'], 'pending')
        self.assertEqual(j['next_attempt'], 0.0)

    def test_apply_scope_revert_ongoing_done_final_to_done(self):
        """現在開催中で古い done_final がある場合、done へ戻す。"""
        at = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)
        # 開催中のイベント (10:00 - 14:00)
        self._insert_entity(
            self.account,
            'LeagueMatchRankingTimePeriod',
            'period-ongoing-revert',
            {
                'id': 'period-ongoing-revert',
                'startTime': '2024-06-15T10:00:00Z',
                'endTime': '2024-06-15T14:00:00Z',
                'leagueMatchSetting': {'leagueMatchEvent': {'id': 'ev-ongoing-revert'}},
            },
        )
        self._insert_canonical_detail(
            self.account,
            'vs',
            'match-ongoing-revert',
            'event',
            {
                'playedTime': '2024-06-15T10:30:00Z',
                'leagueMatch': {'leagueMatchEvent': {'id': 'ev-ongoing-revert'}},
            },
        )
        self.store.db.execute(
            '''INSERT INTO jobs(account, operation, variables_json, state, attempts, next_attempt)
               VALUES (?, 'EventMatchRankingPeriodQuery', '{"eventMatchRankingPeriodId":"period-ongoing-revert"}', 'done_final', 1, 75.0)''',
            (self.account,),
        )
        self.store.db.commit()

        apply_scope(self.store, self.account, at=at)
        j = self.store.db.execute(
            "SELECT state, next_attempt FROM jobs WHERE account=? AND operation='EventMatchRankingPeriodQuery'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j['state'], 'done')
        self.assertEqual(j['next_attempt'], 75.0)

    def test_apply_scope_retry_and_pending_preserved(self):
        """成功stateではない retry や pending のジョブは変更されない。"""
        at = datetime(2024, 6, 20, 12, 0, tzinfo=timezone.utc)
        self._insert_entity(
            self.account,
            'XRankingSeason',
            'season-retry-pending',
            {'id': 'season-retry-pending', 'isCurrent': False, 'endTime': '2024-06-01T00:00:00Z'},
        )
        self.store.db.execute(
            '''INSERT INTO jobs(account, operation, variables_json, state, attempts, next_attempt)
               VALUES
               (?, 'XRankingDetailQuery', '{"id":"season-retry-pending","mode":"retry"}', 'retry', 3, 500.0),
               (?, 'XRankingDetailQuery', '{"id":"season-retry-pending","mode":"pending"}', 'pending', 1, 60.0)''',
            (self.account, self.account),
        )
        self.store.db.commit()

        apply_scope(self.store, self.account, at=at)

        j_retry = self.store.db.execute(
            "SELECT state, next_attempt, attempts FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"season-retry-pending\",\"mode\":\"retry\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_retry['state'], 'retry')
        self.assertEqual(j_retry['next_attempt'], 500.0)
        self.assertEqual(j_retry['attempts'], 3)

        j_pending = self.store.db.execute(
            "SELECT state, next_attempt, attempts FROM jobs WHERE account=? AND operation='XRankingDetailQuery' AND variables_json='{\"id\":\"season-retry-pending\",\"mode\":\"pending\"}'",
            (self.account,),
        ).fetchone()
        self.assertEqual(j_pending['state'], 'pending')
        self.assertEqual(j_pending['next_attempt'], 60.0)
        self.assertEqual(j_pending['attempts'], 1)


if __name__ == '__main__':
    unittest.main()
