import base64, json, re, tempfile, unittest, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/python'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ikarchive.store import Store, now, js
from ikarchive.planner import Planner
import archive

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / 'config/query-catalog.snapshot.json').read_text())

def encoded(text):
    return base64.b64encode(text.encode()).decode()

def vs_id(mode, suffix):
    return encoded(f'VsHistoryDetail-u-analysis:{mode}:20260922T010101_{suffix}')

def coop_id(suffix):
    return encoded(f'CoopHistoryDetail-u-analysis:{suffix}')

def team(count):
    return {'players': [{'id': f'p{i}'} for i in range(count)]}

def vs_detail(mode, suffix, counts=(4, 4), bankara=None, rule='AREA'):
    mine, *others = counts
    detail = {
        'id': vs_id(mode, suffix),
        'vsMode': {'mode': mode},
        'vsRule': {'rule': rule, 'name': rule},
        'myTeam': team(mine),
        'otherTeams': [team(n) for n in others],
        'judgement': 'WIN',
        'playedTime': '2026-09-22T01:01:01Z',
    }
    if bankara:
        detail['bankaraMatch'] = {'mode': bankara}
    return detail

def response(op, body, account='account-a'):
    raw = js(body).encode()
    return {
        'event_id': encoded(op + body['data'][next(iter(body['data']))]['id']),
        'account': account,
        'fetched_at': now(),
        'operation': op,
        'variables': {},
        'status': 200,
        'body_base64': base64.b64encode(raw).decode(),
    }

class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'a.sqlite')
        self.planner = Planner(MANIFEST)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ingest(self, op, body):
        event = response(op, body)
        rid = self.store.record(event)
        self.store.project(rid, self.planner)
        return rid

    def sets(self):
        return {row['analysis_set'] for row in self.store.db.execute('SELECT analysis_set FROM match_classification')}

    def test_public_modes_stay_together_when_roster_is_short(self):
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('REGULAR', 'nav', (4, 3), rule='TURF_WAR')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('BANKARA', 'open', (4, 4), bankara='OPEN', rule='AREA')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('BANKARA', 'challenge', (3, 4), bankara='CHALLENGE', rule='LOFT')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('X_MATCH', 'x', (4, 4), rule='GOAL')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('LEAGUE', 'event', (4, 4), rule='CLAM')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('FEST', 'fest', (4, 2, 2), rule='TURF_WAR')}})
        self.assertEqual(self.sets(), {'nawabari', 'bankara_open', 'bankara_challenge', 'xmatch', 'event', 'fest'})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM analysis_nawabari').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM analysis_fest').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM analysis_nawabari WHERE rule_raw!='TURF_WAR'").fetchone()[0], 0)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='analysis_nawabari_one_vs_one'").fetchone())

    def test_only_private_splits_by_roster_and_tags_do_not_move_it(self):
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('PRIVATE', 'p4', (4, 4), rule='AREA')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('PRIVATE', 'p3', (3, 3), rule='AREA')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('PRIVATE', 'p2', (2, 2), rule='LOFT')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('PRIVATE', 'p1', (1, 1), rule='AREA')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('PRIVATE', 'px', (4, 3), rule='AREA')}})
        self.assertEqual(self.sets(), {'private_four_vs_four', 'private_three_vs_three', 'private_two_vs_two', 'private_one_vs_one', 'private_other'})
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM match_tags WHERE tag='イカップル'").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM analysis_bankara_open').fetchone()[0], 0)
        key = self.store.db.execute("SELECT match_key FROM match_classification WHERE analysis_set='private_four_vs_four'").fetchone()[0]
        archive.apply_tag(self.store, type('A', (), {'tag_action': 'add', 'account': 'account-a', 'match_key': key, 'tag': '対抗戦', 'note': None})())
        self.assertEqual(self.store.db.execute("SELECT analysis_set FROM match_classification WHERE match_key=?", (key,)).fetchone()[0], 'private_four_vs_four')
        tagged = self.store.db.execute('SELECT tag FROM analysis_private_four_vs_four_tags').fetchone()[0]
        self.assertEqual(tagged, '対抗戦')

    def test_salmon_special_modes_are_separate(self):
        for rule, suffix in (('REGULAR', 'regular'), ('BIG_RUN', 'big'), ('TEAM_CONTEST', 'team')):
            detail = {'id': coop_id(suffix), 'rule': rule, 'playedTime': '2026-09-22T01:01:01Z', 'dangerRate': 0}
            self.ingest('CoopHistoryDetailQuery', {'data': {'coopHistoryDetail': detail}})
        self.assertEqual(self.sets(), {'salmon_regular', 'big_run', 'team_contest'})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM analysis_big_run').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM analysis_team_contest').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM analysis_salmon_regular').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM analysis_fest').fetchone()[0], 0)

    def test_public_fixture_open_is_not_private(self):
        body = json.loads((ROOT / 'tests/fixtures/public/VsHistoryDetailQuery.json').read_text())
        self.ingest('VsHistoryDetailQuery', body)
        genres = {row[0] for row in self.store.db.execute('SELECT genre FROM match_classification')}
        self.assertIn('bankara_open', genres)
        self.assertNotIn('private', genres)

    def test_reauth_rewalks_history_without_duplicating_the_match(self):
        body = {'data': {'vsHistoryDetail': vs_detail('PRIVATE', 'dup', (2, 2))}}
        self.ingest('VsHistoryDetailQuery', body)
        again = response('VsHistoryDetailQuery', body)
        again['event_id'] = encoded('second-identical-body')
        rid = self.store.record(again)
        self.store.project(rid, self.planner)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM responses').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM matches').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT analysis_set FROM match_classification").fetchone()[0], 'private_two_vs_two')
        self.store.queue('account-a', 'PrivateBattleHistoriesQuery', {})
        self.store.db.execute("UPDATE jobs SET state='done' WHERE operation='PrivateBattleHistoriesQuery'")
        self.store.db.commit()
        expiry = (datetime.now(timezone.utc) + timedelta(days=700)).timestamp() * 1000
        self.store.remember_auth({'session_iat': 100, 'session_expires_at': expiry})
        self.store.remember_auth_failure('SESSION_EXPIRED')
        self.store.remember_auth({'session_iat': 200, 'session_expires_at': expiry})
        self.assertTrue(self.store.auth_status()['backfill_armed'])
        self.assertGreaterEqual(self.store.apply_backfill('account-a'), 1)
        self.assertEqual(self.store.db.execute("SELECT state FROM jobs WHERE operation='PrivateBattleHistoriesQuery'").fetchone()[0], 'pending')
        self.assertEqual(self.store.apply_backfill('account-a'), 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM matches').fetchone()[0], 1)

    def test_xlsx_separates_genres_and_does_not_store_raw_json(self):
        from ikarchive.xlsx_export import export_xlsx
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('BANKARA', 'openxlsx', (4, 4), bankara='OPEN', rule='AREA')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('X_MATCH', 'xxlsx', (4, 4), rule='GOAL')}})
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': vs_detail('PRIVATE', 'pairxlsx', (2, 2), rule='LOFT')}})
        path = Path(self.tmp.name) / '分析.xlsx'
        result = export_xlsx(self.store, path)
        self.assertFalse(result['edits_return_to_database'])
        self.assertEqual(result['canonical'], 'sqlite')
        self.assertEqual(result['rows']['オープン'], 1)
        self.assertEqual(result['rows']['Xマッチ'], 1)
        self.assertEqual(result['rows']['プラベ2対2'], 1)
        with zipfile.ZipFile(path) as book:
            workbook = book.read('xl/workbook.xml').decode()
            names = [part.split('"', 1)[0] for part in workbook.split('name="')[1:]]
            texts = {name: book.read(f'xl/worksheets/sheet{index}.xml').decode() for index, name in enumerate(names, 1)}
        self.assertIn('openxlsx', texts['オープン'])
        self.assertNotIn('openxlsx', texts['Xマッチ'])
        self.assertIn('xxlsx', texts['Xマッチ'])
        self.assertNotIn('xxlsx', texts['オープン'])
        self.assertIn('pairxlsx', texts['プラベ2対2'])
        self.assertNotIn('pairxlsx', texts['プラベ4対4'])
        self.assertNotIn('イカップル', texts['プラベ2対2'])
        for xml in texts.values():
            self.assertNotIn('json_text', xml)
            for text in re.findall(r'<t xml:space="preserve">(.*?)</t>', xml):
                self.assertLess(len(text), 32767)

    def test_session_failure_is_visible_and_cleared_by_later_success(self):
        later = (datetime.now(timezone.utc) + timedelta(days=700)).timestamp() * 1000
        bullet = (datetime.now(timezone.utc) + timedelta(hours=2)).timestamp() * 1000
        self.store.remember_auth({'session_expires_at': later, 'bullet_expires_at': bullet})
        status = self.store.auth_status()
        self.assertFalse(status['reauth_required'])
        self.assertFalse(status['session_expires_soon'])
        self.assertIsNotNone(status['session_expires_at'])
        self.store.remember_auth_failure('SESSION_EXPIRED')
        self.assertTrue(self.store.auth_status()['reauth_required'])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM issues WHERE code='AUTH_INCIDENT'").fetchone()[0], 1)
        soon = (datetime.now(timezone.utc) + timedelta(days=3)).timestamp() * 1000
        self.store.remember_auth({'session_expires_at': soon, 'bullet_expires_at': bullet})
        status = self.store.auth_status()
        self.assertFalse(status['reauth_required'])
        self.assertTrue(status['session_expires_soon'])

if __name__ == '__main__':
    unittest.main()
