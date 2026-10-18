/** Session-local composer state. Async completions may only consume their own version. */
export class DraftStore {
  constructor() {this.items = new Map();}
  get(id) {return this.items.get(id) || {revision:0,text:'',attachments:[]};}
  save(id, text, attachments) {
    const old = this.get(id);
    if (old.text === text && old.attachments.length === attachments.length && old.attachments.every((file,index) => file === attachments[index])) return old;
    const next = {revision:old.revision+1,text,attachments:[...attachments]};
    this.items.set(id,next);
    return next;
  }
  consume(id, revision) {
    if (this.get(id).revision !== revision) return false;
    this.save(id,'',[]);
    return true;
  }
  async send(id, request) {
    const submitted = this.get(id);
    const result = await request();
    this.consume(id,submitted.revision);
    return result;
  }
  async attachFiles(id, files) {
    if (this.get(id).attachments.length + files.length > 5) throw new Error('最多附加 5 个文本文件。');
    const added = [];
    for (const file of files) {
      if (file.size > 60000) throw new Error(`${file.name} 超过 60 KB，请缩小后添加。`);
      const text = await file.text();
      if (text.includes('\0') || text.includes('\uFFFD')) throw new Error(`${file.name} 不是 UTF-8 文本文件。`);
      added.push({name:file.name,text});
    }
    // Other reads or typing may have completed while these files were loading.
    const latest = this.get(id);
    if (latest.attachments.length + added.length > 5) throw new Error('最多附加 5 个文本文件。');
    return this.save(id,latest.text,[...latest.attachments,...added]);
  }
}

/** A restarted server has a fresh stream even when it restores the same session. */
export function acceptConversationSnapshot(current, next) {
  if (next.id !== current.id) return current;
  if (next.streamId !== current.streamId || next.revision > current.revision) return next;
  return current;
}

/** Apply a whole revision atomically; a gap must be repaired with a fresh snapshot. */
export function applyConversationDelta(current, delta) {
  if (delta.id !== current.id) throw new Error('Conversation changed');
  if (delta.streamId !== current.streamId) throw new Error('Conversation stream changed');
  if (delta.revision <= current.revision) return current;
  if (delta.baseRevision !== current.revision || delta.revision !== delta.baseRevision + 1) throw new Error('Missing conversation revision');
  const entries = new Map(current.entries.map(entry => [entry.id,entry]));
  for (const update of delta.entries || []) {
    const entry = {...entries.get(update.id),...update.set};
    for (const key of update.remove || []) delete entry[key];
    for (const [key, suffix] of Object.entries(update.append || {})) {
      if (typeof entry[key] !== 'string' || typeof suffix !== 'string') throw new Error('Invalid text delta');
      entry[key] += suffix;
    }
    if (entry.id !== update.id) throw new Error('Invalid entry delta');
    entries.set(update.id,entry);
  }
  const order = delta.order || current.entries.map(entry => entry.id);
  const ordered = order.map(id => {
    if (!entries.has(id)) throw new Error('Missing entry');
    return entries.get(id);
  });
  return {...current,...delta.changes,entries:ordered,revision:delta.revision};
}
