// Local interactive login. Never prints session/bullet tokens or redirect contents.
import {read} from 'read';
import {spawn} from 'node:child_process';
import {initialize,nximport} from './nxapi-runtime.mjs';
process.umask(0o077);process.env.NXAPI_DEBUG_FILE='0';delete process.env.DEBUG;
try {
 const {initStorage,paths}=await initialize();
 const {NintendoAccountSessionAuthorisationCoral}=await nximport('api/coral.js');
 const {getToken}=await nximport('common/auth/coral.js');
 const auth=NintendoAccountSessionAuthorisationCoral.create();
 console.log('ブラウザーでニンテンドーアカウントにログインします。\n「この人にする」を右クリックしてリンクをコピーし、下に貼り付けてください。\nリンクや認証情報をGitHubやチャットに貼らないでください。\n');
 console.log(auth.authorise_url+'\n');
 const command=process.platform==='darwin'?['open',[auth.authorise_url]]:process.platform==='win32'?['rundll32',['url.dll,FileProtocolHandler',auth.authorise_url]]:['xdg-open',[auth.authorise_url]];
 const child=spawn(command[0],command[1],{stdio:'ignore',detached:true});child.on('error',()=>{});child.unref();
 const link=await read({prompt:'認証リンク（入力内容は非表示）: ',silent:true,output:process.stderr});
 const url=new URL(link);
 if(url.protocol!=='npf71b963c1b7b6d119:')throw new Error('INVALID_REDIRECT');
 const session=await auth.getSessionToken(new URLSearchParams(url.hash.slice(1)));
 const storage=await initStorage(process.env.NXAPI_DATA_PATH||paths.data);
 // Keep normal nxapi token caching and renewal behavior.
 const {data}=await getToken(storage,session.session_token,process.env.ZNC_PROXY_URL);
 await storage.setItem('NintendoAccountToken.'+data.user.id,session.session_token);
 const users=new Set(await storage.getItem('NintendoAccountIds')||[]);users.add(data.user.id);
 await storage.setItem('NintendoAccountIds',[...users]);await storage.setItem('SelectedUser',data.user.id);
 console.log('\n認証情報をこの端末に保存しました。収集を開始できます。');
 process.exit(0);
} catch(error) {
 console.error('認証できませんでした。再実行してログインし直してください。種類: '+error.name);
 process.exit(1);
}
