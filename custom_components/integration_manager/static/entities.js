let rows=[], sortK='entity_id', sortD=1, open=new Set(), domOn=new Set();
const ageS=iso=>iso?(Date.now()-Date.parse(iso))/1e3:null;
const fmtAge=iso=>{const s=ageS(iso);if(s==null)return '—';
 if(s<60)return Math.round(s)+' s';if(s<3600)return Math.round(s/60)+' min';if(s<86400)return (s/3600).toFixed(1)+' h';return (s/86400).toFixed(1)+' d'};
const ageCls=iso=>{const s=ageS(iso);return s==null?'':s<600?'ok':s<3600?'warn':'bad'};
const fmtTime=iso=>iso?new Date(iso).toLocaleTimeString():'';
function stateCls(st){return st==='unavailable'?'bad':(st==='unknown'||st==null)?'warn':''}
const MQTT_SINCE_2026_5=new Set(['date','time','datetime']);  // these MQTT platforms landed in HA 2026.5
const needsNewHA=r=>r.discovery==='native'&&MQTT_SINCE_2026_5.has(r.entity_id.split('.')[0]);
function render(){
 // an inline edit must not be rebuilt under the cursor -- but only a focused field is one: the button the operator
 // just clicked is inside #tb too and keeps the focus in Chrome and Edge, which stopped the repaint that every
 // successful action asks for, so the page said "ok" over the old row (and over a device it had just deleted)
 const focused=document.activeElement;
 if(focused&&typeof focused.matches==='function'&&focused.matches('input,textarea,select')&&$('#tb').contains(focused)) return;
 const q=$('#q').value.trim().toLowerCase(), integ=$('#integ').value,
       sf=$('#stateFilter').value, onlyD=$('#onlyDisc').checked;
 const stateOk=r=>sf===''||(sf==='enabled'?!r.disabled:sf==='value'?(r.state!=null&&r.state!=='unknown'&&r.state!=='unavailable'):sf==='nostate'?r.state==null:r.state===sf);
 let list=rows.filter(r=>(!integ||r.integration===integ)
   &&(!domOn.size||domOn.has(r.domain))
   &&stateOk(r)
   &&(!onlyD||r.discovery==='mirror')
   &&(!q||[r.entity_id,r.name,r.device&&r.device.name,r.unique_id,r.state].join(' ').toLowerCase().includes(q)));
 list.sort((a,b)=>{let x=a[sortK],y=b[sortK];if(sortK==='device'){x=a.device&&a.device.name;y=b.device&&b.device.name}
   if(sortK==='mqtt'){x=a.mqtt_topic;y=b.mqtt_topic}x=(x??'')+'';y=(y??'')+'';return x.localeCompare(y,undefined,{numeric:true})*sortD});
 $('#n').textContent=`${list.length} / ${rows.length}`;
 const upd=rows.map(r=>r.last_updated).filter(Boolean).sort();
 $('#fresh').textContent=upd.length?`freshest state update ${fmtAge(upd[upd.length-1])} ago (${fmtTime(upd[upd.length-1])}) · ${upd.filter(u=>ageS(u)<300).length} entities updated in the last 5 min, ${upd.filter(u=>ageS(u)>=3600).length} older than 1 h`:'no entity has a state yet';
 const rep=rows.map(r=>r.last_reported).filter(Boolean).sort();
 if(rep.length) $('#fresh').textContent+=` · last report ${fmtAge(rep[rep.length-1])} ago, ${rep.filter(u=>ageS(u)<900).length} entities reported in the last 15 min`;
 const tb=$('#tb'); tb.innerHTML='';
 for(const r of list){
  const tr=document.createElement('tr'); tr.className='e'; tr.dataset.id=r.entity_id;
  const unit=r.attributes&&r.attributes.unit_of_measurement?' '+r.attributes.unit_of_measurement:'';
  tr.innerHTML=`<td class="id">${esc(r.entity_id)}${r.disabled?' <span class="tag">disabled</span>':''}</td>
   <td>${esc(r.name||'')}</td>
   <td class="st ${stateCls(r.state)}">${esc(r.state??'—')}${esc(unit)}</td>
   <td>${esc(r.device?r.device.name:'')}</td>
   <td><span class="tag">${esc(r.integration)}</span></td>
   <td class="upd ${ageCls(r.last_updated)}" title="${esc(r.last_updated||'')}">${r.last_updated?`${fmtTime(r.last_updated)} <span class="mut">· ${fmtAge(r.last_updated)} ago</span>`:'—'}</td>
   <td class="upd ${ageCls(r.last_reported)}" title="${esc(r.last_reported||'')}">${r.last_reported?`${fmtTime(r.last_reported)} <span class="mut">· ${fmtAge(r.last_reported)} ago</span>`:'—'}</td>
   <td class="mut" style="font-size:12px">${r.mqtt_rule&&r.mqtt_rule.exclude?'<span class="tag warn" title="excluded by an MQTT rule: not published, not discovered">excluded</span>':r.mqtt_topic?esc(r.mqtt_topic.split('/').slice(1).join('/')):'—'}${r.mqtt_rule&&r.mqtt_rule.name?` <span class="tag" title="published under this name">as “${esc(r.mqtt_rule.name)}”</span>`:''}${r.discovery==='native'?' <span class="tag ok" title="native HA discovery on its own domain">disc</span>':r.discovery==='mirror'?' <span class="tag warn" title="no MQTT platform in the consuming Home Assistant: mirrored as a sensor with all attributes">mirror</span>':''}${needsNewHA(r)?' <span class="tag bad" title="the MQTT date, time and datetime platforms exist only from Home Assistant 2026.5: a main instance older than that rejects the whole discovery payload of this device, not just this entity, so every other entity of the device disappears there too. Set main_ha_version on the MQTT page to the version that instance runs and this entity is published as a read-only sensor instead, or exclude it from MQTT.">HA 2026.5+</span>':''}</td>`;
  tr.onclick=()=>{open.has(r.entity_id)?open.delete(r.entity_id):open.add(r.entity_id);render()};
  tb.appendChild(tr);
  if(open.has(r.entity_id)){
   const x=document.createElement('tr'); x.className='x';
   const meta={unique_id:r.unique_id,original_name:r.original_name,device_class:r.device_class,entity_category:r.entity_category,
     icon:r.icon,area_id:r.area_id,labels:r.labels,config_entry_id:r.config_entry_id,translation_key:r.translation_key,
     last_changed:r.last_changed,last_updated:r.last_updated,last_reported:r.last_reported,device:r.device,mqtt_topic:r.mqtt_topic,discovery:r.discovery};
   const canEdit=!!r.unique_id;
   x.innerHTML=`<td colspan="8">${canEdit?`<div class="row" style="margin-bottom:8px">
     <input class="act-id" value="${esc(r.entity_id)}" style="min-width:280px" title="new entity_id (same domain)"><button class="act-rename">Rename id</button>
     <input class="act-name" value="${esc(r.name_override||'')}" placeholder="custom name (empty = integration's)" style="min-width:220px"><button class="act-name-save">Save name</button>
     <button class="act-toggle">${r.disabled?'Enable':'Disable'}</button>
     <button class="act-delete" style="border-color:var(--bad)">Delete from registry</button>
     <span class="act-msg mut"></span></div>
    <div class="row" style="margin-bottom:8px"><span class="mut">MQTT only (the entity here stays as it is):</span>
     <button class="act-mqtt">${r.mqtt_rule&&r.mqtt_rule.exclude?'Publish again':'Exclude from MQTT'}</button>
     <input class="act-mqtt-name" value="${esc(r.mqtt_rule&&r.mqtt_rule.name||'')}" placeholder="name on the consuming HA (empty = same)" style="min-width:240px"><button class="act-mqtt-name-save">Save MQTT name</button></div>`:'<div class="mut" style="margin-bottom:8px">entity without a registry entry: cannot be edited</div>'}
     <div class="grid"><div><div class="k">attributes</div><pre>${esc(JSON.stringify(r.attributes,null,1))}</pre></div>
     <div><div class="k">registry / MQTT</div><pre>${esc(JSON.stringify(meta,null,1))}</pre></div></div></td>`;
   x.querySelectorAll('input,button').forEach(el=>el.onclick=e=>e.stopPropagation());
   const act=async(action,body,confirmMsg)=>{ if(confirmMsg&&!confirm(confirmMsg)) return;
     const res=await (await fetch('/api/entities/'+encodeURIComponent(r.entity_id)+'/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})).json();
     const m=x.querySelector('.act-msg'); m.textContent=res.ok?'ok':('ERROR: '+(res.error||res.message||'')); m.className='act-msg '+(res.ok?'ok':'bad');
     if(res.ok){ if(res.entity_id&&res.entity_id!==r.entity_id){open.delete(r.entity_id);open.add(res.entity_id);} await load(); } };
   if(canEdit){
     x.querySelector('.act-rename').onclick=()=>act('rename',{new_entity_id:x.querySelector('.act-id').value.trim()},`Rename ${r.entity_id} → ${x.querySelector('.act-id').value.trim()}? The consuming HA receives the new id and the removal form for the old one.`);
     x.querySelector('.act-name-save').onclick=()=>act('name',{name:x.querySelector('.act-name').value.trim()||null});
     x.querySelector('.act-mqtt').onclick=()=>act(r.mqtt_rule&&r.mqtt_rule.exclude?'mqtt_include':'mqtt_exclude',{},r.mqtt_rule&&r.mqtt_rule.exclude?null:`Stop publishing ${r.entity_id} on MQTT? Its retained document is cleared and the consuming HA gets the removal form.`);
     x.querySelector('.act-mqtt-name-save').onclick=()=>act('mqtt_name',{name:x.querySelector('.act-mqtt-name').value.trim()||null});
     x.querySelector('.act-toggle').onclick=()=>act(r.disabled?'enable':'disable',{},r.disabled?null:`Disable ${r.entity_id}? The integration stops creating it; the consuming HA keeps it, with its customisations, and shows it unavailable.`);
     x.querySelector('.act-delete').onclick=()=>act('delete',{},`Delete ${r.entity_id} from the registry? If the integration still provides it, it comes back at the next restart (as in HA).`);
   }
   tb.appendChild(x);
  }
 }
}
function chips(){
 const counts={}; rows.forEach(r=>counts[r.domain]=(counts[r.domain]||0)+1);
 chipBar('#domains',counts,domOn,()=>{chips();render()});
 const sel=$('#integ'), cur=sel.value, integs=[...new Set(rows.map(r=>r.integration))].sort();
 sel.innerHTML='<option value="">all integrations</option>'+integs.map(i=>`<option ${i===cur?'selected':''}>${esc(i)}</option>`).join('');
}
async function load(){
 try{const r=await fetch('/api/entities');rows=await r.json();$('#ts').textContent=new Date().toLocaleTimeString();chips();render();}
 catch(e){$('#ts').textContent='error: '+e}
}
['#q','#integ','#stateFilter','#onlyDisc'].forEach(s=>$(s).addEventListener('input',render));
document.querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;sortD=(sortK===k)?-sortD:1;sortK=k;render()});
load(); setInterval(load,10000);
