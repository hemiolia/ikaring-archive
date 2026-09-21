// Read Relay's generated literal data using an AST interpreter. Never execute downloaded JS.
import {parse} from 'acorn';
import fs from 'node:fs';
import crypto from 'node:crypto';
function value(n,e) {
 if(!n) return undefined;
 switch(n.type) {
 case 'Literal': return n.value;
 case 'Identifier': if(n.name==='undefined')return undefined; if(e.has(n.name))return e.get(n.name);throw Error('external identifier');
 case 'ArrayExpression':return n.elements.map(x=>value(x,e));
 case 'ObjectExpression':return Object.fromEntries(n.properties.map(p=>{if(p.type!=='Property'||p.computed)throw Error('computed');return [p.key.name??p.key.value,value(p.value,e)]}));
 case 'UnaryExpression':if(n.operator==='!')return !value(n.argument,e);if(n.operator==='-')return -value(n.argument,e);if(n.operator==='void')return undefined;throw Error('unary');
 case 'CallExpression':if(n.callee.type==='FunctionExpression'&&!n.arguments.length)return func(n.callee);throw Error('call');
 default:throw Error(n.type);
 }
}
function func(n){const e=new Map();for(const s of n.body.body){if(s.type==='VariableDeclaration'){for(const d of s.declarations){if(d.id.type!=='Identifier')throw Error('binding');e.set(d.id.name,value(d.init,e));}}else if(s.type==='ReturnStatement')return value(s.argument,e);else if(s.type!=='EmptyStatement')throw Error('statement');}}
export function extract(source){
 const ast=parse(source,{ecmaVersion:'latest',sourceType:'script'});const found={};
 function walk(n){if(!n||typeof n!=='object')return;
  if(n.type==='FunctionExpression'&&n.body.type==='BlockStatement') {try {const v=func(n);if(v?.kind==='Request'&&v.params?.name)found[v.params.name]=v;}catch{}}
  if(n.type==='ObjectExpression') {try{const v=value(n,new Map());if(v?.kind==='Request'&&v.params?.name)found[v.params.name]=v;}catch{}}
  for(const [k,v] of Object.entries(n)){if(k==='start'||k==='end')continue;if(Array.isArray(v))v.forEach(walk);else if(v&&typeof v==='object')walk(v);}
 }
 walk(ast);return {source_sha256:crypto.createHash('sha256').update(source).digest('hex'),queries:found};
}
if(process.argv[1]===new URL(import.meta.url).pathname){const s=fs.readFileSync(process.argv[2],'utf8');console.log(JSON.stringify(extract(s)));}
