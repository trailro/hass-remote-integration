let data=[], open=new Set(), domOn=new Set();
function selKind(sel){if(!sel||typeof sel!=='object')return '';const k=Object.keys(sel)[0];const v=sel[k]||{};
 if(k==='select'&&v.options)return 'select: '+v.options.map(o=>typeof o==='object'?o.value:o).join(' | ');
 if(k==='number')return `number ${v.min??''}…${v.max??''} ${v.unit_of_measurement||''}`.trim();
 if(k==='entity')return 'entity'+(v.domain?' ('+[].concat(v.domain).join(',')+')':'');
 return k}
function fieldsTable(f){const keys=Object.keys(f||{});if(!keys.length)return '<span class="mut">no fields</span>';
 return `<table class="fields"><tr><th>field</th><th>type</th><th>description</th><th>example</th></tr>`+keys.map(k=>{const d=f[k]||{};
  return `<tr><td class="id">${esc(k)}${d.required?' <span class="req">*</span>':''}</td><td class="mut">${esc(selKind(d.selector))}</td>
   <td>${esc(d.description||d.name||'')}</td><td class="mut">${d.example!==undefined?esc(JSON.stringify(d.example)):''}</td></tr>`}).join('')+'</table>'}
function render(){
 const q=$('#q').value.trim().toLowerCase(), onlyC=$('#onlyCustom').checked;
 let doms=data.filter(d=>(!onlyC||d.custom)&&(!domOn.size||domOn.has(d.domain)));
 let total=0, shown=0; data.forEach(d=>total+=d.services.length);
 const out=$('#out'); out.innerHTML='';
 for(const d of doms){
  const svcs=d.services.filter(s=>!q||[d.domain+'.'+s.name,s.title,s.description,Object.keys(s.fields||{}).join(' ')].join(' ').toLowerCase().includes(q));
  if(!svcs.length)continue; shown+=svcs.length;
  const card=document.createElement('div'); card.className='card';
  card.innerHTML=`<div class="row" style="justify-content:space-between"><h2>${esc(d.domain)} <span class="tag">${svcs.length}</span>${d.custom?'<span class="tag ok">custom</span>':''}</h2>
   <span class="mut">${esc(d.title||'')}</span></div><div class="wrap"><table><thead><tr><th>service</th><th>description</th><th>target</th><th>fields</th><th>response</th></tr></thead><tbody></tbody></table></div>`;
  const tb=card.querySelector('tbody');
  for(const s of svcs){
   const full=d.domain+'.'+s.name, tr=document.createElement('tr'); tr.className='s';
   const target=s.target?Object.keys(s.target).join(', '):'';
   tr.innerHTML=`<td class="id">${esc(full)}</td><td>${s.title?'<b>'+esc(s.title)+'</b> — ':''}${esc(s.description||'')}</td><td class="mut">${esc(target||'—')}</td>
    <td class="mut">${Object.keys(s.fields||{}).length}</td><td class="mut">${esc(s.response||'—')}</td>`;
   tr.onclick=()=>{open.has(full)?open.delete(full):open.add(full);render()};
   tb.appendChild(tr);
   if(open.has(full)){const x=document.createElement('tr');x.className='x';
    x.innerHTML=`<td colspan="5"><div class="k">fields (* = required)</div>${fieldsTable(s.fields)}
     ${s.target?`<div class="k">target</div><pre>${esc(JSON.stringify(s.target,null,1))}</pre>`:''}${callForm(d.domain,s)}</td>`;tb.appendChild(x);
    wireCall(x,d.domain,s)}
  }
  out.appendChild(card);
 }
 $('#n').textContent=`${shown} / ${total}`;
}
function fieldInput(name,f){const sel=f.selector||{}, k=Object.keys(sel)[0], v=sel[k]||{}, ex=f.example!==undefined?f.example:(f.default!==undefined?f.default:'');
 const id=`cf_${name}`;
 if(k==='boolean'&&f.required) return `<label>${esc(name)} <span class="req">*</span></label><input type="checkbox" id="${esc(id)}" data-kind="boolean" style="width:auto" ${ex===true?'checked':''}>`;
 if(k==='boolean') return `<label>${esc(name)} <span class="mut">${ex===true||ex===false?'default '+ex:'optional'}</span></label><select id="${esc(id)}" data-kind="bool3"><option value="">— (not sent)</option><option value="true">true</option><option value="false">false</option></select>`;
 const opts=k==='select'&&v.options?v.options.map(o=>typeof o==='object'?{val:o.value,lab:o.label??o.value}:{val:o,lab:o}):null;
 if(opts&&v.multiple){ const pre=new Set([].concat(ex===''?[]:ex).map(String));  // a list, as the selector declares: checkboxes, and a custom entry when allowed
  const listed=new Set(opts.map(o=>String(o.val))), extra=[...pre].filter(x=>!listed.has(x));  // an example the options do not list is a custom value: it belongs in the custom box, not nowhere
  return `<label>${esc(name)}${f.required?' <span class="req">*</span>':''} <span class="mut">one or more</span></label><div id="${esc(id)}" data-kind="multi" class="multi">${opts.map(o=>`<label style="display:inline-flex;gap:4px;margin-right:10px"><input type="checkbox" style="width:auto" value="${esc(o.val)}" ${pre.has(String(o.val))?'checked':''}>${esc(o.lab)}</label>`).join('')}${v.custom_value?`<input type="text" data-custom="1" value="${esc(extra.join(', '))}" placeholder="other values, comma separated">`:''}</div>`; }
 if(opts&&v.custom_value) return `<label>${esc(name)}${f.required?' <span class="req">*</span>':''} <span class="mut">pick or type</span></label><input type="text" id="${esc(id)}" data-kind="text" list="${esc(id)}_list" value="${typeof ex==='string'||typeof ex==='number'?esc(ex):''}"><datalist id="${esc(id)}_list">${opts.map(o=>`<option value="${esc(o.val)}">${esc(o.lab)}</option>`).join('')}</datalist>`;
 if(k==='select'&&v.options) return `<label>${esc(name)}${f.required?' <span class="req">*</span>':''}</label><select id="${esc(id)}" data-kind="select"><option value="">—</option>${v.options.map(o=>{const val=typeof o==='object'?o.value:o, lab=typeof o==='object'?(o.label??o.value):o; return `<option value="${esc(val)}" ${String(ex)===String(val)?'selected':''}>${esc(lab)}</option>`}).join('')}</select>`;
 if(k==='number') return `<label>${esc(name)}${f.required?' <span class="req">*</span>':''} <span class="mut">${esc(v.unit_of_measurement||'')}</span></label><input type="number" id="${esc(id)}" data-kind="number" ${v.min!=null?'min="'+esc(v.min)+'"':''} ${v.max!=null?'max="'+esc(v.max)+'"':''} step="${esc(v.step??'any')}" value="${ex!==''&&typeof ex!=='object'?esc(ex):''}">`;
 if(k==='object'||typeof ex==='object'&&ex!==null) return `<label>${esc(name)}${f.required?' <span class="req">*</span>':''} <span class="mut">JSON</span></label><textarea id="${esc(id)}" data-kind="json" rows="3">${ex!==''&&ex!==null?esc(JSON.stringify(ex,null,1)):''}</textarea>`;
 return `<label>${esc(name)}${f.required?' <span class="req">*</span>':''} <span class="mut">${esc(selKind(sel))}</span></label><input type="text" id="${esc(id)}" data-kind="text" value="${typeof ex==='string'||typeof ex==='number'?esc(ex):''}" placeholder="${esc(f.description||'')}">`;}
function callForm(domain,s){const keys=Object.keys(s.fields||{});
 return `<div class="call"><div class="k">call <b>${esc(domain)}.${esc(s.name)}</b> from here (runs in this container's HA; the same call over MQTT goes to <code>&lt;base&gt;/call/${esc(domain)}/${esc(s.name)}</code>)</div>
  ${s.target?`<label>target entity_id(s) <span class="mut">comma separated${s.target.entity&&[].concat(s.target.entity).some(t=>t.domain)?' · '+[].concat(s.target.entity).map(t=>[].concat(t.domain||[]).join('/')).join(', '):''}</span></label><input type="text" id="ct_entity" placeholder="climate.x, sensor.y">`:''}
  <div class="grid">${keys.map(k=>`<div>${fieldInput(k,s.fields[k])}</div>`).join('')}</div>
  <label>extra service data <span class="mut">JSON, merged over the fields above (for fields the catalog does not list)</span></label><textarea id="ct_extra" rows="2" placeholder="{}"></textarea>
  <div class="row" style="margin-top:8px"><button class="primary" id="ct_go">Call service</button><span id="ct_msg" class="mut"></span></div><pre id="ct_out"></pre></div>`;}
function wireCall(x,domain,s){const go=x.querySelector('#ct_go'); if(!go) return;
 go.onclick=async()=>{const data={}; let bad=''; const badJson=new Set();
  for(const [k,f] of Object.entries(s.fields||{})){const el=x.querySelector(`#cf_${CSS.escape(k)}`); if(!el) continue; const kind=el.dataset.kind;
   if(kind==='boolean'){ data[k]=el.checked; continue; }
   if(kind==='bool3'){ if(el.value!=='') data[k]=el.value==='true'; continue; }
   if(kind==='multi'){ const vals=[...el.querySelectorAll('input[type=checkbox]:checked')].map(c=>c.value);
    const custom=el.querySelector('input[data-custom]'); if(custom) vals.push(...custom.value.split(',').map(t=>t.trim()).filter(Boolean));
    if(vals.length) data[k]=vals; continue; }  // nothing picked: not sent (a required one is reported below)
   const v=el.value; if(v===''||v==null){ if(f.required&&kind==='text') data[k]=''; continue; }  // a required text field may be cleared on purpose
   if(kind==='number') data[k]=Number(v); else if(kind==='json'){ try{data[k]=JSON.parse(v);}catch(e){bad+=`${k}: invalid JSON. `; badJson.add(k);} } else data[k]=v; }
  const extraEl=x.querySelector('#ct_extra'); if(extraEl&&extraEl.value.trim()){ try{const extra=JSON.parse(extraEl.value); if(extra===null||typeof extra!=='object'||Array.isArray(extra)) throw new Error(); Object.assign(data,extra);}catch(e){bad+='extra data: invalid JSON (an object). ';} }
  // checked against the final data: a value given only in the extra JSON counts
  for(const [k,f] of Object.entries(s.fields||{})) if(f.required&&!(k in data)&&!badJson.has(k)) bad+=`${k} is required. `;
  const msg=x.querySelector('#ct_msg'), out=x.querySelector('#ct_out'); if(bad){msg.innerHTML='<span class="bad">'+esc(bad)+'</span>';return;}
  const body={domain,service:s.name,data}; const tEl=x.querySelector('#ct_entity'); if(tEl&&tEl.value.trim()) body.target={entity_id:tEl.value.split(',').map(t=>t.trim()).filter(Boolean)};
  if(!confirm(`Call ${domain}.${s.name} now with ${JSON.stringify(body.data)}${body.target?' on '+body.target.entity_id.join(', '):''}?`)) return;
  go.disabled=true; msg.textContent='calling…'; out.textContent='';
  try{const r=await fetch('/api/services/call',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)}); const j=await r.json();
   msg.innerHTML=j.ok?`<span class="ok">ok</span> in ${j.ms} ms${j.response!==undefined?' · response below':''}`:'<span class="bad">'+esc(j.error)+'</span>'; if(j.ok&&j.response!==undefined) out.textContent=JSON.stringify(j.response,null,1);}
  catch(e){msg.innerHTML='<span class="bad">'+esc(e.message)+'</span>';} finally{go.disabled=false;} };}
function chips(){
 chipBar('#domains',Object.fromEntries(data.map(d=>[d.domain,d.services.length])),domOn,()=>{chips();render()});
}
async function load(){try{const r=await fetch('/api/services');data=await r.json();$('#ts').textContent=new Date().toLocaleTimeString();chips();render()}
 catch(e){$('#ts').textContent='error: '+e}}
['#q','#onlyCustom'].forEach(s=>$(s).addEventListener('input',render));
load();
