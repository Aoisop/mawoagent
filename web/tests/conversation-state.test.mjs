import test from 'node:test';
import assert from 'node:assert/strict';
import {DraftStore, acceptConversationSnapshot, applyConversationDelta} from '../conversation-state.mjs';

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes,no) => {resolve=yes;reject=no;});
  return {promise,resolve,reject};
}
const file = (name, read) => ({name,size:10,text:read});

test('successful send consumes only the submitted draft revision', async () => {
  const drafts = new DraftStore(), request = deferred();
  drafts.save('a','submitted',[]);
  const sent = drafts.send('a',() => request.promise);
  drafts.save('a','next task',[]);
  request.resolve({revision:2});
  assert.deepEqual(await sent,{revision:2});
  assert.equal(drafts.get('a').text,'next task');
});

test('unchanged sent draft clears while a different session remains intact', async () => {
  const drafts = new DraftStore(), request = deferred();
  drafts.save('a','submitted',[{name:'a.txt',text:'body'}]);
  const sent = drafts.send('a',() => request.promise);
  drafts.save('b','other draft',[]);
  // Saving during navigation does not invent an edit to the submitted draft.
  const old = drafts.get('a');drafts.save('a',old.text,[...old.attachments]);
  request.resolve({});await sent;
  assert.equal(drafts.get('a').text,'');
  assert.deepEqual(drafts.get('a').attachments,[]);
  assert.equal(drafts.get('b').text,'other draft');
});

test('failed send retains text and files', async () => {
  const drafts = new DraftStore();
  drafts.save('a','retry',[{name:'a.txt',text:'body'}]);
  await assert.rejects(drafts.send('a',async () => {throw new Error('offline');}),/offline/);
  assert.equal(drafts.get('a').text,'retry');
  assert.equal(drafts.get('a').attachments.length,1);
});

test('attachment completion stays in its original session and preserves newer text', async () => {
  const drafts = new DraftStore(), read = deferred();
  drafts.save('a','before',[]);
  const adding = drafts.attachFiles('a',[file('a.txt',() => read.promise)]);
  drafts.save('b','other session',[]);
  drafts.save('a','edited while reading',[]);
  read.resolve('contents');await adding;
  assert.equal(drafts.get('a').text,'edited while reading');
  assert.equal(drafts.get('a').attachments[0].text,'contents');
  assert.equal(drafts.get('b').attachments.length,0);
});

test('concurrent file reads enforce the combined limit on completion', async () => {
  const drafts = new DraftStore(), first = deferred(), second = deferred();
  const group = read => [0,1,2].map(i => file(`${i}.txt`,() => read.promise));
  const a = drafts.attachFiles('a',group(first));
  const b = drafts.attachFiles('a',group(second));
  first.resolve('a');await a;
  second.resolve('b');await assert.rejects(b,/最多附加/);
  assert.equal(drafts.get('a').attachments.length,3);
});

test('a file arriving during send prevents that new draft from being cleared', async () => {
  const drafts = new DraftStore(), request = deferred();
  drafts.save('a','submitted',[]);
  const sent = drafts.send('a',() => request.promise);
  await drafts.attachFiles('a',[file('next.txt',async () => 'new')]);
  request.resolve({});await sent;
  assert.equal(drafts.get('a').attachments[0].name,'next.txt');
});

function state() {return {id:'a',revision:1,entries:[{id:'answer',text:'前文🙂'}],busy:true};}
function delta(values={}) {return {id:'a',baseRevision:1,revision:2,changes:{},entries:[],...values};}

test('text deltas append unicode without copying or mutating older revisions', () => {
  const old = state();
  const next = applyConversationDelta(old,delta({entries:[{id:'answer',append:{text:'新文'}}]}));
  assert.equal(next.entries[0].text,'前文🙂新文');
  assert.equal(old.entries[0].text,'前文🙂');
  assert.equal(next.revision,2);
});

test('rollback can replace a partial response and remove failed entries', () => {
  const old = state();old.entries.push({id:'failed',text:'discard'});
  const next = applyConversationDelta(old,delta({entries:[{id:'answer',set:{text:'retry'}}],order:['answer'],changes:{busy:false}}));
  assert.deepEqual(next.entries,[{id:'answer',text:'retry'}]);
  assert.equal(next.busy,false);
});

test('entry insertion and removal use authoritative order including the empty list', () => {
  let current = applyConversationDelta(state(),delta({entries:[{id:'tool',set:{id:'tool',kind:'tool',result:''}}],order:['answer','tool']}));
  assert.equal(current.entries[1].kind,'tool');
  current = applyConversationDelta(current,delta({baseRevision:2,revision:3,order:[]}));
  assert.deepEqual(current.entries,[]);
});

test('missing revisions require resync, duplicates are ignored, other sessions rejected', () => {
  const current = state();
  assert.throws(() => applyConversationDelta(current,delta({baseRevision:2,revision:3})),/Missing/);
  assert.throws(() => applyConversationDelta(current,delta({id:'b'})),/Conversation/);
  assert.equal(applyConversationDelta(current,delta({baseRevision:0,revision:1})),current);
  const snapshot = {...state(),revision:5};
  const next = applyConversationDelta(snapshot,delta({baseRevision:5,revision:6,entries:[{id:'answer',append:{text:' recovered'}}]}));
  assert.equal(next.entries[0].text,'前文🙂 recovered');
});


test('server restart accepts a lower revision snapshot from a fresh stream', () => {
  const old = {...state(),streamId:'old-process',revision:100};
  const restarted = {...state(),streamId:'new-process',revision:1};
  assert.equal(acceptConversationSnapshot(old,restarted),restarted);
  assert.equal(acceptConversationSnapshot(old,{...old,revision:50}),old);
  assert.equal(acceptConversationSnapshot(old,{...restarted,id:'b'}),old);
  assert.throws(() => applyConversationDelta(old,delta({streamId:'new-process',baseRevision:100,revision:101})),/stream changed/);
});
