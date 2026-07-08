// 网页逻辑：同源加载 index.json（目录）/ status.json（维护态）/ docs 下 md（文档）
// 部署方式：publish 工作流把 index.json + status.json + docs/ 一并打进 site/ 部署，
// 因此全部走同源 fetch，无 CORS、无限流。

const treeEl = document.getElementById('tree');
const content = document.getElementById('content');
const crumb = document.getElementById('crumb');
const mask = document.getElementById('mask');
const maskMsg = document.getElementById('maskMsg');
const statusTxt = document.getElementById('statusTxt');
const sidebar = document.getElementById('sidebar');
const drawerBg = document.getElementById('drawerBg');

/* ---------- 启动 ---------- */
init();

async function init() {
  await checkStatus();
  await loadIndex();
}

/* ---------- 维护态 ---------- */
async function checkStatus() {
  try {
    const r = await fetch('status.json?t=' + Date.now());
    const s = await r.json();
    if (s.status === 'maintenance') {
      mask.classList.add('show');
      if (s.message) maskMsg.textContent = s.message;
      statusTxt.textContent = '维护中';
      statusTxt.parentElement.querySelector('.led').style.background = 'var(--warn)';
    }
  } catch (e) {
    /* 忽略：状态文件缺失时按正常处理 */
  }
}

/* ---------- 索引树 ---------- */
async function loadIndex() {
  try {
    const r = await fetch('index.json?t=' + Date.now());
    const data = await r.json();
    renderTree(data.tree || {});
  } catch (e) {
    treeEl.innerHTML = '<div class="loading">索引加载失败，请稍后刷新</div>';
  }
}

function renderTree(tree) {
  treeEl.innerHTML = '';
  const years = Object.keys(tree).sort().reverse();
  if (years.length === 0) {
    treeEl.innerHTML = '<div class="loading">暂无总结文档</div>';
    return;
  }
  for (const y of years) {
    const yNode = node('year', y);
    const months = Object.keys(tree[y]).sort().reverse();
    for (const m of months) {
      const mNode = node('month', m + ' 月');
      for (const d of tree[y][m]) {
        const dNode = document.createElement('div');
        dNode.className = 'day';
        dNode.innerHTML = `<div class="row"><span>${d.day}</span><span class="badge">${d.count} 条</span></div>`;
        dNode.querySelector('.row').onclick = () => openDay(d.file, `${y}-${m}-${d.day}`);
        mNode.children.appendChild(dNode);
      }
      yNode.children.appendChild(mNode.row);
    }
    treeEl.appendChild(yNode.row);
  }
}

function node(type, label) {
  const wrap = document.createElement('div');
  wrap.className = 'node-' + type;
  const row = document.createElement('div');
  row.className = 'row';
  row.innerHTML = `<span class="caret">▾</span><span>${label}</span>`;
  const children = document.createElement('div');
  children.className = 'children';
  row.onclick = () => wrap.classList.toggle('collapsed');
  wrap.appendChild(row);
  wrap.appendChild(children);
  return { row: wrap, children };
}

/* ---------- 打开某一天：按需加载 md ---------- */
async function openDay(file, dateLabel) {
  document.querySelectorAll('.day').forEach(e => e.classList.remove('active'));
  // 高亮
  const days = treeEl.querySelectorAll('.day');
  days.forEach(e => {
    if (e.querySelector('.row').textContent.includes(dateLabel.split('-')[2])) e.classList.add('active');
  });
  crumb.textContent = dateLabel;
  content.innerHTML = '<div class="loading"><div class="spinner"></div>正在加载 ' + dateLabel + ' 的总结…</div>';
  try {
    const r = await fetch(file + '?t=' + Date.now());
    const md = await r.text();
    content.innerHTML = mdToHtml(md);
    closeDrawer();
  } catch (e) {
    content.innerHTML = '<div class="empty">文档加载失败，请稍后重试</div>';
  }
}

/* ---------- 极简 Markdown 渲染 ---------- */
function mdToHtml(md) {
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const lines = md.split('\n');
  let html = '', inList = false;
  const closeList = () => { if (inList) { html += '</ul>'; inList = false; } };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    if (/^# /.test(line)) { closeList(); html += `<div class="doc-date">${inline(esc(line.slice(2)))}</div>`; }
    else if (/^## /.test(line)) { closeList(); html += `<h2 class="doc-title">${inline(esc(line.slice(3)))}</h2>`; }
    else if (/^### /.test(line)) { closeList(); html += `<h3>${inline(esc(line.slice(4)))}</h3>`; }
    else if (/^> /.test(line)) { closeList(); html += `<blockquote>${inline(esc(line.slice(2)))}</blockquote>`; }
    else if (/^[-*] /.test(line)) { if (!inList) { html += '<ul>'; inList = true; } html += `<li>${inline(esc(line.slice(2)))}</li>`; }
    else if (line.trim() === '') { closeList(); }
    else { closeList(); html += `<p>${inline(esc(line))}</p>`; }
  }
  closeList();
  return html;
}

function inline(s) {
  return s
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

/* ---------- 移动端抽屉 ---------- */
function openDrawer() { sidebar.classList.add('open'); drawerBg.classList.add('show'); }
function closeDrawer() { sidebar.classList.remove('open'); drawerBg.classList.remove('show'); }
document.getElementById('menuBtn').onclick = openDrawer;
drawerBg.onclick = closeDrawer;
