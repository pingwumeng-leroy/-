/* 合胜宏 · 抖音内容工作台 前端逻辑 */
const $ = id => document.getElementById(id);
let STATE = {templates: [], llm: false, asr: false};
let CUR = {scriptId: null, script: null, talkPath: null, reportOpen: {}};
let POLL = null;
let HPOLL = null;

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let j;
  try { j = await r.json(); } catch (e) { throw new Error('服务返回异常'); }
  if (!j.ok) throw new Error(j.error || '请求失败');
  return j.data !== undefined ? j.data : j;
}
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('show'), 2600);
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function gotoStep(id) {
  document.querySelectorAll('.card').forEach(c => c.classList.toggle('active', c.id === id));
  document.querySelectorAll('.step').forEach(s => s.classList.toggle('active', s.dataset.target === id));
  window.scrollTo({top: 0, behavior: 'smooth'});
  if (id === 's4') loadShootScripts();
  if (id === 's5') loadJobs();
}
document.querySelectorAll('.step').forEach(s => s.onclick = () => gotoStep(s.dataset.target));

/* ---------- 初始化 ---------- */
async function boot() {
  try {
    const s = await api('/api/state');
    STATE = {templates: s.templates, llm: s.llm_enabled, asr: s.asr_enabled};
    $('brandSub').textContent = s.company.name || '四川合胜宏科技集团有限公司';
    const b = $('engineBadge');
    b.textContent = s.llm_enabled ? 'AI 已接入' : '内置引擎模式';
    b.className = 'badge ' + (s.llm_enabled ? 'on' : 'off');
    b.title = s.llm_enabled ? '分析与文案由你的大模型生成' : '未配置大模型，使用内置规则引擎，全流程可用';
    $('tplSelect').innerHTML = s.templates.map(t =>
      `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('');
    descTpl();
    $('tplSelect').onchange = descTpl;
  } catch (e) { toast('初始化失败：' + e.message); }
  await Promise.all([loadVideos(), loadReports(), loadScripts(), loadJobs(),
                     loadProviders(), loadHarvestJobs()]);
}
function descTpl() {
  const t = STATE.templates.find(x => x.id === $('tplSelect').value);
  if (!t) return;
  $('tplDesc').textContent = `${t.width}×${t.height} 竖屏 · 片头 ${t.intro_seconds}s / 片尾 ${t.outro_seconds}s · ` +
    (t.burn_subtitle ? '自动烧录字幕' : '无字幕') + (t.watermark ? ' · 带水印' : '');
  $('ovTrimHead').value = t.trim_head || 0;
  $('ovTrimTail').value = t.trim_tail || 0;
  $('ovSpeed').value = t.speed || 1;
}

/* ---------- 1 素材 ---------- */
async function loadVideos() {
  const list = await api('/api/videos');
  $('nVideos').textContent = list.length + ' 条素材';
  $('videoList').innerHTML = list.length ? list.map(v => {
    const thumb = v.cover_path ? `<img class="thumb" src="/media/${esc(v.cover_path.split('/').pop())}"/>` : '<div class="thumb"></div>';
    const st = {'new':'待补文案','transcribed':'已提取','analyzed':'已分析'}[v.status] || v.status;
    const cls = v.status === 'analyzed' ? 'ok' : (v.status === 'transcribed' ? 'blue' : '');
    return `<div class="item">${thumb}<div class="body">
      <div class="t">${esc(v.title || '未命名')}</div>
      <div class="d">${esc(v.source_type === 'douyin' ? '抖音采集' : '本地上传')} · ${v.duration||0}s · ${esc(v.transcript_source || '暂无口播文字')}</div>
      </div><div class="acts">
        <span class="pill ${cls}">${st}</span>
        <button class="btn" onclick="openTranscript(${v.id})">补文案</button>
        <button class="btn primary" onclick="doAnalyze(${v.id})">分析 →</button>
        <button class="btn" onclick="delVideo(${v.id})">删</button>
      </div></div>`;
  }).join('') : '<div class="empty">还没有素材。用上面的抖音链接采集，或直接上传一个视频文件。</div>';
}

async function fetchDouyin() {
  const url = $('dyUrl').value.trim();
  if (!url) return toast('先粘贴链接');
  const btn = $('btnFetch'); btn.disabled = true; btn.textContent = '采集中…（可能要等一会）';
  try {
    await api('/api/videos/fetch', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url, cookie: $('dyCookie').value.trim()})});
    toast('采集成功'); await loadVideos();
  } catch (e) { toast('采集失败：' + e.message); }
  finally { btn.disabled = false; btn.textContent = '采集这条视频'; }
}

function bindUpload(inputId, dropId, handler) {
  const input = $(inputId), drop = $(dropId);
  input.onchange = () => { if (input.files[0]) handler(input.files[0]); input.value = ''; };
  ['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, e => {e.preventDefault(); drop.classList.add('over');}));
  ['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {e.preventDefault(); drop.classList.remove('over');}));
  drop.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f) handler(f); });
}
bindUpload('fileSrc', 'dropSrc', async f => {
  const fd = new FormData(); fd.append('file', f);
  toast('上传中…');
  try { await api('/api/videos/upload', {method:'POST', body:fd}); toast('上传成功'); await loadVideos(); }
  catch (e) { toast('上传失败：' + e.message); }
});

function openTranscript(vid) {
  const box = document.createElement('div');
  box.className = 'item'; box.id = 'tr_' + vid;
  box.innerHTML = `<div class="body" style="width:100%">
    <label>粘贴这条视频的口播文字（最准，推荐）或上传字幕文件</label>
    <textarea id="trText_${vid}" rows="5" placeholder="把同行视频里说的话打出来／粘贴进来；留空则尝试自动转写"></textarea>
    <div class="row">
      <input type="file" id="trFile_${vid}" accept=".txt,.srt,.vtt,.md"/>
      <button class="btn primary" onclick="doTranscript(${vid})">保存并提取</button>
      <button class="btn" onclick="this.closest('.item').remove()">取消</button>
    </div></div>`;
  const anchor = document.querySelector(`button[onclick="openTranscript(${vid})"]`).closest('.item');
  anchor.after(box);
}
async function doTranscript(vid) {
  const fd = new FormData();
  const t = $('trText_' + vid); if (t) fd.append('manual_text', t.value);
  const f = $('trFile_' + vid); if (f && f.files[0]) fd.append('file', f.files[0]);
  fd.append('mode', 'auto');
  try {
    const r = await api(`/api/videos/${vid}/transcript`, {method:'POST', body:fd});
    toast(`已提取（${r.source}，${r.length} 字）`);
    await loadVideos();
  } catch (e) { toast('提取失败：' + e.message); }
}
async function doAnalyze(vid) {
  toast('分析中…');
  try { await api(`/api/videos/${vid}/analyze`, {method:'POST'}); toast('分析报告已入库'); 
    await loadVideos(); await loadReports(); gotoStep('s2'); }
  catch (e) { toast('分析失败：' + e.message); }
}
async function delVideo(vid) {
  if (!confirm('删除这条素材及其报告？')) return;
  await api(`/api/videos/${vid}`, {method:'DELETE'});
  await loadVideos(); await loadReports();
}

/* ---------- 2 报告 ---------- */
function parseField(v) {
  if (!v) return '—';
  try { const x = JSON.parse(v); return Array.isArray(x)
    ? (x.length ? x.map(i => typeof i === 'object' ? JSON.stringify(i) : i).join(' / ') : '—')
    : (typeof x === 'object' ? JSON.stringify(x, null, 1) : x); }
  catch (e) { return v; }
}
/* ---------- 报告详情：轻量 AI 十维，网页内详情页 ----------
   只消费 report_json 里的 facts / ai_analysis 两个节点，不新增接口、不改表结构。
   按确认结果渲染第一/二/三/四/六/八章；第五章「画面与剪辑」、
   第七章「与博主最近作品对比」不生成也不渲染。                     */
function num(v) {
  if (v == null || v === '') return '—';
  const n = Number(v);
  return isNaN(n) ? '—' : n.toLocaleString('zh-CN');
}
function pct(v) { return v == null ? '—' : (Number(v) * 100).toFixed(1) + '%'; }
function bullets(arr) {
  const a = (arr || []).filter(x => x != null && String(x).trim());
  return a.length ? a.map(x => `<li>${esc(x)}</li>`).join('') : '<li class="muted">—</li>';
}
function secHead(no, title) { return `<h4><i>${no}</i>${esc(title)}</h4>`; }
function insightBox(txt) { return txt ? `<p class="insight">${esc(txt)}</p>` : ''; }
function hasDeep(r) {
  try {
    const R = JSON.parse(r.report_json || 'null');
    return !!(R && R.ai_analysis && R.facts);
  } catch (e) { return false; }
}
function radarSVG(dims) {
  const n = 10, cx = 120, cy = 105, R = 76;
  const pt = (i, r) => {
    const a = -Math.PI / 2 + i * 2 * Math.PI / n;
    return [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
  };
  const polygon = k => Array.from({length: n}, (_, i) =>
    pt(i, R * k).map(v => v.toFixed(1)).join(',')).join(' ');
  const rings = [0.25, 0.5, 0.75, 1].map(k =>
    `<polygon points="${polygon(k)}" class="ring"/>`).join('');
  const axes = Array.from({length: n}, (_, i) => {
    const p = pt(i, R);
    return `<line x1="${cx}" y1="${cy}" x2="${p[0].toFixed(1)}" y2="${p[1].toFixed(1)}" class="axis"/>`;
  }).join('');
  const vals = dims.slice(0, n).map(d => Math.max(1, Math.min(10, Number(d.score) || 5)) / 10);
  while (vals.length < n) vals.push(0.5);
  const area = vals.map((v, i) => pt(i, R * v).map(x => x.toFixed(1)).join(',')).join(' ');
  const dots = vals.map((v, i) => {
    const p = pt(i, R * v);
    return `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="2.6"/>`;
  }).join('');
  const labels = Array.from({length: n}, (_, i) => {
    const p = pt(i, R + 14);
    return `<text x="${p[0].toFixed(1)}" y="${p[1].toFixed(1)}" text-anchor="middle" class="lab">${i + 1}</text>`;
  }).join('');
  const legend = dims.slice(0, n).map((d, i) =>
    `<li><b>${i + 1}</b>${esc(d.name || '')}</li>`).join('');
  return '<svg viewBox="0 0 240 210" class="radar" xmlns="http://www.w3.org/2000/svg">' +
    rings + axes + `<polygon points="${area}" class="area"/>` + dots + labels + '</svg>' +
    `<ol class="radarlegend">${legend}</ol>`;
}
function renderDeepReport(r, R) {
  const ai = R.ai_analysis || {}, f = R.facts || {};
  const e = f.engagement || {}, t = f.title || {}, c = f.comments || null;
  const ins = ai.insights || {}, dims = ai.dimensions || [];
  const ratioRows = [
    ['评论/点赞', e.comment_like_ratio],
    ['收藏/点赞', e.collect_like_ratio],
    ['转发/点赞', e.share_like_ratio]
  ].map(([k, v]) => `<div class="ratiobar"><span>${k}</span>` +
    `<div class="track"><div style="width:${v == null ? 0 : Math.min(100, Number(v) * 100).toFixed(1)}%"></div></div>` +
    `<b>${pct(v)}</b></div>`).join('');
  const tags = (t.hashtags || []).map(x => `<span class="tag">#${esc(x)}</span>`).join('') ||
    '<span class="muted">无话题标签</span>';
  const catBars = c ? (c.category_share || []).slice(0, 6).map(x =>
    `<div class="cbar"><span>${esc(x.name)}</span>` +
    `<div class="track"><div style="width:${(Number(x.ratio) * 100).toFixed(1)}%"></div></div>` +
    `<b>${pct(x.ratio)}</b></div>`).join('') : '';
  const topRows = c ? (c.top || []).map(x =>
    `<tr><td class="num">${num(x.likes)}</td><td class="num">${num(x.replies)}</td>` +
    `<td>${esc(x.text)}</td><td>${esc((x.cats || []).join('、'))}</td></tr>`).join('') : '';
  const span = c && c.time_span
    ? `${c.time_span.first} ~ ${c.time_span.last}（${c.time_span.days} 天）` : '—';
  const dimRows = dims.map(d =>
    `<tr><td>${esc(d.name)}</td><td class="num">${num(d.score)}</td>` +
    `<td>${esc(d.finding)}</td><td>${esc(d.why_it_works)}</td><td>${esc(d.reusable)}</td></tr>`).join('');
  const secs = num(f.duration && f.duration.seconds);

  return `<div class="rpt">
    <div class="rpt-top">
      <div class="rpt-meta">${esc(r.author || f.author || '—')} · ${secs} 秒 · ${num(e.likes)} 赞 · ${num(e.comments)} 评论 · ${esc(r.source_provider || '')} · 模型 ${esc(ai.model || '—')}</div>
      <blockquote class="rpt-oneline">${esc(ai.one_line || r.summary || '—')}</blockquote>
      <div class="rpt-sub"><b>关键爆款因素</b><ol>${bullets(ai.key_drivers)}</ol></div>
      ${ai.formula ? `<div class="formula"><b>可套用公式：</b>${esc(ai.formula)}</div>` : ''}
    </div>
    <section class="rpt-sec">
      ${secHead('一', '互动数据结构')}
      <div class="metrics">
        <div class="metric"><span>点赞</span><b>${num(e.likes)}</b></div>
        <div class="metric"><span>评论</span><b>${num(e.comments)}</b></div>
        <div class="metric"><span>收藏</span><b>${num(e.collects)}</b></div>
        <div class="metric"><span>转发</span><b>${num(e.shares)}</b></div>
      </div>
      ${ratioRows}
      ${insightBox(ins['1'])}
      ${e.note ? `<p class="muted" style="margin-top:8px">${esc(e.note)}</p>` : ''}
    </section>
    <section class="rpt-sec">
      ${secHead('二', '时长策略')}
      <div class="metrics"><div class="metric"><span>视频时长</span><b>${secs}s</b></div></div>
      ${insightBox(ins['2'])}
    </section>
    <section class="rpt-sec">
      ${secHead('三', '标题与话题')}
      <div>${tags}</div>
      <p class="muted" style="margin:8px 0 0">标题正文 ${num(t.title_len)} 字 · ${t.has_number ? '含数字' : '无数字'} · ${t.has_question ? '含疑问/悬念' : '无疑问词'} · 地域词 ${esc(t.city_hit || '无')}</p>
      ${insightBox(ins['3'])}
    </section>
    <section class="rpt-sec">
      ${secHead('四', '开头钩子与讲述节奏')}
      <div class="warnbox">⚠ 轻量档未下载视频、未做语音转写，<b>没有逐字稿</b>。本节仅为基于标题 / 视频文案 / 评论样本的钩子类型推断，不含前 3 秒原话、语速与信息密度曲线。</div>
      <p style="margin:0">规则判定钩子类型：<b>${esc(r.hook_type || '—')}</b></p>
      ${insightBox(ins['4'])}
    </section>
    <section class="rpt-sec">
      ${secHead('六', '评论区')}
      ${c ? `<div class="metrics">
          <div class="metric"><span>样本条数</span><b>${num(c.count)}</b></div>
          <div class="metric"><span>@ 好友</span><b>${num(c.mention_count)}</b></div>
          <div class="metric"><span>时间跨度</span><b style="font-size:12px">${esc(span)}</b></div>
        </div>
        <div class="muted" style="margin-bottom:6px">评论情绪（按点赞加权，关键词分类）</div>
        ${catBars}
        <table><thead><tr><th>赞</th><th>回复</th><th>最高赞评论</th><th>类型</th></tr></thead>
        <tbody>${topRows || '<tr><td colspan="4" class="muted">无评论样本</td></tr>'}</tbody></table>`
        : '<p class="muted">本次未取到评论数据（可能预算用尽或上游无公开评论）。</p>'}
      ${insightBox(ins['6'])}
    </section>
    <section class="rpt-sec">
      ${secHead('八', 'AI 十维拆解')}
      <div class="radarbox">${radarSVG(dims)}</div>
      <table><thead><tr><th>维度</th><th>分数</th><th>发现</th><th>为什么有效</th><th>怎么复用</th></tr></thead>
      <tbody>${dimRows}</tbody></table>
      <div class="rpt-sub"><b>可改进</b><ol>${bullets(ai.weaknesses)}</ol></div>
      <div class="rpt-sub"><b>同公式新选题</b><ol>${bullets(ai.next_ideas)}</ol></div>
    </section>
    <div class="rpt-foot">数据源 ${esc(r.source_provider || '—')} · 分析引擎 ${esc(ai.model || r.engine || '—')} · 按配置不生成第五章「画面与剪辑」与第七章「与博主最近作品对比」</div>
  </div>`;
}
function reportDetail(r) {
  if (hasDeep(r)) {
    try { return renderDeepReport(r, JSON.parse(r.report_json)); }
    catch (e) { return `<div class="kv"><span class="k">解析失败</span><span>${esc(e.message)}</span></div>`; }
  }
  return `<div class="kv">
    <span class="k">可复用模式</span><span>${esc(r.reusable_pattern)}</span>
    <span class="k">内容结构</span><span>${esc(parseField(r.structure))}</span>
    <span class="k">受众痛点</span><span>${esc(parseField(r.pain_points))}</span>
    <span class="k">高频提问</span><span>${esc(parseField(r.top_comments))}</span>
    <span class="k">政策引用</span><span>${esc(parseField(r.policy_refs))}</span>
    <span class="k">合规风险</span><span>${esc(r.risk_notes)}</span>
    <span class="k">总结</span><span>${esc(r.summary)}</span>
  </div>`;
}

async function loadReports() {
  const list = await api('/api/reports');
  $('nReports').textContent = list.length + ' 份报告';
  $('reportList').innerHTML = list.length ? list.map(r => `
    <div class="item"><div class="body">
      <div class="t">${esc(r.title || '未命名报告')}</div>
      <div class="d">${esc(r.topic||'其他')} · ${esc(r.city||'—')} · ${esc(r.hook_type||'—')} · ${r.engine==='llm'?'AI 十维':(r.engine==='manual'?'手工':'规则')} 引擎${r.likes ? ' · ' + r.likes + ' 赞' : ''}${r.author ? ' · ' + esc(r.author) : ''}${r.harvest_job_id ? ' · 采集 #' + r.harvest_job_id : ''}</div>
      <div class="detail ${hasDeep(r) ? 'deep' : ''} ${CUR.reportOpen[r.id] ? '' : 'hidden'}" id="rd_${r.id}">
        ${reportDetail(r)}
      </div>
    </div><div class="acts">
      <span class="pill ${r.engine==='llm'?'ok':''}">${r.engine==='llm'?'AI 十维':(r.engine==='manual'?'手工':'内置')}</span>
      ${r.source_provider ? `<span class="pill ${r.source_provider.indexOf('tikhub')>=0?'ok':'blue'}">${esc(srcLabel(r.source_provider))}</span>` : ''}
      <button class="btn" onclick="toggleReport(${r.id})">详情</button>
      <button class="btn" onclick="delReport(${r.id})">删</button>
    </div></div>`).join('') : '<div class="empty">经验库还是空的。去第 1 步分析一条视频，或在上面手动补一条经验。</div>';
}
function toggleReport(id) {
  CUR.reportOpen[id] = !CUR.reportOpen[id];
  $('rd_' + id).classList.toggle('hidden', !CUR.reportOpen[id]);
}
async function delReport(id) {
  if (!confirm('删除这份报告？')) return;
  await api(`/api/reports/${id}`, {method:'DELETE'}); await loadReports();
}
async function addManualReport() {
  const topic = $('manualTopic').value.trim(), pattern = $('manualPattern').value.trim();
  if (!topic || !pattern) return toast('选题和结构模式都要填');
  await api('/api/reports/manual', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({title:`手工经验 · ${topic}`, topic, city:'成都', hook_type:'提问式',
      reusable_pattern:pattern, pain_points:[topic+'办理顺序不清楚'],
      top_comments:[topic+'要多久？'], summary:pattern})});
  $('manualTopic').value = ''; $('manualPattern').value = '';
  toast('已存入经验库'); await loadReports();
}

/* ---------- 2b 批量采集：一段文字 → N 份报告 ---------- */
function srcLabel(v) {
  if (!v) return '';
  if (v.indexOf('tikhub') >= 0) return 'TikHub';
  if (v.indexOf('fallback') >= 0) return '降级演示';
  if (v.indexOf('mock') >= 0) return '演示数据';
  return v;
}
async function loadProviders() {
  try {
    const list = await api('/api/providers');
    STATE.providers = list;
    const cur = list.find(p => p.current) || list[0];
    $('hvProvider').innerHTML = list.map(p =>
      `<option value="${esc(p.id)}">${esc(p.label)}${p.ok ? '' : '（未配置）'}</option>`).join('');
    if (cur) $('hvProvider').value = cur.id;
    $('providerBadge').textContent = '当前：' + (cur ? cur.label : '演示数据');
  } catch (e) { toast('数据源加载失败：' + e.message); }
}
async function startHarvest() {
  const text = $('hvText').value.trim();
  if (!text) return toast('先输入一段文字或关键词');
  const btn = $('btnHarvest'); btn.disabled = true;
  try {
    const j = await api('/api/harvest/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        text, count: +$('hvCount').value || 15,
        publish_days: +$('hvDays').value,
        provider: $('hvProvider').value,
        likes_mode: 'percentile', use_seed_fallback: true
      })});
    toast('采集任务已启动'); pollHarvest(j.id);
  } catch (e) { toast('启动失败：' + e.message); btn.disabled = false; }
}
function pollHarvest(id) {
  clearInterval(HPOLL);
  HPOLL = setInterval(async () => {
    try {
      const j = await api(`/api/harvest/${id}`);
      $('hvStatus').innerHTML = `状态：<b>${j.status}</b>　进度 ${j.progress}%　候选 ${j.found} / 入库 ${j.imported} / 调用 ${j.cost_calls}` +
        `<div class="bar"><div style="width:${j.progress}%"></div></div>` + esc(j.log || '');
      if (['done', 'failed', 'stopped'].indexOf(j.status) >= 0) {
        clearInterval(HPOLL); $('btnHarvest').disabled = false;
        await loadReports(); await loadHarvestJobs();
        toast(j.status === 'done' ? `完成，入库 ${j.imported} 份报告` : '任务结束');
      }
    } catch (e) { clearInterval(HPOLL); $('btnHarvest').disabled = false; }
  }, 1200);
}
async function loadHarvestJobs() {
  const list = await api('/api/harvest');
  $('harvestList').innerHTML = list.length ? list.map(j => `
    <div class="item"><div class="body">
      <div class="t">任务 #${j.id} · ${esc((j.input_text || '').slice(0, 40))}</div>
      <div class="d">${esc(j.provider)} · 候选 ${j.found} / 入库 ${j.imported} / 调用 ${j.cost_calls} · ${esc(j.created_at)}</div>
    </div><div class="acts">
      <span class="pill ${j.status==='done'?'ok':(j.status==='failed'?'warn':'blue')}">${j.status}</span>
      <button class="btn" onclick="watchHarvest(${j.id})">看日志</button>
    </div></div>`).join('') : '<div class="empty">还没有采集任务。在上面输入一段文字即可开始。</div>';
}
async function watchHarvest(id) {
  const j = await api(`/api/harvest/${id}`);
  $('hvStatus').textContent = j.log || j.error || '无日志';
  gotoStep('s2');
}

/* ---------- 3 文案 ---------- */
async function genScript() {
  const req = {
    topic:$('reqTopic').value, city:$('reqCity').value, seconds:+$('reqSeconds').value,
    hook_style:$('reqHook').value, points:$('reqPoints').value, business:$('reqBusiness').value,
    cta:$('reqCta').value, tone:'平实、像跟成都老板当面聊'
  };
  const btn = $('btnGen'); btn.disabled = true; btn.textContent = '生成中…';
  try {
    const r = await api('/api/scripts/generate', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(req)});
    CUR.scriptId = r.id; CUR.script = r;
    $('scTitle').value = r.title || ''; $('scBody').value = r.body || '';
    $('scriptMeta').textContent = `#${r.id} · ${r.engine==='llm'?'AI 生成':'内置引擎'} · 引用 ${(JSON.parse(r.source_report_ids||'[]')).length||0} 份报告`;
    $('keywordsBox').innerHTML = '搜索词：' + (r.keywords||[]).map(k=>`<span>${esc(k)}</span>`).join('');
    $('btnSaveScript').disabled = false; $('btnFinal').disabled = false;
    await checkScript(true); await loadScripts();
    toast('文案已生成，可以直接改');
  } catch (e) { toast('生成失败：' + e.message); }
  finally { btn.disabled = false; btn.textContent = '生成全新文案'; }
}
async function saveScript() {
  if (!CUR.scriptId) return;
  await api(`/api/scripts/${CUR.scriptId}`, {method:'PUT', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:$('scTitle').value, body:$('scBody').value})});
  toast('已保存'); await checkScript(true); await loadScripts();
}
async function checkScript(silent) {
  const r = await api('/api/sensitive', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:$('scTitle').value + '\n' + $('scBody').value})});
  $('sensitiveBox').innerHTML = r.hits.length
    ? r.hits.map(h=>`<div class="hit">⚠ 敏感词「${esc(h.word)}」出现 ${h.count} 次，发布前必须改掉</div>`).join('')
    : '<div class="pass">✓ 合规检查通过，未命中风险词</div>';
  if (!silent) toast(r.hits.length ? '发现敏感词' : '检查通过');
}
function copyScript() {
  const t = $('scTitle').value + '\n\n' + $('scBody').value;
  navigator.clipboard.writeText(t).then(()=>toast('已复制')).catch(()=>toast('复制失败，请手动选中'));
}
async function finalizeScript() {
  if (!CUR.scriptId) return;
  await saveScript();
  await api(`/api/scripts/${CUR.scriptId}`, {method:'PUT', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:$('scTitle').value, body:$('scBody').value, status:'final'})});
  toast('已定稿，去第 4 步拍摄'); await loadScripts(); gotoStep('s4');
}
async function loadScripts() {
  const list = await api('/api/scripts');
  $('nScripts').textContent = list.length + ' 份文案';
  $('scriptList').innerHTML = list.length ? list.map(s=>`
    <div class="item"><div class="body">
      <div class="t">${esc(s.title||'未命名')}</div>
      <div class="d">${esc((s.body||'').replace(/\n/g,' ').slice(0,90))}…</div>
    </div><div class="acts">
      <span class="pill ${s.status==='final'?'ok':''}">${s.status==='final'?'已定稿':'草稿'}</span>
      <button class="btn" onclick="openScript(${s.id})">编辑</button>
      <button class="btn" onclick="delScript(${s.id})">删</button>
    </div></div>`).join('') : '<div class="empty">还没有文案。填左边的选题要求，点「生成全新文案」。</div>';
}
async function openScript(id) {
  const s = await api(`/api/scripts/${id}`);
  CUR.scriptId = id; CUR.script = s;
  $('scTitle').value = s.title || ''; $('scBody').value = s.body || '';
  $('btnSaveScript').disabled = false; $('btnFinal').disabled = false;
  $('scriptMeta').textContent = `#${s.id} · ${s.status==='final'?'已定稿':'草稿'}`;
  await checkScript(true); gotoStep('s3'); window.scrollTo({top:0,behavior:'smooth'});
}
async function delScript(id) {
  if (!confirm('删除这份文案？')) return;
  await api(`/api/scripts/${id}`, {method:'DELETE'});
  if (CUR.scriptId === id) { CUR.scriptId = null; }
  await loadScripts();
}

/* ---------- 4 拍摄 ---------- */
async function loadShootScripts() {
  const list = await api('/api/scripts');
  const finals = list.filter(s=>s.status==='final');
  const pool = finals.length ? finals : list;
  $('shootScript').innerHTML = pool.length
    ? pool.map(s=>`<option value="${s.id}">${s.status==='final'?'✅ ':'📝 '}${esc((s.title||'未命名').slice(0,40))}</option>`).join('')
    : '<option value="">（还没有文案，请先在第 3 步生成）</option>';
  showShootScript();
}
async function showShootScript() {
  const id = $('shootScript').value;
  if (!id) { $('shootPaper').textContent = '请先在第 3 步生成并定稿一份文案。'; return; }
  const s = await api(`/api/scripts/${id}`);
  $('shootPaper').textContent = (s.title||'') + '\n\n' + (s.body||'');
}
function copyShoot() {
  navigator.clipboard.writeText($('shootPaper').textContent).then(()=>toast('已复制，可粘进提词器'));
}
bindUpload('fileTalk','dropTalk', async f => {
  const id = $('shootScript').value;
  if (!id) return toast('先选一份文案');
  const fd = new FormData(); fd.append('file', f); fd.append('script_id', id);
  $('talkInfo').textContent = '上传中…';
  try {
    const r = await api('/api/clips/upload', {method:'POST', body:fd});
    CUR.talkPath = r.path;
    $('talkInfo').innerHTML = `✓ 已上传 <b>${esc(r.filename)}</b>，时长 ${r.duration}s，${r.width}×${r.height}。可以点下面按钮去剪辑。`;
    toast('口播视频已上传');
  } catch (e) { $('talkInfo').textContent = '上传失败：' + e.message; toast('上传失败：'+e.message); }
});

/* ---------- 5 剪辑 ---------- */
async function startRender() {
  const sid = $('shootScript').value;
  if (!sid) return toast('请先选择文案');
  if (!CUR.talkPath) return toast('请先上传口播原片');
  // 临时覆盖模板参数
  const t = STATE.templates.find(x=>x.id===$('tplSelect').value);
  if (t) {
    t.trim_head = +$('ovTrimHead').value || 0;
    t.trim_tail = +$('ovTrimTail').value || 0;
    t.speed = Math.min(2, Math.max(.5, +$('ovSpeed').value || 1));
    await api('/api/templates', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(STATE.templates)}).catch(()=>{});
  }
  $('btnRender').disabled = true;
  try {
    const job = await api('/api/clips/render', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({script_id:+sid, video_path:CUR.talkPath, template_id:$('tplSelect').value})});
    toast('已开始剪辑，请稍候'); pollJob(job.id); await loadJobs();
  } catch (e) { toast('启动失败：'+e.message); $('btnRender').disabled = false; }
}
function pollJob(id) {
  clearInterval(POLL);
  POLL = setInterval(async () => {
    try {
      const j = await api(`/api/jobs/${id}`);
      $('renderStatus').innerHTML = `状态：<b>${j.status}</b>　进度 ${j.progress}%\n` +
        `<div class="bar"><div style="width:${j.progress}%"></div></div>` + esc(j.log || '');
      if (j.status === 'done') {
        clearInterval(POLL); $('btnRender').disabled = false;
        $('previewBox').innerHTML = `<video controls src="${j.download}" poster=""></video>`;
        const d = $('downloadBtn'); d.href = j.download.replace('/api/outputs/','/api/outputs/') + '/download';
        d.classList.remove('hidden'); toast('剪辑完成，可以下载了'); await loadJobs();
      } else if (j.status === 'failed') {
        clearInterval(POLL); $('btnRender').disabled = false;
        $('renderStatus').textContent = '失败：' + (j.error||''); toast('剪辑失败，看右侧日志');
      }
    } catch (e) { clearInterval(POLL); }
  }, 1500);
}
async function loadJobs() {
  const list = await api('/api/jobs');
  $('nJobs').textContent = list.length + ' 个任务';
  $('jobList').innerHTML = list.length ? list.map(j=>`
    <div class="item"><div class="body">
      <div class="t">任务 #${j.id} · ${esc(j.template_id)}</div>
      <div class="d">${esc(j.created_at)} · ${j.output_path ? esc(j.output_path.split('/').pop()) : (j.error||'').slice(0,80)}</div>
    </div><div class="acts">
      <span class="pill ${j.status==='done'?'ok':(j.status==='failed'?'warn':'blue')}">${j.status}</span>
      ${j.status==='done' ? `<button class="btn" onclick="playJob(${j.id})">查看</button>` : ''}
    </div></div>`).join('') : '<div class="empty">还没有剪辑任务。</div>';
}
async function playJob(id) {
  const j = await api(`/api/jobs/${id}`);
  $('previewBox').innerHTML = `<video controls src="${j.download}"></video>`;
  const d = $('downloadBtn'); d.href = j.download + '/download'; d.classList.remove('hidden');
  gotoStep('s5');
}

/* ---------- 设置 ---------- */
function openSettings() { $('mask').classList.remove('hidden'); $('drawer').classList.add('open'); loadSettings(); }
function closeSettings() { $('mask').classList.add('hidden'); $('drawer').classList.remove('open'); }
async function loadSettings() {
  const s = await api('/api/settings');
  $('setLlmEnabled').value = String(!!s.llm.enabled);
  $('setLlmUrl').value = s.llm.base_url || ''; $('setLlmKey').value = '';
  $('setLlmKey').placeholder = s.llm.api_key ? '已保存（' + s.llm.api_key + '），留空不修改' : 'sk-…';
  $('setLlmModel').value = s.llm.model || '';
  $('setAsrEnabled').value = String(!!s.asr.enabled);
  $('setAsrUrl').value = s.asr.base_url || ''; $('setAsrModel').value = s.asr.model || '';
  $('setAsrKey').value = '';
  $('setAsrKey').placeholder = s.asr.api_key ? '已保存（' + s.asr.api_key + '），留空不修改' : '';
  $('setCookie').value = s.fetch.douyin_cookie || '';
  $('setShort').value = s.company.short || ''; $('setCity').value = s.company.city || '';
  const a = s.auth || {};
  STATE.authHasPwd = !!a.password;
  $('setAuthEnabled').value = String(!!a.enabled);
  $('setAuthUser').value = a.username || 'boss';
  $('setAuthPwd').value = '';
  $('setAuthPwd').placeholder = a.password ? '已保存，留空不修改' : '设置一个访问密码';
  const se = s.search || {}, tk = se.tikhub || {}, hv = s.harvest || {};
  $('setProvider').value = se.provider || 'mock';
  $('setTikhubUrl').value = tk.base_url || '';
  $('setTikhubKey').value = '';
  $('setTikhubKey').placeholder = tk.api_key ? '已保存（' + tk.api_key + '），留空不修改' : 'Bearer Token';
  $('setMaxCalls').value = hv.max_calls || 60;
  $('setCommentPages').value = hv.comment_pages || 3;
  await loadSeeds();
  $('tplEditor').innerHTML = STATE.templates.map((t,i)=>`
    <div class="tplrow"><b>${esc(t.name)}</b>
      <label>片头秒数</label><input data-ti="${i}" data-k="intro_seconds" type="number" step="0.1" value="${t.intro_seconds}"/>
      <label>片尾秒数</label><input data-ti="${i}" data-k="outro_seconds" type="number" step="0.1" value="${t.outro_seconds}"/>
      <label>片尾文案</label><input data-ti="${i}" data-k="outro_text" value="${esc(t.outro_text)}"/>
      <label>水印文字</label><input data-ti="${i}" data-k="watermark" value="${esc(t.watermark)}"/>
      <label>字幕字号</label><input data-ti="${i}" data-k="subtitle_size" type="number" value="${t.subtitle_size}"/>
      <label>背景色</label><input data-ti="${i}" data-k="bg_color" value="${esc(t.bg_color)}"/>
      <label>强调色</label><input data-ti="${i}" data-k="accent_color" value="${esc(t.accent_color)}"/>
    </div>`).join('');
}
async function saveSettings() {
  const authOn = $('setAuthEnabled').value === 'true';
  if (authOn && !$('setAuthPwd').value && !STATE.authHasPwd) {
    toast('启用访问密码必须先填一个密码');
    return false;
  }
  const data = {
    llm:{enabled:$('setLlmEnabled').value==='true', base_url:$('setLlmUrl').value.trim(),
         model:$('setLlmModel').value.trim()},
    asr:{enabled:$('setAsrEnabled').value==='true', base_url:$('setAsrUrl').value.trim(),
         model:$('setAsrModel').value.trim()},
    fetch:{douyin_cookie:$('setCookie').value.trim()},
    company:{short:$('setShort').value.trim(), city:$('setCity').value.trim()},
    auth:{enabled:authOn, username:$('setAuthUser').value.trim() || 'boss'},
    search:{provider:$('setProvider').value,
            tikhub:{base_url:$('setTikhubUrl').value.trim()}},
    harvest:{max_calls:+$('setMaxCalls').value || 60,
             comment_pages:+$('setCommentPages').value || 3}
  };
  if ($('setLlmKey').value) data.llm.api_key = $('setLlmKey').value;
  if ($('setAsrKey').value) data.asr.api_key = $('setAsrKey').value;
  if ($('setAuthPwd').value) data.auth.password = $('setAuthPwd').value;
  if ($('setTikhubKey').value) data.search.tikhub.api_key = $('setTikhubKey').value;
  try {
    await api('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    $('setMsg').textContent = authOn
      ? '✓ 已保存。访问密码已生效，刷新页面会要求输入账号密码。'
      : '✓ 已保存。刷新页面后顶部状态会更新。';
    toast('设置已保存');
    const s = await api('/api/state');
    const b = $('engineBadge'); b.textContent = s.llm_enabled?'AI 已接入':'内置引擎模式';
    b.className = 'badge ' + (s.llm_enabled?'on':'off');
    STATE.llm = s.llm_enabled;
    STATE.authHasPwd = authOn;
    return true;
  } catch (e) { $('setMsg').textContent = '保存失败：'+e.message; return false; }
}

async function testProvider() {
  const btn = $('btnTestProvider'), box = $('providerTestMsg');
  btn.disabled = true;
  box.textContent = '正在保存设置并发起 1 次测试调用…';
  try {
    if (!await saveSettings()) { box.textContent = '设置保存失败，无法测试'; return; }
    const r = await api('/api/providers/test', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({provider: $('setProvider').value})
    });
    if (!r.ok) {
      box.textContent = '✗ 连接失败：' + (r.error || '未知错误') +
        (r.kind ? '（' + r.kind + '）' : '');
      return;
    }
    const sample = (r.sample || []).map(s => `${s.title}（${s.likes} 赞）`).join('；');
    box.textContent = '✓ ' + (r.message || '连接正常') +
      '｜返回 ' + r.items + ' 条｜消耗 ' + r.cost_calls + ' 次调用' +
      (sample ? '\n样例：' + sample : '');
    await loadProviders();
  } catch (e) {
    box.textContent = '✗ 测试失败：' + e.message;
  } finally { btn.disabled = false; }
}

async function testLLM() {
  const btn = $('btnTestLlm'), box = $('llmTestMsg');
  btn.disabled = true;
  box.textContent = '正在保存设置并发起 1 次测试调用…';
  try {
    if (!await saveSettings()) { box.textContent = '设置保存失败，无法测试'; return; }
    const r = await api('/api/llm/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    box.textContent = r.ok
      ? `✓ ${r.message}｜模型 ${r.model}｜回复 ${r.reply || 'OK'}`
      : `✗ ${r.message}｜模型 ${r.model || '—'}\n${r.error || ''}\n` +
        '若提示模型不存在，把上面「模型名」改成上游实际支持的名称即可，其余代码不用动。';
  } catch (e) {
    box.textContent = '✗ 测试失败：' + e.message;
  } finally { btn.disabled = false; }
}

async function saveTemplates() {
  document.querySelectorAll('#tplEditor input').forEach(inp=>{
    const i = +inp.dataset.ti, k = inp.dataset.k;
    STATE.templates[i][k] = (k.includes('seconds') || k==='subtitle_size')
      ? (+inp.value || 0) : inp.value;
  });
  await api('/api/templates', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify(STATE.templates)});
  toast('模板已保存'); descTpl();
}
async function loadSeeds() {
  const list = await api('/api/seeds');
  $('seedList').innerHTML = list.length
    ? '已录入 ' + list.length + ' 个：' + list.map(s =>
        `<span class="pill" style="margin:4px 4px 0 0;display:inline-block">${esc(s.nickname || s.homepage)} · <a onclick="delSeed(${s.id})" style="cursor:pointer">删除</a></span>`).join('')
    : '还没有种子账号';
}
async function importSeeds() {
  const batch = $('seedBatch').value.trim();
  if (!batch) return toast('先粘贴账号清单');
  try {
    const r = await api('/api/seeds', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({batch})});
    $('seedBatch').value = '';
    toast('导入 ' + r.added + ' 个'); await loadSeeds();
  } catch (e) { toast('导入失败：' + e.message); }
}
async function delSeed(id) {
  await api('/api/seeds/' + id, {method:'DELETE'});
  await loadSeeds();
}
async function resetData() {
  if (!confirm('确定清空全部素材、分析报告、文案、剪辑任务和成片文件？\n配置与剪辑模板会保留。此操作不可撤销。')) return;
  if (!confirm('再确认一次：服务器上的视频文件会被真实删除。')) return;
  try {
    const r = await api('/api/admin/reset', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({confirm:'RESET'})});
    toast(`已清空，删除 ${r.deleted_files} 个文件`);
    setTimeout(() => location.reload(), 900);
  } catch (e) { toast('清空失败：' + e.message); }
}

boot();
