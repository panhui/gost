'use strict';
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let state = {nodes:[],groups:[],rules:[],audit:[]}, csrf = '', publicUrl = '', page = 'overview', filter = '', installation = null, busy = false;
const labels = {overview:'概览',rules:'转发规则',nodes:'设备组与节点',audit:'操作记录',settings:'系统设置'};
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
  if(!rule.exit_nodes?.length) return pill('出口无可用设备','red');
  const entries = (rule.entry_nodes||[]).map(node).filter(Boolean);
  if(!entries.length) return pill('入口无在线设备','amber');
  if(!entries.some(n=>n.synced && n.running && !n.error)) return pill('入口待同步','amber');
  return rule.failover_active ? pill('故障转移中','amber') : pill('配置已应用','green');
}
function accessAddresses(rule) {
  const ids = rule.entry_nodes?.length ? rule.entry_nodes : state.nodes.filter(n=>n.group_id===rule.entry_id && n.enabled).map(n=>n.id);
  return ids.map(node).filter(Boolean).map(n=>(n.host.includes(':')?'['+n.host+']':n.host)+':'+rule.listen_port).join(' / ') || '入口组尚无设备';
}
function heading(title, subtitle, action='') {return `<div class="page-heading"><div><p class="eyebrow">${page==='overview'?'NETWORK OVERVIEW':'GOST CONTROL PLANE'}</p><h1>${title}</h1><p class="subtitle">${subtitle}</p></div>${action}</div>`;}
function ruleTable(rules) {
  if(!rules.length) return `<div class="empty"><div class="empty-icon">⇄</div><strong>${filter?'没有匹配的规则':'创建你的第一条线路'}</strong><p>${filter?'试试其他名称、节点或落地地址':'选择入口和出口，填写监听端口与国外落地地址。'}</p>${filter?'':'<button class="primary" data-action="add-rule">＋ 新建转发规则</button>'}</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>规则 / 协议</th><th>入口 → 出口</th><th>落地地址</th><th>状态</th><th>操作</th></tr></thead><tbody>${rules.map(rule=>`<tr><td><div class="rule-name"><span class="rule-icon">⇄</span><div><strong>${esc(rule.name)}</strong><span class="cell-sub mono">${rule.protocol.toUpperCase()} · :${rule.listen_port}</span></div></div></td><td>${esc(group(rule.entry_id)?.name)}<span class="route-arrow">→</span>${esc(group(rule.exit_id)?.name)}<span class="cell-sub mono">TLS · 出口隧道 :${rule.tunnel_port} · ${rule.exit_nodes?.length||0} 台参与负载</span></td><td><span class="mono">${esc(rule.target_host.includes(':')?'['+rule.target_host+']':rule.target_host)}:${rule.target_port}${rule.targets.length>1?' +'+(rule.targets.length-1)+' 个目标':''}</span><span class="cell-sub">${esc(accessAddresses(rule))}</span></td><td>${ruleStatus(rule)}</td><td><div class="row-actions"><button class="toggle ${rule.enabled?'on':''}" data-action="toggle-rule" data-id="${rule.id}" role="switch" aria-checked="${Boolean(rule.enabled)}" aria-label="${rule.enabled?'停用':'启用'} ${esc(rule.name)}"></button><button data-action="diagnose-rule" data-id="${rule.id}">诊断</button><button data-action="edit-rule" data-id="${rule.id}">编辑</button><button data-action="delete-rule" data-id="${rule.id}">删除</button></div></td></tr>`).join('')}</tbody></table></div>`;
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
      `<section class="panel"><div class="panel-heading"><div><h2>最近的转发规则</h2><p>配置已应用表示代理已加载配置。点击诊断可检查 DNS 与端口，实际业务请从入口测试</p></div><button class="text-button" data-page="rules">查看全部 →</button></div>${ruleTable(state.rules.slice(0,5))}</section>` +
      `<div class="two-columns"><section class="panel"><div class="panel-heading"><h2>快速开始</h2><button class="text-button" data-action="add-group">添加设备组 →</button></div><div class="onboarding"><div class="step"><b>1</b><div><strong>先创建入口组与出口组</strong><p>设置下线时间和备用组，将同一条组安装命令执行到多台服务器。</p></div></div><div class="step"><b>2</b><div><strong>创建转发规则</strong><p>设置入口监听端口，选择出口，填写国外落地 IP 和端口。</p></div></div><div class="step"><b>3</b><div><strong>确认配置已生效</strong><p>代理每 10 秒同步一次。放行对应端口后，从入口验证连接。</p></div></div></div></section><section class="panel"><div class="panel-heading"><h2>最近操作</h2><button class="text-button" data-page="audit">全部记录 →</button></div><div class="activity">${events(state.audit.slice(0,4))}</div></section></div>`;
  } else if(page==='rules') {
    const rules = state.rules.filter(r=>[r.name,r.targets.join(' '),group(r.entry_id)?.name,group(r.exit_id)?.name,String(r.listen_port)].some(v=>String(v||'').toLowerCase().includes(filter.toLowerCase())));
    content = heading('转发规则','配置入口、出口与落地地址；保存后自动下发到关联节点。','<button class="primary" data-action="add-rule">＋ 新建转发规则</button>') + `<section class="panel"><div class="panel-heading"><h2>全部规则 <span class="muted">· ${state.rules.length}</span></h2>${pill('TCP / UDP')}</div><div class="searchbar"><div class="search"><input id="rule-search" type="search" placeholder="搜索规则、节点、IP 或端口" aria-label="搜索规则" value="${esc(filter)}"></div></div><div id="rule-results">${ruleTable(rules)}</div></section>`;
  } else if(page==='nodes') {
    content = heading('设备组与节点','先建设备组，再使用组安装命令加入机器。在线出口按新连接轮询分流。','<button class="primary" data-action="add-group">＋ 添加设备组</button>') +
      (state.groups.length ? state.groups.map(g=>`<section class="panel group-panel"><div class="panel-heading group-heading"><div><h2>${esc(g.name)} ${pill(g.role==='entry'?'入口组':'出口组','purple')}</h2><p>${g.node_count} 台设备 · ${g.online_count} 台在线 · ${g.eligible_count} 台就绪 · 负载下线 ${g.offline_after} 秒</p><p>故障转移：${esc(group(g.failover_id)?.name || '未设置')}${g.remark?' · '+esc(g.remark):''}</p></div><div class="row-actions"><button class="primary" data-action="install-group" data-id="${g.id}">组安装命令</button><button class="secondary" data-action="edit-group" data-id="${g.id}">编辑组</button><button class="secondary" data-action="delete-group" data-id="${g.id}">删除组</button></div></div>${g.node_count?`<div class="table-wrap"><table><thead><tr><th>设备 / 公网地址</th><th>心跳 / 配置</th><th>状态</th><th>操作</th></tr></thead><tbody>${state.nodes.filter(n=>n.group_id===g.id).map(n=>`<tr><td><strong>${esc(n.name)}</strong><span class="cell-sub mono">${esc(n.host)}</span>${n.error?`<span class="cell-sub node-error">${esc(n.error)}</span>`:''}</td><td>${n.service_count} 条规则<span class="cell-sub">${n.last_seen?'最后心跳 '+new Date(n.last_seen*1000).toLocaleTimeString('zh-CN'):'待安装'}</span></td><td>${nodeStatus(n)}${n.eligible?pill('负载就绪','green'):''}</td><td><div class="row-actions"><button class="secondary" data-action="toggle-node" data-id="${n.id}">${n.enabled?'停用负载':'启用负载'}</button><button class="secondary" data-action="edit-node" data-id="${n.id}">编辑设备</button><button class="secondary" data-action="delete-node" data-id="${n.id}">移除设备</button></div></td></tr>`).join('')}</tbody></table></div>`:`<div class="empty"><strong>还没有设备加入</strong><p>将本组安装命令执行到服务器，设备会自动注册到这里。</p><button class="secondary" data-action="install-group" data-id="${g.id}">生成组安装命令</button></div>`}</section>`).join('') : `<section class="panel"><div class="empty"><div class="empty-icon">▦</div><strong>创建第一个设备组</strong><p>先创建入口组与出口组，再将服务器加入对应组。</p><button class="primary" data-action="add-group">＋ 添加设备组</button></div></section>`) +
      '<div class="notice">同一组安装命令可在多台机器上重复使用，不限时间；同机重装保留设备身份。超过组设置的心跳时间、停用负载或 GOST 异常的出口不参与负载。组内全部不可用时使用备用组。请放行组内每台机器对应的转发端口。</div>';
  } else if(page==='audit') {
    content = heading('操作记录','最近的管理员操作和节点注册记录，最多保留 200 条。') + `<section class="panel"><div class="panel-heading"><h2>最近 30 条记录</h2>${pill('操作审计')}</div><div class="activity">${events(state.audit)}</div></section>`;
  } else {
    content = heading('系统设置','管理控制台访问与部署信息。') + `<div class="two-columns"><section class="panel"><div class="panel-heading"><h2>管理员密码</h2></div><form id="password-form"><div class="settings-content"><label>当前密码<input type="password" name="old_password" autocomplete="current-password" required></label><label>新密码<input type="password" name="new_password" autocomplete="new-password" minlength="12" required><small>至少 12 位。修改后所有已登录会话将注销。</small></label><p class="form-error"></p><button class="primary" type="submit">更新密码</button></div></form></section><section class="panel"><div class="panel-heading"><h2>部署信息</h2></div><div class="settings-content"><p class="muted">节点连接地址</p><code>${esc(publicUrl)}</code><p class="muted">转发引擎</p><p>GOST ${esc(state.gost_version)} · Relay over TLS</p><p class="muted">面板版本</p><p>${esc(state.panel_version)}</p><button class="primary" data-action="upgrade-panel" ${!state.upgrade?.enabled || ['queued','running'].includes(state.upgrade?.state)?'disabled':''}>一键升级面板</button><p class="subtitle">${esc(state.upgrade?.message || '')}${state.upgrade?.commit?' · '+esc(state.upgrade.commit.slice(0,7)):''}</p><p class="subtitle">${state.upgrade?.enabled?'从 GitHub main 升级；自动备份数据库，保留节点、规则、密码与证书。升级时面板会短暂重启。':'先在服务器执行一次新版安装脚本，以启用一键升级服务。'}</p><div class="notice">面板证书由安装脚本生成并固定在节点端。更换面板证书后，请重新生成安装脚本并安装节点。数据库和证书请定期备份。</div></div></section></div>`;
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
function groupOptions(role, current='', excluded='') {
  return '<option value="">无</option>'+state.groups.filter(g=>g.role===role && g.id!==excluded).map(g=>`<option value="${g.id}" ${g.id===current?'selected':''}>${esc(g.name)}</option>`).join('');
}
function groupForm(id) {
  const item = id ? group(id) : {name:'',role:'entry',offline_after:60,failover_id:'',remark:''};
  formModal(id?'编辑设备组':'添加设备组','groups',id,
    field('名称','name',item.name,'text','required maxlength="64" placeholder="例如：广州入口组"',true)+
    `<label class="full">类型<select name="role" ${id?'disabled':''}><option value="entry" ${item.role==='entry'?'selected':''}>入口组</option><option value="exit" ${item.role==='exit'?'selected':''}>出口组 · 多机自动负载均衡</option></select></label>`+
    field('负载下线（秒）','offline_after',item.offline_after,'number','required min="20" max="3600"',true)+
    '<p class="subtitle full">超过此时间未收到设备心跳，视为下线并从出口负载移除。默认 60 秒。</p>'+
    `<label class="full">故障转移组<select name="failover_id">${groupOptions(item.role,item.failover_id,id)}</select><small>同类型备用组。主组没有可用设备时切换，主组恢复后优先使用主组。</small></label>`+
    `<label class="full">备注<textarea name="remark" maxlength="500">${esc(item.remark)}</textarea></label>`,
    '组内所有设备使用同一条安装命令。入口多机需要客户端选择可用入口地址，或另行配置 DNS / 外部负载入口。');
}
function nodeForm(id) {
  const item = node(id);
  formModal('编辑设备','nodes',id,field('设备名称','name',item.name,'text','required maxlength="64"',true)+
    `<label class="full">所属设备组<select name="group_id" required>${state.groups.filter(g=>g.role===item.role).map(g=>`<option value="${g.id}" ${g.id===item.group_id?'selected':''}>${esc(g.name)}</option>`).join('')}</select></label>`+
    field('公网 IP / 域名','host',item.host,'text','required placeholder="例如：203.0.113.10"',true),
    '安装时根据与面板建立连接的公网地址自动识别。经过反向代理或 NAT 时请核对地址；编辑保存后约 10 秒同步配置。');
}
function ruleForm(id) {
  const item = id ? state.rules.find(r=>r.id===id) : {name:'',protocol:'tcp',listen_port:'',tunnel_port:'',targets:[],enabled:1};
  if(!state.groups.some(n=>n.role==='entry') || !state.groups.some(n=>n.role==='exit')) {toast('请先添加入口设备组和出口设备组'); page='nodes'; render(); return;}
  const selectNode = (role,label,key) => `<label class="full">${label}<select name="${key}" required>${state.groups.filter(n=>n.role===role).map(n=>`<option value="${n.id}" ${item[key]===n.id?'selected':''}>${esc(n.name)} · ${n.online_count} / ${n.node_count} 在线</option>`).join('')}</select></label>`;
  formModal(id?'编辑转发规则':'添加转发规则','rules',id,
    field('名称','name',item.name,'text','required maxlength="64" placeholder="例如：香港落地 · Web"',true)+
    selectNode('entry','入口设备组','entry_id')+
    `<label class="full">监听端口 <span class="muted">· 留空自动分配 2000–60000</span><input type="number" name="listen_port" value="${esc(item.listen_port)}" min="1" max="65535" placeholder="留空则随机分配可用端口"></label>`+
    selectNode('exit','出口设备组','exit_id')+
    `<div class="connection-info full"><span class="muted">连接信息</span><p>协议：TLS 隧道 <span class="route-arrow">/</span> 认证：已开启 <span class="route-arrow">/</span> 延迟优化：已开启</p></div>`+
    `<label class="full">目标地址<textarea name="targets" class="target-input" required placeholder="一行一个，空行会被忽略。格式如下：&#10;&#10;1.2.3.4:5678&#10;[2001:db8::1]:80&#10;example.com:443">${esc((item.targets||[]).join('\n'))}</textarea><small>支持 IP 和域名，最多 32 个目标；多个目标在出口按轮询方式分配新连接。</small></label>`+
    `<details class="advanced full"><summary>高级选项</summary><div class="fields"><label>转发协议<select name="protocol"><option value="tcp" ${item.protocol==='tcp'?'selected':''}>TCP</option><option value="udp" ${item.protocol==='udp'?'selected':''}>UDP · 通过 TLS 传输</option></select></label>`+
    field('出口隧道端口','tunnel_port',item.tunnel_port,'number','min="1" max="65535" placeholder="留空自动分配"')+
    `<label class="full">保存后状态<select name="enabled"><option value="true" ${item.enabled?'selected':''}>启用规则</option><option value="false" ${!item.enabled?'selected':''}>停用规则</option></select></label></div></details>`,
    '保存后约 10 秒同步到入口和出口。请放行入口监听端口及出口 TCP 隧道端口。修改配置会短暂重启相关节点的 GOST 进程。');
}
async function showInstallation(id, grouped=false, regenerate=false) {
  const data = await api(`/api/${grouped?'groups':'nodes'}/${id}/install`,{method:'POST',body:{regenerate}});
  installation = {...data,id,grouped};
  modal((grouped?'安装设备组 · ':'安装节点 · ')+(grouped?group(id):node(id)).name,`<div class="modal-body"><p class="subtitle">将同一条命令执行到每台对应服务器，安装后自动加入本组；同机重装保留设备身份。也可下载脚本后执行 sudo bash install-node.sh。</p><div class="notice">安装命令不限时间，可重复安装；打开此窗口复用现有组命令。重新生成命令会撤销旧命令；删除设备组后命令失效。每台设备使用独立的代理凭证，请妥善保存命令。</div><label>一键安装命令<textarea class="install-command" readonly aria-label="一键安装命令">${esc(data.command)}</textarea></label><p class="subtitle">GOST 安装包通过面板缓存下载，无需节点直接访问 GitHub。支持 Debian / Ubuntu / Rocky / AlmaLinux，amd64 与 arm64。自动识别设备公网地址，安装完成后请在设备组中核对；经过反向代理时可编辑地址，或执行命令前设置 GOST_NODE_HOST 环境变量。</p></div>`,`<div class="modal-foot">${grouped?'<button class="secondary" data-action="regenerate-install">重新生成命令</button>':''}<button class="secondary" data-action="download-install">下载脚本</button><button class="primary" data-action="copy-install">复制命令</button></div>`);
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
      button.disabled=true;toast('正在检查 DNS、端口、TLS 与落地连接…');
      const result=await api(`/api/rules/${id}/diagnose`,{method:'POST',body:{}});
      modal('线路诊断',`<div class="modal-body">${result.checks.map(check=>`<div class="notice"><strong>${esc(check.label)} · ${check.ok?'通过':'失败'}</strong><p class="mono">${esc(check.address)}</p><p>${esc(check.message)}</p></div>`).join('')}<p class="subtitle">${esc(result.scope)}</p></div>`,`<div class="modal-foot"><button class="primary" data-action="close">关闭</button></div>`);
    }
    if(action==='logout') {await api('/api/logout',{method:'POST',body:{}});showLogin();}
  } catch(error) {if(action==='confirm-delete') $('#modal .form-error').textContent=error.message;else toast(error.message);}
  finally {button.disabled=false;}
});
document.addEventListener('change',event=>{if(event.target.name==='role' && event.target.closest('#editor')?.dataset.type==='groups') {const form=event.target.closest('form');form.querySelector('[name=failover_id]').innerHTML=groupOptions(event.target.value,'',form.dataset.id);}});
document.addEventListener('input',event=>{if(event.target.id==='rule-search') {filter=event.target.value;const rules=state.rules.filter(r=>[r.name,r.targets.join(' '),group(r.entry_id)?.name,group(r.exit_id)?.name,String(r.listen_port)].some(v=>String(v||'').toLowerCase().includes(filter.toLowerCase())));$('#rule-results').innerHTML=ruleTable(rules);}});
document.addEventListener('submit',async event=>{
  event.preventDefault();const form=event.target, button=form.querySelector('[type=submit]');button.disabled=true;
  const data=Object.fromEntries(new FormData(form));
  try {
    if(form.id==='login-form') {const result=await api('/api/login',{method:'POST',body:data});csrf=result.csrf;const session=await api('/api/session');publicUrl=session.public_url;$('#login-error').textContent='';form.reset();$('#login').hidden=true;$('#shell').hidden=false;await load();}
    if(form.id==='editor') {
      const {type,id}=form.dataset;
      if(type==='rules') {data.enabled=data.enabled==='true';data.listen_port=data.listen_port?Number(data.listen_port):null;data.tunnel_port=data.tunnel_port?Number(data.tunnel_port):null;}
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
