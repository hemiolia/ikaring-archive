import tempfile, unittest
from pathlib import Path
from test_archive import Store, Planner, MANIFEST, response
from ikarchive.rates import weapon_snapshots

class WeaponSnapshotTests(unittest.TestCase):
    def test_real_vibes_is_separate_per_weapon_and_excludes_rankings(self):
        weapon={'id':'weapon-a','name':'スプラシューター','stats':{'vibes':12.5,'maxWeaponPower':1900,'currentWeaponPowerOrder':{'weaponPower':1800}}}
        data={'weaponRecords':{'nodes':[weapon]},'allWeapons':{'nodes':[weapon,{'id':'weapon-b','name':'わかばシューター','stats':{'vibes':0}}]},'bestNineRanking':{'nodes':[{'id':'someone-else','stats':{'vibes':999}}]}}
        items=weapon_snapshots(data)
        self.assertEqual(len(items),4)
        self.assertEqual(sorted(i['value'] for i in items if i['series_id'].endswith('|vibes')),[0,12.5])
        self.assertTrue(all('someone-else' not in i['series_id'] for i in items))
    def test_same_body_keeps_observed_times_and_spool_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'test.sqlite');planner=Planner(MANIFEST)
            body={'data':{'weaponRecords':{'nodes':[{'id':'weapon-a','name':'スプラシューター','stats':{'vibes':12.5}}]}}}
            for date in ('2026-09-21T00:00:00Z','2026-09-22T00:00:00Z'):
                e=response('WeaponQuery',body);e['fetched_at']=date
                rid=store.record(e);store.project(rid,planner);store.project(store.record(e),planner)
            self.assertEqual(store.db.execute('SELECT count(*) FROM responses').fetchone()[0],1)
            rows=store.db.execute('SELECT played_time,value,source FROM rate_points ORDER BY played_time').fetchall()
            self.assertEqual([tuple(r) for r in rows],[(d,12.5,'api_snapshot') for d in ('2026-09-21T00:00:00Z','2026-09-22T00:00:00Z')])
            store.close()
            store=Store(Path(tmp)/'test.sqlite')
            self.assertEqual(store.db.execute('SELECT count(*) FROM rate_points').fetchone()[0],2)
            store.close()
    def test_missing_null_bool_values_are_not_zero(self):
        data={'weapons':{'nodes':[{'id':'a','stats':{'vibes':None}},{'id':'b','stats':{'vibes':False}},{'id':'c','stats':{}}]}}
        self.assertEqual(weapon_snapshots(data),[])
