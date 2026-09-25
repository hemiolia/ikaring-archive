import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from test_archive import Store, MANIFEST, response
from ikarchive.collector import sync
from ikarchive.publish import publish_outputs

class AutomaticSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'database'/'archive.sqlite3')
    def tearDown(self):
        self.store.close();self.tmp.cleanup()
    def test_long_crawl_refreshes_live_history_without_looping_identical_body(self):
        manifest={**MANIFEST,'queries':{k:MANIFEST['queries'][k] for k in ('RegularBattleHistoriesQuery','HistoryRecordQuery')},'expected':2}
        seen=[];store=self.store
        class Bridge:
            def call(self,command,**kw):
                if command=='init':return {'account':'account-a'}
                op=kw['operation'];seen.append(op)
                body={'data':{'regularBattleHistories':{'historyGroups':{'nodes':[]}}}} if op=='RegularBattleHistoriesQuery' else {'data':{'playHistory':{}}}
                e=response(op,body,variables=kw['variables']);p=store.output_root/'spool'/(e['event_id']+'.json');p.write_text(json.dumps(e));return {'spool_file':str(p)}
            def close(self):pass
        with patch('ikarchive.collector.catalog',return_value=manifest),patch('ikarchive.collector.Bridge',Bridge),patch('ikarchive.collector.fetch_assets',return_value=0),patch('ikarchive.collector.time.monotonic',side_effect=[0,0,0,121,121,121]):
            result=sync(store,budget=10,delay=0)
        self.assertEqual(seen,['RegularBattleHistoriesQuery','HistoryRecordQuery','RegularBattleHistoriesQuery'])
        self.assertEqual(result['requests'],3)
        self.assertEqual(store.db.execute('SELECT count(*) FROM responses').fetchone()[0],2)
    def test_outputs_are_regenerated_and_export_failure_is_visible(self):
        result=publish_outputs(self.store)
        self.assertNotIn('error',result)
        page=self.store.output_root/'exports/gui/index.html'
        book=self.store.output_root/'exports/分析.xlsx'
        self.assertTrue(page.is_file());self.assertTrue(book.is_file())
        old=page.read_bytes()
        self.assertIsNotNone(self.store._control('exports_updated_at'))
        with patch('ikarchive.publish.write_gui',side_effect=OSError('test')):
            failed=publish_outputs(self.store)
        self.assertEqual(failed,{'error':'OSError'})
        self.assertEqual(self.store.sync_health()['export_error'],'OSError')
        self.assertEqual(page.read_bytes(),old)
        publish_outputs(self.store)
        self.assertIsNone(self.store._control('export_error'))
    def test_missing_history_is_never_reported_current(self):
        health=self.store.sync_health()
        self.assertEqual(health['state'],'delayed')
        self.assertEqual(len(health['histories']),7)
        self.assertTrue(all(h['stale'] for h in health['histories']))
