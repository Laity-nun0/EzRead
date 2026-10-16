"use strict";
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
function element(tag, attrs = {}, ...children) { return { tag, attrs, children: children.flat(Infinity).filter(value => value != null), value: attrs.value || '', disabled: Boolean(attrs.disabled), listeners: {}, replaceChildren(...values) { this.children = values.flat(Infinity); }, append(...values) { this.children.push(...values.flat(Infinity)); }, addEventListener(name, callback) { this.listeners[name] = callback; } }; }
const fixed={provider:'deepseek',base_url:'https://api.deepseek.com',model:'deepseek-flash',max_output_tokens:393216,has_key:true};
const settings={features:{translation:{...fixed,thinking:'disabled'},chat:{...fixed,thinking:'enabled',reasoning_effort:'high'}}};
const calls=[];
const context=vm.createContext({el:element,field:(label,input)=>element('label',{},label,input),api:async url=>{calls.push(url);return settings;},console});
vm.runInContext(fs.readFileSync(`${__dirname}/../static/api-translation-ui.js`,'utf8'),context);
function all(node) { return [node,...node.children.flatMap(value=>typeof value==='object'?all(value):[])]; }
(async()=>{
 const picker=context.apiTranslationPicker(settings,{},false,true), controls=all(picker.root);
 assert.equal(calls.length,0); assert.equal(picker.ready(),true); assert.equal(picker.value().model,'deepseek-flash'); assert.equal(picker.value().thinking,'disabled');
 assert.equal(picker.value().reasoning_effort,undefined); assert.equal(controls.filter(node=>node.tag==='select').length,0);
 assert.equal(controls.filter(node=>node.tag==='input'&&['text','url','number'].includes(node.attrs.type)).length,0);
 const thinking=controls.find(node=>node.attrs['aria-label']==='思考模式'),high=controls.find(node=>node.attrs['aria-label']==='高 · high'),max=controls.find(node=>node.attrs['aria-label']==='最高 · max');
 thinking.checked=true;thinking.listeners.change(); assert.equal(high.checked,true); assert.equal(picker.value().reasoning_effort,'high');
 high.checked=false;max.checked=true;assert.equal(picker.value().reasoning_effort,'max');
 thinking.checked=false;thinking.listeners.change();assert.equal(picker.value().reasoning_effort,undefined);
 picker.disable(true);assert.equal(thinking.disabled,true);picker.disable(false);assert.equal(thinking.disabled,false);
 picker.setConfig({...fixed,model:'frozen-old-model'});assert.equal(picker.value().model,'frozen-old-model');assert.equal(picker.ready(),false);
 picker.key.value='secret';picker.dispose();assert.equal(picker.key.value,'');
 const chat=context.apiTranslationPicker(settings,{},true,false,'chat');assert.equal(chat.value().thinking,'enabled');assert.equal(chat.value().reasoning_effort,'high');
 const chatModel=all(chat.root).find(node=>node.attrs['aria-label']==='问答默认模型');
 assert.deepEqual(chatModel.children.map(node=>node.attrs.value),['deepseek-flash','deepseek-v4-pro']);
 assert.equal(chatModel.value,'deepseek-flash');chatModel.value='deepseek-v4-pro';assert.equal(chat.value().model,'deepseek-v4-pro');
 assert.notEqual(all(chat.root).find(node=>node.attrs.type==='radio').attrs.name,high.attrs.name,'Each picker owns a separate radio group.');
 const missing=context.apiTranslationPicker({...settings,features:{translation:{...fixed,has_key:false}}});assert.equal(missing.ready(),false);
 await context.loadTranslationApiSettings();assert.deepEqual(calls,['/api/translation-api']);
 console.log('Fixed DeepSeek picker: independent feature defaults, thinking, frozen snapshot, key disposal and local-only loading passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
