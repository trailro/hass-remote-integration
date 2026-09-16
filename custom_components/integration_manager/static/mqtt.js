async function mqtt(){
  const s=await (await fetch('api/mqtt/status')).json();
  const skipped=s.oversized_skipped?` <span class="err">${s.oversized_skipped} document(s) skipped: ${esc(s.last_oversized||'')}</span>`:'';
  $('#mqconn').innerHTML=(s.enabled?(s.connected?'<span class="ok">connected</span>':`<span class="bad">disconnected</span> ${s.connect_error?'<span class="err">'+esc(s.connect_error)+'</span>':''}`):'<span class="warn">disabled</span>')+skipped;
  $('#mqbroker').innerHTML=`${esc(s.host)}:${esc(s.port)}${s.tls?' · TLS':''} ·${s.has_identity?`<code>${esc(s.wanted_base_topic)}</code>/… <span class="mut">(derived from the running integration)</span>`:'<span class="warn">no identity: start an integration first, then MQTT connects as hass_&lt;domain&gt;</span>'}${s.identity_moved?' <span class="warn">identity changed: reconnect to move to '+esc(s.wanted_base_topic)+'</span>':''}${s.foreign_count?` <span class="bad">base topic in use by something else: ${s.foreign_count} foreign retained topics (e.g. ${esc((s.foreign_topics||[])[0]||'')})</span>`:''}`;
  $('#mqcount').textContent=`${s.published} / ${s.cleared} · ${s.entities_last_run} of ${s.entities_total} entities with a state in the last run${s.entities_registry_only?` · ${s.entities_registry_only} registry-only (disabled/no state: no document, discovery only)`:''}`;
  $('#mqfull').textContent=`${s.last_full_republish||'—'} · incremental ${s.last_incremental_republish||'—'} (${s.entities_last_incremental??0} changed, ${s.unchanged_skipped||0} unchanged skipped so far)`;
  $('#mqrules').innerHTML=`${s.rules||0} per-entity rule(s): exclude / rename / disable-by-default only on the MQTT side · edit per entity on the <a href="/entities" style="color:#58a6ff">entities page</a> or <a href="#" id="mqrulesedit" style="color:#58a6ff">as JSON</a> (globs like <code>sensor.*_rssi</code> allowed)`;
  $('#mqrulesedit').onclick=async e=>{e.preventDefault(); const r=await (await fetch('api/mqtt/rules')).json(); const txt=prompt('MQTT rules JSON: {"<entity_id or glob>": {"exclude": true, "name": "...", "enabled_by_default": false, "entity_category": "diagnostic", "device_class": "...", "icon": "mdi:..."}}', JSON.stringify(r.rules)); if(txt===null) return; let rules; try{rules=JSON.parse(txt);}catch(err){log('invalid JSON: '+err.message);return;} const res=await post('api/mqtt/rules',{rules}); log(res.ok?`rules saved: ${Object.keys(res.rules).length}, cleared ${res.cleared}, republished ${res.republished}`:'ERROR: '+res.error); mqtt();};
  const h=s.health||{}; $('#mqhealth').innerHTML=`<span class="${h.state==='ok'?'ok':h.state==='stopped'?'mut':h.state==='degraded'?'warn':'bad'}">${esc(h.state||'—')}</span>${h.reason?' · '+esc(h.reason):''} · <code>${esc(s.health_topic||'')}</code> retained, refreshed every 60 s${h.integration?` · ${esc(h.integration)} ${esc(h.version||'')}: ${h.entities_with_state??'?'} entities with a state, ${h.entities_unavailable??0} unavailable, last update ${h.last_state_update_age_s!=null?h.last_state_update_age_s+' s ago':'never'}`:''} · manager device on the consuming HA (<code>binary_sensor.${esc(s.base_topic)}_integration</code>, <code>sensor.${esc(s.base_topic)}_health</code>, update entities, resource sensors): ${s.discovery_enabled?'<span class="ok">on</span> (with discovery)':s.manager_discovery?'<span class="ok">on</span> (manager_discovery)':'<span class="mut">off</span>'}${s.manager_commands?' · actions <span class="warn">on</span>':''} · <code>${esc(s.manager_topic||'')}</code>`;
  $('#mqdisc').innerHTML=(s.discovery_enabled?`<span class="ok">on</span>`:'<span class="warn">off</span> (keep it off while running in shadow mode)')+` · prefix <code>${esc(s.discovery_prefix)}</code> · ${s.discovery_devices} devices, ${s.discovery_components} components (${s.discovery_mirrored} mirrored as sensor, ${s.discovery_disabled} disabled) · <a href="/api/mqtt/discovery" style="color:#58a6ff">preview</a>`;
  $('#mqcmd').textContent=`${s.commands} · last: ${esc(s.last_command||'—')} · topic: ${esc(s.cmd_base)}/<domain>/<object_id>/<field>`;
  const hist=s.recent_commands||[]; $('#mqhist').textContent=`${s.history_size||0} in history · a call repeating its _id within 5 min is answered from history, not run again`;
  const ht=$('#mqhistt'); ht.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove());
  for(const c of hist){const tr=document.createElement('tr'); const cls=c.state==='ok'||c.state==='late-ok'?'ok':c.state==='running'?'mut':c.state==='duplicate'?'warn':'bad';
    tr.innerHTML=`<td class="mut" style="white-space:nowrap">${esc((c.received||'').slice(11))}</td><td>${esc(c.kind)}</td><td>${esc(c.what)}${c.id!=null?` <span class="mut">#${esc(String(c.id))}</span>`:''}</td><td class="mut" style="font-size:12px">${esc(c.data||'')}</td><td class="${cls}">${esc(c.state)}${c.error?' · '+esc(c.error):''}</td><td class="mut">${c.duration_ms??''}</td>`; ht.appendChild(tr);}
  $('#mqcall').textContent=`${s.services_published} services published on ${esc(s.base_topic)}/services/<domain> · calls: ${s.calls} · last: ${esc(s.last_call||'—')} · topic: ${esc(s.call_base)}/<domain>/<service> (JSON)`;
}
async function mqttConfigLoad(){
  const c=await (await fetch('api/mqtt/config')).json();
  for(const k of ['base_topic',...MQ_TEXT,...Object.keys(MQ_INT)]) $('#mq_'+k).value=c[k]??'';
  MQ_BOOL.forEach(k=>$('#mq_'+k).checked=!!c[k]); $('#mq_base_topic').disabled=true; $('#mq_base_topic').title='derived from the running integration';
  $('#mq_exclude_integrations').value=(c.exclude_integrations||[]).join(',');
}
const MQ_TEXT=['host','username','discovery_prefix','ca_certs'], MQ_INT={port:1883,republish_interval_s:300,full_republish_interval_min:60}, MQ_BOOL=['enabled','discovery_enabled','force_base_topic','manager_discovery','manager_commands','tls','tls_insecure'];
$('#mqsave').onclick=async()=>{
  const body={password:$('#mq_password').value, exclude_integrations:$('#mq_exclude_integrations').value.split(',').map(s=>s.trim()).filter(Boolean)};
  MQ_TEXT.forEach(k=>body[k]=$('#mq_'+k).value); MQ_BOOL.forEach(k=>body[k]=$('#mq_'+k).checked);
  for(const [k,d] of Object.entries(MQ_INT)) body[k]=parseInt($('#mq_'+k).value,10)||d; body.discovery_prefix=body.discovery_prefix||'homeassistant';
  const r=await post('api/mqtt/config',body); log(r.ok?'MQTT config saved':'ERROR: '+JSON.stringify(r)); $('#mq_password').value=''; await mqtt();
};
$('#mqform').addEventListener('submit',e=>e.preventDefault());  // Enter in a field must not reload the page (no inline handler: CSP)
$('#mqrepub').onclick=async()=>{const r=await post('api/mqtt/republish');log(`republished ${r.published} entities`);await mqtt()};
$('#mqreconn').onclick=async()=>{await post('api/mqtt/reconnect');log('MQTT reconnecting');await mqtt()};
setInterval(mqtt,15000); mqtt().catch(e=>log('mqtt: '+e)); mqttConfigLoad().catch(e=>log('mqtt config: '+e));
// ----- health rules -----
async function healthRules(){
  const r=await (await fetch('api/settings')).json(); if(document.activeElement!==$('#hstale')&&document.activeElement!==$('#hunav')){$('#hstale').value=r.health_stale_s; $('#hunav').value=r.health_unavailable_pct;}
  const t=$('#hrules'); if(t.contains(document.activeElement)) return; t.querySelectorAll('tr:not(:first-child)').forEach(e=>e.remove());
  const st=await (await fetch('api/status',{headers:{'X-Requested-With':'fetch'}})).json(); const INSTALLED=st.installed||{}, RUN=st.running;
  for(const d of Object.keys(INSTALLED)){ const own=(r.health||{})[d]||{}; const tr=document.createElement('tr');
    tr.innerHTML=`<td><b>${esc(d)}</b>${RUN&&RUN.domain===d?' <span class="tag ok">running</span>':''}</td><td><select data-h="mode" data-d="${esc(d)}"><option value="">periodic (default)</option><option value="periodic" ${own.mode==='periodic'?'selected':''}>periodic</option><option value="event" ${own.mode==='event'?'selected':''}>event</option></select></td>
     <td><input type="number" min="60" max="86400" data-h="stale_s" data-d="${esc(d)}" value="${esc(own.stale_s??'')}" placeholder="${esc(r.health_stale_s)}" style="width:90px"></td><td><input type="number" min="1" max="100" data-h="unavailable_pct" data-d="${esc(d)}" value="${esc(own.unavailable_pct??'')}" placeholder="${esc(r.health_unavailable_pct)}" style="width:70px"></td><td><button data-hs="${esc(d)}">Save</button></td>`;
    t.appendChild(tr); }
  t.querySelectorAll('button[data-hs]').forEach(b=>b.onclick=async()=>{const d=b.dataset.hs; const rules={}; t.querySelectorAll(`[data-d="${d}"]`).forEach(el=>{ if(el.value!=='') rules[el.dataset.h]=el.dataset.h==='mode'?el.value:parseInt(el.value,10); });
    const cur=(await (await fetch('api/settings')).json()).health||{}; cur[d]=rules; const res=await post('api/settings',{health:cur}); $('#hmsg').textContent=res.ok?`rules of ${d} saved (empty = default)`:'ERROR: '+res.error; });
}
$('#hsave').onclick=async()=>{const r=await post('api/settings',{health_stale_s:parseInt($('#hstale').value,10)||900,health_unavailable_pct:parseInt($('#hunav').value,10)||50}); $('#hmsg').textContent=r.ok?'defaults saved':'ERROR: '+r.error; healthRules();};
healthRules().catch(()=>{});
