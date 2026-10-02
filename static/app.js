'use strict';
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let state = {nodes:[],groups:[],rules:[],audit:[]}, csrf = '', publicUrl = '', page = 'overview', filter = '', installation = null, busy = false;
const labels = {overview:'概览',rules:'转发规则',nodes:'设备组',audit:'操作记录',settings:'系统设置'};
let toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(()=>$('#toast').hidden = true, 4500); }
function showLogin() { $('#shell').hidden = true; $('#login').hidden = false; $('#password-form')?.reset(); csrf = ''; }
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
function group(id) { return state.groups.find(item=>item.id===id); }
function node(id) { return state.nodes.find(item=>item.id===id); }
function pill(text, color='') {return `<span class="pill ${color}">${esc(text)}</span>`;}
function nodeStatus(item) {
  if(!item.enabled) return pill('负载已停用');
  if(!item.online) return pill(item.last_seen?'离线':'待安装');
  if(item.error) return pill('运行异常','red');
  if(!item.synced) return pill('同步中','amber');
  if(item.service_count && !item.running) return pill('服务未运行','red');
  return pill('在线','green');
}
function ruleStatus(rule) {
  if(!rule.enabled) return pill('已停用');
  if(rule.mode!=='direct' && !rule.exit_nodes?.length) return pill('出口无可用设备','red');
  const entries = (rule.entry_nodes||[]).map(node).filter(Boolean);
  if(!entries.length) return pill('入口无在线设备','amber');
  if(!entries.some(n=>n.synced && n.running && !n.error)) return pill('入口待同步','amber');
  return rule.failover_active ? pill('故障转移中','amber') : pill('已应用','green');
}
function accessAddresses(rule) {
  const ids = rule.entry_nodes?.length ? rule.entry_nodes : state.nodes.filter(n=>n.group_id===rule.entry_id && n.enabled).map(n=>n.id);
  return ids.map(node).filter(Boolean).map(n=>(n.host.includes(':')?'['+n.host+']':n.host)+':'+rule.listen_port).join(' / ') || '入口组尚无设备';
}
function heading(title, subtitle='', action='') {return `<div class="page-heading"><div><h1>${esc(title)}</h1>${subtitle?`<p class="subtitle">${esc(subtitle)}</p>`:''}</div>${action}</div>`;}
function ruleTable(rules) {
  if(!rules.length) return `<div class="empty"><strong>${filter?'没有匹配的规则':'还没有转发规则'}</strong><p>${filter?'试试其他名称、地址或端口':'创建入口设备组后，即可添加直连或隧道规则。'}</p>${filter?'':'<button class="primary" data-action="add-rule">＋ 添加规则</button>'}</div>`;
  return `<div class="rule-list">${rules.map(rule=>`<article class="rule-row"><div class="rule-title"><h3>${esc(rule.name)}</h3>${pill(rule.protocol.toUpperCase())}${ruleStatus(rule)}</div><div class="rule-details"><div><span class="detail-label">入口地址</span><strong class="mono">${esc(accessAddresses(rule))}</strong><span class="cell-sub">${esc(group(rule.entry_id)?.name)}</span></div><div><span class="detail-label">${rule.mode==='direct'?'转发方式':'出口设备组'}</span><strong>${rule.mode==='direct'?'直接转发':esc(group(rule.exit_id)?.name)}</strong><span class="cell-sub">${rule.mode==='direct'?'入口 → 目标':`TLS 隧道 · ${rule.exit_nodes?.length||0} 台可用 · :${rule.tunnel_port}`}</span></div><div><span class="detail-label">目标地址</span><strong class="mono">${esc(rule.targets[0])}</strong>${rule.targets.length>1?`<details class="more-targets" data-expand="targets-${rule.id}"><summary>另 ${rule.targets.length-1} 个目标</summary>${rule.targets.slice(1).map(t=>`<span class="cell-sub mono">${esc(t)}</span>`).join('')}</details>`:''}</div></div><div class="row-actions rule-actions"><button class="secondary" data-action="toggle-rule" data-id="${rule.id}" role="switch" aria-checked="${Boolean(rule.enabled)}" aria-label="${rule.enabled?'停用':'启用'} ${esc(rule.name)}">${rule.enabled?'停用':'启用'}</button><button class="secondary" data-action="diagnose-rule" data-id="${rule.id}">诊断</button><button class="secondary" data-action="edit-rule" data-id="${rule.id}">编辑</button><button class="text-button destructive" data-action="delete-rule" data-id="${rule.id}">删除</button></div></article>`).join('')}</div>`;
}
function events(items) {return items.length ? items.map(item=>`<div class="event"><span>◷</span><div>${esc(item.message)}</div><time>${new Date(item.time*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</time></div>`).join('') : '<div class="empty">暂时没有操作记录</div>';}
function render() {
  $('#breadcrumb').textContent = labels[page];
  document.querySelectorAll('[data-page]').forEach(button=>button.classList.toggle('active',button.dataset.page===page));
  const entries = state.nodes.filter(n=>n.role==='entry'), exits = state.nodes.filter(n=>n.role==='exit');
  const active = state.rules.filter(r=>r.enabled);
  let content = '';
  if(page==='overview') {
    content = heading('概览','','<button class="primary" data-action="add-rule">＋ 添加规则</button>') +
      `<div class="stats"><div class="stat"><span class="stat-label">启用规则</span><div class="stat-value">${active.length}<small>/ ${state.rules.length}</small></div></div><div class="stat"><span class="stat-label">入口在线</span><div class="stat-value">${entries.filter(n=>n.online).length}<small>/ ${entries.length}</small></div></div><div class="stat"><span class="stat-label">出口在线</span><div class="stat-value">${exits.filter(n=>n.online).length}<small>/ ${exits.length}</small></div></div><div class="stat"><span class="stat-label">设备组</span><div class="stat-value">${state.groups.length}</div></div></div>` +
      `<section class="panel"><div class="panel-heading"><h2>最近规则</h2><button class="text-button" data-page="rules">查看全部 →</button></div>${ruleTable(state.rules.slice(0,5))}</section>`;
  } else if(page==='rules') {
    const rules = state.rules.filter(r=>[r.name,r.targets.join(' '),group(r.entry_id)?.name,group(r.exit_id)?.name,String(r.listen_port)].some(v=>String(v||'').toLowerCase().includes(filter.toLowerCase())));
    content = heading('转发规则','','<button class="primary" data-action="add-rule">＋ 添加规则</button>') + `<section class="panel"><div class="panel-heading"><h2>全部规则 <span class="muted">· ${state.rules.length}</span></h2></div><div class="searchbar"><div class="search"><input id="rule-search" type="search" placeholder="搜索名称、地址或端口" aria-label="搜索规则" value="${esc(filter)}"></div></div><div id="rule-results">${ruleTable(rules)}</div></section>`;
  } else if(page==='nodes') {
    content = heading('设备组','同组安装命令可用于多台服务器，安装后自动加入。','<button class="primary" data-action="add-group">＋ 添加设备组</button>') +
      (state.groups.length ? state.groups.map(g=>`<section class="panel group-panel"><div class="panel-heading group-heading"><div><h2>${esc(g.name)} ${pill(g.role==='entry'?'入口组':'出口组','purple')}</h2><p>${g.online_count} / ${g.node_count} 台在线 · ${g.eligible_count} 台就绪</p><details class="group-settings" data-expand="group-${g.id}"><summary>下线 ${g.offline_after} 秒${g.failover_id?' · 备用：'+esc(group(g.failover_id)?.name):''}</summary><p>故障转移：${esc(group(g.failover_id)?.name || '未设置')}${g.remark?'<br>备注：'+esc(g.remark):''}</p></details></div><div class="row-actions"><button class="primary" data-action="install-group" data-id="${g.id}">安装命令</button><button class="secondary" data-action="edit-group" data-id="${g.id}">编辑</button><button class="secondary" data-action="delete-group" data-id="${g.id}">删除</button></div></div>${g.node_count?`<div class="table-wrap"><table class="device-table"><thead><tr><th>设备 / 公网地址</th><th>心跳 / 配置</th><th>状态</th><th>操作</th></tr></thead><tbody>${state.nodes.filter(n=>n.group_id===g.id).map(n=>`<tr><td data-label="设备"><strong>${esc(n.name)}</strong><span class="cell-sub mono">${esc(n.host)}</span>${n.error?`<span class="cell-sub node-error">${esc(n.error)}</span>`:''}</td><td data-label="配置">${n.service_count} 条规则<span class="cell-sub">${n.last_seen?'最后心跳 '+new Date(n.last_seen*1000).toLocaleTimeString('zh-CN'):'待安装'}</span></td><td data-label="状态">${nodeStatus(n)}</td><td><div class="row-actions"><button class="secondary" data-action="toggle-node" data-id="${n.id}">${n.enabled?'停用负载':'启用负载'}</button><button class="secondary" data-action="edit-node" data-id="${n.id}">编辑</button><button class="secondary" data-action="delete-node" data-id="${n.id}">移除</button></div></td></tr>`).join('')}</tbody></table></div>`:`<div class="empty"><strong>还没有设备加入</strong><p>将本组安装命令执行到服务器，设备会自动注册到这里。</p><button class="secondary" data-action="install-group" data-id="${g.id}">生成组安装命令</button></div>`}</section>`).join('') : `<section class="panel"><div class="empty"><strong>创建第一个设备组</strong><p>安装命令可将服务器自动加入对应组。</p><button class="primary" data-action="add-group">＋ 添加设备组</button></div></section>`);
  } else if(page==='audit') {
    content = heading('操作记录') + `<section class="panel"><div class="panel-heading"><h2>最近 30 条记录</h2></div><div class="activity">${events(state.audit)}</div></section>`;
  } else {
    content = heading('系统设置') + `<div class="two-columns"><section class="panel"><div class="panel-heading"><h2>管理员密码</h2></div><form id="password-form"><div class="settings-content"><label>当前密码<input type="password" name="old_password" autocomplete="current-password" required></label><label>新密码<input type="password" name="new_password" autocomplete="new-password" minlength="12" required><small>至少 12 位。修改后所有已登录会话将注销。</small></label><p class="form-error"></p><button class="primary" type="submit">更新密码</button></div></form></section><section class="panel"><div class="panel-heading"><h2>部署信息</h2></div><div class="settings-content"><p class="muted">节点连接地址</p><code>${esc(publicUrl)}</code><p class="muted">转发引擎</p><p>GOST ${esc(state.gost_version)}</p><p class="muted">面板版本</p><p>${esc(state.panel_version)}</p><button class="primary" data-action="upgrade-panel" ${!state.upgrade?.enabled || ['queued','running'].includes(state.upgrade?.state)?'disabled':''}>一键升级面板</button><p class="subtitle">${esc(state.upgrade?.message || '')}${state.upgrade?.commit?' · '+esc(state.upgrade.commit.slice(0,7)):''}</p><p class="subtitle">${state.upgrade?.enabled?'升级前自动备份，保留现有配置。面板会短暂重启。':'先在服务器执行一次新版安装脚本，以启用一键升级服务。'}</p><details class="advanced"><summary>备份与证书</summary><p>请定期备份数据库和证书。更换面板证书后，需重新安装节点。</p></details></div></section></div>`;
  }
  const focusedSearch = document.activeElement?.id==='rule-search', selection = focusedSearch ? $('#rule-search').selectionStart : null;
  const expanded = Array.from(document.querySelectorAll('#content details[open][data-expand]'),element=>element.dataset.expand);
  const passwordForm = $('#password-form'), passwordValues = passwordForm ? Object.fromEntries(new FormData(passwordForm)) : null;
  const focusedPassword = passwordForm?.contains(document.activeElement) ? {name:document.activeElement.name,start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd} : null;
  $('#content').innerHTML = content;
  document.querySelectorAll('#content details[data-expand]').forEach(element=>{element.open=expanded.includes(element.dataset.expand);});
  if(passwordValues && $('#password-form')) {for(const [key,value] of Object.entries(passwordValues)) $('#password-form').elements[key].value=value;if(focusedPassword) {const input=$('#password-form').elements[focusedPassword.name];input?.focus();if(input && focusedPassword.start!==null) input.setSelectionRange(focusedPassword.start,focusedPassword.end);}}
  if(focusedSearch && $('#rule-search')) { $('#rule-search').focus(); $('#rule-search').setSelectionRange(selection,selection); }
}
function modal(title, body, foot='') {$('#modal-content').innerHTML = `<div class="modal-head"><h2>${esc(title)}</h2><button class="icon-button" data-action="close" aria-label="关闭">×</button></div>${body}${foot}`; if(!$('#modal').open) $('#modal').showModal();}
function formModal(title, type, id, fields, notice='') {
  modal(title,`<form id="editor" data-type="${type}" data-id="${id||''}"><div class="modal-body"><div class="fields">${fields}</div>${notice?`<div class="notice">${notice}</div>`:''}<p class="form-error" role="alert"></p></div><div class="modal-foot"><button type="button" class="secondary" data-action="close">取消</button><button type="submit" class="primary">保存${type==='rules'?'并下发':''}</button></div></form>`);
}
function field(label, key, value='', type='text', extra='', full=false) {return `<label class="${full?'full':''}">${label}<input type="${type}" name="${key}" value="${esc(value)}" ${extra}></label>`;}
function groupOptions(role, current='', excluded='') {
  return '<option value="">无</option>'+state.groups.filter(g=>g.role===role && g.id!==excluded).map(g=>`<option value="${g.id}" ${g.id===current?'selected':''}>${esc(g.name)}</option>`).join('');
}
function groupForm(id) {
  const item = id ? group(id) : {name:'',role:'entry',offline_after:60,failover_id:'',remark:''};
  formModal(id?'编辑设备组':'添加设备组','groups',id,
    field('名称','name',item.name,'text','required maxlength="64" placeholder="例如：广州入口组"',true)+
    `<label class="full">设备类型<select name="role" ${id?'disabled':''}><option value="entry" ${item.role==='entry'?'selected':''}>入口组</option><option value="exit" ${item.role==='exit'?'selected':''}>出口组 · 多机自动负载均衡</option></select></label>`+
    field('负载下线（秒）','offline_after',item.offline_after,'number','required min="20" max="3600"',true)+
    '<p class="subtitle full">超过此时间未收到心跳，移出负载。</p>'+
    `<label class="full">故障转移组<select name="failover_id">${groupOptions(item.role,item.failover_id,id)}</select><small>主组全部下线时切换，恢复后自动切回。</small></label>`+
    `<label class="full">备注<textarea name="remark" maxlength="500">${esc(item.remark)}</textarea></label>`);
}
function nodeForm(id) {
  const item = node(id);
  formModal('编辑设备','nodes',id,field('设备名称','name',item.name,'text','required maxlength="64"',true)+
    `<label class="full">所属设备组<select name="group_id" required>${state.groups.filter(g=>g.role===item.role).map(g=>`<option value="${g.id}" ${g.id===item.group_id?'selected':''}>${esc(g.name)}</option>`).join('')}</select></label>`+
    field('公网 IP / 域名','host',item.host,'text','required placeholder="例如：203.0.113.10"',true),
    '请填写其他机器可访问的公网 IP 或域名。');
}
function updateRuleMode() {
  const form = $('#editor');
  if(!form || form.dataset.type!=='rules') return;
  const direct = !form.elements.exit_id.value;
  const tunnel = form.elements.tunnel_port;
  tunnel.disabled=direct; tunnel.closest('label').hidden=direct;
  $('#route-hint').textContent=direct?'入口 → 目标，直接转发':'入口 → TLS 隧道 → 出口 → 目标';
  $('#rule-port-help').textContent=direct?'放行入口监听端口。保存后约 10 秒生效。':'放行入口监听端口和出口 TCP 隧道端口。保存后约 10 秒生效。';
}
function ruleForm(id) {
  const item = id ? state.rules.find(r=>r.id===id) : {name:'',protocol:'tcp',listen_port:'',tunnel_port:'',targets:[],enabled:1,mode:state.groups.some(g=>g.role==='exit')?'tunnel':'direct'};
  if(!state.groups.some(g=>g.role==='entry')) {toast('请先添加入口设备组');page='nodes';render();return;}
  const options = (role,current) => state.groups.filter(g=>g.role===role).map(g=>`<option value="${g.id}" ${current===g.id?'selected':''}>${esc(g.name)}（${g.online_count} 台在线）</option>`).join('');
  formModal(id?'编辑规则':'添加规则','rules',id,
    field('规则名称','name',item.name,'text','required maxlength="64" placeholder="例如：Web 转发"',true)+
    `<label class="full">入口设备组<select name="entry_id" required>${options('entry',item.entry_id)}</select></label>`+
    field('监听端口','listen_port',item.listen_port,'number','min="1" max="65535" placeholder="留空自动分配"')+
    `<label>协议<select name="protocol"><option value="tcp" ${item.protocol==='tcp'?'selected':''}>TCP</option><option value="udp" ${item.protocol==='udp'?'selected':''}>UDP</option></select></label>`+
    `<label class="full">出口设备组<select name="exit_id"><option value="" ${item.mode==='direct'?'selected':''}>不使用隧道，直接转发</option>${options('exit',item.exit_id || (item.mode==='tunnel'?state.groups.find(g=>g.role==='exit')?.id:null))}</select><small id="route-hint"></small></label>`+
    `<label class="full">目标地址<textarea name="targets" class="target-input" required placeholder="1.2.3.4:5678&#10;example.com:443&#10;[2001:db8::1]:80">${esc((item.targets||[]).join('\n'))}</textarea><small>一行一个，多个目标轮询转发。</small></label>`+
    `<details class="advanced full"><summary>高级选项</summary><div class="fields">`+
    field('出口隧道端口','tunnel_port',item.tunnel_port || '','number','min="1" max="65535" placeholder="留空自动分配"',true)+
    `<label class="full">保存后状态<select name="enabled"><option value="true" ${item.enabled?'selected':''}>启用</option><option value="false" ${!item.enabled?'selected':''}>停用</option></select></label></div></details><p id="rule-port-help" class="subtitle full"></p>`);
  updateRuleMode();
}
async function showInstallation(id, grouped=false, regenerate=false) {
  const data = await api(`/api/${grouped?'groups':'nodes'}/${id}/install`,{method:'POST',body:{regenerate}});
  installation = {...data,id,grouped};
  modal((grouped?'安装设备组 · ':'安装节点 · ')+(grouped?group(id):node(id)).name,`<div class="modal-body"><p class="subtitle">在服务器上执行此命令，安装后自动加入${grouped?'本组':'面板'}。${grouped?'命令长期有效，可用于多台设备。':'命令长期有效，可重复安装此设备。'}</p><label>一键安装命令<textarea class="install-command" readonly aria-label="一键安装命令">${esc(data.command)}</textarea></label><details class="advanced"><summary>安装说明</summary><p>支持 Debian / Ubuntu / Rocky / AlmaLinux，amd64 与 arm64。GOST 从面板下载。</p><p>同机重装保留设备身份。安装后核对公网地址；也可通过 GOST_NODE_HOST 环境变量指定。</p><p>请妥善保存命令。重新生成会撤销旧命令，已安装设备继续运行。</p></details></div>`,`<div class="modal-foot">${grouped?'<button class="secondary" data-action="regenerate-install">重新生成</button>':''}<button class="secondary" data-action="download-install">下载脚本</button><button class="primary" data-action="copy-install">复制命令</button></div>`);
}
function confirmation(type,id) {
  const item = type==='nodes' ? node(id) : type==='groups'?group(id):state.rules.find(r=>r.id===id);
  modal('删除'+(type==='nodes'?'设备':type==='groups'?'设备组':'规则'),`<div class="modal-body"><p class="subtitle">确定删除「${esc(item.name)}」？${type==='nodes'?'设备将退出负载，代理下次同步时停止转发；离线机器请手动停止 gost-agent 服务。':type==='groups'?'请先移除组内设备、关联规则及其他组的故障转移引用。':'在线节点将在下次同步时移除此规则。'}</p><p class="form-error" role="alert"></p></div>`,`<div class="modal-foot"><button class="secondary" data-action="close">取消</button><button class="danger" data-action="confirm-delete" data-type="${type}" data-id="${id}">确认删除</button></div>`);
}
document.addEventListener('click', async event=>{
  const button = event.target.closest('button');
  if(!button) return;
  const {action,id,type} = button.dataset;
  if(button.dataset.page) {page=button.dataset.page;filter='';render();return;}
  try {
    if(action==='close') $('#modal').close();
    if(action==='refresh') await load();
    if(action==='add-node' || action==='add-group') groupForm();
    if(action==='edit-group') groupForm(id);
    if(action==='install-group') {button.disabled=true; await showInstallation(id,true);}
    if(action==='delete-group') confirmation('groups',id);
    if(action==='toggle-node') {button.disabled=true;const item=node(id);await api(`/api/nodes/${id}`,{method:'PUT',body:{...item,enabled:!item.enabled}});toast(item.enabled?'设备已停用，等待配置同步':'设备已启用，等待配置同步');await load();}
    if(action==='edit-node') nodeForm(id);
    if(action==='add-rule') ruleForm();
    if(action==='edit-rule') ruleForm(id);
    if(action==='install-node') {button.disabled=true; await showInstallation(id);}
    if(action==='regenerate-install') {button.disabled=true;await showInstallation(installation.id,true,true);toast('已生成新命令，旧命令已撤销；已安装设备继续运行');}
    if(action==='copy-install') {
      try {await navigator.clipboard.writeText(installation.command);toast('安装命令已复制');}
      catch { $('.install-command').focus();$('.install-command').select();toast('请按 Ctrl+C 或 Command+C 复制命令'); }
    }
    if(action==='download-install') {const url = URL.createObjectURL(new Blob([installation.script],{type:'text/x-shellscript'}));const a=document.createElement('a');a.href=url;a.download='install-node.sh';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
    if(action==='delete-node') confirmation('nodes',id);
    if(action==='delete-rule') confirmation('rules',id);
    if(action==='confirm-delete') {button.disabled=true;await api(`/api/${type}/${id}`,{method:'DELETE'});$('#modal').close();toast('已删除');await load();}
    if(action==='toggle-rule') {button.disabled=true;const item=state.rules.find(r=>r.id===id);await api(`/api/rules/${id}`,{method:'PUT',body:{...item,enabled:!item.enabled}});toast(item.enabled?'规则已停用，等待节点同步':'规则已启用，等待节点同步');await load();}
    if(action==='upgrade-panel') {button.disabled=true;const result=await api('/api/upgrade',{method:'POST',body:{}});toast(result.message);await load();}
    if(action==='diagnose-rule') {
      button.disabled=true;toast('正在检查线路…');
      const result=await api(`/api/rules/${id}/diagnose`,{method:'POST',body:{}});
      modal('线路诊断',`<div class="modal-body">${result.checks.map(check=>`<div class="notice"><strong>${esc(check.label)} · ${check.ok?'通过':'失败'}</strong><p class="mono">${esc(check.address)}</p><p>${esc(check.message)}</p></div>`).join('')}<p class="subtitle">${esc(result.scope)}</p></div>`,`<div class="modal-foot"><button class="primary" data-action="close">关闭</button></div>`);
    }
    if(action==='logout') {await api('/api/logout',{method:'POST',body:{}});showLogin();}
  } catch(error) {if(action==='confirm-delete') $('#modal .form-error').textContent=error.message;else toast(error.message);}
  finally {button.disabled=false;}
});
document.addEventListener('change',event=>{if(event.target.name==='exit_id') updateRuleMode();if(event.target.name==='role' && event.target.closest('#editor')?.dataset.type==='groups') {const form=event.target.closest('form');form.querySelector('[name=failover_id]').innerHTML=groupOptions(event.target.value,'',form.dataset.id);}});
document.addEventListener('input',event=>{if(event.target.id==='rule-search') {filter=event.target.value;const rules=state.rules.filter(r=>[r.name,r.targets.join(' '),group(r.entry_id)?.name,group(r.exit_id)?.name,String(r.listen_port)].some(v=>String(v||'').toLowerCase().includes(filter.toLowerCase())));$('#rule-results').innerHTML=ruleTable(rules);}});
document.addEventListener('submit',async event=>{
  event.preventDefault();const form=event.target, button=form.querySelector('[type=submit]');button.disabled=true;
  const data=Object.fromEntries(new FormData(form));
  try {
    if(form.id==='login-form') {const result=await api('/api/login',{method:'POST',body:data});csrf=result.csrf;const session=await api('/api/session');publicUrl=session.public_url;$('#login-error').textContent='';form.reset();$('#login').hidden=true;$('#shell').hidden=false;await load();}
    if(form.id==='editor') {
      const {type,id}=form.dataset;
      if(type==='rules') {data.mode=data.exit_id?'tunnel':'direct';data.exit_id=data.exit_id || null;data.enabled=data.enabled==='true';data.listen_port=data.listen_port?Number(data.listen_port):null;data.tunnel_port=data.tunnel_port?Number(data.tunnel_port):null;}
      if(type==='nodes' && id) {data.role=node(id).role;data.enabled=Boolean(node(id).enabled);}
      if(type==='groups' && id) data.role=group(id).role;
      await api(`/api/${type}${id?'/'+id:''}`,{method:id?'PUT':'POST',body:data});$('#modal').close();toast(type==='rules'?'规则已保存，等待节点同步':type==='groups'?'设备组已保存，可生成组安装命令':'设备已保存，等待配置同步');await load();
    }
    if(form.id==='password-form') {await api('/api/password',{method:'POST',body:data});showLogin();toast('密码已更新，请重新登录');}
  } catch(error) {const target=form.id==='login-form'?$('#login-error'):form.querySelector('.form-error');target.textContent=error.message;}
  finally {button.disabled=false;}
});
$('#modal').addEventListener('click',event=>{if(event.target===$('#modal')) {const bounds=$('#modal').getBoundingClientRect();if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom) $('#modal').close();}});
(async()=>{try {const session=await api('/api/session');csrf=session.csrf;publicUrl=session.public_url;$('#shell').hidden=false;await load();}catch {showLogin();}setInterval(load,10000);})();
