const escapeHTML = value => String(value).replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));

function safeLink(destination) {
  // Reject browser URL normalization tricks as well as executable schemes.
  if (!destination || /[\s\u0000-\u001f\u007f\\]/.test(destination)) return false;
  if (/^(?:https?:\/\/|mailto:)/i.test(destination)) return true;
  return !/^[a-z][a-z\d+.-]*:/i.test(destination) && !destination.startsWith('//');
}

function closingMarker(text, marker, start) {
  let position = text.indexOf(marker,start);
  while (position !== -1) {
    let slashes = 0;
    for (let index = position-1; index >= 0 && text[index] === '\\'; index--) slashes++;
    if (slashes % 2 === 0) return position;
    position = text.indexOf(marker,position+marker.length);
  }
  return -1;
}

function inlineMarkdown(text, depth = 0) {
  if (depth > 16) return escapeHTML(text);
  const output = [];
  for (let index = 0; index < text.length;) {
    const character = text[index];
    if (character === '\\' && /[!"#$%&'()*+,\-./:;<=>?@[\]\\^_`{|}~]/.test(text[index+1] || '')) {
      output.push(escapeHTML(text[index+1]));index += 2;continue;
    }
    if (character === '`') {
      const marker = text.slice(index).match(/^`+/)[0], end = text.indexOf(marker,index+marker.length);
      if (end !== -1) {
        output.push('<code>'+escapeHTML(text.slice(index+marker.length,end).replace(/\n/g,' '))+'</code>');
        index = end+marker.length;continue;
      }
    }
    const marker = text.slice(index,index+2);
    if (marker === '**' || marker === '__') {
      const end = closingMarker(text,marker,index+2);
      if (end > index+2) {
        output.push('<strong>'+inlineMarkdown(text.slice(index+2,end),depth+1)+'</strong>');
        index = end+2;continue;
      }
    }
    if (character === '[') {
      const labelEnd = closingMarker(text,'](',index+1);
      if (labelEnd !== -1) {
        let end = labelEnd+2, parentheses = 1;
        for (; end < text.length; end++) {
          if (text[end] === '\\') {end++;continue;}
          if (text[end] === '(') parentheses++;
          if (text[end] === ')' && --parentheses === 0) break;
        }
        if (parentheses === 0) {
          const raw = text.slice(labelEnd+2,end).trim();
          const target = raw.match(/^(?:<([^<>]*)>|([^\s]+?))(?:\s+["']([^"']*)["'])?$/);
          const destination = target && (target[1] ?? target[2]);
          if (destination && safeLink(destination)) {
            const title = target[3] === undefined ? '' : ' title="'+escapeHTML(target[3])+'"';
            output.push('<a href="'+escapeHTML(destination)+'"'+title+' rel="noopener noreferrer">'+inlineMarkdown(text.slice(index+1,labelEnd),depth+1)+'</a>');
          } else output.push(escapeHTML(text.slice(index,end+1)));
          index = end+1;continue;
        }
      }
    }
    output.push(escapeHTML(character));index++;
  }
  return output.join('');
}

function tableCells(line) {
  const cells = [], text = line.trim();
  let current = '', codeMarker = '';
  for (let index = 0; index < text.length; index++) {
    if (text[index] === '\\' && index+1 < text.length) {current += text[index]+text[++index];continue;}
    if (text[index] === '`') {
      const marker = text.slice(index).match(/^`+/)[0];
      if (!codeMarker) codeMarker = marker;else if (codeMarker === marker) codeMarker = '';
      current += marker;index += marker.length-1;continue;
    }
    if (text[index] === '|' && !codeMarker) {cells.push(current.trim());current = '';}
    else current += text[index];
  }
  cells.push(current.trim());
  if (text.startsWith('|')) cells.shift();
  if (text.endsWith('|') && cells.at(-1) === '') cells.pop();
  return cells;
}

function tableHeader(lines, index) {
  if (index+1 >= lines.length || !lines[index].includes('|')) return null;
  const headers = tableCells(lines[index]), separators = tableCells(lines[index+1]);
  if (!headers.length || headers.length !== separators.length || !separators.every(cell => /^:?-{3,}:?$/.test(cell))) return null;
  return {headers,alignments:separators.map(cell => cell.startsWith(':') ? (cell.endsWith(':') ? 'center':'left') : cell.endsWith(':') ? 'right':'' )};
}

const listMarker = line => /^( *)([-+*]|\d{1,9}[.)])[ \t]+(.*)$/.exec(line);
const fenceMarker = line => /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
const headingMarker = line => /^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$/.exec(line);

function markdownBlocks(lines, depth = 0) {
  if (depth > 16) return '<p>'+escapeHTML(lines.join('\n'))+'</p>';
  const output = [];
  for (let index = 0; index < lines.length;) {
    if (!lines[index].trim()) {index++;continue;}
    const fence = fenceMarker(lines[index]);
    if (fence) {
      const code = [], closing = new RegExp('^ {0,3}'+fence[1][0]+'{'+fence[1].length+',}\\s*$');
      const language = fence[2].trim().split(/\s+/)[0] || 'CODE';
      index++;
      while (index < lines.length && !closing.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index++;
      output.push('<pre><span class="code-label">'+escapeHTML(language)+'</span><code>'+escapeHTML(code.join('\n'))+'</code></pre>');
      continue;
    }
    const heading = headingMarker(lines[index]);
    if (heading) {
      const level = heading[1].length;
      output.push('<h'+level+'>'+inlineMarkdown(heading[2].replace(/[ \t]+#+[ \t]*$/,''))+'</h'+level+'>');index++;continue;
    }
    if (index+1 < lines.length && /^ {0,3}(?:=+|-+)[ \t]*$/.test(lines[index+1])) {
      const level = lines[index+1].trim()[0] === '=' ? 1 : 2;
      output.push('<h'+level+'>'+inlineMarkdown(lines[index].trim())+'</h'+level+'>');index += 2;continue;
    }
    const table = tableHeader(lines,index);
    if (table) {
      const cell = (tag,text,column) => '<'+tag+(table.alignments[column] ? ' class="align-'+table.alignments[column]+'"':'')+'>'+inlineMarkdown(text)+'</'+tag+'>';
      const rows = [];index += 2;
      while (index < lines.length && lines[index].trim() && lines[index].includes('|') && !fenceMarker(lines[index]) && !headingMarker(lines[index]) && !listMarker(lines[index])) {
        const values = tableCells(lines[index++]);
        rows.push('<tr>'+table.headers.map((_,column) => cell('td',values[column] || '',column)).join('')+'</tr>');
      }
      output.push('<div class="markdown-table-wrap"><table><thead><tr>'+table.headers.map((text,column) => cell('th',text,column)).join('')+'</tr></thead><tbody>'+rows.join('')+'</tbody></table></div>');
      continue;
    }
    const first = listMarker(lines[index]);
    if (first) {
      const indent = first[1].length, ordered = /^\d/.test(first[2]), tag = ordered ? 'ol':'ul', items = [];
      const start = ordered && parseInt(first[2],10) !== 1 ? ' start="'+parseInt(first[2],10)+'"' : '';
      while (index < lines.length) {
        const item = listMarker(lines[index]);
        if (!item || item[1].length !== indent || /^\d/.test(item[2]) !== ordered) break;
        const body = [item[3]];index++;
        while (index < lines.length) {
          if (!lines[index].trim()) {
            let next = index+1;
            while (next < lines.length && !lines[next].trim()) next++;
            if (next >= lines.length || (lines[next].match(/^ */)[0].length <= indent && !listMarker(lines[next]))) break;
            if (lines[next].match(/^ */)[0].length <= indent) {index = next;break;}
            body.push('');index++;continue;
          }
          const leading = lines[index].match(/^ */)[0].length;
          if (leading <= indent) break;
          body.push(lines[index++].slice(Math.min(leading,indent+2)));
        }
        let content = markdownBlocks(body,depth+1);
        if (!body.some(line => !line.trim())) content = content.replace(/^<p>([\s\S]*?)<\/p>/,'$1');
        items.push('<li>'+content+'</li>');
      }
      output.push('<'+tag+start+'>'+items.join('')+'</'+tag+'>');continue;
    }
    const paragraph = [lines[index++]];
    while (index < lines.length && lines[index].trim() && !fenceMarker(lines[index]) && !headingMarker(lines[index]) && !listMarker(lines[index]) && !tableHeader(lines,index)) {
      if (index+1 < lines.length && /^ {0,3}(?:=+|-+)[ \t]*$/.test(lines[index+1])) break;
      paragraph.push(lines[index++]);
    }
    output.push('<p>'+inlineMarkdown(paragraph.join('\n'))+'</p>');
  }
  return output.join('');
}

/** Render a safe, dependency-free subset of Markdown for streamed chat text. */
export function renderMarkdown(value) {
  return markdownBlocks(String(value ?? '').replace(/\r\n?/g,'\n').split('\n'));
}

/** Patch keyed entries in place so scroll, focus, and native controls survive. */
function patchNode(node, next) {
  if (node.nodeType !== next.nodeType || node.nodeName !== next.nodeName) {
    node.replaceWith(next);
    return next;
  }
  if (node.nodeType === 3) {
    if (node.data !== next.data) {
      if (next.data.startsWith(node.data)) node.appendData(next.data.slice(node.data.length));
      else node.data = next.data;
    }
    return node;
  }
  if (node.nodeType !== 1) return node;
  for (const attribute of Array.from(node.attributes)) {
    if (attribute.name !== 'data-chat-key' && !next.hasAttribute(attribute.name)) node.removeAttribute(attribute.name);
  }
  for (const attribute of Array.from(next.attributes)) {
    if (node.getAttribute(attribute.name) !== attribute.value) node.setAttribute(attribute.name, attribute.value);
  }
  const previous = Array.from(node.childNodes), incoming = Array.from(next.childNodes);
  for (let index = 0; index < Math.max(previous.length, incoming.length); index++) {
    if (!incoming[index]) previous[index].remove();
    else if (!previous[index]) node.append(incoming[index]);
    else patchNode(previous[index], incoming[index]);
  }
  return node;
}

export function reconcileEntries(container, entries) {
  const previous = new Map(Array.from(container.children).map(node => [node.dataset.chatKey, node]));
  let cursor = container.firstElementChild;
  for (const {key, html} of entries) {
    let node = previous.get(key);
    const wasCursor = node && node === cursor;
    previous.delete(key);
    if (!node || node.chatHTML !== html) {
      const template = container.ownerDocument.createElement('template');
      template.innerHTML = html;
      const next = template.content.firstElementChild;
      node = node ? patchNode(node, next) : next;
      node.dataset.chatKey = key;
      node.chatHTML = html;
    }
    if (wasCursor) cursor = node;
    if (node !== cursor) container.insertBefore(node, cursor);
    cursor = node.nextElementSibling;
  }
  for (const node of previous.values()) node.remove();
}
