#!/usr/bin/env python3
"""イカリング3の取得・保存・監査CLI（Python標準ライブラリのみ）。"""
import argparse, base64, json, os, plistlib, shutil, sqlite3, subprocess, sys, uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent/'src/python'))
from ikarchive.store import Store, now, js
from ikarchive.locking import acquire
from ikarchive.planner import Planner
from ikarchive.collector import catalog, sync, ROOT
from ikarchive.xlsx_export import export_xlsx

OUTPUT=Path(os.environ.get('IKARING_ARCHIVE_DATA_DIR',str(Path.home()/'Documents/イカリング3アーカイブ')))
DEFAULT=OUTPUT/'database/archive.sqlite3'
AUTH_INCIDENTS={'AUTH_REQUIRED','AUTH_EXPIRED','SESSION_EXPIRED'}

def notify_auth_incident(code):
    stamp=OUTPUT/'logs'/'auth-notice-stamp'
    incident=OUTPUT/'logs'/'auth-incident.json'
    OUTPUT.joinpath('logs').mkdir(parents=True,exist_ok=True,mode=0o700)
    incident.write_text(js({'code':code,'at':now()}),encoding='utf8')
    incident.chmod(0o600)
    fresh=True
    if stamp.exists():
        try:fresh=stamp.read_text(encoding='utf8').split('\n',1)[0]!=code or (__import__('time').time()-stamp.stat().st_mtime)>6*3600
        except OSError:fresh=True
    if not fresh:return
    stamp.write_text(code+'\n'+now(),encoding='utf8')
    if sys.platform!='darwin':return
    message='イカリング3の取得が認証で止まった。この間の戦績は保存されていない。ログインが必要。コード: '+code
    subprocess.run(['osascript','-e','display notification '+json.dumps(message)+' with title "イカリング3アーカイブ"'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def apply_tag(store,args):
    if args.tag_action!='list':
        if not args.match_key:raise ValueError('MATCH_KEY_REQUIRED')
        if not isinstance(args.tag,str) or not args.tag.strip():raise ValueError('EMPTY_TAG')
    account=args.account
    if not account:
        sql='SELECT DISTINCT account FROM matches'+( ' WHERE match_key=?' if args.match_key else '')
        rows=[r[0] for r in store.db.execute(sql,((args.match_key,) if args.match_key else ()))]
        if len(rows)!=1:raise ValueError('ACCOUNT_REQUIRED')
        account=rows[0]
    if args.tag_action=='list':
        sql='SELECT match_key,tag,note,created_at,updated_at FROM match_tags WHERE account=?'
        params=[account]
        if args.match_key:
            sql+=' AND match_key=?';params.append(args.match_key)
        return [dict(r) for r in store.db.execute(sql+' ORDER BY match_key,tag',params)]
    if not store.db.execute('SELECT 1 FROM matches WHERE account=? AND match_key=?',(account,args.match_key)).fetchone():
        raise ValueError('MATCH_NOT_FOUND')
    tag=args.tag.strip()
    if args.tag_action=='add':
        store.db.execute('''INSERT INTO match_tags(account,match_key,tag,note,created_at,updated_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(account,match_key,tag) DO UPDATE SET note=excluded.note,updated_at=excluded.updated_at''',(account,args.match_key,tag,args.note,now(),now()))
    else:
        store.db.execute('DELETE FROM match_tags WHERE account=? AND match_key=? AND tag=?',(account,args.match_key,tag))
    store.db.commit()
    return {'account_selected':True,'match_key':args.match_key,'tag':tag,'action':args.tag_action}

def audit(store):
    row=store.db.execute('SELECT json_text FROM manifests ORDER BY fetched_at DESC LIMIT 1').fetchone()
    if not row:return {'error':'CATALOG_MISSING','all_server_records_verified':False}
    m=json.loads(row[0]);p=Planner(m);ops=[]
    for name in p.queries:
        states=[dict(r) for r in store.db.execute('SELECT state,count(*) count FROM jobs WHERE operation=? GROUP BY state',(name,))]
        responses=store.db.execute('SELECT count(*) FROM responses WHERE operation=?',(name,)).fetchone()[0]
        entities=p.routes.get(name,{}).get('bindings',{})
        ops.append({'operation':name,'classification':'excluded_action' if name in p.excluded else 'unsupported' if name in p.unsupported else 'related' if entities else 'root','reason':p.excluded.get(name) or p.unsupported.get(name),'states':states,'responses':responses,'needs_entities':entities})
    return {'catalog_at':m.get('fetched_at'),'version':m.get('version'),'extracted':len(p.queries),'read_routes':len(p.routes),'unsupported':p.unsupported,'operations':ops,'storage':store.status(),'all_server_records_verified':False,'limit':'Server-internal records and records no longer exposed by SplatNet cannot be proven complete from the client API.'}

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=DEFAULT)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('init','status','audit','verify','refresh-catalog'):sub.add_parser(name)
    p=sub.add_parser('sync');p.add_argument('--account');p.add_argument('--nxapi-data');p.add_argument('--budget',type=int,default=250);p.add_argument('--delay',type=float,default=1.5)
    p=sub.add_parser('backup');p.add_argument('destination',type=Path)
    p=sub.add_parser('export');p.add_argument('directory',type=Path)
    p=sub.add_parser('export-xlsx');p.add_argument('destination',nargs='?',type=Path,default=OUTPUT/'exports'/'分析.xlsx')
    p=sub.add_parser('import');p.add_argument('directory',type=Path);p.add_argument('--account',required=True)
    p=sub.add_parser('sql');p.add_argument('query')
    p=sub.add_parser('install-service');p.add_argument('--interval',type=int,default=120);p.add_argument('--account');p.add_argument('--nxapi-data')
    p=sub.add_parser('tag');p.add_argument('tag_action',choices=('add','remove','list'));p.add_argument('--account');p.add_argument('--match-key');p.add_argument('--tag');p.add_argument('--note')
    sub.add_parser('login')
    p=sub.add_parser('watch');p.add_argument('--interval',type=int,default=120)
    args=parser.parse_args();args.db=args.db.resolve()
    if args.command=='watch':
        import time
        if args.interval<60:raise ValueError('interval must be >=60')
        while True:
            subprocess.run([sys.executable,str(ROOT/'archive.py'),'--db',str(args.db),'sync'])
            time.sleep(args.interval)
    if args.command=='login':
        # Interactive nxapi login runs in the user's terminal, never passes tokens as argv.
        binary=ROOT/'src/node/login.mjs'
        return subprocess.call([os.environ.get('NODE','node'),str(binary)])
    args.db.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with open(str(args.db)+'.lock','a') as lock:
        try:acquire(lock)
        except BlockingIOError:print(js({'state':'another_sync_running'}));return 0
        store=Store(args.db)
        try:
            if args.command=='init':result=store.status()
            elif args.command=='refresh-catalog':
                m=catalog(store,True);p=Planner(m);result={'version':m.get('version'),'extracted':len(p.queries),'read_routes':len(p.routes),'unsupported':p.unsupported}
            elif args.command=='sync':
                if args.budget<1 or args.delay<0:raise ValueError('budget>=1, delay>=0')
                result=sync(store,args.account,args.nxapi_data,args.budget,args.delay)
            elif args.command=='status':result=store.status()
            elif args.command=='audit':result=audit(store)
            elif args.command=='verify':
                failures=store.verify();result={'ok':not failures,'failures':failures,'bodies_checked':store.db.execute('SELECT count(*) FROM bodies').fetchone()[0]}
                print(json.dumps(result,ensure_ascii=False,indent=2));return 1 if failures else 0
            elif args.command=='backup':
                dest=args.destination.resolve()
                if dest.exists():raise ValueError('Destination already exists; refusing overwrite')
                dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                target=sqlite3.connect(dest)
                try:store.db.backup(target)
                finally:target.close()
                check=sqlite3.connect(dest)
                try:integrity=check.execute('PRAGMA integrity_check').fetchone()[0]
                finally:check.close()
                result={'backup':str(dest),'integrity':integrity}
            elif args.command=='export-xlsx':result=export_xlsx(store,args.destination)
            elif args.command=='export':
                out=args.directory.resolve();out.mkdir(parents=True,exist_ok=True,mode=0o700);count=0
                with (out/'manifest.jsonl').open('x') as index:
                    for r in store.db.execute('SELECT r.*,b.body FROM responses r JOIN bodies b ON b.sha256=r.body_sha256 ORDER BY r.id'):
                        filename=f'{r["id"]}-{r["body_sha256"]}.json';(out/filename).write_bytes(r['body']);meta=dict(r);meta.pop('body');meta.pop('json_text');meta['file']=filename;index.write(js(meta)+'\n');count+=1
                assets=0
                with (out/'assets.jsonl').open('x') as index:
                    for r in store.db.execute("SELECT a.*,b.body FROM assets a JOIN bodies b ON b.sha256=a.body_sha256 WHERE a.state='done'"):
                        filename=r['body_sha256']+'.bin';(out/filename).write_bytes(r['body']);meta=dict(r);meta.pop('body');meta['file']=filename;index.write(js(meta)+'\n');assets+=1
                result={'export':str(out),'responses':count,'assets':assets}
            elif args.command=='import':
                p=Planner(catalog(store));count=0;rejected=[]
                for file in sorted(args.directory.rglob('*.json')):
                    try:
                        raw=file.read_bytes();data=json.loads(raw);op=None
                        if isinstance(data,dict) and 'body_base64' in data and 'event_id' in data:
                            if data['account']!=args.account:raise ValueError('account mismatch')
                            e=data
                        else:
                            # Explicit nxapi dump wrappers. The source bytes are kept verbatim.
                            result=data.get('result') if isinstance(data,dict) else None
                            if isinstance(result,dict):
                                from ikarchive.planner import identity
                                ident=identity(result.get('id'))
                                if ident:op='VsHistoryDetailQuery' if ident[0]=='vs' else 'CoopHistoryDetailQuery'
                            e={'event_id':'import-'+args.account+'-'+__import__('hashlib').sha256(raw).hexdigest(),'account':args.account,'fetched_at':now(),'operation':'nxapi-import','variables':{'filename':str(file)},'status':200,'body_base64':base64.b64encode(raw).decode()}
                        rid=store.record(e)
                        if op:
                            # An additional derived envelope is explicitly labeled, original is retained.
                            body={'data':{'vsHistoryDetail' if op=='VsHistoryDetailQuery' else 'coopHistoryDetail':result}}
                            derived={**e,'event_id':e['event_id']+'-projection','operation':op,'body_base64':base64.b64encode(js(body).encode()).decode()}
                            drid=store.record(derived);store.project(drid,p)
                        store.project(rid,p);count+=1
                    except (ValueError,OSError,TypeError) as exc:rejected.append({'file':str(file),'error':type(exc).__name__})
                result={'imported':count,'rejected':rejected}
            elif args.command=='tag':result=apply_tag(store,args)
            elif args.command=='sql':
                store.db.execute('PRAGMA query_only=ON');result=[dict(r) for r in store.db.execute(args.query)]
            elif args.command=='install-service':
                if sys.platform!='darwin':raise ValueError('LaunchAgent is macOS-only; use cron on other systems')
                if args.interval<60:raise ValueError('interval must be >=60 seconds')
                (OUTPUT/'logs').mkdir(parents=True,exist_ok=True,mode=0o700)
                label='local.ikaring3.archive';dest=Path.home()/'Library/LaunchAgents'/f'{label}.plist'
                if dest.exists():
                    backup=dest.with_suffix('.plist.backup-'+uuid.uuid4().hex[:8]);shutil.copy2(dest,backup)
                command=[sys.executable,str(ROOT/'archive.py'),'--db',str(args.db),'sync']
                if args.account:command+=['--account',args.account]
                if args.nxapi_data:command+=['--nxapi-data',args.nxapi_data]
                node=shutil.which('node')
                env={'PATH':str(Path(node).parent)+':/usr/bin:/bin' if node else '/usr/bin:/bin','NODE':node,'IKARING_ARCHIVE_DATA_DIR':str(OUTPUT.resolve())}
                if 'NXAPI_DATA_PATH' in os.environ:env['NXAPI_DATA_PATH']=os.environ['NXAPI_DATA_PATH']
                payload={'Label':label,'ProgramArguments':command,'WorkingDirectory':str(ROOT),'StartInterval':args.interval,'RunAtLoad':True,'ProcessType':'Background','EnvironmentVariables':env,'StandardOutPath':str(OUTPUT/'logs/service.log'),'StandardErrorPath':str(OUTPUT/'logs/service-errors.log')}
                dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(plistlib.dumps(payload))
                subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{label}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(dest)],check=True)
                result={'service':str(dest),'interval_seconds':args.interval,'login_required':True}
            if args.command=='sync':
                incident=OUTPUT/'logs'/'auth-incident.json'
                if incident.exists():incident.unlink()
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        finally:store.close()

if __name__=='__main__':
    try:sys.exit(main())
    except KeyboardInterrupt:sys.exit(130)
    except (RuntimeError,ValueError,sqlite3.Error,OSError) as exc:
        code=str(exc)
        if code in AUTH_INCIDENTS:notify_auth_incident(code)
        print(json.dumps({'error':code},ensure_ascii=False),file=sys.stderr);sys.exit(1)
