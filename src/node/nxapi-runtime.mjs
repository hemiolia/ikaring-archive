import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
export const root=path.resolve(fileURLToPath(import.meta.resolve('nxapi')),'../../..');
export const nximport=relative=>import(pathToFileURL(path.join(root,'dist',relative)));
export async function initialize(){
 process.env.NXAPI_DEBUG_FILE='0';
 const pkg=JSON.parse(fs.readFileSync(path.join(root,'package.json'),'utf8'));
 const {addUserAgent}=await nximport('util/useragent.js');
 addUserAgent('ikaring-archive/0.1.0 (+https://github.com/hemiolia/ikaring-archive)');
 const {NxapiClientAssertionProvider,setClientAssertionProvider}=await nximport('util/nxapi-auth.js');
 const client=process.env.NXAPI_AUTH_CLIENT_ID||pkg.__nxapi_auth?.cli?.client_id;
 if(client)setClientAssertionProvider(new NxapiClientAssertionProvider(client,undefined,'ca:gf ca:er ca:dr ca:na'));
 return nximport('util/storage.js');
}
