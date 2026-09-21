import base64, hashlib, json, sqlite3, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from .planner import walk, identity, decoded_id
from .classify import classify_detail, classify_coop
from .rates import observations, turf_form

def now():return datetime.now(timezone.utc).isoformat()
def js(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True)
def digest(body):return hashlib.sha256(body).hexdigest()

class Store:
    def __init__(self,path):
        self.path=Path(path);self.output_root=self.path.parent.parent if self.path.parent.name=='database' else self.path.parent
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.db=sqlite3.connect(path,timeout=30);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript((Path(__file__).resolve().parents[3]/'sql/schema.sql').read_text());self.db.commit()
        self._reclassify();self.path.chmod(0o600)
    def close(self):self.db.close()
    def issue(self,code,context,response=None,run=None):
        self.db.execute('INSERT INTO issues(run_id,response_id,code,context,created_at) VALUES(?,?,?,?,?)',(run,response,code,js(context),now()))
    def queue(self,account,operation,variables,kind=None,key=None):
        self.db.execute('INSERT OR IGNORE INTO jobs(account,operation,variables_json,kind,match_key) VALUES(?,?,?,?,?)',(account,operation,js(variables),kind,key))
    def record(self,e,run=None):
        raw=base64.b64decode(e['body_base64'],validate=True);sha=digest(raw);text=None;error=None
        try:
            text=raw.decode('utf8');json.loads(text,parse_constant=lambda s:(_ for _ in ()).throw(ValueError(s)))
        except (ValueError,UnicodeError) as exc:text=None;error=type(exc).__name__
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO bodies VALUES(?,?,?)',(sha,raw,len(raw)))
            variables=js(e['variables'])
            existing=self.db.execute('SELECT id FROM responses WHERE account=? AND operation=? AND variables_json=? AND body_sha256=?',(e['account'],e['operation'],variables,sha)).fetchone()
            if existing:return existing[0]
            self.db.execute('''INSERT OR IGNORE INTO responses(event_id,run_id,account,fetched_at,operation,variables_json,query_id,app_version,http_status,headers_json,body_sha256,json_text,parse_error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(e['event_id'],run,e['account'],e['fetched_at'],e['operation'],variables,e.get('query_id'),e.get('app_version'),e.get('status'),js(e.get('headers',{})),sha,text,error))
        return self.db.execute('SELECT id FROM responses WHERE event_id=?',(e['event_id'],)).fetchone()[0]
    def project(self,rid,planner,country='JP'):
        r=self.db.execute('SELECT * FROM responses WHERE id=?',(rid,)).fetchone()
        if r['projected']:return
        obj=json.loads(r['json_text']) if r['json_text'] else {}
        op=r['operation'];account=r['account'];variables=json.loads(r['variables_json']);data=obj.get('data') if isinstance(obj,dict) else None
        okay=r['http_status']==200 and isinstance(data,dict) and not obj.get('errors')
        if isinstance(data,dict) and not any(v is not None for v in data.values()):okay=False
        omissions=planner.missing_fields(op,data,variables) if r['query_id'] and isinstance(data,dict) and op in planner.routes else []
        if omissions:okay=False
        with self.db:
            for path in omissions:self.issue('SELECTED_FIELD_MISSING',{'operation':op,'path':path},rid)
            if not okay:self.issue('INCOMPLETE_RESPONSE',{'operation':op,'status':r['http_status'],'graphql_errors':bool(obj.get('errors')) if isinstance(obj,dict) else False},rid)
            if isinstance(data,dict):
                self._matches(r,data,okay)
                if okay and ('Histories' in op or op=='CoopHistoryQuery'):
                    previous=self.db.execute('SELECT response_id FROM endpoint_heads WHERE account=? AND operation=?',(account,op)).fetchone()
                    if previous:
                        before={x[0] for x in self.db.execute('SELECT match_key FROM sightings WHERE response_id=?',(previous[0],))}
                        after={x[0] for x in self.db.execute('SELECT match_key FROM sightings WHERE response_id=?',(rid,))}
                        if before and after and before.isdisjoint(after):self.issue('POSSIBLE_HISTORY_GAP',{'operation':op,'previous_response':previous[0],'previous_count':len(before),'current_count':len(after)},rid)
                self._assets(r,data)
                if op in planner.routes:
                    for event,a,b,p in planner.visit(op,data,variables):
                        if event=='entity':
                            if isinstance(a,str) and (a=='Image' or a.endswith('Image')):
                                u=b.get('url')
                                if isinstance(u,str) and u.startswith('https://'):
                                    self.db.execute('INSERT OR IGNORE INTO assets(url) VALUES(?)',(u,))
                                    self.db.execute('INSERT OR IGNORE INTO asset_refs VALUES(?,?,?)',(rid,u,js(p+('url',))))
                            eid=b.get('id')
                            if eid:self.db.execute('INSERT INTO entities VALUES(?,?,?,?,?) ON CONFLICT(account,typename,entity_id) DO UPDATE SET response_id=excluded.response_id,json_text=excluded.json_text',(account,a,eid,rid,js(b)))
                            for q,v in planner.related(a,b,country):
                                ident=identity(eid) if eid else None
                                self.queue(account,q,v,*(ident or (None,None)))
                        elif event in ('next','page'):
                            if event=='page':
                                field,child=p
                                binding={k:v for k,v in variables.items() if not k.startswith('page') and k!='cursor'}
                                fingerprint=digest(js(child).encode())
                                try:self.db.execute('INSERT INTO page_fingerprints VALUES(?,?,?,?,?)',(account,op,js(binding),js(field),fingerprint))
                                except sqlite3.IntegrityError:
                                    self.issue('PAGINATION_REPEATED_PAGE',{'operation':op,'field':field},rid);continue
                            self.queue(account,a,b)
                        elif event=='issue':self.issue(a,b,rid)
                else:self.issue('UNKNOWN_OPERATION',op,rid)
            if okay:
                self.db.execute('INSERT INTO endpoint_heads VALUES(?,?,?) ON CONFLICT(account,operation) DO UPDATE SET response_id=excluded.response_id WHERE (SELECT fetched_at FROM responses WHERE id=excluded.response_id)>=(SELECT fetched_at FROM responses WHERE id=endpoint_heads.response_id)',(account,op,rid))
            self.db.execute('UPDATE responses SET projected=1 WHERE id=?',(rid,))
            self.db.execute('UPDATE jobs SET state=?,attempts=attempts+1,next_attempt=?,last_response_id=? WHERE account=? AND operation=? AND variables_json=? AND (last_response_id IS NULL OR (SELECT fetched_at FROM responses WHERE id=last_response_id)<=?)',('done' if okay else 'retry',time.time()+(86400 if okay else 300),rid,account,op,r['variables_json'],r['fetched_at']))
    def _matches(self,r,data,okay):
        for path,v in walk(data):
            if not isinstance(v,dict) or not isinstance(v.get('id'),str):continue
            ident=identity(v['id'])
            if not ident:continue
            kind,key=ident;a=r['account'];t=r['fetched_at']
            self.db.execute('INSERT INTO matches VALUES(?,?,?,?,?,NULL) ON CONFLICT(account,kind,match_key) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen),first_seen=MIN(first_seen,excluded.first_seen)',(a,kind,key,t,t))
            self.db.execute('INSERT OR IGNORE INTO match_refs VALUES(?,?,?,?)',(a,kind,v['id'],key))
            self.queue(a,'VsHistoryDetailQuery' if kind=='vs' else 'CoopHistoryDetailQuery',{'vsResultId' if kind=='vs' else 'coopHistoryDetailId':v['id']},kind,key)
            self.db.execute('INSERT OR IGNORE INTO sightings VALUES(?,?,?,?,?,?)',(r['id'],a,kind,key,js(path),js(v)))
            # Only a full detail operation can replace the canonical detail projection.
            full=(r['operation']=='VsHistoryDetailQuery' and kind=='vs' and len(path)==1) or (r['operation']=='CoopHistoryDetailQuery' and kind=='coop' and len(path)==1)
            if full:
                self.db.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?)',(r['id'],a,kind,key,js(v)))
                if okay:self.db.execute('UPDATE matches SET detail_response_id=? WHERE account=? AND kind=? AND match_key=? AND (detail_response_id IS NULL OR (SELECT fetched_at FROM responses WHERE id=detail_response_id)<=?)',(r['id'],a,kind,key,r['fetched_at']))
                if kind in ('vs','coop'):
                    self._write_classification(a,kind,key)
                    self._write_rates(a,kind,key)
    def _assets(self,r,data):
        for path,v in walk(data):
            if not isinstance(v,str) or not v.startswith('https://'):continue
            # Preserve every URL in raw data, download fields explicitly representing media.
            if not path or str(path[-1]).lower() not in ('url','thumbnailurl','imageurl','originalurl'):continue
            u=urlparse(v)
            if any(x in '/'.join(map(str,path)).lower() for x in ('image','photo','thumbnail','album','icon','mask','original')):
                self.db.execute('INSERT OR IGNORE INTO assets(url) VALUES(?)',(v,))
                self.db.execute('INSERT OR IGNORE INTO asset_refs VALUES(?,?,?)',(r['id'],v,js(path)))
    def recover(self,planner,spool,run=None):
        for f in sorted(Path(spool).glob('*.json')):
            e=json.loads(f.read_text());rid=self.record(e,run);self.project(rid,planner);f.unlink()
        for r in self.db.execute('SELECT id FROM responses WHERE projected=0').fetchall():self.project(r[0],planner)
    def status(self):
        counts={t:self.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('responses','matches','documents','pending_details','issues','entities','assets')}
        counts['jobs']=[dict(r) for r in self.db.execute('SELECT state,count(*) count FROM jobs GROUP BY state')]
        counts['assets_by_state']=[dict(r) for r in self.db.execute('SELECT state,count(*) count FROM assets GROUP BY state')]
        counts['last_run']=dict(r) if (r:=self.db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 1').fetchone()) else None
        counts['analysis']=[dict(r) for r in self.db.execute('SELECT kind,genre,roster_class,analysis_set,count(*) count FROM match_classification GROUP BY 1,2,3,4 ORDER BY 1,2,3')]
        counts['tags']=[dict(r) for r in self.db.execute('SELECT tag,count(*) count FROM match_tags GROUP BY tag ORDER BY tag')]
        counts['all_server_records_verified']=False
        counts['auth']=self.auth_status()
        return counts
    def _control(self,key,value=None):
        if value is None:return (row[0] if (row:=self.db.execute('SELECT value FROM control WHERE key=?',(key,)).fetchone()) else None)
        self.db.execute('INSERT INTO control VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
    def remember_auth(self,info):
        def iso(ms):
            if not isinstance(ms,(int,float)):return None
            return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat()
        with self.db:
            previous_iat=self._control('auth_session_iat')
            failure=self._control('auth_last_failure')
            iat=info.get('session_iat')
            if failure in ('AUTH_REQUIRED','AUTH_EXPIRED','SESSION_EXPIRED') or (previous_iat and iat is not None and previous_iat!=str(int(iat))):
                self._control('backfill_armed','1');self._control('backfill_reset_done','0')
            if iat is not None:self._control('auth_session_iat',str(int(iat)))
            for key,value in (('auth_session_expires_at',iso(info.get('session_expires_at'))),('auth_bullet_expires_at',iso(info.get('bullet_expires_at'))),('auth_last_ok_at',now())):
                if value:self._control(key,value)
            self.db.execute("DELETE FROM control WHERE key IN ('auth_last_failure','auth_last_failure_at','last_sync_error','last_sync_error_at')")
    def remember_auth_failure(self,code):
        with self.db:
            self._control('auth_last_failure',code)
            self._control('auth_last_failure_at',now())
            self.issue('AUTH_INCIDENT',{'code':code})
    def remember_sync_error(self,code):
        with self.db:
            self._control('last_sync_error',code)
            self._control('last_sync_error_at',now())
    def auth_status(self):
        session=self._control('auth_session_expires_at')
        failure=self._control('auth_last_failure')
        ok=self._control('auth_last_ok_at')
        remaining=None
        if session:
            try:remaining=(datetime.fromisoformat(session)-datetime.now(timezone.utc)).total_seconds()
            except ValueError:remaining=None
        return {
            'session_expires_at':session,
            'bullet_expires_at':self._control('auth_bullet_expires_at'),
            'last_ok_at':ok,
            'last_failure':failure,
            'last_failure_at':self._control('auth_last_failure_at'),
            'last_sync_error':self._control('last_sync_error'),
            'reauth_required':failure in ('AUTH_REQUIRED','SESSION_EXPIRED') and (not ok or (self._control('auth_last_failure_at') or '')>=(ok or '')),
            'session_expires_soon':remaining is not None and remaining<14*86400,
            'backfill_armed':self._control('backfill_armed')=='1',
        }
    def apply_backfill(self,account):
        """After re-authentication, walk saved history pages again once. Do not delete rows."""
        if self._control('backfill_armed')!='1' or self._control('backfill_reset_done')=='1':return 0
        with self.db:
            self._control('backfill_reset_done','1')
            cur=self.db.execute('''UPDATE jobs SET state='pending',next_attempt=0 WHERE account=? AND state='done' AND (operation LIKE '%Histor%' OR operation='CoopHistoryQuery' OR (operation IN ('VsHistoryDetailQuery','CoopHistoryDetailQuery') AND match_key IN (SELECT match_key FROM matches WHERE account=? AND detail_response_id IS NULL)))''',(account,account))
            return cur.rowcount
    def finish_backfill(self,account):
        if self._control('backfill_armed')!='1':return
        pending=self.db.execute("SELECT count(*) FROM jobs WHERE account=? AND state!='done' AND (operation LIKE '%Histor%' OR operation='CoopHistoryQuery')",(account,)).fetchone()[0]
        if pending==0:
            with self.db:self.db.execute("DELETE FROM control WHERE key IN ('backfill_armed','backfill_reset_done')")
    def _reclassify(self):
        for row in self.db.execute("SELECT account,kind,match_key FROM matches WHERE kind IN ('vs','coop') AND detail_response_id IS NOT NULL"):
            self._write_classification(row['account'],row['kind'],row['match_key'])
            self._write_rates(row['account'],row['kind'],row['match_key'])
        self.db.commit()
    def _write_classification(self,account,kind,key):
        row=self.db.execute('''SELECT d.json_text,m.detail_response_id FROM matches m
            JOIN documents d ON d.response_id=m.detail_response_id AND d.account=m.account AND d.kind=m.kind AND d.match_key=m.match_key
            WHERE m.account=? AND m.kind=? AND m.match_key=? AND m.detail_response_id IS NOT NULL''',(account,kind,key)).fetchone()
        if not row:
            self.db.execute('DELETE FROM match_classification WHERE account=? AND kind=? AND match_key=?',(account,kind,key));return
        detail=json.loads(row['json_text'])
        info=classify_detail(detail) if kind=='vs' else classify_coop(detail)
        self.db.execute('''INSERT INTO match_classification(account,kind,match_key,genre,mode_raw,bankara_mode,rule_raw,rule_name,roster_class,analysis_set,team_count,my_player_count,opponent_counts,detail_response_id,classified_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account,kind,match_key) DO UPDATE SET genre=excluded.genre,mode_raw=excluded.mode_raw,bankara_mode=excluded.bankara_mode,rule_raw=excluded.rule_raw,rule_name=excluded.rule_name,roster_class=excluded.roster_class,analysis_set=excluded.analysis_set,team_count=excluded.team_count,my_player_count=excluded.my_player_count,opponent_counts=excluded.opponent_counts,detail_response_id=excluded.detail_response_id,classified_at=excluded.classified_at''',
            (account,kind,key,info['genre'],info['mode_raw'],info['bankara_mode'],info['rule_raw'],info['rule_name'],info['roster_class'],info['analysis_set'],info['team_count'],info['my_player_count'],js(info['opponent_counts']),row['detail_response_id'],now()))
    def _write_rates(self,account,kind,key):
        row=self.db.execute('''SELECT d.json_text,c.genre,c.rule_raw,json_extract(d.json_text,'$.playedTime') played_time,json_extract(d.json_text,'$.judgement') judgement
            FROM match_classification c JOIN documents d ON d.response_id=c.detail_response_id AND d.account=c.account AND d.kind=c.kind AND d.match_key=c.match_key
            WHERE c.account=? AND c.kind=? AND c.match_key=?''',(account,kind,key)).fetchone()
        self.db.execute('DELETE FROM rate_points WHERE account=? AND match_key=? AND source=?',(account,key,'api'))
        if not row:return
        detail=json.loads(row['json_text'])
        for item in observations(detail,row['genre'],row['rule_raw']):
            self.db.execute('''INSERT INTO rate_points(account,series_id,label,genre,rule_raw,match_key,played_time,value,source,priority)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account,series_id,match_key) DO UPDATE SET label=excluded.label,value=excluded.value,played_time=excluded.played_time,priority=excluded.priority,source=excluded.source''',
                (account,item['series_id'],item['label'],item['genre'],item['rule_raw'],key,row['played_time'],item['value'],item['source'],item['priority']))
        if row['genre']=='nawabari':self._rebuild_turf_form(account)
    def _rebuild_turf_form(self,account):
        rows=self.db.execute('''SELECT a.match_key,a.played_time,a.judgement FROM analysis_nawabari a WHERE a.account=? ORDER BY a.played_time,a.match_key''',(account,)).fetchall()
        self.db.execute("DELETE FROM rate_points WHERE account=? AND series_id='nawabari||form'",(account,))
        for row,streak in zip(rows,turf_form(row['judgement'] for row in rows)):
            self.db.execute('''INSERT INTO rate_points(account,series_id,label,genre,rule_raw,match_key,played_time,value,source,priority)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(account,'nawabari||form','チョーシ','nawabari','TURF_WAR',row['match_key'],row['played_time'],streak,'derived_judgement','primary'))
    def verify(self):
        errors=[]
        if self.db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':errors.append('integrity_check')
        if self.db.execute('PRAGMA foreign_key_check').fetchall():errors.append('foreign_key_check')
        for r in self.db.execute('SELECT sha256,body,byte_length FROM bodies'):
            if digest(r['body'])!=r['sha256'] or len(r['body'])!=r['byte_length']:errors.append(r['sha256'])
        return errors
