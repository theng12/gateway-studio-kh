const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../gateway/admin_ui.html'), 'utf8');

function harness(api, overrides={}) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'', textContent:'', innerHTML:'', disabled:false, hidden:false, readOnly:false, className:'', style:{}});
    return nodes.get(id);
  };
  const start = source.indexOf('const authorState =');
  const end = source.indexOf('async function loadStatus()', start);
  assert.ok(start >= 0 && end > start, 'authoring controller must exist');
  const ctx = vm.createContext({document:{getElementById:node}, api,
    esc: s => String(s ?? '').replace(/[<>&"']/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c])),
    confirm:()=>false, JSON, Promise, console,
    fetch:async()=>{throw Error('unconfigured download')}, adminHeaders:()=>({}),
    URL:{createObjectURL:()=> 'blob:fixture', revokeObjectURL:()=>{}},
    downloadBlob:()=>{}, setTimeout:()=>{}, window:{addEventListener:()=>{}}, ...overrides,
  });
  vm.runInContext(source.slice(start, end), ctx);
  return {ctx, node, state:()=>vm.runInContext('authorState',ctx)};
}
const draft = {id:'a'.repeat(64), key:'com.fixture.topic@1.0.0', item_id:'com.fixture.topic',
  pack_id:'com.fixture.pack', version:'1.0.0', document_text:'{"name":"Original"}', revision:1, sealed:false};

test('save preserves exact unfinished text and a failed save does not erase it', async()=>{
  let fail = true;
  const sent=[];
  const h=harness(async(path, options)=>{
    if (!options) return {...draft};
    sent.push(JSON.parse(options.body));
    if (fail) throw Error('revision conflict; reload');
    return {...draft, document_text:sent.at(-1).document_text, revision:2};
  });
  await h.ctx.openAuthorDraft(draft.id);
  h.node('author-document').value='{ unfinished text';
  h.ctx.authorEdited();
  await h.ctx.saveAuthorDraft();
  assert.equal(h.node('author-document').value,'{ unfinished text');
  assert.equal(h.state().dirty,true);
  assert.match(h.node('author-message').textContent,/revision conflict/);
  fail=false;
  await h.ctx.saveAuthorDraft();
  assert.equal(sent[1].document_text,'{ unfinished text');
  assert.equal(sent[1].revision,1);
  assert.equal(h.state().dirty,false);
  assert.equal(h.state().draft.revision,2);
});

test('dirty navigation is refused and an old read cannot replace another draft', async()=>{
  let finish;
  const h=harness(async path=>path.endsWith('/slow') ? new Promise(resolve=>{finish=resolve;}) : {...draft});
  await h.ctx.openAuthorDraft(draft.id);
  h.node('author-document').value='unsaved'; h.ctx.authorEdited();
  await h.ctx.openAuthorDraft('other');
  assert.equal(h.node('author-document').value,'unsaved');
  h.state().dirty=false;
  const pending=h.ctx.openAuthorDraft('slow');
  h.state().generation += 1; // Models a newer navigation superseding this read.
  h.state().draft={...draft,id:'b'.repeat(64)};
  finish({...draft,document_text:'stale response'});
  await pending;
  assert.notEqual(h.node('author-document').value,'stale response');
});

test('unavailable tools leave stored draft readable and candidate state stays explicit', async()=>{
  const h=harness(async path=>path.endsWith('/drafts') ? {drafts:[{...draft,sealed:true}]} :
    path.endsWith('/'+draft.id) ? {...draft,sealed:true} :
    {available:false,detail:'Install Story dependencies locally.'});
  await h.ctx.loadAuthoring();
  assert.match(h.node('author-prerequisites').textContent,/Install Story/);
  assert.match(h.node('author-drafts').innerHTML,/com.fixture.pack/);
  await h.ctx.openAuthorDraft(draft.id);
  assert.equal(h.node('author-document').readOnly,true);
  assert.equal(h.node('author-save').disabled,true);
  assert.match(h.node('author-message').textContent,/candidate/i);
});

test('preview escapes authored text and invalidates on further editing', async()=>{
  const h=harness(async(path,options)=>options ? {document:{name:'<img src=x onerror=alert(1)>',description:'draft',subjects:['one'],prompt_context:'<script>bad</script>'}} : {...draft});
  await h.ctx.openAuthorDraft(draft.id);
  await h.ctx.previewAuthorDraft();
  assert.ok(!h.node('author-preview').innerHTML.includes('<img'));
  assert.match(h.node('author-preview').innerHTML,/&lt;img/);
  h.node('author-document').value='changed'; h.ctx.authorEdited();
  assert.ok(!h.node('author-preview').innerHTML.includes('&lt;img'));
  assert.equal(h.node('author-export').disabled,true);
});

test('editing while another draft loads prevents that delayed read from erasing new text', async()=>{
  let finish;
  const h=harness(async path=>path.endsWith('/slow') ? new Promise(resolve=>{finish=resolve;}) : {...draft});
  await h.ctx.openAuthorDraft(draft.id);
  const pending=h.ctx.openAuthorDraft('slow');
  h.node('author-document').value='new edit while loading'; h.ctx.authorEdited();
  finish({...draft, id:'b'.repeat(64), document_text:'other draft'});
  await pending;
  assert.equal(h.node('author-document').value,'new edit while loading');
  assert.equal(h.state().dirty,true);
});

test('creation copies the selected template under the reserved identity without editing the source', async()=>{
  const original={schema_version:1,kind:'topic-pack',id:'com.fixture.original',name:'Template'};
  let created;
  const h=harness(async(path,opts)=>{
    if (path.endsWith('/template')) return {document:structuredClone(original)};
    if (opts?.method === 'POST') {created=JSON.parse(opts.body); return {...draft,document_text:created.document_text};}
    if (path.endsWith('/drafts')) return {drafts:[draft]};
    return {available:true,reservations:[{key:draft.key,item_id:draft.item_id,pack_id:draft.pack_id,version:draft.version}],templates:[{key:'template',name:'Template'}]};
  });
  await h.ctx.loadAuthoring();
  h.node('author-reservation').value=draft.key; h.node('author-template').value='template';
  await h.ctx.createAuthorDraft();
  assert.equal(created.key,draft.key);
  assert.equal(JSON.parse(created.document_text).id,draft.item_id);
  assert.equal(original.id,'com.fixture.original');
  assert.equal(h.state().draft.id,draft.id);
});

test('export authenticates, sends the revision and records the sealed state after a lost download', async()=>{
  const calls=[]; let current={...draft};
  const h=harness(async path=>path.endsWith('/drafts') ? {drafts:[]} :
    path.endsWith('/'+draft.id) ? current : {available:true,reservations:[]}, {
      confirm:()=>true, adminHeaders:()=>({'X-API-Key':'fixture-only'}),
      fetch:async(path,opts)=>{calls.push([path,opts]);current={...draft,document_text:'{"name":"Newer"}',revision:2,sealed:true};throw Error('connection lost');},
    });
  await h.ctx.openAuthorDraft(draft.id);
  await h.ctx.exportAuthorDraft();
  assert.equal(calls[0][0],'/admin/api/pack-authoring/drafts/'+draft.id+'/export');
  assert.equal(JSON.parse(calls[0][1].body).revision,1);
  assert.equal(calls[0][1].headers['X-API-Key'],'fixture-only');
  assert.equal(h.state().draft.sealed,true);
  assert.equal(h.state().draft.revision,2);
  assert.equal(h.state().savedText,'{"name":"Newer"}');
  assert.equal(h.state().dirty,false);
  assert.equal(h.node('author-document').value,'{"name":"Newer"}');
  assert.equal(h.node('author-document').readOnly,true);
  assert.match(h.node('author-message').textContent,/not confirmed/);
  assert.match(h.node('author-message').textContent,/sealed/i);
});

test('an obsolete export refresh cannot replace a newer editor generation', async()=>{
  let finishRefresh;
  const h=harness(async path=>path.endsWith('/drafts') ? {drafts:[]} :
    path.endsWith('/'+draft.id) ? new Promise(resolve=>{finishRefresh=resolve;}) : {available:true,reservations:[]}, {
      confirm:()=>true,
      fetch:async()=>{throw Error('connection lost');},
    });
  h.state().draft={...draft};
  h.state().savedText=draft.document_text;
  h.node('author-document').value=draft.document_text;
  const pending=h.ctx.exportAuthorDraft();
  await new Promise(resolve=>setImmediate(resolve));
  const newer={...draft,id:'b'.repeat(64),document_text:'newer editor'};
  h.state().generation += 1;
  h.state().draft=newer;
  h.state().savedText=newer.document_text;
  h.node('author-document').value=newer.document_text;
  finishRefresh({...draft,sealed:true});
  await pending;
  assert.equal(h.state().draft.id,newer.id);
  assert.equal(h.node('author-document').value,'newer editor');
});

test('a failed export refresh blocks editing until the draft is reopened', async()=>{
  const h=harness(async path=>{
    if (path.endsWith('/drafts')) return {drafts:[]};
    if (path.endsWith('/'+draft.id)) throw Error('refresh unavailable');
    return {available:true,reservations:[]};
  }, {
    confirm:()=>true,
    fetch:async()=>{throw Error('connection lost');},
  });
  h.state().draft={...draft};
  h.state().savedText=draft.document_text;
  h.node('author-document').value=draft.document_text;
  await h.ctx.exportAuthorDraft();
  assert.equal(h.node('author-document').readOnly,true);
  assert.equal(h.node('author-save').disabled,true);
  assert.equal(h.node('author-validate').disabled,true);
  assert.equal(h.node('author-export').disabled,true);
  assert.match(h.node('author-message').textContent,/reopen the draft/i);
});

test('sealed candidate downloads without another freeze confirmation', async()=>{
  const downloads=[];
  const h=harness(async path=>path.endsWith('/drafts') ? {drafts:[]} :
    path.endsWith('/'+draft.id) ? {...draft,sealed:true} : {available:false,detail:'Offline'}, {
      fetch:async()=>({ok:true,blob:async()=>({})}),
      downloadBlob:(url,name)=>downloads.push([url,name]),
    });
  await h.ctx.openAuthorDraft(draft.id);
  await h.ctx.exportAuthorDraft();
  assert.deepEqual(downloads,[['blob:fixture','topic-pack-candidate.sspack']]);
  assert.match(h.node('author-message').textContent,/Not published or installed/);
});
