import base64, json, tempfile, unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/python'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ikarchive.store import Store, now, js
from ikarchive.planner import Planner
from ikarchive.xlsx_export import _vs_rows, export_xlsx, VS_HEADERS

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / 'config/query-catalog.snapshot.json').read_text())

def encoded(text):
    return base64.b64encode(text.encode()).decode()

def vs_id(mode, suffix):
    return encoded(f'VsHistoryDetail-u-analysis:{mode}:20260922T010101_{suffix}')

def make_player(player_id, is_myself=False, weapon_name='スプラシューター', kill=3, assist=1, death=2, special=2, paint=1000, result_none=False):
    p = {
        'id': player_id,
        'name': f'Player_{player_id}',
        'isMyself': is_myself,
        'paint': paint,
    }
    if weapon_name is not None:
        p['weapon'] = {'name': weapon_name}
    if result_none:
        p['result'] = None
    elif kill is not None or assist is not None or death is not None or special is not None:
        p['result'] = {
            'kill': kill,
            'assist': assist,
            'death': death,
            'special': special,
        }
    return p

def legacy_vs_rows(db, view):
    sql = f'''SELECT a.played_time,a.rule_name,a.rule_raw,a.stage,a.judgement,a.knockout,a.duration,a.my_player_count,a.opponent_counts,
        (SELECT p.weapon FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT p.kills FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT p.assists FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT p.deaths FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT p.specials FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT p.paint FROM battle_players p WHERE p.account=a.account AND p.match_key=a.match_key AND p.is_myself=1 LIMIT 1),
        (SELECT group_concat(t.tag,'、') FROM match_tags t WHERE t.account=a.account AND t.match_key=a.match_key),
        a.match_key
        FROM {view} a ORDER BY a.played_time,a.match_key'''
    return [VS_HEADERS] + [tuple(row) for row in db.execute(sql)]

class XlsxProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'projection.sqlite')
        self.planner = Planner(MANIFEST)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ingest(self, op, body, account='account-a'):
        raw = js(body).encode()
        detail_root = body['data'][next(iter(body['data']))]
        ident = detail_root.get('id', 'dummy_id')
        event = {
            'event_id': encoded(f'{op}:{account}:{ident}'),
            'account': account,
            'fetched_at': now(),
            'operation': op,
            'variables': {},
            'status': 200,
            'body_base64': base64.b64encode(raw).decode(),
        }
        rid = self.store.record(event)
        self.store.project(rid, self.planner)
        return rid

    def test_public_fixture_projection_and_equivalence(self):
        fixture_path = ROOT / 'tests/fixtures/public/VsHistoryDetailQuery.json'
        fixture_data = json.loads(fixture_path.read_text(encoding='utf-8'))
        self.ingest('VsHistoryDetailQuery', fixture_data)

        discon_path = ROOT / 'tests/fixtures/public/VsHistoryDetailQueryDisconnection.json'
        discon_data = json.loads(discon_path.read_text(encoding='utf-8'))
        self.ingest('VsHistoryDetailQuery', discon_data)

        views = [r[0] for r in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name LIKE 'analysis_%' AND name NOT LIKE '%_by_rule' AND name NOT LIKE '%_tags'"
        )]
        for view in views:
            count = self.store.db.execute(f'SELECT count(*) FROM {view}').fetchone()[0]
            if count > 0:
                legacy = legacy_vs_rows(self.store.db, view)
                new_rows = _vs_rows(self.store.db, view)
                self.assertEqual(legacy, new_rows, f'Mismatch in view: {view}')

    def test_synthetic_cases_legacy_equivalence(self):
        # 1. result null
        d1 = {
            'id': vs_id('BANKARA', 'result_null'),
            'vsMode': {'mode': 'BANKARA'},
            'vsRule': {'rule': 'AREA', 'name': 'ガチエリア'},
            'vsStage': {'name': 'ヤガラ市場'},
            'judgement': 'WIN',
            'knockout': 'WIN',
            'duration': 300,
            'playedTime': '2026-09-22T01:00:00Z',
            'bankaraMatch': {'mode': 'OPEN'},
            'myTeam': {'players': [make_player('p1', is_myself=True, weapon_name='わかばシューター', result_none=True, paint=850)]},
            'otherTeams': [{'players': [make_player('p2', is_myself=False)]}],
        }
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': d1}})

        # 2. 本人不在
        d2 = {
            'id': vs_id('BANKARA', 'no_myself'),
            'vsMode': {'mode': 'BANKARA'},
            'vsRule': {'rule': 'LOFT', 'name': 'ガチヤグラ'},
            'vsStage': {'name': 'マテガイ放水路'},
            'judgement': 'LOSE',
            'knockout': 'NEITHER',
            'duration': 300,
            'playedTime': '2026-09-22T01:05:00Z',
            'bankaraMatch': {'mode': 'OPEN'},
            'myTeam': {'players': [make_player('p3', is_myself=False), make_player('p4', is_myself=False)]},
            'otherTeams': [{'players': [make_player('p5', is_myself=False)]}],
        }
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': d2}})

        # 3. ゼロ成績
        d3 = {
            'id': vs_id('BANKARA', 'zero_stats'),
            'vsMode': {'mode': 'BANKARA'},
            'vsRule': {'rule': 'GOAL', 'name': 'ガチホコバトル'},
            'vsStage': {'name': 'ナメロウ金属'},
            'judgement': 'LOSE',
            'knockout': 'LOSE',
            'duration': 120,
            'playedTime': '2026-09-22T01:10:00Z',
            'bankaraMatch': {'mode': 'OPEN'},
            'myTeam': {'players': [make_player('p6', is_myself=True, weapon_name='スクリュースロッシャー', kill=0, assist=0, death=0, special=0, paint=0)]},
            'otherTeams': [{'players': [make_player('p7', is_myself=False)]}],
        }
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': d3}})

        # 4. 本人別team (otherTeamsに本人が存在)
        d4 = {
            'id': vs_id('BANKARA', 'other_team_myself'),
            'vsMode': {'mode': 'BANKARA'},
            'vsRule': {'rule': 'CLAM', 'name': 'ガチアサリ'},
            'vsStage': {'name': 'チョウザメ造船'},
            'judgement': 'DRAW',
            'knockout': 'NEITHER',
            'duration': 250,
            'playedTime': '2026-09-22T01:15:00Z',
            'bankaraMatch': {'mode': 'OPEN'},
            'myTeam': {'players': [make_player('p8', is_myself=False)]},
            'otherTeams': [{'players': [make_player('p9', is_myself=True, weapon_name='リッター4K', kill=12, assist=4, death=1, special=3, paint=1200)]}],
        }
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': d4}})

        # 5. isMyself が整数 1
        d5 = {
            'id': vs_id('BANKARA', 'int_is_myself'),
            'vsMode': {'mode': 'BANKARA'},
            'vsRule': {'rule': 'AREA', 'name': 'ガチエリア'},
            'vsStage': {'name': 'ユノハナ大渓谷'},
            'judgement': 'WIN',
            'knockout': 'WIN',
            'duration': 180,
            'playedTime': '2026-09-22T01:20:00Z',
            'bankaraMatch': {'mode': 'OPEN'},
            'myTeam': {'players': [make_player('p10', is_myself=1, weapon_name='シャープマーカー', kill=5, assist=2, death=3, special=1, paint=900)]},
            'otherTeams': [{'players': [make_player('p11', is_myself=0)]}],
        }
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': d5}})

        # 6. タグ（複数タグとタグなし）
        match_keys = [r[0] for r in self.store.db.execute("SELECT match_key FROM analysis_bankara_open ORDER BY played_time")]
        self.store.db.execute("INSERT INTO match_tags(account,match_key,tag,created_at,updated_at) VALUES('account-a',?,'ガチ','2026-09-22T00:00:00Z','2026-09-22T00:00:00Z')", (match_keys[0],))
        self.store.db.execute("INSERT INTO match_tags(account,match_key,tag,created_at,updated_at) VALUES('account-a',?,'武器練','2026-09-22T00:00:00Z','2026-09-22T00:00:00Z')", (match_keys[0],))
        self.store.db.execute("INSERT INTO match_tags(account,match_key,tag,created_at,updated_at) VALUES('account-a',?,'単一タグ','2026-09-22T00:00:00Z','2026-09-22T00:00:00Z')", (match_keys[1],))
        self.store.db.commit()

        legacy = legacy_vs_rows(self.store.db, 'analysis_bankara_open')
        new_rows = _vs_rows(self.store.db, 'analysis_bankara_open')

        self.assertEqual(legacy, new_rows)
        self.assertEqual(len(new_rows), 6)  # ヘッダー + 5試合

        # ゼロ成績が None ではなく 0 であることの確認
        zero_row = next(r for r in new_rows[1:] if r[16] == match_keys[2])
        self.assertEqual(zero_row[9], 'スクリュースロッシャー')
        self.assertEqual(zero_row[10], 0)
        self.assertEqual(zero_row[11], 0)
        self.assertEqual(zero_row[12], 0)
        self.assertEqual(zero_row[13], 0)
        self.assertEqual(zero_row[14], 0)

        # 本人不在行の全成績が None であることの確認
        no_myself_row = next(r for r in new_rows[1:] if r[16] == match_keys[1])
        self.assertIsNone(no_myself_row[9])
        self.assertIsNone(no_myself_row[10])
        self.assertIsNone(no_myself_row[11])
        self.assertIsNone(no_myself_row[12])
        self.assertIsNone(no_myself_row[13])
        self.assertIsNone(no_myself_row[14])

        # result_null行の武器・塗りはあり、成績が None であることの確認
        res_null_row = next(r for r in new_rows[1:] if r[16] == match_keys[0])
        self.assertEqual(res_null_row[9], 'わかばシューター')
        self.assertIsNone(res_null_row[10])
        self.assertIsNone(res_null_row[11])
        self.assertIsNone(res_null_row[12])
        self.assertIsNone(res_null_row[13])
        self.assertEqual(res_null_row[14], 850)
        self.assertEqual(res_null_row[15], 'ガチ、武器練')

    def test_same_match_key_different_accounts(self):
        # 同一 match_key を持つ対戦を2つのアカウントで取り込む
        detail_a = {
            'id': vs_id('BANKARA', 'shared_key'),
            'vsMode': {'mode': 'BANKARA'},
            'vsRule': {'rule': 'AREA', 'name': 'ガチエリア'},
            'vsStage': {'name': 'ゴンベエ海峡'},
            'judgement': 'WIN',
            'knockout': 'WIN',
            'duration': 200,
            'playedTime': '2026-09-22T02:00:00Z',
            'bankaraMatch': {'mode': 'OPEN'},
            'myTeam': {'players': [make_player('p_acc_a', is_myself=True, weapon_name='スプラシューター', kill=7, paint=1100)]},
            'otherTeams': [{'players': [make_player('p_other_a', is_myself=False)]}],
        }
        detail_b = {
            'id': vs_id('BANKARA', 'shared_key'),
            'vsMode': {'mode': 'BANKARA'},
            'vsRule': {'rule': 'AREA', 'name': 'ガチエリア'},
            'vsStage': {'name': 'ゴンベエ海峡'},
            'judgement': 'WIN',
            'knockout': 'WIN',
            'duration': 200,
            'playedTime': '2026-09-22T02:00:00Z',
            'bankaraMatch': {'mode': 'OPEN'},
            'myTeam': {'players': [make_player('p_acc_b', is_myself=True, weapon_name='.52ガロン', kill=10, paint=1400)]},
            'otherTeams': [{'players': [make_player('p_other_b', is_myself=False)]}],
        }
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': detail_a}}, account='account-1')
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': detail_b}}, account='account-2')

        shared_match_key = self.store.db.execute("SELECT match_key FROM matches WHERE account='account-1'").fetchone()[0]
        self.store.db.execute("INSERT INTO match_tags(account,match_key,tag,created_at,updated_at) VALUES('account-1',?,'タグ1','2026-09-22T00:00:00Z','2026-09-22T00:00:00Z')", (shared_match_key,))
        self.store.db.execute("INSERT INTO match_tags(account,match_key,tag,created_at,updated_at) VALUES('account-2',?,'タグ2','2026-09-22T00:00:00Z','2026-09-22T00:00:00Z')", (shared_match_key,))
        self.store.db.commit()

        legacy = legacy_vs_rows(self.store.db, 'analysis_bankara_open')
        new_rows = _vs_rows(self.store.db, 'analysis_bankara_open')
        self.assertEqual(legacy, new_rows)
        self.assertEqual(len(new_rows), 3)  # ヘッダー + 2アカウント分の2行

        row_1 = [r for r in new_rows[1:] if r[9] == 'スプラシューター'][0]
        row_2 = [r for r in new_rows[1:] if r[9] == '.52ガロン'][0]
        self.assertEqual(row_1[15], 'タグ1')
        self.assertEqual(row_2[15], 'タグ2')
        self.assertEqual(row_1[10], 7)
        self.assertEqual(row_2[10], 10)

    def test_select_count_bounded_by_trace_callback(self):
        # 15試合を取り込む
        for i in range(15):
            d = {
                'id': vs_id('BANKARA', f'bulk_{i:02d}'),
                'vsMode': {'mode': 'BANKARA'},
                'vsRule': {'rule': 'AREA', 'name': 'ガチエリア'},
                'vsStage': {'name': '海女美術大学'},
                'judgement': 'WIN' if i % 2 == 0 else 'LOSE',
                'knockout': 'WIN' if i % 2 == 0 else 'LOSE',
                'duration': 180 + i,
                'playedTime': f'2026-09-22T03:{i:02d}:00Z',
                'bankaraMatch': {'mode': 'OPEN'},
                'myTeam': {'players': [make_player(f'bulk_p_{i}', is_myself=True, kill=i)]},
                'otherTeams': [{'players': [make_player(f'bulk_o_{i}', is_myself=False)]}],
            }
            self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': d}})

        queries = []
        def trace(statement):
            stripped = statement.strip().upper()
            if stripped.startswith('SELECT'):
                queries.append(statement)

        self.store.db.set_trace_callback(trace)
        try:
            rows = _vs_rows(self.store.db, 'analysis_bankara_open')
        finally:
            self.store.db.set_trace_callback(None)

        self.assertEqual(len(rows), 16)  # ヘッダー + 15試合
        # SELECT発行が試合数(15)に比例せず、最大2本（tags map取得 1本 + 試合&JSON取得 1本）であること
        self.assertLessEqual(len(queries), 2, f'Expected <= 2 SELECT queries, but got {len(queries)}: {queries}')

    def test_vs_headers_and_row_schema_integrity(self):
        self.assertEqual(VS_HEADERS, ('対戦日時','ルール','ルールコード','ステージ','勝敗','ノックアウト','試合秒','自分側人数','相手人数','ブキ','キル','アシスト','デス','スペシャル','塗り','タグ','試合ID'))
        d = {
            'id': vs_id('BANKARA', 'schema_check'),
            'vsMode': {'mode': 'BANKARA'},
            'vsRule': {'rule': 'AREA', 'name': 'ガチエリア'},
            'vsStage': {'name': 'タカアシ経済特区'},
            'judgement': 'WIN',
            'knockout': 'WIN',
            'duration': 300,
            'playedTime': '2026-09-22T04:00:00Z',
            'bankaraMatch': {'mode': 'OPEN'},
            'myTeam': {'players': [make_player('sc_p1', is_myself=True, weapon_name='バケットスロッシャー', kill=6, assist=2, death=1, special=2, paint=1050)]},
            'otherTeams': [{'players': [make_player('sc_p2', is_myself=False)]}],
        }
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': d}})
        rows = _vs_rows(self.store.db, 'analysis_bankara_open')
        self.assertEqual(rows[0], VS_HEADERS)
        self.assertEqual(len(rows[1]), len(VS_HEADERS))
        self.assertEqual(rows[1][0], '2026-09-22T04:00:00Z')
        self.assertEqual(rows[1][1], 'ガチエリア')
        self.assertEqual(rows[1][2], 'AREA')
        self.assertEqual(rows[1][3], 'タカアシ経済特区')
        self.assertEqual(rows[1][4], 'WIN')
        self.assertEqual(rows[1][5], 'WIN')
        self.assertEqual(rows[1][6], 300)
        self.assertEqual(rows[1][7], 1)
        self.assertEqual(rows[1][8], '[1]')
        self.assertEqual(rows[1][9], 'バケットスロッシャー')
        self.assertEqual(rows[1][10], 6)
        self.assertEqual(rows[1][11], 2)
        self.assertEqual(rows[1][12], 1)
        self.assertEqual(rows[1][13], 2)
        self.assertEqual(rows[1][14], 1050)
        self.assertIsNone(rows[1][15])
        self.assertTrue(len(rows[1][16]) > 0)

    def test_export_xlsx_compatibility(self):
        d = {
            'id': vs_id('BANKARA', 'export_test'),
            'vsMode': {'mode': 'BANKARA'},
            'vsRule': {'rule': 'AREA', 'name': 'ガチエリア'},
            'vsStage': {'name': 'スメーシーワールド'},
            'judgement': 'WIN',
            'knockout': 'WIN',
            'duration': 290,
            'playedTime': '2026-09-22T05:00:00Z',
            'bankaraMatch': {'mode': 'OPEN'},
            'myTeam': {'players': [make_player('ep_p1', is_myself=True, kill=8, paint=1300)]},
            'otherTeams': [{'players': [make_player('ep_p2', is_myself=False)]}],
        }
        self.ingest('VsHistoryDetailQuery', {'data': {'vsHistoryDetail': d}})
        xlsx_path = Path(self.tmp.name) / 'test_out.xlsx'
        result = export_xlsx(self.store, xlsx_path)
        self.assertTrue(xlsx_path.is_file())
        self.assertGreaterEqual(result['rows']['バンカラマッチ（オープン）'], 1)

if __name__ == '__main__':
    unittest.main()
