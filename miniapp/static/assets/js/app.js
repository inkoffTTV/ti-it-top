(() => {
  'use strict';
  const tg = window.Telegram?.WebApp;
  tg?.ready?.(); tg?.expand?.();
  const state = {
    tab:'home', data:null, cache:new Map(), adminSection:'dashboard',
    activitySection:'chat', appStatus:'PENDING', ticketStatus:'OPEN'
  };
  const content = document.getElementById('content');
  const nav = document.getElementById('nav');
  const token = tg?.initData || ((location.hostname==='localhost'||location.hostname==='127.0.0.1')?'dev:test':'');

  const esc = v => String(v ?? '').replace(/[&<>\"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[m]));
  const initials = v => esc(String(v||'?').trim().slice(0,2).toUpperCase());
  const toast = s => { const e=document.getElementById('toast'); e.textContent=s; e.classList.add('show'); clearTimeout(window.__toast); window.__toast=setTimeout(()=>e.classList.remove('show'),2200); };
  const loading = t => `<div class="loading"><div class="spinner"></div><div>${esc(t||'Загрузка…')}</div></div>`;
  const errorBox = e => `<div class="error"><b>Не удалось загрузить раздел</b><div style="margin-top:6px">${esc(e?.message||e)}</div><button class="btn small" data-retry style="margin-top:10px">Повторить</button></div>`;

  async function api(path, opts={}) {
    const cacheKey = opts.method === 'POST' || opts.method === 'DELETE' ? null : path;
    if(cacheKey && state.cache.has(cacheKey)) return state.cache.get(cacheKey);
    const headers = {'X-Telegram-Init-Data':token, ...(opts.headers||{})};
    if(opts.body && typeof opts.body !== 'string' && !(opts.body instanceof FormData)){
      headers['Content-Type']='application/json'; opts={...opts,body:JSON.stringify(opts.body)};
    }
    const controller = new AbortController();
    const timer=setTimeout(()=>controller.abort(), opts.timeout ?? 7000);
    try{
      const r=await fetch(path,{...opts,headers,signal:controller.signal});
      let body=null; try{body=await r.json()}catch{}
      if(!r.ok) throw new Error(body?.detail || body?.error || `HTTP ${r.status}`);
      if(cacheKey) state.cache.set(cacheKey,body);
      return body;
    }catch(e){
      if(e.name==='AbortError') throw new Error('Сервер не ответил вовремя.');
      throw e;
    }finally{clearTimeout(timer)}
  }
  function clearCache(){state.cache.clear()}

  // Initial payload is intentionally limited to identity/auth context.
  // Existing backend remains the source of truth.
  async function loadIdentity(){
    try {
      return await api('/api/me',{timeout:4000});
    } catch {
      // Compatibility fallback for the current backend during migration.
      const b=await api('/api/bootstrap',{timeout:5000});
      return {
        telegram_id:b.user?.telegram_id, username:b.user?.username, first_name:b.user?.first_name,
        last_name:b.user?.last_name, is_admin:!!b.is_admin, profile:b.profile, team:b.team,
        tournaments:b.tournaments, applications:b.applications, invites:b.invites,
        unread_notifications:b.unread_notifications
      };
    }
  }

  function setNav(){
    const admin=!!state.data?.is_admin;
    nav.querySelector('[data-tab="admin"]')?.classList.toggle('hidden',!admin);
    nav.querySelectorAll('button').forEach(b=>b.classList.toggle('active',b.dataset.tab===state.tab));
  }

  function profileView(d){
    const p=d?.profile;
    return `<section class="hero"><div class="eyebrow">PROFILE</div><h1>${esc(d?.first_name||d?.username||'Игрок')}</h1><p>Профиль и подтверждённые игровые данные.</p></section>
      <section class="card" style="margin-top:10px"><div class="section-head"><h3>Профиль игрока</h3>${p?.mmr_verified?'<span class="pill ok">MMR подтверждён</span>':'<span class="pill">Не подтверждён</span>'}</div>
      <div class="grid"><div class="item"><div class="muted">MMR</div><b>${esc(p?.exact_mmr ?? p?.mmr ?? '—')}</b></div>
      <div class="item"><div class="muted">Ранг</div><b>${esc(p?.rank_name || p?.rank || 'Не определён')}</b></div></div>
      <div class="stack"><div class="row"><span class="muted">Steam</span><span>${esc(p?.steam_id64||'—')}</span></div><div class="row"><span class="muted">Источник</span><span>${esc(p?.verification_source||p?.source||'—')}</span></div></div></section>`;
  }

  function homeView(d){
    const p=d?.profile,t=d?.team;
    return `<section class="hero"><div class="eyebrow">TI:IT TOP · DOTA 2</div><h1>Привет, ${esc(d?.first_name||d?.username||'игрок')}.</h1><p>Турнирный центр без тяжёлой предварительной загрузки.</p>
      <div class="toolbar"><button class="btn primary" data-go="profile">Мой профиль</button><button class="btn" data-go="tournaments">Турниры</button></div></section>
      <div class="grid"><div class="card stat"><span class="muted">Точный MMR</span><b>${esc(p?.exact_mmr??'—')}</b></div><div class="card stat"><span class="muted">Команда</span><b>${esc(t?.name||'—')}</b></div></div>`;
  }

  async function loadTeam(root){
    const d=await api('/api/bootstrap',{timeout:5000});
    // Bootstrap is used only for compatibility here and is requested only after Team is opened.
    state.cache.set('/api/team-data',d?.team||null);
    const t=d?.team;
    root.innerHTML=`<section class="hero"><div class="eyebrow">TEAM</div><h1>${esc(t?.name||'Моя команда')}</h1><p>${t?esc(`${t.roster_count||0}/${t.max_roster||6} игроков`):'Команда не создана.'}</p></section>
      ${t?`<section class="card" style="margin-top:10px"><div class="section-head"><h3>Состав</h3><span class="pill">${esc(t.role_mode||'PLAYER_CHOICE')}</span></div><div class="list">${(t.roster||[]).map(m=>`<div class="item row"><div class="grow"><b>${esc(m.name)}</b><div class="sub">${esc(m.role||'PLAYER')} · pos ${esc(m.position??'—')}</div></div><span class="pill">${esc(m.exact_mmr??m.mmr??'—')} MMR</span></div>`).join('')||'<div class="empty">Состав пуст.</div>'}</div></section>`:'<section class="card" style="margin-top:10px"><button class="btn primary" data-create-team>Создать команду</button></section>'}`;
    bind();
  }

  async function loadPlayers(root){
    const r=await api('/api/players',{timeout:6000});
    const items=Array.isArray(r?.items)?r.items:[];
    root.innerHTML=`<section class="card"><div class="section-head"><h3>Игроки</h3></div><div class="toolbar"><input id="playerSearch" class="input" placeholder="Поиск игрока"><button class="btn small" data-search>Найти</button></div><div class="list" id="playerList" style="margin-top:10px">${items.map(p=>`<div class="item row"><div class="avatar">${initials(p.name)}</div><div class="grow"><b>${esc(p.name)}</b><div class="sub">${esc(p.rank||'Ранг не определён')} · ${esc(p.mmr??'—')} MMR</div><div class="sub">${Array.isArray(p.positions)&&p.positions.length?p.positions.map(x=>'pos '+x).join(' · '):'позиции не указаны'}</div></div></div>`).join('')||'<div class="empty">Игроков нет.</div>'}</div></section>`;
    bind();
  }

  async function loadTournaments(root){
    const d=state.data;
    let tours=d?.tournaments;
    let apps=d?.applications;
    if(!Array.isArray(tours)){
      const b=await api('/api/bootstrap',{timeout:5000}); tours=b?.tournaments||[]; apps=b?.applications||[];
    }
    root.innerHTML=`<section class="hero"><div class="eyebrow">TOURNAMENTS</div><h1>Турниры</h1><p>Регистрация и статусы заявок.</p></section><section class="card" style="margin-top:10px"><div class="list">${(tours||[]).map(t=>`<div class="item"><div class="row"><div class="grow"><b>${esc(t.name)}</b><div class="sub">${esc(t.status)} · ${esc(t.slots_left??'—')}/${esc(t.max_teams??'—')} мест</div></div>${t.registration_open?'<span class="pill ok">Регистрация</span>':'<span class="pill">Закрыта</span>'}</div>${t.description?`<div class="sub" style="margin-top:6px">${esc(t.description)}</div>`:''}</div>`).join('')||'<div class="empty">Турниров пока нет.</div>'}</div></section>`;
  }

  async function loadActivity(root){
    const r=state.activitySection==='chat'
      ? await api('/api/chat/messages',{timeout:5000})
      : state.activitySection==='events' ? await api('/api/events',{timeout:5000})
      : await api('/api/notifications',{timeout:5000});
    let items=r?.items||[];
    let title=state.activitySection==='chat'?'💬 Общий чат':state.activitySection==='events'?'📰 События':'🔔 Уведомления';
    root.innerHTML=`<section class="card"><div class="section-head"><h3>${title}</h3></div><div class="list">${
      items.map(x=>`<div class="item"><b>${esc(x.name||x.title||x.user||'')}</b><div style="margin-top:4px">${esc(x.text||x.body||x.message||'')}</div><div class="sub" style="margin-top:4px">${esc(x.created_at||'')}</div></div>`).join('') ||
      '<div class="empty">Пока ничего нет.</div>'}</div></section>`;
  }

  function adminView(){
    const sections=[['dashboard','⌂','Обзор'],['users','👥','Пользователи'],['teams','👑','Команды'],['tournaments','🏆','Турниры'],['applications','📋','Заявки'],['verification','🔎','Проверка'],['support','💬','Поддержка'],['broadcast','📢','Рассылка'],['stats','📊','Статистика'],['diagnostics','🌐','Диагностика'],['admins','🛡','Администраторы'],['ai','🤖','AI']];
    return `<section class="hero"><div class="eyebrow">CONTROL CENTER</div><h1>Админ-панель</h1><p>Каждый инструмент грузится отдельно.</p></section><div class="admin-grid">${sections.map(([id,ic,t])=>`<button class="admin-tile ${state.adminSection===id?'active':''}" data-admin="${id}"><span>${ic}</span><b>${t}</b></button>`).join('')}</div><div id="adminRoot">${loading('Загрузка…')}</div>`;
  }

  async function loadAdmin(root){
    const sec=state.adminSection;
    try{
      if(sec==='dashboard'){
        const s=await api('/api/admin/stats',{timeout:5000});
        root.innerHTML=`<section class="card"><div class="section-head"><h3>Обзор</h3></div><div class="grid"><div class="item"><span class="muted">Пользователи</span><b>${esc(s?.users??s?.total_users??'—')}</b></div><div class="item"><span class="muted">Команды</span><b>${esc(s?.teams??'—')}</b></div><div class="item"><span class="muted">Турниры</span><b>${esc(s?.tournaments??'—')}</b></div><div class="item"><span class="muted">Проверено</span><b>${esc(s?.verified_players??'—')}</b></div></div></section>`;
      } else if(sec==='users'){
        const r=await api('/api/admin/users?page=1',{timeout:6000});
        root.innerHTML=`<section class="card"><div class="section-head"><h3>👥 Пользователи</h3></div><div class="list">${(r?.items||[]).map(u=>`<div class="item row"><div class="grow"><b>${esc(u.name)}</b><div class="sub">${u.username?'@'+esc(u.username)+' · ':''}TG ${esc(u.telegram_id)}</div></div><span class="pill">${esc(u.mmr??'—')} MMR</span></div>`).join('')||'<div class="empty">Пусто.</div>'}</div></section>`;
      } else if(sec==='teams'){
        const r=await api('/api/admin/teams',{timeout:6000});
        root.innerHTML=`<section class="card"><div class="section-head"><h3>👑 Команды</h3></div><div class="list">${(r?.items||[]).map(t=>`<div class="item"><b>${esc(t.name)}</b><div class="sub">${esc(t.members)}/6 · капитан ${esc(t.captain||'—')}</div></div>`).join('')||'<div class="empty">Пусто.</div>'}</div></section>`;
      } else if(sec==='applications'){
        const r=await api('/api/admin/applications?status='+encodeURIComponent(state.appStatus),{timeout:6000});
        root.innerHTML=`<section class="card"><div class="section-head"><h3>📋 Заявки</h3></div><div class="list">${(r?.items||[]).map(a=>`<div class="item"><div class="row"><div class="grow"><b>#${esc(a.id)} · ${esc(a.team)}</b><div class="sub">${esc(a.tournament)} · ${esc(a.captain)}</div></div><span class="pill">${esc(a.status)}</span></div></div>`).join('')||'<div class="empty">Заявок нет.</div>'}</div></section>`;
      } else if(sec==='support'){
        const r=await api('/api/admin/support?status='+encodeURIComponent(state.ticketStatus),{timeout:6000});
        root.innerHTML=`<section class="card"><div class="section-head"><h3>💬 Поддержка</h3></div><div class="list">${(r?.items||[]).map(t=>`<div class="item"><b>#${esc(t.id)} · ${esc(t.user)}</b><div class="sub">${esc(t.message)}</div>${t.admin_reply?`<div class="notice" style="margin-top:6px">${esc(t.admin_reply)}</div>`:''}</div>`).join('')||'<div class="empty">Обращений нет.</div>'}</div></section>`;
      } else if(sec==='stats'){
        const s=await api('/api/admin/stats',{timeout:5000});
        root.innerHTML=`<section class="card"><div class="section-head"><h3>📊 Статистика</h3></div><div class="grid">${Object.entries(s||{}).map(([k,v])=>`<div class="item"><span class="muted">${esc(k)}</span><b>${esc(v)}</b></div>`).join('')}</div></section>`;
      } else if(sec==='diagnostics'){
        const r=await api('/api/admin/diagnostics',{timeout:10000});
        root.innerHTML=`<section class="card"><div class="section-head"><h3>🌐 Диагностика</h3></div><pre style="white-space:pre-wrap;word-break:break-word">${esc(r?.text||'Пусто')}</pre></section>`;
      } else if(sec==='admins'){
        const r=await api('/api/admin/admins',{timeout:5000});
        root.innerHTML=`<section class="card"><div class="section-head"><h3>🛡 Администраторы</h3></div><div class="list">${(r?.items||[]).map(a=>`<div class="item"><b>${esc(a.name)}</b><div class="sub">${esc(a.telegram_id)}</div></div>`).join('')||'<div class="empty">Пусто.</div>'}</div></section>`;
      } else if(sec==='ai'){
        let s=null; try{s=await api('/api/admin/ai/status',{timeout:3000})}catch{}
        root.innerHTML=`<section class="card"><div class="section-head"><h3>🤖 AI</h3><span class="pill ${s?.configured?'ok':'bad'}">${s?.configured?'Готов':'Не настроен'}</span></div><textarea id="aiInput" class="input" placeholder="Вопрос по данным TI:IT TOP"></textarea><button class="btn primary" data-ai-send style="margin-top:10px">Спросить AI</button><div id="aiAnswer" style="margin-top:10px"></div></section>`;
      } else {
        root.innerHTML=`<section class="card"><div class="section-head"><h3>${esc(sec)}</h3></div><div class="notice">Раздел подготовлен под отдельный API-запрос. Тяжёлый общий обзор больше не вызывается автоматически.</div></section>`;
      }
    }catch(e){root.innerHTML=errorBox(e)}
    bind();
  }

  async function render(){
    setNav();
    if(state.tab==='home') content.innerHTML=homeView(state.data);
    else if(state.tab==='profile') content.innerHTML=profileView(state.data);
    else if(state.tab==='admin'){content.innerHTML=adminView();await loadAdmin(document.getElementById('adminRoot'));return}
    else {
      content.innerHTML=loading('Открываем раздел…');
      try{
        if(state.tab==='team') await loadTeam(content);
        else if(state.tab==='players') await loadPlayers(content);
        else if(state.tab==='tournaments') await loadTournaments(content);
        else if(state.tab==='activity'){
          content.innerHTML=`<section class="hero"><div class="eyebrow">COMMUNITY</div><h1>💬 Активность</h1><div class="toolbar"><button class="btn small ${state.activitySection==='chat'?'primary':''}" data-activity="chat">Чат</button><button class="btn small ${state.activitySection==='events'?'primary':''}" data-activity="events">События</button><button class="btn small ${state.activitySection==='notifications'?'primary':''}" data-activity="notifications">Уведомления</button></div><div id="activityRoot">${loading('Загружаем…')}</div></section>`;
          await loadActivity(document.getElementById('activityRoot'));
        }
      }catch(e){content.innerHTML=errorBox(e)}
    }
    bind();
  }

  function bind(){
    document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=async()=>{state.tab=b.dataset.tab;await render()});
    document.getElementById('refresh')?.addEventListener('click',async()=>{clearCache();state.data=await loadIdentity();await render()});
    document.querySelectorAll('[data-go]').forEach(b=>b.onclick=async()=>{state.tab=b.dataset.go;await render()});
    document.querySelectorAll('[data-admin]').forEach(b=>b.onclick=async()=>{state.adminSection=b.dataset.admin;await loadAdmin(document.getElementById('adminRoot'))});
    document.querySelectorAll('[data-activity]').forEach(b=>b.onclick=async()=>{state.activitySection=b.dataset.activity;await loadActivity(document.getElementById('activityRoot'))});
    document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=()=>render());
    document.querySelector('[data-search]')?.addEventListener('click',async()=>{
      const q=document.getElementById('playerSearch')?.value.trim()||'';
      state.cache.delete('/api/players'); const r=await api('/api/players?q='+encodeURIComponent(q),{timeout:6000});
      const box=document.getElementById('playerList'); if(!box)return;
      box.innerHTML=(r.items||[]).map(p=>`<div class="item row"><div class="grow"><b>${esc(p.name)}</b><div class="sub">${esc(p.rank||'Ранг не определён')} · ${esc(p.mmr??'—')} MMR</div></div></div>`).join('')||'<div class="empty">Никого не найдено.</div>';
    });
    document.querySelector('[data-ai-send]')?.addEventListener('click',async()=>{
      const q=document.getElementById('aiInput')?.value.trim(); if(!q)return;
      const out=document.getElementById('aiAnswer'); out.innerHTML=loading('AI думает…');
      try{const r=await api('/api/admin/ai/ask',{method:'POST',timeout:15000,body:{question:q,history:[]}});out.innerHTML=`<div class="notice" style="white-space:pre-wrap">${esc(r?.answer||'Пустой ответ')}</div>`}
      catch(e){out.innerHTML=errorBox(e)}
    });
    document.querySelector('[data-create-team]')?.addEventListener('click',async()=>{
      const name=prompt('Название команды'); if(!name?.trim())return;
      try{await api('/api/team/create',{method:'POST',body:{name:name.trim()},timeout:7000});clearCache();state.data=await loadIdentity();toast('Команда создана');await render()}
      catch(e){toast(e.message)}
    });
  }

  window.addEventListener('unhandledrejection',e=>toast(e.reason?.message||e.reason||'Ошибка'));
  window.addEventListener('error',e=>toast(e.error?.message||e.message||'Ошибка'));

  (async()=>{
    content.innerHTML=loading('Открываем TI:IT TOP…');
    try { state.data=await loadIdentity(); setNav(); await render(); }
    catch(e){content.innerHTML=errorBox(e)}
  })();
})();