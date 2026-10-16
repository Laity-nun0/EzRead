"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
function el(tag,attrs={},...children){return{tag,attrs,children:children.flat(Infinity).filter(value=>value!=null),append(...items){this.children.push(...items.flat(Infinity));},replaceChildren(...items){this.children=items.flat(Infinity);}};}
function text(node){return node.children.map(value=>typeof value==='object'?text(value):String(value)).join(' ');}
const nodes=new Map(),calls=[];let fail=false,resolve;
const usage={scope:'application_run',requests:{attempted:3,succeeded:1,failed:1,cancelled:1},tokens:{input:12,output:5,total:17},usage_reported:1,models:[{provider:'deepseek',model:'mock-model',requests:{attempted:3,succeeded:1,failed:1,cancelled:1},tokens:{input:12,output:null,total:12},usage_reported:1,usage:{cached_tokens:2,'completion_tokens_details.reasoning_tokens':4}}],last:{provider:'deepseek',model:'mock-model',channel:'qa',status:'success',at:'2026-10-15T01:00:00Z'},account_balance:null};
const value={configuration:{provider:'deepseek',label:'DeepSeek',model:'mock-model',base_url:'https://api.deepseek.com',configured:true,message:'仅本机配置完整'},usage};
const context=vm.createContext({state:{papers:[],status:null,usage:null},el,button:(label,action)=>el('button',{action},label),apiProviderLabel:value=>value,console,$:selector=>{if(!nodes.has(selector))nodes.set(selector,{open:false,replaceChildren(){}});return nodes.get(selector);},renderLibrary(){},api:url=>{calls.push(url);return new Promise((ok,bad)=>{resolve=()=>fail?bad(Error('local offline')):ok(value);});}});
vm.runInContext(fs.readFileSync(`${__dirname}/../static/tasks/status.js`,'utf8'),context);
(async()=>{
  const first=context.refreshStatus(),second=context.refreshStatus();assert.equal(calls.length,1);resolve();await Promise.all([first,second]);
  assert.deepEqual(calls,['/api/ai/status']);
  const status=text(context.apiStatusPanel());assert.match(status,/配置已就绪（未在线验证）/);assert.doesNotMatch(status,/已连接|认证成功|登录/);
  const rendered=text(context.usagePanel(false));assert.match(rendered,/请求 3 · 成功 1 · 失败 1 · 取消 1/);assert.match(rendered,/输入 token 12 · 输出 token 5 · 合计 17/);assert.match(rendered,/覆盖 1\/3/);assert.match(rendered,/输出 token 未知/);assert.match(rendered,/cached_tokens 2/);assert.match(rendered,/思考 token：4/);assert.match(rendered,/余额与费用请到供应商控制台/);assert.doesNotMatch(rendered,/%|重置|订阅/);
  context.state.usage={...usage,models:[{...usage.models[0],usage:{}}],tokens:{input:null,output:null,total:null},usage_reported:0};assert.match(text(context.usagePanel(false)),/输入 token 未知/);assert.match(text(context.usagePanel(false)),/思考 token：未知/);
  fail=true;const failed=context.refreshStatus();resolve();await assert.rejects(failed,/local offline/);assert.equal(context.state.status.configuration.configured,true);assert.match(text(context.apiStatusPanel()),/状态读取失败/);
  console.log('API status: shared local reads, configuration-only readiness, run usage, partial coverage, unknown tokens and retained state on failure passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
