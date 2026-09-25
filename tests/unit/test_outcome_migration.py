import base64
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/python'))
from ikarchive.store import Store, js, now, digest
from ikarchive.planner import Planner

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / 'config/query-catalog.snapshot.json').read_text())

def encoded(s):
    return base64.b64encode(s.encode()).decode()

def make_response(op, body_obj, account='account-a', variables=None, status=200, query_id=None):
    raw = js(body_obj).encode()
    return {
        'event_id': str(uuid.uuid4()),
        'account': account,
        'fetched_at': now(),
        'operation': op,
        'variables': variables or {},
        'status': status,
        'query_id': query_id,
        'body_base64': base64.b64encode(raw).decode(),
    }

class OutcomeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'test.sqlite'
        self.store = Store(self.db_path)
        manifest_text = js(MANIFEST)
        self.store.db.execute(
            'INSERT OR IGNORE INTO manifests VALUES(?,?,?)',
            (digest(manifest_text.encode()), now(), manifest_text)
        )
        self.store.db.execute(
            "INSERT OR IGNORE INTO runs(id, started_at, status) VALUES(?, ?, 'completed')",
            (1, now())
        )
        self.store.db.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_200_with_errors_and_null_repaired_to_retry(self):
        """200+errors+nullはretryとなり、旧移行で誤unavailable/doneになったjobもretryへ修復されること。"""
        rid_str = encoded('VsHistoryDetail-u-test:REGULAR:20260901T010101_err')
        v = {'vsResultId': rid_str}
        q_id = MANIFEST['queries']['VsHistoryDetailQuery']['params']['id']
        e = make_response('VsHistoryDetailQuery', {
            'data': {'vsHistoryDetail': None},
            'errors': [{'message': 'Internal GraphQL error'}]
        }, variables=v, query_id=q_id)
        rid = self.store.record(e)

        # 旧移行で誤って 'unavailable' にされた状態を再現
        initial_next_attempt = 1000.0
        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'VsHistoryDetailQuery', js(v), 'vs', 'u-test:20260901T010101_err', 'unavailable', 1, initial_next_attempt, rid)
        )
        orig_context = {'operation': 'VsHistoryDetailQuery', 'status': 200, 'graphql_errors': True, 'custom_flag': 42}
        self.store.db.execute(
            "INSERT INTO issues(run_id,response_id,code,context,created_at) VALUES(?,?,?,?,?)",
            (None, rid, 'INCOMPLETE_RESPONSE', js(orig_context), '2026-09-01T00:00:00Z')
        )
        self.store.db.commit()

        self.store._repair_job_outcomes()

        job = self.store.db.execute("SELECT * FROM jobs WHERE operation='VsHistoryDetailQuery'").fetchone()
        self.assertEqual(job['state'], 'retry')
        self.assertNotEqual(job['next_attempt'], initial_next_attempt)
        self.assertGreater(job['next_attempt'], time.time() - 10)

        issue = self.store.db.execute("SELECT * FROM issues WHERE response_id=?", (rid,)).fetchone()
        self.assertEqual(issue['code'], 'INCOMPLETE_RESPONSE')
        self.assertEqual(json.loads(issue['context'])['custom_flag'], 42)
        self.assertEqual(issue['created_at'], '2026-09-01T00:00:00Z')

    def test_normal_detail_null_becomes_unavailable_and_reclassifies_issue(self):
        """正常detail nullがunavailableとなり、誤INCOMPLETE_RESPONSEがDETAIL_UNAVAILABLEへ分類変更され元内容が維持されること。"""
        rid_str = encoded('VsHistoryDetail-u-test:REGULAR:20260901T010101_null')
        v = {'vsResultId': rid_str}
        q_id = MANIFEST['queries']['VsHistoryDetailQuery']['params']['id']
        e = make_response('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': None}}, variables=v, query_id=q_id)
        rid = self.store.record(e)

        initial_next_attempt = 2000.0
        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'VsHistoryDetailQuery', js(v), 'vs', 'u-test:20260901T010101_null', 'retry', 1, initial_next_attempt, rid)
        )
        orig_context = {'operation': 'VsHistoryDetailQuery', 'status': 200, 'graphql_errors': False, 'tag': 'keep-me'}
        self.store.db.execute(
            "INSERT INTO issues(run_id,response_id,code,context,created_at) VALUES(?,?,?,?,?)",
            (1, rid, 'INCOMPLETE_RESPONSE', js(orig_context), '2026-09-02T12:00:00Z')
        )
        self.store.db.commit()

        self.store._repair_job_outcomes()

        job = self.store.db.execute("SELECT * FROM jobs WHERE operation='VsHistoryDetailQuery'").fetchone()
        self.assertEqual(job['state'], 'unavailable')
        self.assertNotEqual(job['next_attempt'], initial_next_attempt)
        self.assertGreater(job['next_attempt'], time.time() + 80000)

        issue = self.store.db.execute("SELECT * FROM issues WHERE response_id=?", (rid,)).fetchone()
        self.assertEqual(issue['code'], 'DETAIL_UNAVAILABLE')
        self.assertEqual(issue['run_id'], 1)
        self.assertEqual(issue['created_at'], '2026-09-02T12:00:00Z')
        self.assertEqual(json.loads(issue['context'])['tag'], 'keep-me')

    def test_normal_current_fest_null_becomes_done_and_reclassifies_issue(self):
        """正常currentFest nullがdoneとなり、誤INCOMPLETE_RESPONSEがEXPECTED_ABSENCEへ分類変更され元内容が維持されること。"""
        v = {}
        q_id = MANIFEST['queries']['useCurrentFestQuery']['params']['id']
        e = make_response('useCurrentFestQuery', {'data': {'currentFest': None}}, variables=v, query_id=q_id)
        rid = self.store.record(e)

        initial_next_attempt = 3000.0
        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'useCurrentFestQuery', js(v), None, None, 'retry', 1, initial_next_attempt, rid)
        )
        orig_context = {'operation': 'useCurrentFestQuery', 'status': 200, 'meta': 'fest_not_active'}
        self.store.db.execute(
            "INSERT INTO issues(run_id,response_id,code,context,created_at) VALUES(?,?,?,?,?)",
            (1, rid, 'INCOMPLETE_RESPONSE', js(orig_context), '2026-09-03T10:00:00Z')
        )
        self.store.db.commit()

        self.store._repair_job_outcomes()

        job = self.store.db.execute("SELECT * FROM jobs WHERE operation='useCurrentFestQuery'").fetchone()
        self.assertEqual(job['state'], 'done')
        self.assertNotEqual(job['next_attempt'], initial_next_attempt)
        self.assertGreater(job['next_attempt'], time.time() + 80000)

        issue = self.store.db.execute("SELECT * FROM issues WHERE response_id=?", (rid,)).fetchone()
        self.assertEqual(issue['code'], 'EXPECTED_ABSENCE')
        self.assertEqual(issue['run_id'], 1)
        self.assertEqual(issue['created_at'], '2026-09-03T10:00:00Z')
        self.assertEqual(json.loads(issue['context'])['meta'], 'fest_not_active')

    def test_missing_required_fields_not_successful(self):
        """必須フィールドが欠落している場合はdoneやunavailableなどの成功状態にならないこと。"""
        # useCurrentFestQuery の selections に必須フィールド 'extraRequired' を追加したカスタム manifest
        custom_manifest = json.loads(js(MANIFEST))
        custom_manifest['queries']['useCurrentFestQuery']['operation']['selections'].append({
            'kind': 'ScalarField',
            'name': 'extraRequired',
            'alias': None,
            'args': None,
            'storageKey': None
        })
        custom_text = js(custom_manifest)
        self.store.db.execute("DELETE FROM manifests")
        self.store.db.execute(
            'INSERT INTO manifests VALUES(?,?,?)',
            (digest(custom_text.encode()), now(), custom_text)
        )

        v = {}
        q_id = custom_manifest['queries']['useCurrentFestQuery']['params']['id']
        # currentFest は null だが extraRequired が欠落しているレスポンス
        e = make_response('useCurrentFestQuery', {'data': {'currentFest': None}}, variables=v, query_id=q_id)
        rid = self.store.record(e)

        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'useCurrentFestQuery', js(v), None, None, 'pending', 0, 0, rid)
        )
        self.store.db.commit()

        self.store._repair_job_outcomes()

        job = self.store.db.execute("SELECT * FROM jobs WHERE operation='useCurrentFestQuery'").fetchone()
        # 必須欠落があるため done にはならず retry となる
        self.assertEqual(job['state'], 'retry')

    def test_other_jobs_unaffected(self):
        """対象外のjobや正常detailを持つjobは変更されないこと。"""
        # 1. 対象外のoperation
        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'LatestBattleHistoriesQuery', js({}), None, None, 'pending', 0, 1111.0, None)
        )
        # 2. 正常detailが存在するVsHistoryDetailQuery
        rid_str = encoded('VsHistoryDetail-u-ok:REGULAR:20260901T010101_ok')
        v = {'vsResultId': rid_str}
        q_id = MANIFEST['queries']['VsHistoryDetailQuery']['params']['id']
        e = make_response('VsHistoryDetailQuery', {
            'data': {'vsHistoryDetail': {'id': rid_str, 'playedTime': '2026-09-01T01:01:01Z'}}
        }, variables=v, query_id=q_id)
        rid = self.store.record(e)
        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'VsHistoryDetailQuery', js(v), 'vs', 'u-ok:20260901T010101_ok', 'done', 1, 2222.0, rid)
        )
        self.store.db.commit()

        self.store._repair_job_outcomes()

        j1 = self.store.db.execute("SELECT * FROM jobs WHERE operation='LatestBattleHistoriesQuery'").fetchone()
        self.assertEqual(j1['state'], 'pending')
        self.assertEqual(j1['next_attempt'], 1111.0)

        j2 = self.store.db.execute("SELECT * FROM jobs WHERE operation='VsHistoryDetailQuery'").fetchone()
        self.assertEqual(j2['state'], 'done')
        self.assertEqual(j2['next_attempt'], 2222.0)

    def test_repeated_migration_preserves_next_attempt_and_issue_count_and_content(self):
        """繰返し移行を実行しても、next_attemptやissueの件数・内容が不変であること。"""
        # job 1: 正常detail null
        rid_str1 = encoded('VsHistoryDetail-u-rep1:REGULAR:20260901T010101_x')
        v1 = {'vsResultId': rid_str1}
        q_id1 = MANIFEST['queries']['VsHistoryDetailQuery']['params']['id']
        e1 = make_response('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': None}}, variables=v1, query_id=q_id1)
        rid1 = self.store.record(e1)
        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'VsHistoryDetailQuery', js(v1), 'vs', 'u-rep1:20260901T010101_x', 'retry', 1, 500.0, rid1)
        )
        self.store.db.execute(
            "INSERT INTO issues(run_id,response_id,code,context,created_at) VALUES(?,?,?,?,?)",
            (1, rid1, 'INCOMPLETE_RESPONSE', js({'info': 'detail_missing'}), '2026-09-01T10:00:00Z')
        )

        # job 2: pager
        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'VsHistoryDetailPagerRefetchQuery', js({}), None, None, 'pending', 0, 600.0, None)
        )
        e_pager = make_response('VsHistoryDetailPagerRefetchQuery', {'data': {'vsHistoryDetail': None}})
        rid_pager = self.store.record(e_pager)
        self.store.db.execute(
            "INSERT INTO issues(run_id,response_id,code,context,created_at) VALUES(?,?,?,?,?)",
            (1, rid_pager, 'INCOMPLETE_RESPONSE', js({'pager': 'ctx'}), '2026-09-01T11:00:00Z')
        )
        self.store.db.commit()

        # 1回目の実行
        self.store._repair_job_outcomes()

        jobs_pass1 = {
            (r['operation'], r['variables_json']): (r['state'], r['next_attempt'])
            for r in self.store.db.execute("SELECT operation, variables_json, state, next_attempt FROM jobs").fetchall()
        }
        issues_pass1 = [
            dict(r) for r in self.store.db.execute("SELECT id, run_id, response_id, code, context, created_at FROM issues ORDER BY id").fetchall()
        ]

        # 時間差を設けて2回目の実行
        time.sleep(0.05)
        self.store._repair_job_outcomes()

        jobs_pass2 = {
            (r['operation'], r['variables_json']): (r['state'], r['next_attempt'])
            for r in self.store.db.execute("SELECT operation, variables_json, state, next_attempt FROM jobs").fetchall()
        }
        issues_pass2 = [
            dict(r) for r in self.store.db.execute("SELECT id, run_id, response_id, code, context, created_at FROM issues ORDER BY id").fetchall()
        ]

        self.assertEqual(jobs_pass1, jobs_pass2)
        self.assertEqual(issues_pass1, issues_pass2)
        self.assertEqual(len(issues_pass2), 2)

    def test_pager_superseded_and_issue_reclassified(self):
        """pager jobがsupersededへ移り、誤INCOMPLETE_RESPONSEがSUPERSEDED_RESPONSEへ分類変更され元contextが維持されること。"""
        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'VsHistoryDetailPagerRefetchQuery', js({}), None, None, 'pending', 0, 700.0, None)
        )
        e = make_response('VsHistoryDetailPagerRefetchQuery', {'data': {'vsHistoryDetail': None}})
        rid = self.store.record(e)
        orig_context = {'reason': 'superseded_pager', 'count': 5}
        self.store.db.execute(
            "INSERT INTO issues(run_id,response_id,code,context,created_at) VALUES(?,?,?,?,?)",
            (1, rid, 'INCOMPLETE_RESPONSE', js(orig_context), '2026-09-05T00:00:00Z')
        )
        self.store.db.commit()

        self.store._repair_job_outcomes()

        job = self.store.db.execute("SELECT * FROM jobs WHERE operation='VsHistoryDetailPagerRefetchQuery'").fetchone()
        self.assertEqual(job['state'], 'superseded')

        issue = self.store.db.execute("SELECT * FROM issues WHERE response_id=?", (rid,)).fetchone()
        self.assertEqual(issue['code'], 'SUPERSEDED_RESPONSE')
        self.assertEqual(issue['run_id'], 1)
        self.assertEqual(issue['created_at'], '2026-09-05T00:00:00Z')
        self.assertEqual(json.loads(issue['context'])['reason'], 'superseded_pager')

    def test_manifest_unpersisted_handling(self):
        """manifest未保存の場合、query_idありでは保守的にスキップし、query_idなし（人工/インポート）では既存意味を維持すること。"""
        # manifests を空にする
        self.store.db.execute("DELETE FROM manifests")
        self.store.db.commit()

        # 1. query_id ありの job -> スキップされる
        rid_str1 = encoded('VsHistoryDetail-u-m1:REGULAR:20260901T010101_x')
        v1 = {'vsResultId': rid_str1}
        e1 = make_response('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': None}}, variables=v1, query_id='some-query-id')
        rid1 = self.store.record(e1)
        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'VsHistoryDetailQuery', js(v1), 'vs', 'u-m1:20260901T010101_x', 'retry', 1, 777.0, rid1)
        )

        # 2. query_id なし（人工/インポート）の job -> 既存意味を維持して unavailable に移行
        rid_str2 = encoded('VsHistoryDetail-u-m2:REGULAR:20260901T010101_y')
        v2 = {'vsResultId': rid_str2}
        e2 = make_response('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': None}}, variables=v2, query_id=None)
        rid2 = self.store.record(e2)
        self.store.db.execute(
            "INSERT INTO jobs(account,operation,variables_json,kind,match_key,state,attempts,next_attempt,last_response_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ('account-a', 'VsHistoryDetailQuery', js(v2), 'vs', 'u-m2:20260901T010101_y', 'retry', 1, 888.0, rid2)
        )
        self.store.db.commit()

        self.store._repair_job_outcomes()

        # query_id ありはスキップされ不変
        j1 = self.store.db.execute("SELECT * FROM jobs WHERE match_key='u-m1:20260901T010101_x'").fetchone()
        self.assertEqual(j1['state'], 'retry')
        self.assertEqual(j1['next_attempt'], 777.0)

        # query_id なしは unavailable へ移行
        j2 = self.store.db.execute("SELECT * FROM jobs WHERE match_key='u-m2:20260901T010101_y'").fetchone()
        self.assertEqual(j2['state'], 'unavailable')
        self.assertNotEqual(j2['next_attempt'], 888.0)
