import base64, io, json, os, plistlib, sqlite3, sys, tempfile, unittest, uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/python'))
sys.path.insert(0, str(ROOT))

from ikarchive.store import Store, js, now
from ikarchive.planner import Planner

MANIFEST = json.loads((ROOT / 'config/query-catalog.snapshot.json').read_text())

def response(op, body, account='account-a', variables=None, status=200):
    raw = body if isinstance(body, bytes) else js(body).encode()
    return {
        'event_id': str(uuid.uuid4()),
        'account': account,
        'fetched_at': now(),
        'operation': op,
        'variables': variables or {},
        'status': status,
        'body_base64': base64.b64encode(raw).decode()
    }

class PortabilityTests(unittest.TestCase):
    def test_replay_code_normalization_and_id_routes(self):
        p = Planner(MANIFEST)
        # 1. Real Replay type replayCode='RABC-1234-5678-9XYZ' -> code='RABC123456789XYZ'
        obj = {'replayCode': 'RABC-1234-5678-9XYZ'}
        routes = list(p.related('Replay', obj, 'JP'))
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0][0], 'DownloadSearchReplayQuery')
        self.assertEqual(routes[0][1]['code'], 'RABC123456789XYZ')

        # Hyphens and whitespace stripping + uppercase
        obj_messy = {'replayCode': '  rabc - 1234 - 5678 - 9xyz \t'}
        routes_messy = list(p.related('Replay', obj_messy, 'JP'))
        self.assertEqual(routes_messy, [('DownloadSearchReplayQuery', {'code': 'RABC123456789XYZ'})])

        # Empty / whitespace-only / hyphens-only replayCode must not queue
        self.assertEqual(list(p.related('Replay', {'replayCode': ''}, 'JP')), [])
        self.assertEqual(list(p.related('Replay', {'replayCode': '   -- -  '}, 'JP')), [])
        self.assertEqual(list(p.related('Replay', {}, 'JP')), [])
        self.assertEqual(list(p.related('Replay', {'replayCode': None}, 'JP')), [])

        # Verify id-related routes are preserved
        sideorder = list(p.related('SideOrderTryResult', {'id': 'side-test-id'}, 'JP'))
        self.assertIn(('SideOrderChallengeDetailQuery', {'tryResultId': 'side-test-id'}), sideorder)
        self.assertIn('SideOrderChallengeDetailPointContainerPaginationQuery', [n for n, v in sideorder])

    def test_image_entity_asset_collection_minimal_manifest(self):
        # Minimal manifest: portrait.url with no image/photo keys in the path
        minimal_manifest = {
            'queries': {
                'CustomPortraitQuery': {
                    'params': {'operationKind': 'query', 'id': 'custom-portrait-query'},
                    'operation': {
                        'argumentDefinitions': [],
                        'selections': [
                            {
                                'kind': 'LinkedField',
                                'name': 'portrait',
                                'alias': None,
                                'concreteType': 'Image',
                                'selections': [
                                    {'kind': 'ScalarField', 'name': 'url', 'alias': None}
                                ]
                            }
                        ]
                    }
                }
            }
        }
        planner = Planner(minimal_manifest)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / 'test.sqlite')
            try:
                target_url = 'https://example.com/portraits/sample.png'
                data = {'portrait': {'url': target_url}}
                e = response('CustomPortraitQuery', {'data': data})
                rid = store.record(e)
                store.project(rid, planner)

                # Verify url in assets table
                asset_row = store.db.execute('SELECT * FROM assets WHERE url=?', (target_url,)).fetchone()
                self.assertIsNotNone(asset_row)
                self.assertEqual(asset_row['state'], 'pending')

                # Verify asset_refs
                ref_rows = store.db.execute('SELECT * FROM asset_refs WHERE url=?', (target_url,)).fetchall()
                self.assertEqual(len(ref_rows), 1)
                self.assertEqual(ref_rows[0]['response_id'], rid)
                self.assertEqual(json.loads(ref_rows[0]['path']), ['portrait', 'url'])

                # Verify that Image without id does not insert into entities
                entity_count = store.db.execute('SELECT count(*) FROM entities').fetchone()[0]
                self.assertEqual(entity_count, 0)

                # Negative test: non-https url is not inserted into assets
                http_url = 'http://example.com/portraits/insecure.png'
                e2 = response('CustomPortraitQuery', {'data': {'portrait': {'url': http_url}}})
                rid2 = store.record(e2)
                store.project(rid2, planner)
                self.assertIsNone(store.db.execute('SELECT * FROM assets WHERE url=?', (http_url,)).fetchone())
            finally:
                store.close()

    def test_launchagent_environment_inheritance_mock(self):
        import archive
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db = Path(tmpdir) / 'archive.sqlite3'
            temp_output = Path(tmpdir) / 'data'
            mock_env = {
                'NXAPI_DATA_PATH': '/custom/nxapi/data',
                'SECRET_ACCESS_TOKEN': 'super-secret-token',
                'AWS_SECRET_KEY': 'dont-leak-me',
            }

            with patch('sys.argv', ['archive.py', '--db', str(temp_db), 'install-service']), \
                 patch.object(archive, 'OUTPUT', temp_output), \
                 patch('subprocess.run') as mock_run, \
                 patch('pathlib.Path.write_bytes') as mock_write, \
                 patch('shutil.copy2'), \
                 patch.dict(os.environ, mock_env, clear=False), \
                 redirect_stdout(io.StringIO()):
                archive.main()

            self.assertTrue(mock_write.called)
            payload = plistlib.loads(mock_write.call_args[0][0])
            env_vars = payload.get('EnvironmentVariables', {})

            # IKARING_ARCHIVE_DATA_DIR must match str(OUTPUT.resolve())
            self.assertEqual(env_vars.get('IKARING_ARCHIVE_DATA_DIR'), str(temp_output.resolve()))

            # NXAPI_DATA_PATH must be inherited from parent environment
            self.assertEqual(env_vars.get('NXAPI_DATA_PATH'), '/custom/nxapi/data')

            # Unrelated / secret variables must NOT be copied
            self.assertNotIn('SECRET_ACCESS_TOKEN', env_vars)
            self.assertNotIn('AWS_SECRET_KEY', env_vars)

            # Ensure launchctl was NOT actually executed (mocked)
            self.assertTrue(mock_run.called)
            for call in mock_run.call_args_list:
                args = call[0][0]
                self.assertEqual(args[0], 'launchctl')

        # Test case where NXAPI_DATA_PATH is absent in parent environment
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db = Path(tmpdir) / 'archive.sqlite3'
            temp_output = Path(tmpdir) / 'data'

            env_clean = os.environ.copy()
            env_clean.pop('NXAPI_DATA_PATH', None)
            with patch('sys.argv', ['archive.py', '--db', str(temp_db), 'install-service']), \
                 patch.object(archive, 'OUTPUT', temp_output), \
                 patch('subprocess.run'), \
                 patch('pathlib.Path.write_bytes') as mock_write_no_nxapi, \
                 patch('shutil.copy2'), \
                 patch.dict(os.environ, env_clean, clear=True), \
                 redirect_stdout(io.StringIO()):
                archive.main()

            self.assertTrue(mock_write_no_nxapi.called)
            payload2 = plistlib.loads(mock_write_no_nxapi.call_args[0][0])
            env_vars2 = payload2.get('EnvironmentVariables', {})
            self.assertEqual(env_vars2.get('IKARING_ARCHIVE_DATA_DIR'), str(temp_output.resolve()))
            self.assertNotIn('NXAPI_DATA_PATH', env_vars2)

if __name__ == '__main__':
    unittest.main()
