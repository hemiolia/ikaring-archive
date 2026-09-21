import {Buffer} from 'node:buffer';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';
import {extract} from './extract-queries.mjs';
import {initialize,nximport} from './nxapi-runtime.mjs';
process.env.NXAPI_DEBUG_FILE='0';
const origin='https://api.lp1.av5ja.srv.nintendo.net';
let api,account,spool;
const emit=x=>process.stdout.write(JSON.stringify(x)+'\n');
// nxapi renews the bullet token when x-bullettoken-remaining drops to this many seconds.
const RENEW_AT_SECONDS=300;
let storage,sessionToken;
// Errors from nxapi may contain credential-bearing objects; never serialize them.
console.warn=()=>{};console.error=()=>{};
function sessionExpiresAt(token){
 try{
  const payload=JSON.parse(Buffer.from(token.split('.')[1],'base64url').toString());
  return typeof payload.exp==='number'?payload.exp*1000:null;
 }catch{return null;}
}
async function savedBulletExpiry(){
 const saved=sessionToken&&await storage.getItem('BulletToken.'+sessionToken);
 return saved&&typeof saved.expires_at==='number'?saved.expires_at:null;
}
async function renewIfClose(expiresAt){
 if(!api?.onTokenShouldRenew||!expiresAt)return expiresAt;
 if(expiresAt-Date.now()>RENEW_AT_SECONDS*1000)return expiresAt;
 await api.onTokenShouldRenew(Math.max(0,Math.floor((expiresAt-Date.now())/1000)));
 return await savedBulletExpiry()||expiresAt;
}
function authCode(error){
 const name=error?.name||'';
 const message=String(error?.message||'');
 if(message==='AUTH_REQUIRED'||name==='AUTH_REQUIRED')return 'AUTH_REQUIRED';
 if(name==='NintendoAccountSessionTokenExpiredError'||name==='NintendoAccountSessionTokenInvalidError')return 'SESSION_EXPIRED';
 if(/session token expired|session token has expired or was revoked|invalid_grant/i.test(message))return 'SESSION_EXPIRED';
 const code=error?.cause?.code||error?.code;
 if(['ENOTFOUND','ECONNRESET','ETIMEDOUT','EAI_AGAIN','ECONNREFUSED','UND_ERR_CONNECT_TIMEOUT'].includes(code)||message==='fetch failed')return 'NETWORK';
 return 'BRIDGE_FAILURE';
}
async function init(req){
 const loaded=await initialize();
 const {getBulletToken}=await nximport('common/auth/splatnet3.js');
 storage=await loaded.initStorage(req.data_path||process.env.NXAPI_DATA_PATH||loaded.paths.data);
 account=req.account||await storage.getItem('SelectedUser');
 sessionToken=account&&await storage.getItem('NintendoAccountToken.'+account);
 if(!sessionToken){emit({error:'AUTH_REQUIRED'});return;}
 const session_expires_at=sessionExpiresAt(sessionToken);
 const session_iat=(()=>{try{const p=JSON.parse(Buffer.from(sessionToken.split('.')[1],'base64url').toString());return typeof p.iat==='number'?p.iat:null;}catch{return null;}})();
 if(session_expires_at&&session_expires_at<=Date.now()){emit({error:'SESSION_EXPIRED',session_expires_at});return;}
 const result=await getBulletToken(storage,sessionToken,process.env.ZNC_PROXY_URL,true);
 api=result.splatnet;spool=req.spool;fs.mkdirSync(spool,{recursive:true,mode:0o700});
 const bullet_expires_at=await renewIfClose(result.data?.expires_at||await savedBulletExpiry());
 emit({account,country:api.na_country,version:api.version,session_expires_at,session_iat,bullet_expires_at});
}
async function query(req){
 if(!api)throw Error('AUTH_REQUIRED');
 let response;
 for(let attempt=0;attempt<2;attempt++){
  response=await fetch(origin+'/api/graphql',{method:'POST',signal:AbortSignal.timeout(45000),headers:{'User-Agent':api.useragent,'Accept':'*/*','Referer':origin+'/','X-Requested-With':'XMLHttpRequest','Authorization':'Bearer '+api.bullet_token,'Content-Type':'application/json','X-Web-View-Ver':req.version||api.version,'Accept-Language':api.language},body:JSON.stringify({variables:req.variables,extensions:{persistedQuery:{version:1,sha256Hash:req.query_id}}})});
  const body=Buffer.from(await response.arrayBuffer());
  const headers=Object.fromEntries([...response.headers].filter(([k])=>!['set-cookie','authorization','cookie'].includes(k.toLowerCase())));
  const envelope={event_id:randomUUID(),account,fetched_at:new Date().toISOString(),operation:req.operation,variables:req.variables,query_id:req.query_id,app_version:req.version||api.version,status:response.status,headers,body_base64:body.toString('base64')};
  const target=path.join(spool,envelope.event_id+'.json');const fd=fs.openSync(target+'.tmp','wx',0o600);try{fs.writeFileSync(fd,JSON.stringify(envelope));fs.fsyncSync(fd);}finally{fs.closeSync(fd);}fs.renameSync(target+'.tmp',target);
  if(response.status===401&&attempt===0&&api.onTokenExpired){await api.onTokenExpired(response);continue;}
  const remaining=Number(response.headers.get('x-bullettoken-remaining'));
  if(Number.isFinite(remaining)&&remaining<=RENEW_AT_SECONDS&&api.onTokenShouldRenew)await api.onTokenShouldRenew(remaining);
  emit({spool_file:target});return;
 }
}
async function inventory(req){
 const html=await fetch(origin+'/',{signal:AbortSignal.timeout(45000)}).then(r=>{if(!r.ok)throw Error('HTTP');return r.text()});
 const urls=[...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map(x=>new URL(x[1],origin)).filter(u=>u.origin===origin&&u.pathname.startsWith('/static/'));
 if(!urls.length)throw Error('NO_APP_BUNDLE');
 const queries={},sources=[];let expected=0;let version=null;
 for(const url of urls){const source=await fetch(url,{signal:AbortSignal.timeout(45000)}).then(r=>{if(!r.ok)throw Error('HTTP');return r.text()});const marker=source.indexOf('revision_info_not_set');if(marker>=0){const vicinity=source.slice(Math.max(0,marker-250),marker+180);const rev=vicinity.match(/"([a-f0-9]{40})"/);const ver=vicinity.match(/`(\d+\.\d+\.\d+)-/);if(rev&&ver)version=ver[1]+'-'+rev[1].slice(0,8);}const result=extract(source);Object.assign(queries,result.queries);const count=[...source.matchAll(/kind:"Request"/g)].length;expected+=count;sources.push({url:url.href,sha256:result.source_sha256,request_markers:count});}
 if(Object.keys(queries).length!==expected)throw Error('INCOMPLETE_QUERY_EXTRACTION');
 // The web version is present as a literal in the application bundle. Authentication remains nxapi's responsibility.
 emit({fetched_at:new Date().toISOString(),origin,sources,queries,expected,version});
}
for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})){
 try{const req=JSON.parse(line);if(req.command==='init')await init(req);else if(req.command==='query')await query(req);else if(req.command==='inventory')await inventory(req);else throw Error('UNKNOWN_COMMAND');}
 catch(e){emit({error:authCode(e),error_type:e?.name||'Error'});}
}
process.exit(0);
