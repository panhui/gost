'use strict';
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let state = {nodes:[],rules:[],audit:[]}, csrf = '', publicUrl = '', page = 'overview', filter = '', installation = null, busy = false;
const labels = {overview:'概览',rules:'转发规则',nodes:'节点管理',audit:'操作记录',settings:'系统设置'};
let toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(()=>$('#toast').hidden = true, 4500); }
function showLogin() { $('#shell').hidden = true; $('#login').hidden = false; csrf = ''; }
async function api(path, options={}) {
  const response = await fetch(path, {credentials:'same-origin', ...options, headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}, body:options.body ? JSON.stringify(options.body) : undefined});
  const data = await response.json();
  if (!response.ok) { if(response.status===401 && path!='/api/login') showLogin(); throw new Error(data.error || '请求失败'); }
  return data;
}
async function load() {
  if (!csrf || busy) return;
  busy = true;
  try { state = await api('/api/state'); $('#refresh-state').textContent = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'})+' 已同步'; $('#gost-version').textContent = state.gost_version; $('#nav-rule-count').textContent = state.rules.length; render(); }
  catch(error) { $('#refresh-state').textContent = '同步失败'; toast(error.message); }
  finally {busy = false;}
}
function node(id) { return state.nodes.find(item=>item.id===id); }
function pill(text, color='') {return `<span class="pill ${color}">${esc(text)}</span>`;}
function nodeStatus(item) {
  if(!item.online) return pill(item.last_seen?'离线':'待安装');
  if(item.error) return pill('运行异常','red');
  if(!item.synced) return pill('同步中','amber');
  if(item.service_count && !item.running) return pill('服务未运行','red');
  return pill('在线','green');
}
function ruleStatus(rule) {
  if(!rule.enabled) return pill('已停用');
  const pair = [node(rule.entry_id),node(rule.exit_id)];
  if(pair.some(n=>!n || !n.online)) return pill('节点离线','amber');
  if(pair.some(n=>n.error || !n.running)) return pill('运行异常','red');
  if(pair.some(n=>!n.synced)) return pill('待同步','amber');
  return pill('已生效','green');
}
function heading(title, subtitle, action='') {return `<div class="page-heading"><div><p class="eyebrow">${page==='overview'?'NETWORK OVERVIEW':'GOST CONTROL PLANE'}</p><h1>${title}</h1><p class="subtitle">${subtitle}</p></div>${action}</div>`;}
function ruleTable(rules) {
  if(!rules.length) return `<div class="empty"><div class="empty-icon">⇄</div><strong>${filter?'没有匹配的规则':'创建你的第一条线路'}</strong><p>${filter?'试试其他名称、节点或落地地址':'选择入口和出口，填写监听端口与国外落地地址。'}</p>${filter?'':'<button class="primary" data-action="add-rule">＋ 新建转发规则</button>'}</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>规则 / 协议</th><th>入口 → 出口</th><th>落地地址</th><th>状态</th><th>操作</th></tr></thead><tbody>${rules.map(rule=>`<tr><td><div class="rule-name"><span class="rule-icon">⇄</span><div><strong>${esc(rule.name)}</strong><span class="cell-sub mono">${rule.protocol.toUpperCase()} · :${rule.listen_port}</span></div></div></td><td>${esc(node(rule.entry_id)?.name)}<span class="route-arrow">→</span>${esc(node(rule.exit_id)?.name)}<span class="cell-sub mono">TLS · 出口隧道 :${rule.tunnel_port}</span></td><td><span class="mono">${esc(rule.target_host.includes(':')?'['+rule.target_host+']':rule.target_host)}:${rule.target_port}${rule.targets.length>1?' +'+(rule.targets.length-1)+' 个目标':''}</span><span class="cell-sub">${esc(node(rule.entry_id)?.host)}:${rule.listen_port} 接入</span></td><td>${ruleStatus(rule)}</td><td><div class="row-actions"><button class="toggle ${rule.enabled?'on':''}" data-action="toggle-rule" data-id="${rule.id}" role="switch" aria-checked="${Boolean(rule.enabled)}" aria-label="${rule.enabled?'停用':'启用'} ${esc(rule.name)}"></button><button data-action="edit-rule" data-id="${rule.id}">编辑</button><button data-action="delete-rule" data-id="${rule.id}">删除</button></div></td></tr>`).join('')}</tbody></table></div>`;
}
function events(items) {return items.length ? items.map(item=>`<div class="event"><span>◷</span><div>${esc(item.message)}</div><time>${new Date(item.time*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</time></div>`).join('') : '<div class="empty">暂时没有操作记录</div>';}
function render() {
  $('#breadcrumb').textContent = labels[page];
  document.querySelectorAll('[data-page]').forEach(button=>button.classList.toggle('active',button.dataset.page===page));
  const entries = state.nodes.filter(n=>n.role==='entry'), exits = state.nodes.filter(n=>n.role==='exit');
  const active = state.rules.filter(r=>r.enabled), online = state.nodes.filter(n=>n.online);
  let content = '';
  if(page==='overview') {
    content = heading('线路，一目了然。','管理节点与转发规则，让每一段连接都在掌握之中。','<button class="primary" data-action="add-rule">＋ 新建转发规则</button>') +
      `<div class="stats"><div class="stat"><div class="stat-label"><span>转发规则</span><span>⇄</span></div><div class="stat-value">${state.rules.length}<small>条</small></div><div class="stat-note"><span class="green">${active.length} 条启用</span> · ${state.rules.length-active.length} 条停用</div></div><div class="stat"><div class="stat-label"><span>入口节点</span><span>↳</span></div><div class="stat-value">${entries.length}<small>台</small></div><div class="stat-note">${entries.filter(n=>n.online).length} 台在线 · 国内接入</div></div><div class="stat"><div class="stat-label"><span>出口节点</span><span>↗</span></div><div class="stat-value">${exits.length}<small>台</small></div><div class="stat-note">${exits.filter(n=>n.online).length} 台在线 · 隧道中转</div></div><div class="stat"><div class="stat-label"><span>节点在线率</span><span>◉</span></div><div class="stat-value">${state.nodes.length?Math.round(online.length/state.nodes.length*100):0}<small>%</small></div><div class="stat-note">${online.length} / ${state.nodes.length} 台节点在线</div></div></div>` +
      `<section class="panel"><div class="panel-heading"><div><h2>线路拓扑</h2><p>三段连接，一条完整的转发路径</p></div>${pill('TLS 加密传输','purple')}</div><div class="topology"><div class="stage"><div class="stage-icon">▦</div><h3>国内入口机</h3><p>${entries.length} 台节点 · 监听端口</p></div><div class="flow-line"><span>认证 TLS 隧道</span></div><div class="stage"><div class="stage-icon">⇄</div><h3>出口机</h3><p>${exits.length} 台节点 · 中转转发</p></div><div class="flow-line"><span>TCP / UDP 转发</span></div><div class="stage"><div class="stage-icon">◎</div><h3>国外落地机</h3><p>目标 IP : 端口</p></div></div><div class="topology-note"><span>面板管理配置，业务流量直接在节点之间传输</span><span>落地机无需安装代理</span></div></section>` +
      `<section class="panel"><div class="panel-heading"><div><h2>最近的转发规则</h2><p>已生效表示两端配置已应用，实际连通性请从入口测试</p></div><button class="text-button" data-page="rules">查看全部 →</button></div>${ruleTable(state.rules.slice(0,5))}</section>` +
      `<div class="two-columns"><section class="panel"><div class="panel-heading"><h2>快速开始</h2><button class="text-button" data-action="add-node">添加节点 →</button></div><div class="onboarding"><div class="step"><b>1</b><div><strong>添加并安装入口与出口</strong><p>填写名称和公网地址，复制生成的命令到对应服务器执行。</p></div></div><div class="step"><b>2</b><div><strong>创建转发规则</strong><p>设置入口监听端口，选择出口，填写国外落地 IP 和端口。</p></div></div><div class="step"><b>3</b><div><strong>确认配置已生效</strong><p>代理每 10 秒同步一次。放行对应端口后，从入口验证连接。</p></div></div></div></section><section class="panel"><div class="panel-heading"><h2>最近操作</h2><button class="text-button" data-page="audit">全部记录 →</button></div><div class="activity">${events(state.audit.slice(0,4))}</div></section></div>`;
  } else if(page==='rules') {
    const rules = state.rules.filter(r=>[r.name,r.targets.join(' '),node(r.entry_id)?.name,node(r.exit_id)?.name,String(r.listen_port)].some(v=>String(v||'').toLowerCase().includes(filter.toLowerCase())));
    content = heading('转发规则','配置入口、出口与落地地址；保存后自动下发到关联节点。','<button class="primary" data-action="add-rule">＋ 新建转发规则</button>') + `<section class="panel"><div class="panel-heading"><h2>全部规则 <span class="muted">· ${state.rules.length}</span></h2>${pill('TCP / UDP')}</div><div class="searchbar"><div class="search"><input id="rule-search" type="search" placeholder="搜索规则、节点、IP 或端口" aria-label="搜索规则" value="${esc(filter)}"></div></div><div id="rule-results">${ruleTable(rules)}</div></section>`;
  } else if(page==='nodes') {
    content = heading('节点管理','一个节点，一条安装命令。代理主动连接面板，无需开放管理端口。','<button class="primary" data-action="add-node">＋ 添加节点</button>') + (state.nodes.length ? `<div class="node-grid">${state.nodes.map(n=>`<article class="node-card"><div class="node-top"><div class="node-kind">${n.role==='entry'?'↳':'↗'}</div>${nodeStatus(n)}</div><h3>${esc(n.name)}</h3><div class="host mono">${esc(n.host)}</div><div class="node-meta"><span>${n.role==='entry'?'国内入口':'出口中转'} <b>· ${n.service_count} 条规则</b></span><span>${n.last_seen?'上次心跳 '+new Date(n.last_seen*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'尚未安装'}</span></div>${n.error?`<div class="node-error">${esc(n.error)}</div>`:''}<div class="node-actions"><button class="secondary" data-action="install-node" data-id="${n.id}">安装脚本</button><button class="secondary" data-action="edit-node" data-id="${n.id}">编辑</button><button class="secondary" data-action="delete-node" data-id="${n.id}">删除</button></div></article>`).join('')}</div>` : `<section class="panel"><div class="empty"><div class="empty-icon">▦</div><strong>还没有节点</strong><p>先添加一台国内入口机和一台出口机，构建你的第一条线路。</p><button class="primary" data-action="add-node">＋ 添加节点</button></div></section>`) + '<div class="notice">在线表示最近 40 秒内收到代理心跳。每台出口为每条规则监听一个 TCP 隧道端口；UDP 数据也通过 TLS 连接传输。请在服务器防火墙与云安全组放行这些端口。</div>';
  } else if(page==='audit') {
    content = heading('操作记录','最近的管理员操作和节点注册记录，最多保留 200 条。') + `<section class="panel"><div class="panel-heading"><h2>最近 30 条记录</h2>${pill('操作审计')}</div><div class="activity">${events(state.audit)}</div></section>`;
  } else {
    content = heading('系统设置','管理控制台访问与部署信息。') + `<div class="two-columns"><section class="panel"><div class="panel-heading"><h2>管理员密码</h2></div><form id="password-form"><div class="settings-content"><label>当前密码<input type="password" name="old_password" autocomplete="current-password" required></label><label>新密码<input type="password" name="new_password" autocomplete="new-password" minlength="12" required><small>至少 12 位。修改后所有已登录会话将注销。</small></label><p class="form-error"></p><button class="primary" type="submit">更新密码</button></div></form></section><section class="panel"><div class="panel-heading"><h2>部署信息</h2></div><div class="settings-content"><p class="muted">节点连接地址</p><code>${esc(publicUrl)}</code><p class="muted">转发引擎</p><p>GOST ${esc(state.gost_version)} · Relay over TLS</p><div class="notice">面板证书由安装脚本生成并固定在节点端。更换面板证书后，请重新生成安装脚本并安装节点。数据库和证书请定期备份。</div></div></section></div>`;
  }
  const focusedSearch = document.activeElement?.id==='rule-search', selection = focusedSearch ? $('#rule-search').selectionStart : null;
  $('#content').innerHTML = content;
  if(focusedSearch && $('#rule-search')) { $('#rule-search').focus(); $('#rule-search').setSelectionRange(selection,selection); }
}
function modal(title, body, foot='') {$('#modal-content').innerHTML = `<div class="modal-head"><h2>${esc(title)}</h2><button class="icon-button" data-action="close" aria-label="关闭">×</button></div>${body}${foot}`; if(!$('#modal').open) $('#modal').showModal();}
function formModal(title, type, id, fields, notice='') {
  modal(title,`<form id="editor" data-type="${type}" data-id="${id||''}"><div class="modal-body"><div class="fields">${fields}</div>${notice?`<div class="notice">${notice}</div>`:''}<p class="form-error" role="alert"></p></div><div class="modal-foot"><button type="button" class="secondary" data-action="close">取消</button><button type="submit" class="primary">保存${type==='rules'?'并下发':''}</button></div></form>`);
}
function field(label, key, value='', type='text', extra='', full=false) {return `<label class="${full?'full':''}">${label}<input type="${type}" name="${key}" value="${esc(value)}" ${extra}></label>`;}
function nodeForm(id) {
  const item = id ? node(id) : {name:'',host:'',role:'entry'};
  formModal(id?'编辑节点':'添加节点','nodes',id,field('节点名称','name',item.name,'text','required maxlength="64" placeholder="例如：广州入口 01"',true)+`<label class="full">节点类型<select name="role" ${id?'disabled':''}><option value="entry" ${item.role==='entry'?'selected':''}>国内入口机 · 接收用户连接</option><option value="exit" ${item.role==='exit'?'selected':''}>出口机 · 通过隧道转发到落地</option></select></label>`+field('公网 IP / 域名','host',item.host,'text','required placeholder="例如：203.0.113.10"',true),'请填写其他节点可访问的地址。添加后在节点卡片生成安装脚本，在对应 Linux 服务器执行。');
}
function ruleForm(id) {
  const item = id ? state.rules.find(r=>r.id===id) : {name:'',protocol:'tcp',listen_port:'',tunnel_port:'',targets:[],enabled:1};
  if(!state.nodes.some(n=>n.role==='entry') || !state.nodes.some(n=>n.role==='exit')) {toast('请先添加至少一个入口节点和一个出口节点'); page='nodes'; render(); return;}
  const selectNode = (role,label,key) => `<label class="full">${label}<select name="${key}" required>${state.nodes.filter(n=>n.role===role).map(n=>`<option value="${n.id}" ${item[key]===n.id?'selected':''}>${esc(n.name)} · ${esc(n.host)}</option>`).join('')}</select></label>`;
  formModal(id?'编辑转发规则':'添加转发规则','rules',id,
    field('名称','name',item.name,'text','required maxlength="64" placeholder="例如：香港落地 · Web"',true)+
    selectNode('entry','入口','entry_id')+
    `<label class="full">监听端口 <span class="muted">· 留空自动分配 2000–60000</span><input type="number" name="listen_port" value="${esc(item.listen_port)}" min="1" max="65535" placeholder="留空则随机分配可用端口"></label>`+
    selectNode('exit','出口','exit_id')+
    `<div class="connection-info full"><span class="muted">连接信息</span><p>协议：TLS 隧道 <span class="route-arrow">/</span> 认证：已开启 <span class="route-arrow">/</span> 延迟优化：已开启</p></div>`+
    `<label class="full">目标地址<textarea name="targets" class="target-input" required placeholder="一行一个，空行会被忽略。格式如下：&#10;&#10;1.2.3.4:5678&#10;[2001:db8::1]:80&#10;example.com:443">${esc((item.targets||[]).join('\n'))}</textarea><small>支持 IP 和域名，最多 32 个目标；多个目标在出口按轮询方式分配新连接。</small></label>`+
    `<details class="advanced full"><summary>高级选项</summary><div class="fields"><label>转发协议<select name="protocol"><option value="tcp" ${item.protocol==='tcp'?'selected':''}>TCP</option><option value="udp" ${item.protocol==='udp'?'selected':''}>UDP · 通过 TLS 传输</option></select></label>`+
    field('出口隧道端口','tunnel_port',item.tunnel_port,'number','min="1" max="65535" placeholder="留空自动分配"')+
    `<label class="full">保存后状态<select name="enabled"><option value="true" ${item.enabled?'selected':''}>启用规则</option><option value="false" ${!item.enabled?'selected':''}>停用规则</option></select></label></div></details>`,
    '保存后约 10 秒同步到入口和出口。请放行入口监听端口及出口 TCP 隧道端口。修改配置会短暂重启相关节点的 GOST 进程。');
}
async function showInstallation(id) {
  const data = await api(`/api/nodes/${id}/install`,{method:'POST',body:{}});
  installation = {...data,id};
  modal('安装节点 · '+node(id).name,`<div class="modal-body"><p class="subtitle">在对应服务器终端执行以下命令，或下载安装脚本后执行 sudo bash install-node.sh。</p><div class="notice">凭证有效期 1 小时，注册后立即失效。生成新脚本会使此前未使用的脚本失效。重新安装会替换该节点的代理凭证。</div><label>一键安装命令<textarea class="install-command" readonly aria-label="一键安装命令">${esc(data.command)}</textarea></label><p class="subtitle">GOST 安装包通过面板缓存下载，无需节点直接访问 GitHub。支持 Debian / Ubuntu / Rocky / AlmaLinux，amd64 与 arm64。安装完成后等待节点上线，再添加转发规则。</p></div>`,`<div class="modal-foot"><button class="secondary" data-action="download-install">下载脚本</button><button class="primary" data-action="copy-install">复制命令</button></div>`);
}
function confirmation(type,id) {
  const item = type==='nodes' ? node(id) : state.rules.find(r=>r.id===id);
  modal('删除'+(type==='nodes'?'节点':'规则'),`<div class="modal-body"><p class="subtitle">确定删除「${esc(item.name)}」？${type==='nodes'?'请先删除关联规则。代理下次同步时会停止转发；离线机器请手动停止 gost-agent 服务。':'在线节点将在下次同步时移除此规则。'}</p><p class="form-error" role="alert"></p></div>`,`<div class="modal-foot"><button class="secondary" data-action="close">取消</button><button class="danger" data-action="confirm-delete" data-type="${type}" data-id="${id}">确认删除</button></div>`);
}
document.addEventListener('click', async event=>{
  const button = event.target.closest('button');
  if(!button) return;
  const {action,id,type} = button.dataset;
  if(button.dataset.page) {page=button.dataset.page;filter='';render();return;}
  try {
    if(action==='close') $('#modal').close();
    if(action==='refresh') await load();
    if(action==='add-node') nodeForm();
    if(action==='edit-node') nodeForm(id);
    if(action==='add-rule') ruleForm();
    if(action==='edit-rule') ruleForm(id);
    if(action==='install-node') {button.disabled=true; await showInstallation(id);}
    if(action==='copy-install') {
      try {await navigator.clipboard.writeText(installation.command);toast('安装命令已复制');}
      catch { $('.install-command').focus();$('.install-command').select();toast('请按 Ctrl+C 或 Command+C 复制命令'); }
    }
    if(action==='download-install') {const url = URL.createObjectURL(new Blob([installation.script],{type:'text/x-shellscript'}));const a=document.createElement('a');a.href=url;a.download='install-node.sh';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    if(action==='delete-node') confirmation('nodes',id);
    if(action==='delete-rule') confirmation('rules',id);
    if(action==='confirm-delete') {button.disabled=true;await api(`/api/${type}/${id}`,{method:'DELETE'});$('#modal').close();toast('已删除');await load();}
    if(action==='toggle-rule') {button.disabled=true;const item=state.rules.find(r=>r.id===id);await api(`/api/rules/${id}`,{method:'PUT',body:{...item,enabled:!item.enabled}});toast(item.enabled?'规则已停用，等待节点同步':'规则已启用，等待节点同步');await load();}
    if(action==='logout') {await api('/api/logout',{method:'POST',body:{}});showLogin();}
  } catch(error) {if(action==='confirm-delete') $('#modal .form-error').textContent=error.message;else toast(error.message);}
  finally {button.disabled=false;}
});
document.addEventListener('input',event=>{if(event.target.id==='rule-search') {filter=event.target.value;const rules=state.rules.filter(r=>[r.name,r.targets.join(' '),node(r.entry_id)?.name,node(r.exit_id)?.name,String(r.listen_port)].some(v=>String(v||'').toLowerCase().includes(filter.toLowerCase())));$('#rule-results').innerHTML=ruleTable(rules);}});
document.addEventListener('submit',async event=>{
  event.preventDefault();const form=event.target, button=form.querySelector('[type=submit]');button.disabled=true;
  const data=Object.fromEntries(new FormData(form));
  try {
    if(form.id==='login-form') {const result=await api('/api/login',{method:'POST',body:data});csrf=result.csrf;const session=await api('/api/session');publicUrl=session.public_url;$('#login-error').textContent='';form.reset();$('#login').hidden=true;$('#shell').hidden=false;await load();}
    if(form.id==='editor') {
      const {type,id}=form.dataset;
      if(type==='rules') {data.enabled=data.enabled==='true';data.listen_port=data.listen_port?Number(data.listen_port):null;data.tunnel_port=data.tunnel_port?Number(data.tunnel_port):null;}
      if(type==='nodes' && id) data.role=node(id).role;
      await api(`/api/${type}${id?'/'+id:''}`,{method:id?'PUT':'POST',body:data});$('#modal').close();toast(type==='rules'?'规则已保存，等待节点同步':'节点已保存，请生成安装脚本');await load();
    }
    if(form.id==='password-form') {await api('/api/password',{method:'POST',body:data});showLogin();toast('密码已更新，请重新登录');}
  } catch(error) {const target=form.id==='login-form'?$('#login-error'):form.querySelector('.form-error');target.textContent=error.message;}
  finally {button.disabled=false;}
});
$('#modal').addEventListener('click',event=>{if(event.target===$('#modal')) {const bounds=$('#modal').getBoundingClientRect();if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom) $('#modal').close();}});
(async()=>{try {const session=await api('/api/session');csrf=session.csrf;publicUrl=session.public_url;$('#shell').hidden=false;await load();}catch {showLogin();}setInterval(load,10000);})();
