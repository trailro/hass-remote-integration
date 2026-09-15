let REG={}, RUN=null, INSTALLED={};
async function status(){
  const s=await (await fetch('api/status')).json();
  $('#ha').textContent=s.ha_version;
  $('#comps').textContent=s.components.join(', ');
  REG=s.registry||{}; RUN=s.running; INSTALLED=s.installed||{};
  renderIntegrations(s);
  $('#ov-last').textContent=s.state.last_action||'—';
  const sm=s.smoke_test||{}; if(sm.pending) $('#ov-smoke').innerHTML=`<span class="warn">smoke test of ${esc(sm.pending.domain)} ${esc(sm.pending.tag)} at ${esc(sm.pending.at.slice(11,16))}${sm.pending.auto_rollback?' (auto rollback armed)':''}</span>`;
  else if(sm.last) $('#ov-smoke').innerHTML=`<span class="${sm.last.state==='ok'?'ok':'bad'}">smoke test ${esc(sm.last.domain)} ${esc(sm.last.tag)} ${esc(sm.last.at.slice(11,16))}: ${esc(sm.last.state)}${sm.last.reason?' ('+esc(sm.last.reason)+')':''}${sm.last.action!=='none'?' → '+esc(sm.last.action):''}</span>`;
  else $('#ov-smoke').innerHTML='<span class="mut">none recorded</span>';
  $('#ov-err').textContent=s.state.last_error||'—';
  const ps=s.state.pending_start; $('#ov-pending-row').hidden=!ps;
  if(ps){ $('#ov-pending').innerHTML=`${esc(ps.domain)} ${esc(ps.tag||'')} on Home Assistant ${esc(ps.ha||'(any)')} at the next boot${ps.blocked?` · <span class="bad">blocked: ${esc(ps.blocked)}</span>`:''} <button id="ov-cancel-pending">Cancel</button>`;
    $('#ov-cancel-pending').onclick=async()=>{ if(!confirm('Cancel the deferred start?')) return; const r=await post('api/run/cancel_pending_start'); log(r.ok?'deferred start cancelled':'ERROR: '+r.error); status(); }; }
  const r=s.running||{}; $('#ov-run').innerHTML=r.domain?`<b>${esc(r.domain)}</b> ${esc(r.running_tag||'')}${r.loaded_as_integration?'':' <span class="warn">not loaded</span>'} · entries: ${(r.entries||[]).map(e=>`${esc(e.title)} <span class="${e.disabled_by?'mut':e.state==='loaded'?'ok':'warn'}">${e.disabled_by?'disabled':esc(e.state)}</span>`).join(', ')||'<span class="mut">none</span>'} · patches: ${esc(r.patch||'—')} · <a href="/config?domain=${encodeURIComponent(r.domain)}">Integration</a>`:'<span class="mut">nothing running</span>';
  $('#restart').disabled=s.busy;
  $('#restartnote').textContent=s.state.restart_required?'restart required (new code for an already loaded integration)':'';
  return s;
}
let LAST_FP=null;
function renderIntegrations(s){
  const fp=JSON.stringify([s.installed,s.running&&s.running.domain,s.updates]); if(fp===LAST_FP) return; LAST_FP=fp;  // no rebuild (and no dropdown reset) when nothing changed
  const t=$('#inst'); t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove());
  const doms=Object.entries(INSTALLED); $('#instnote').innerHTML=doms.length?'':'no integration installed yet: pick one on the <a href="/install">Install</a> page';
  for(const [d,x] of doms){
    const tr=document.createElement('tr');
    const upd=x.update_available?` <a href="/config?domain=${encodeURIComponent(d)}" style="text-decoration:none"><span class="tag warn" title="newer stable release on GitHub (weekly check)">update ${esc(x.update_available)}</span></a>`:'';
    const vers=upd+Object.keys(x.versions||{}).sort().map(v=>`<span class="tag ${x.running&&x.running_tag===v?'ok':''}" title="${esc((x.versions[v]||{}).version||'')}">${esc(v)}${x.running&&x.running_tag===v?' · running':x.running_tag===v?' · last run':''}</span>`).join(' ')||'<span class="mut">none in store</span>';
    const ents=(x.entries||[]).length?(x.entries.map(e=>`${esc(e.title)} <span class="${e.disabled_by?'mut':e.state==='loaded'?'ok':'warn'}">${e.disabled_by?'disabled':esc(e.state)}</span>`).join(', ')):'<span class="mut">none (use its Config page)</span>';
    const st=x.running?`<span class="ok">running ${esc(x.running_tag||'')}</span>${x.loaded_as_integration?'':' <span class="warn">not loaded</span>'}`:'<span class="mut">stopped</span>';
    const sel=`<select data-sel="${esc(d)}">${Object.keys(x.versions||{}).sort().reverse().map(v=>`<option ${v===(x.running_tag||x.newest_tag)?'selected':''}>${esc(v)}</option>`).join('')}</select>`;
    tr.innerHTML=`<td><b>${esc(d)}</b><br><span class="mut">${esc(x.name||'')}</span></td><td>${vers}</td><td>${ents}</td><td>${st}</td>
      <td>${x.running?`<button data-a="stop" data-d="${esc(d)}">Stop</button>`:`${sel} <button data-a="start" data-d="${esc(d)}">Start</button>`} <a href="/config?domain=${encodeURIComponent(d)}"><button>Config</button></a> ${x.running?'':`<button data-a="uninstall" data-d="${esc(d)}">Uninstall</button>`}</td>`;
    t.appendChild(tr);
  }
  t.querySelectorAll('button[data-a]').forEach(b=>b.onclick=async()=>{const d=b.dataset.d, a=b.dataset.a;
    if(a==='start'){ const tag=t.querySelector(`select[data-sel="${d}"]`).value; const other=RUN&&RUN.domain&&RUN.domain!==d?` ${RUN.domain} is stopped first (its entries disabled).`:'';
      if(!confirm(`Start ${d} ${tag}?${other} Its files are deployed, requirements installed, patches applied, config entries enabled; MQTT identity becomes hass_${d}.`)) return;
      log(`preflight and start of ${d} ${tag}…`); const r=await startIntegration({domain:d,tag}); if(r.cancelled){log(r.error);return;} log(r.ok?`started ${r.domain} ${r.tag}${r.restart_required?' — RESTART REQUIRED to load the new code':''}${r.pip_failed&&r.pip_failed.length?' · pip failed: '+r.pip_failed.join(', '):''}${r.mqtt&&r.mqtt.connect_error?' · MQTT: '+r.mqtt.connect_error:''}`:'ERROR: '+r.error); await status(); await mqttSummary(); }
    if(a==='stop'){ if(!confirm(`Stop ${d}? Its config entries are disabled; MQTT disconnects (retained state stays on the broker, marked offline).`)) return; const r=await post('api/run/stop'); log(r.ok?`stopped ${r.stopped}`:'ERROR: '+r.error); await status(); await mqttSummary(); }
    if(a==='uninstall'){ if(!confirm(`Uninstall ${d}? Every version, the deployed files and its config entries are removed.`)) return; const r=await post(`api/installed/${d}/uninstall`); log(r.ok?`uninstalled ${d}`:'ERROR: '+r.error); await status(); await mqttSummary(); }
  });
}
async function mqttSummary(){
  const s=await (await fetch('api/mqtt/status')).json(); const h=s.health||{};
  $('#ov-health').innerHTML=`<span class="${h.state==='ok'?'ok':h.state==='stopped'?'mut':h.state==='degraded'?'warn':'bad'}">${esc(h.state||'—')}</span>${h.reason?' · '+esc(h.reason):''}${h.integration?` · ${h.entities_with_state??'?'} entities with a state, ${h.entities_unavailable??0} unavailable, ${h.entities_unknown??0} unknown · last report ${h.last_report_age_s!=null?h.last_report_age_s+' s ago':'never'}`:''}`;
  $('#ov-mqtt').innerHTML=s.enabled?(s.connected?`<span class="ok">connected</span> to ${esc(s.host)}:${esc(s.port)} as <code>${esc(s.base_topic)}</code> · ${s.published} published · discovery ${s.discovery_enabled?'<span class="ok">on</span>':'<span class="warn">off</span>'}`:`<span class="bad">disconnected</span> ${esc(s.connect_error||'')}`):'<span class="warn">disabled</span>';
}
// ----- notifications -----
async function notifications(){
  const r=await (await fetch('api/notifications')).json(); const list=r.notifications||[];
  $('#ntcount').textContent=list.length?String(list.length):''; $('#ntall').hidden=list.length<2;
  const t=$('#ntlist'); t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove());
  for(const n of list){ const tr=document.createElement('tr');
    tr.innerHTML=`<td class="mut" style="white-space:nowrap">${n.created_at?esc(new Date(n.created_at).toLocaleString()):''}</td><td><b>${esc(n.title||n.notification_id)}</b></td><td style="white-space:pre-wrap">${esc(n.message||'')}</td><td><button data-nt="${esc(n.notification_id)}">Dismiss</button></td>`; t.appendChild(tr); }
  if(!list.length){ const tr=document.createElement('tr'); tr.innerHTML='<td colspan="4" class="mut">none</td>'; t.appendChild(tr); }
  t.querySelectorAll('button[data-nt]').forEach(b=>b.onclick=async()=>{ const res=await post(`api/notifications/${encodeURIComponent(b.dataset.nt)}/dismiss`); $('#ntmsg').textContent=res.ok?'dismissed':'ERROR: '+res.error; notifications(); });
}
$('#ntall').onclick=async()=>{ if(!confirm('Dismiss every notification?')) return; const res=await post('api/notifications/dismiss_all'); $('#ntmsg').textContent=res.ok?`dismissed ${res.dismissed}`:'ERROR: '+res.error; notifications(); };
notifications().catch(()=>{}); setInterval(notifications,15000);
// ----- timeline -----
let EVK=new Set();
const KIND_CLS={error:'bad',rollback:'bad',smoke:'warn',health:'warn',restore:'warn',boot:'ok',start:'ok',switch:'ok',cutover:'warn',change:'warn'};
async function timeline(){
  const r=await (await fetch('api/events?limit=60'+(EVK.size?'&kind='+[...EVK].join(','):''))).json();
  const counts={}; (r.kinds||[]).forEach(k=>counts[k]=0); (r.events||[]).forEach(e=>counts[e.kind]=(counts[e.kind]||0)+1);
  $('#evkinds').innerHTML=Object.keys(counts).map(k=>`<span class="tag ${EVK.has(k)?'on':''}" data-k="${esc(k)}" style="cursor:pointer">${esc(k)}${counts[k]?' '+counts[k]:''}</span>`).join(' ');
  $('#evkinds').querySelectorAll('.tag').forEach(el=>el.onclick=()=>{const k=el.dataset.k; EVK.has(k)?EVK.delete(k):EVK.add(k); timeline();});
  const t=$('#evlist'); t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove());
  for(const e of (r.events||[]).slice().reverse()){ const tr=document.createElement('tr'); const cls=KIND_CLS[e.kind]||'mut';
    tr.innerHTML=`<td class="mut" style="white-space:nowrap">${esc((e.ts||'').replace('T',' ').slice(0,19))}</td><td><span class="tag ${cls==='mut'?'':cls}">${esc(e.kind)}</span></td><td class="${cls==='mut'?'':cls}">${esc(e.message)}</td>`; t.appendChild(tr); }
  if(!(r.events||[]).length){ const tr=document.createElement('tr'); tr.innerHTML='<td colspan="3" class="mut">nothing recorded yet</td>'; t.appendChild(tr); }
}
$('#evrefresh').onclick=timeline; timeline().catch(()=>{}); setInterval(timeline,30000);
$('#restart').onclick=async()=>{if(!confirm('Restart the process?'))return;log('restarting…');await post('api/restart');setTimeout(()=>location.reload(),6000)};
async function resources(){
  const m=await (await fetch('api/manager')).json(); const r=m.resources||{}, u=m.updates||{};
  const v=(x,unit)=>x==null?'—':esc(String(x))+unit;
  const ups=[['home_assistant','Home Assistant'],['manager','hass-remote-integration']].filter(([k])=>u[k]&&u[k].latest_version&&u[k].latest_version!==u[k].installed_version)
    .map(([k,label])=>`<a href="${esc(/^https?:\/\//i.test(u[k].release_url||'')?u[k].release_url:'#')}" target="_blank" rel="noopener" style="text-decoration:none"><span class="tag warn">${label} ${esc(u[k].latest_version)}</span></a>`).join(' ');
  $('#ov-res').innerHTML=`memory ${v(r.memory_mb,' MiB')} · CPU ${v(r.cpu_pct,' %')} · event loop lag ${v(r.loop_lag_ms,' ms')} (max ${v(r.loop_lag_max_ms,' ms')}) · ${v(r.threads,'')} threads · volume ${v(r.volume_used_pct,' % used')}, ${v(r.volume_free_gb,' GB free')}`+(r.memory_mb==null?' <span class="mut">(first sample within a minute of the start)</span>':'')+(ups?' · '+ups:'');
}
// ----- resource history -----
function spark(label,unit,pts,color,digits){
  const vals=pts.filter(p=>p[1]!=null);
  if(vals.length<2) return `<div class="spark"><div class="lbl"><span>${label}</span><span>—</span></div><div class="mut" style="font-size:12px;height:64px">not enough samples yet</div></div>`;
  const t0=vals[0][0], t1=vals[vals.length-1][0], vs=vals.map(p=>p[1]); let lo=Math.min(...vs), hi=Math.max(...vs); const span=hi-lo;
  if(span===0){ hi+=1; lo=Math.max(0,lo-1); } else { lo-=span*0.05; hi+=span*0.05; }
  const W=300, H=64, f=v=>Number(v).toFixed(digits);
  const xy=vals.map(([t,v])=>`${((t-t0)/Math.max(1,t1-t0)*W).toFixed(1)},${(H-(v-lo)/(hi-lo)*H).toFixed(1)}`).join(' ');
  const from=new Date(t0*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  return `<div class="spark"><div class="lbl"><span>${label}</span><span>min ${f(Math.min(...vs))} · max ${f(Math.max(...vs))} · now <span class="now">${f(vs[vs.length-1])}${unit}</span></span></div><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${label} since ${from}"><polyline fill="none" stroke="${color}" stroke-width="1.5" vector-effect="non-scaling-stroke" points="${xy}"/></svg><div class="lbl"><span>${from}</span><span>now</span></div></div>`;
}
async function resourceHistory(){
  let r; try{ r=await (await fetch('api/manager/history?hours='+$('#resrange').value)).json(); }catch(e){ return; }
  if(document.activeElement!==$('#reshours')) $('#reshours').value=r.retention_h;
  const col=i=>(r.rows||[]).map(x=>[x[0],x[i]]);
  $('#sparks').innerHTML=spark('Memory',' MiB',col(1),'#58a6ff',0)+spark('CPU',' %',col(2),'#3fb950',1)+spark('Event loop lag (worst)',' ms',col(4),'#d29922',0)+spark('Volume used',' %',col(5),'#8b98a5',1);
  const tr=r.trend||{};
  $('#restrend').textContent=`${r.samples} samples`+(r.hours<Number($('#resrange').value)?` (history is kept ${r.retention_h} h)`:'')+(tr.memory_mib_per_h!=null?` · memory ${tr.memory_mib_per_h>=0?'+':''}${tr.memory_mib_per_h} MiB/h over ${tr.span_h} h`:'');
}
$('#resrange').onchange=resourceHistory;
$('#reshours').onchange=async()=>{ const v=parseInt($('#reshours').value,10); const res=await post('api/settings',{resource_history_h:v}); $('#resmsg').textContent=res.ok?'saved':'ERROR: '+(res.error||res.message); resourceHistory(); };
resourceHistory().catch(()=>{}); setInterval(resourceHistory,60000);
status().catch(e=>log('error: '+e)); mqttSummary().catch(()=>{}); resources().catch(()=>{});
setInterval(status,15000); setInterval(mqttSummary,15000); setInterval(resources,30000);
