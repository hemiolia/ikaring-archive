import base64, json, os, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/python'))
sys.path.insert(0, str(ROOT))

import archive
from ikarchive.store import Store, js, now
from ikarchive.planner import Planner
from ikarchive.locking import acquire

MANIFEST = json.loads((ROOT / 'config/query-catalog.snapshot.json').read_text())

class ReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'database' / 'archive.sqlite3'
        # 1. 正常なDBを初期化
        s = Store(self.db_path)
        s.db.execute(
            "INSERT INTO manifests(sha256, fetched_at, json_text) VALUES(?,?,?)",
            ('test-sha', now(), json.dumps(MANIFEST))
        )
        rid = 'VsHistoryDetail-u-test:REGULAR:20260922T010101_11111111-1111-1111-1111-111111111111'
        detail = {
            'id': base64.b64encode(rid.encode()).decode(),
            'playedTime': '2026-09-22T01:01:01Z',
            'vsMode': {'mode': 'REGULAR'},
            'vsRule': {'rule': 'TURF_WAR', 'name': 'ナワバリバトル'},
            'judgement': 'WIN',
            'myTeam': {'players': [{'name': 'PlayerA'}]},
            'otherTeams': [{'players': [{'name': 'PlayerB'}]}]
        }
        raw = js({'data': {'vsHistoryDetail': detail}}).encode()
        eid = 'test-event-1'
        s.record({
            'event_id': eid,
            'account': 'account-test',
            'fetched_at': now(),
            'operation': 'VsHistoryDetailQuery',
            'variables': {},
            'status': 200,
            'body_base64': base64.b64encode(raw).decode()
        })
        p = Planner(MANIFEST)
        r = s.db.execute('SELECT id FROM responses WHERE event_id=?', (eid,)).fetchone()[0]
        s.project(r, p)
        s.db.commit()
        s.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _run_cli(self, *cli_args):
        cmd = [sys.executable, str(ROOT / 'archive.py'), '--db', str(self.db_path)] + list(cli_args)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return proc

    def test_cli_readonly_during_exclusive_lock(self):
        """別プロセスが.lock排他ロックを保持している状態でもCLI status/audit/sqlが利用可能。"""
        lock_file = Path(str(self.db_path) + '.lock')
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_file, 'a') as f:
            acquire(f)

            # status
            p_status = self._run_cli('status')
            self.assertEqual(p_status.returncode, 0, f"status stderr: {p_status.stderr}")
            status_data = json.loads(p_status.stdout)
            self.assertIn('responses', status_data)
            self.assertNotIn('another_sync_running', p_status.stdout)

            # audit
            p_audit = self._run_cli('audit')
            self.assertEqual(p_audit.returncode, 0, f"audit stderr: {p_audit.stderr}")
            audit_data = json.loads(p_audit.stdout)
            self.assertIn('version', audit_data)
            self.assertNotIn('another_sync_running', p_audit.stdout)

            # sql
            p_sql = self._run_cli('sql', 'SELECT count(*) as c FROM responses')
            self.assertEqual(p_sql.returncode, 0, f"sql stderr: {p_sql.stderr}")
            sql_data = json.loads(p_sql.stdout)
            self.assertEqual(sql_data, [{'c': 1}])

            # backup
            dest_backup = Path(self.tmp.name) / 'backup.sqlite3'
            p_backup = self._run_cli('backup', str(dest_backup))
            self.assertEqual(p_backup.returncode, 0, f"backup stderr: {p_backup.stderr}")
            backup_data = json.loads(p_backup.stdout)
            self.assertEqual(backup_data.get('integrity'), 'ok')

            # tag list
            p_tag = self._run_cli('tag', 'list', '--account', 'account-test')
            self.assertEqual(p_tag.returncode, 0, f"tag list stderr: {p_tag.stderr}")


    def test_write_commands_respect_exclusive_lock(self):
        """書込操作（tag add, syncなど）は排他ロックを要求し既存経路を維持すること。"""
        lock_file = Path(str(self.db_path) + '.lock')
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_file, 'a') as f:
            acquire(f)

            # tag add must not proceed when lock is held
            p_tag_add = self._run_cli('tag', 'add', '--account', 'account-test', '--match-key', 'dummy', '--tag', 'test')
            self.assertEqual(p_tag_add.returncode, 0)
            self.assertIn('another_sync_running', p_tag_add.stdout)

            # init must not proceed when lock is held
            p_init = self._run_cli('init')
            self.assertEqual(p_init.returncode, 0)
            self.assertIn('another_sync_running', p_init.stdout)


    def test_readonly_does_not_reclassify(self):
        """読み取り専用接続およびCLI読取操作が分類更新(_reclassify)を実行しないこと。"""
        # 手動で classified_at を過去の日時に設定
        fixed_time = '2020-01-01T00:00:00+00:00'
        writer = sqlite3.connect(self.db_path)
        writer.execute("UPDATE match_classification SET classified_at=?", (fixed_time,))
        writer.commit()
        writer.close()

        # Store(readonly=True)で接続
        ro_store = Store(self.db_path, readonly=True)
        row = ro_store.db.execute("SELECT classified_at FROM match_classification").fetchone()
        self.assertEqual(row['classified_at'], fixed_time)
        ro_store.close()

        # CLI status
        p_status = self._run_cli('status')
        self.assertEqual(p_status.returncode, 0)

        # CLI audit
        p_audit = self._run_cli('audit')
        self.assertEqual(p_audit.returncode, 0)

        # classified_at が不変であることを確認
        check = sqlite3.connect(self.db_path)
        check.row_factory = sqlite3.Row
        row_after = check.execute("SELECT classified_at FROM match_classification").fetchone()
        self.assertEqual(row_after['classified_at'], fixed_time)
        check.close()

    def test_sql_write_refusal_even_when_query_only_overridden(self):
        """PRAGMA query_only を解除するSQLを受けても mode=ro が書込みを拒否すること。"""
        ro_store = Store(self.db_path, readonly=True)
        # query_only=OFF を実行
        ro_store.db.execute("PRAGMA query_only=OFF")
        # INSERT を試みると attempt to write a readonly database エラーになること
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            ro_store.db.execute("INSERT INTO matches VALUES('acc','vs','k','t','t',NULL)")
        self.assertIn("readonly database", str(ctx.exception).lower())

        # UPDATE を試みても拒否されること
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            ro_store.db.execute("UPDATE matches SET first_seen='changed'")
        self.assertIn("readonly database", str(ctx.exception).lower())
        ro_store.close()

        # CLI sql 経由での書き込み試行もエラーとなること
        p_sql = self._run_cli('sql', "INSERT INTO matches VALUES('acc','vs','k','t','t',NULL)")
        self.assertNotEqual(p_sql.returncode, 0)
        self.assertIn("readonly database", p_sql.stderr.lower())

    def test_nonexistent_database_not_created(self):
        """読取対象が存在しないならエラーとなり、空DBや親ディレクトリを作成しないこと。"""
        nonexistent_dir = Path(self.tmp.name) / 'no_such_directory'
        nonexistent_db = nonexistent_dir / 'missing.sqlite3'

        # Store(readonly=True) 直接呼び出し
        with self.assertRaises(FileNotFoundError):
            Store(nonexistent_db, readonly=True)

        self.assertFalse(nonexistent_db.exists())
        self.assertFalse(nonexistent_dir.exists())

        # CLI status
        cmd = [sys.executable, str(ROOT / 'archive.py'), '--db', str(nonexistent_db), 'status']
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Database not found", proc.stderr)
        self.assertFalse(nonexistent_db.exists())
        self.assertFalse(nonexistent_dir.exists())
        self.assertFalse(Path(str(nonexistent_db) + '.lock').exists())

        # CLI sql
        cmd_sql = [sys.executable, str(ROOT / 'archive.py'), '--db', str(nonexistent_db), 'sql', 'SELECT 1']
        proc_sql = subprocess.run(cmd_sql, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc_sql.returncode, 1)
        self.assertFalse(nonexistent_db.exists())
        self.assertFalse(nonexistent_dir.exists())

    def test_read_transaction_snapshot_isolation(self):
        """一貫性のある read transaction 内で実行されていること。"""
        ro_store = Store(self.db_path, readonly=True)
        ro_store.db.execute('BEGIN')

        # 最初の読み取り
        r1 = ro_store.db.execute("SELECT count(*) FROM control").fetchone()[0]

        # 別接続から書き込みコミット
        writer = sqlite3.connect(self.db_path)
        writer.execute("INSERT INTO control VALUES('test_key', 'test_val')")
        writer.commit()
        writer.close()

        # トランザクション内では追加された行が見えない（スナップショット分離の一貫性）
        r2 = ro_store.db.execute("SELECT count(*) FROM control").fetchone()[0]
        self.assertEqual(r1, r2)

        ro_store.db.rollback()
        # トランザクション終了後は最新が見える
        r3 = ro_store.db.execute("SELECT count(*) FROM control").fetchone()[0]
        self.assertEqual(r3, r1 + 1)
        ro_store.close()
