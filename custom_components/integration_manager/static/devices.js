let rows=[], sortK='name', sortD=1, open=new Set(), modelOn=new Set();
function ordered(list){ // via-device tree: parents first, children indented
 if(!$('#tree').checked) return list.map(r=>({r,depth:0}));
 const byId=Object.fromEntries(list.map(r=>[r.id,r])); const out=[]; const seen=new Set();
 const kids=id=>list.filter(r=>r.via_device_id===id);
 const walk=(r,depth)=>{ if(seen.has(r.id)) return; seen.add(r.id); out.push({r,depth}); kids(r.id).forEach(k=>walk(k,depth+1)); };
 list.filter(r=>!r.via_device_id||!byId[r.via_device_id]).forEach(r=>walk(r,0)); list.forEach(r=>walk(r,0));
 return out;
}
function render(){
 // an inline edit must not be rebuilt under the cursor -- but only a focused field is one: the button the operator
 // just clicked is inside #tb too and keeps the focus in Chrome and Edge, which stopped the repaint that every
 // successful action asks for, so the page said "ok" over the old row (and over a device it had just deleted)
 const focused=document.activeElement;
 if(focused&&typeof focused.matches==='function'&&focused.matches('input,textarea,select')&&$('#tb').contains(focused)) return;
 const q=$('#q').value.trim().toLowerCase(), integ=$('#integ').value;
 let list=rows.filter(r=>(!integ||r.integrations.includes(integ))&&(!modelOn.size||modelOn.has(r.model||'—'))
   &&(!q||[r.name,r.model,r.manufacturer,r.identifier,r.entities.map(e=>e.entity_id).join(' ')].join(' ').toLowerCase().includes(q)));
 list.sort((a,b)=>{let x=a[sortK],y=b[sortK]; if(sortK==='entities')return (a.entities.length-b.entities.length)*sortD;
   if(sortK==='via'){x=a.via_name;y=b.via_name} x=(x??'')+'';y=(y??'')+''; return x.localeCompare(y,undefined,{numeric:true})*sortD});
 $('#n').textContent=`${list.length} / ${rows.length}`;
 const tb=$('#tb'); tb.innerHTML='';
 for(const {r,depth} of ordered(list)){
  const tr=document.createElement('tr'); tr.className='d';
  const unav=r.unavailable; const pad=depth?`<span class="tree">${'└ '.repeat(1)}</span>`:'';
  tr.innerHTML=`<td style="padding-left:${8+depth*18}px">${pad}<b>${esc(r.name)}</b>${r.name_by_user?` <span class="mut">(${esc(r.original_name)})</span>`:''}${r.disabled_by?' <span class="tag">disabled</span>':''}</td>
   <td>${esc(r.manufacturer?r.manufacturer+' ':'')}${esc(r.model||'')}</td><td class="id">${esc(r.identifier)}</td>
   <td class="mut">${esc(r.via_name||'')}</td><td>${r.entities.length}</td>
   <td class="${unav?'bad':'mut'}">${unav||'—'}</td><td class="id mut">${esc(r.discovery_id)}</td>`;
  tr.onclick=()=>{open.has(r.id)?open.delete(r.id):open.add(r.id);render()};
  tb.appendChild(tr);
  if(open.has(r.id)){
   const x=document.createElement('tr'); x.className='x';
   const ents=r.entities.map(e=>`<tr><td class="id">${esc(e.entity_id)}</td><td>${esc(e.name||'')}</td><td class="${e.state==='unavailable'?'bad':e.state==null||e.state==='unknown'?'warn':''}">${esc(e.state??'—')}${e.unit?' '+esc(e.unit):''}</td><td class="mut">${e.disabled?'disabled':''}</td></tr>`).join('');
   const meta={id:r.id,identifiers:r.identifiers,connections:r.connections,manufacturer:r.manufacturer,model:r.model,model_id:r.model_id,serial_number:r.serial_number,
     sw_version:r.sw_version,hw_version:r.hw_version,area:r.area,via_device_id:r.via_device_id,config_entries:r.config_entries,disabled_by:r.disabled_by,
     discovery_id:r.discovery_id,discovery_topic:r.discovery_topic,device_block:r.device_block};
   x.innerHTML=`<td colspan="7"><div class="row" style="margin-bottom:8px">
     <input class="act-name" value="${esc(r.name_by_user||'')}" placeholder="custom name (empty = ${esc(r.original_name||'')})" style="min-width:260px"><button class="act-name-save">Save name</button>
     <button class="act-delete" style="border-color:var(--bad)">Delete device</button><span class="act-msg mut"></span></div>
     <div class="grid"><div><div class="k">entities (${r.entities.length})</div><table class="ents">${ents}</table></div>
     <div><div class="k">registry / discovery</div><pre>${esc(JSON.stringify(meta,null,1))}</pre></div></div></td>`;
   x.querySelectorAll('input,button').forEach(el=>el.onclick=e=>e.stopPropagation());
   const act=async(action,body,confirmMsg)=>{ if(confirmMsg&&!confirm(confirmMsg)) return;
     const res=await (await fetch('api/devices/'+encodeURIComponent(r.id)+'/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})).json();
     const m=x.querySelector('.act-msg'); m.textContent=res.ok?'ok':('ERROR: '+(res.error||res.message||'')); m.className='act-msg '+(res.ok?'ok':'bad'); if(res.ok) await load(); };
   x.querySelector('.act-name-save').onclick=()=>act('name',{name:x.querySelector('.act-name').value.trim()||null});
   x.querySelector('.act-delete').onclick=()=>act('delete',{},`Delete device ${r.name}? Its entities of this integration are removed (others are detached); the integration may refuse, like in HA. If it still provides the device, it comes back at restart.`);
   tb.appendChild(x);
  }
 }
}
function chips(){
 const counts={}; rows.forEach(r=>{const m=r.model||'—';counts[m]=(counts[m]||0)+1});
 chipBar('#models',counts,modelOn,()=>{chips();render()});
 const sel=$('#integ'), cur=sel.value, integs=[...new Set(rows.flatMap(r=>r.integrations))].sort();
 sel.innerHTML='<option value="">all integrations</option>'+integs.map(i=>`<option ${i===cur?'selected':''}>${esc(i)}</option>`).join('');
}
async function load(){try{const r=await fetch('api/devices');rows=await r.json();$('#ts').textContent=new Date().toLocaleTimeString();chips();render()}
 catch(e){$('#ts').textContent='error: '+e}}
['#q','#integ','#tree'].forEach(s=>$(s).addEventListener('input',render));
document.querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;sortD=(sortK===k)?-sortD:1;sortK=k;render()});
load(); setInterval(load,30000);
