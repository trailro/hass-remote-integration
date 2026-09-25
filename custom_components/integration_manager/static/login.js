// the login form (a static file: the Content-Security-Policy allows no inline script)
document.getElementById('f').onsubmit=async e=>{
  e.preventDefault();
  const err=document.getElementById('err'); err.textContent='';
  let r;
  try{ r=await fetch('api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})}); }
  catch(x){ err.textContent='no answer from the server'; return; }
  const d=await r.json().catch(()=>({}));
  if(!d.ok){ err.textContent=d.error||('login failed ('+r.status+')'); return; }
  // `next` is relative to this page (it sits next to the others): the app's port and an ingress prefix alike
  let dest='./';
  try{ const u=new URL(new URLSearchParams(location.search).get('next')||'./',location.href); if(u.origin===location.origin) dest=u.href; }catch(e){}
  location.href=dest;  // only this server: the URL parser, not a prefix check, decides (tabs and backslashes are normalized away)
};
