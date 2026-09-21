import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {extract} from '../../src/node/extract-queries.mjs';

test('extracts Relay literal data without running bundle code',()=>{
 const source='globalThis.DANGEROUS=true; const q=function(){var a=[];return {kind:"Request",operation:{argumentDefinitions:a,selections:[]},params:{name:"ExampleQuery",id:"hash",operationKind:"query"}}}();';
 const result=extract(source);assert.equal(result.queries.ExampleQuery.params.id,'hash');assert.equal(globalThis.DANGEROUS,undefined);
});
test('rejects executable/computed request data instead of evaluating it',()=>{
 const source='const q=function(){return {kind:"Request",params:evil()}}();';
 assert.deepEqual(extract(source).queries,{});
});
test('manifest contains current detail and Salmon Run routes, no duplicates',()=>{
 const data=JSON.parse(fs.readFileSync(new URL('../../config/query-catalog.snapshot.json',import.meta.url)));
 assert.equal(Object.keys(data.queries).length,113);
 for(const q of Object.values(data.queries))assert.match(q.params.id,/^[a-f0-9]{64}$/);
 assert.ok(data.queries.CoopHistoryDetailQuery);assert.ok(data.queries.CoopRecordPlayHistoryRefetchQuery);
 assert.ok(data.queries.DefeatEnemyRecordRefetchQuery);
});
