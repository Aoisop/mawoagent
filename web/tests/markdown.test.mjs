import test from 'node:test';
import assert from 'node:assert/strict';
import {renderMarkdown} from '../chat-view.mjs';

test('headings, paragraphs, strong text and inline code retain their meaning', () => {
  assert.equal(renderMarkdown('# 项目概览\n\n正文 **重点** 和 `a < b`。\n第二行\n\n### 下一步 ###\n\n标题\n===='),
    '<h1>项目概览</h1><p>正文 <strong>重点</strong> 和 <code>a &lt; b</code>。\n第二行</p><h3>下一步</h3><h1>标题</h1>');
  assert.equal(renderMarkdown('###### 六级标题'),'<h6>六级标题</h6>');
});

test('unordered and ordered lists support nested steps and ordered starting numbers', () => {
  assert.equal(renderMarkdown('- **检查**\n  - 读取文件\n  - 查看 `diff`\n- 修改\n\n3. 验证\n4. 完成'),
    '<ul><li><strong>检查</strong><ul><li>读取文件</li><li>查看 <code>diff</code></li></ul></li><li>修改</li></ul><ol start="3"><li>验证</li><li>完成</li></ol>');
  assert.equal(renderMarkdown('- 第一项\n  补充说明\n\n段落'),'<ul><li>第一项\n补充说明</li></ul><p>段落</p>');
});

test('tables render headers, alignment and cells inside a scroll wrapper', () => {
  const result = renderMarkdown('| 名称 | 数量 | 说明 |\n| :--- | ---: | :---: |\n| **文件** | 2 | `a|b` |\n| A\\|B | 3 | [文档](https://example.com/docs) |');
  assert.match(result,/^<div class="markdown-table-wrap"><table><thead><tr>/);
  assert.match(result,/<th class="align-left">名称<\/th><th class="align-right">数量<\/th><th class="align-center">说明<\/th>/);
  assert.match(result,/<td class="align-left"><strong>文件<\/strong><\/td>/);
  assert.match(result,/<td class="align-center"><code>a\|b<\/code><\/td>/);
  assert.match(result,/<td class="align-left">A\|B<\/td>/);
  assert.match(result,/<a href="https:\/\/example.com\/docs"/);
  assert.match(result,/<\/tbody><\/table><\/div>$/);
  assert.equal(renderMarkdown('A | B\n--- | ---\none |'),'<div class="markdown-table-wrap"><table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>one</td><td></td></tr></tbody></table></div>');
});

test('ordinary pipes and incomplete table delimiters stay readable as text', () => {
  assert.equal(renderMarkdown('a | b\nnot a separator'),'<p>a | b\nnot a separator</p>');
  assert.equal(renderMarkdown('| a | b |\n| -- |'),'<p>| a | b |\n| -- |</p>');
});

test('fenced code is escaped and never parsed as Markdown or HTML', () => {
  const source = '```html\n<img src=x onerror=alert(1)>\n**literal**\n| A | B |\n```\n\nAfter';
  assert.equal(renderMarkdown(source),'<pre><span class="code-label">html</span><code>&lt;img src=x onerror=alert(1)&gt;\n**literal**\n| A | B |</code></pre><p>After</p>');
  assert.equal(renderMarkdown('~~~~js\n```\n~~~~'),'<pre><span class="code-label">js</span><code>```</code></pre>');
  assert.equal(renderMarkdown('```\npartial <script>'),'<pre><span class="code-label">CODE</span><code>partial &lt;script&gt;</code></pre>');
  assert.equal(renderMarkdown('`[x](javascript:alert(1))`'),'<p><code>[x](javascript:alert(1))</code></p>');
});

test('all user HTML, labels, code languages and attribute punctuation are escaped', () => {
  const result = renderMarkdown('# <script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n| <b> |\n| --- |\n| <svg/onload=alert(1)> |\n\n```<img>\n&"\'\n```');
  assert.doesNotMatch(result,/<(?:script|img|svg|b)(?:\s|>|\/)/i);
  assert.match(result,/&lt;script&gt;/);
  assert.match(result,/<span class="code-label">&lt;img&gt;<\/span>/);
  assert.match(result,/&amp;&quot;&#39;/);
  const link = renderMarkdown('[<img>](https://example.com/?q="x")');
  assert.equal(link,'<p><a href="https://example.com/?q=&quot;x&quot;" rel="noopener noreferrer">&lt;img&gt;</a></p>');
});

test('links accept safe protocols, local paths, fragments and balanced parentheses', () => {
  const result = renderMarkdown('[site](https://example.com/a_(b)) [email](mailto:dev@example.com) [local](./guide.md) [anchor](#section) [title](https://example.com "Read docs")');
  assert.match(result,/href="https:\/\/example.com\/a_\(b\)"/);
  assert.match(result,/href="mailto:dev@example.com"/);
  assert.match(result,/href="\.\/guide.md"/);
  assert.match(result,/href="#section"/);
  assert.match(result,/title="Read docs"/);
  assert.equal((result.match(/rel="noopener noreferrer"/g) || []).length,5);
});

test('executable schemes and browser normalization tricks never become links', () => {
  for (const destination of ['javascript:alert(1)','JaVaScRiPt:alert(1)','data:text/html,<script>alert(1)</script>','vbscript:msgbox(1)','file:///etc/passwd','java\tscript:alert(1)','java\nscript:alert(1)','javascript:\u0000alert(1)','//example.com','\\\\example.com']) {
    const result = renderMarkdown('[click]('+destination+')');
    assert.doesNotMatch(result,/<a\b|<script\b/i,destination);
  }
});

test('empty values and Windows line endings are handled without mutating inputs', () => {
  assert.equal(renderMarkdown(null),'');
  assert.equal(renderMarkdown(''),'');
  assert.equal(renderMarkdown('## Title\r\n\r\n- A\r\n- B'),'<h2>Title</h2><ul><li>A</li><li>B</li></ul>');
});
