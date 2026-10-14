const $ = (id) => document.getElementById(id);
const KEY = "palacewalk.step1", PKEY = "palacewalk.result";
const SECTIONS = ["start", "quiz", "loading", "result", "place", "auth", "notes"];
let Q = null, seq = [], skipTaste = false, idx = 0, answers = {}, last = null, rec = null;
let user = null, savedByPlace = new Map(), lastTaste = null, curPlaceId = null, pending = null, backTo = "start", authMode = "login";

const TAG_KO = {politics:"정치·권력",architecture:"건축·의례",people:"사람들의 삶",structure:"건물의 구조미",garden:"정원·자연",color:"단청·색채",stone:"석조·담장",eclectic:"동서양 혼합",view:"전망",water:"물",shade:"그늘",quiet:"고요",grand:"웅장",hidden:"아늑·은밀",open:"탁 트인"};
const WANT_KO = {comfort:"위로와 안정",refresh:"기분 전환",inspire:"영감",quiet:"고요"};
const COMP_KO = {alone:"혼자",couple:"연인·배우자",friends:"친구·가족",care:"보호가 필요한 동행"};
const STAIRS_KO = {none:"계단 거의 없음",few:"계단 조금",some:"계단 보통",many:"계단·오르막 있음"};

const store = {
  get(s, k){ try{ return JSON.parse(s.getItem(k)||"null"); }catch(e){ return null; } },
  set(s, k, v){ try{ s.setItem(k, JSON.stringify(v)); }catch(e){} },
  del(s, k){ try{ s.removeItem(k); }catch(e){} },
};
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  kids.flat().forEach(k => n.append(k));
  return n;
};
let current = "start";
function show(id, focusId){
  current = id;
  SECTIONS.forEach(s => $(s).hidden = s !== id);
  window.scrollTo(0, 0);
  if (focusId) $(focusId).focus({preventScroll: true});
}
function announce(msg){ $("status").textContent = msg; }
function setErr(id, msg){ $(id).hidden = !msg; $(id).textContent = msg || ""; }

async function api(path, body, method){
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 90000);
  try{
    const m = method || (body ? "POST" : "GET");
    const r = await fetch(path, {method: m, headers: body ? {"Content-Type":"application/json"} : {}, body: body ? JSON.stringify(body) : undefined, signal: ctl.signal});
    let d = null; try{ d = await r.json(); }catch(e){}
    if (!r.ok){ const err = new Error(d && typeof d.detail === "string" ? d.detail : "서버와 통신하지 못했어요. 잠시 후 다시 시도해 주세요."); err.status = r.status; throw err; }
    return d;
  }catch(e){
    if (e.name === "AbortError") throw new Error("응답이 너무 오래 걸려요. 잠시 후 다시 시도해 주세요.");
    if (e instanceof TypeError) throw new Error("네트워크 연결을 확인해 주세요.");
    throw e;
  }finally{ clearTimeout(timer); }
}

function setSeq(skip){
  skipTaste = !!skip;
  seq = skipTaste ? Q.questions.filter(q => q.section !== "taste") : Q.questions;
}
function answeredCount(){ return seq.filter(q => answers[q.id]).length; }

function validSaved(s){
  if (!s || typeof s.answers !== "object" || !s.answers) return null;
  const ok = {};
  for (const q of Q.questions){
    const a = s.answers[q.id];
    if (a && q.options.some(o => o.id === a)) ok[q.id] = a;
  }
  return Object.keys(ok).length ? {answers: ok, idx: Math.max(+s.idx || 0, 0), skip: !!s.skip} : null;
}
function saveProgress(){ store.set(localStorage, KEY, {answers, idx, skip: skipTaste}); }

/* ---------- 계정 상태 ---------- */
function renderTopbar(){
  $("btn-account").textContent = user ? user.nickname : "로그인";
  $("btn-account").setAttribute("aria-label", user ? `${user.nickname} 계정` : "로그인");
  $("btn-notes").hidden = !user;
  $("btn-last").hidden = !(user && lastTaste);
}
async function loadAccountData(){
  try{
    const m = await api("/api/me"); user = m.user;
  }catch(e){ user = null; }
  savedByPlace = new Map(); lastTaste = null;
  if (user){
    try{ (await api("/api/saved")).items.forEach(i => savedByPlace.set(i.place_id, i)); }catch(e){}
    try{ lastTaste = (await api("/api/assessments/last-answers")).answers; }catch(e){}
  }
  renderTopbar();
}

function openAuth(mode, hint){
  authMode = mode;
  const signup = mode === "signup";
  $("auth-h").textContent = signup ? "가입하기" : "로그인";
  $("auth-submit").textContent = signup ? "가입하기" : "로그인";
  $("auth-toggle").textContent = signup ? "이미 계정이 있나요? 로그인" : "계정이 없나요? 가입하기";
  $("nick-row").hidden = !signup; $("pw-hint").hidden = !signup;
  $("f-pw").setAttribute("autocomplete", signup ? "new-password" : "current-password");
  $("auth-hint").hidden = !hint; $("auth-hint").textContent = hint || "";
  setErr("auth-error", "");
  show("auth", "auth-h");
}
async function submitAuth(ev){
  ev.preventDefault();
  const email = $("f-email").value.trim(), pw = $("f-pw").value;
  if (!email || !pw){ setErr("auth-error", "이메일과 비밀번호를 입력해 주세요."); return; }
  if (authMode === "signup" && pw.length < 8){ setErr("auth-error", "비밀번호는 8자 이상이어야 해요."); return; }
  $("auth-submit").disabled = true; setErr("auth-error", "");
  try{
    const body = authMode === "signup" ? {email, password: pw, nickname: $("f-nick").value.trim() || null} : {email, password: pw};
    await api(authMode === "signup" ? "/api/auth/signup" : "/api/auth/login", body);
    $("f-pw").value = "";
    await loadAccountData();
    announce(`${user.nickname}님, 환영해요`);
    const p = pending; pending = null;
    if (p && p.type === "save" && rec){ showPlace(p.placeId); if (savedByPlace.has(p.placeId)) msg("이미 저장된 장소예요."); else await toggleSave(); }
    else if (p && p.back) show(p.back, null);
    else init(false);
  }catch(e){
    setErr("auth-error", e.status === 422 ? "이메일 형식과 비밀번호(8자 이상)를 확인해 주세요." : e.message);
  }finally{ $("auth-submit").disabled = false; }
}

/* ---------- 문항 ---------- */
async function init(reload = true){
  show("start");
  $("resume").hidden = true; setErr("load-error", "");
  const saved = store.get(sessionStorage, PKEY);
  if (!Q){
    try{ Q = await api("/api/questions"); }
    catch(e){
      setErr("load-error", "문항을 불러오지 못했어요. ");
      $("load-error").append(el("button", {textContent: "다시 시도", onclick: () => init()}));
      return;
    }
  }
  setSeq(false);
  if (reload) await loadAccountData(); else renderTopbar();
  $("btn-start").disabled = false;
  if (saved && saved.result && saved.result.profile && saved.answers){
    try{ answers = saved.answers; last = saved.result; rec = saved.rec || null; showResult(last, false); return; }
    catch(e){ store.del(sessionStorage, PKEY); answers = {}; last = null; show("start"); }
  }
  const sv = validSaved(store.get(localStorage, KEY));
  if (sv){
    $("resume").hidden = false;
    $("resume").textContent = `이전에 ${Object.keys(sv.answers).length}문항까지 답했어요. `;
    $("resume").append(el("button", {textContent: "이어서 하기", onclick: () => { answers = sv.answers; setSeq(sv.skip); idx = Math.min(sv.idx, seq.length - 1); render(true); }}));
  }
}

function render(focus){
  show("quiz");
  const q = seq[idx], n = seq.length;
  const sec = Q.sections.find(s => s.id === q.section);
  $("bar").style.width = (answeredCount() / n * 100) + "%";
  $("progress").setAttribute("aria-valuemax", n);
  $("progress").setAttribute("aria-valuenow", answeredCount());
  $("sec").textContent = `${sec.title}${q.category ? " · " + q.category : ""} — ${idx + 1}/${n}`;
  $("qtext").textContent = q.text;
  const box = $("opts"); box.textContent = "";
  q.options.forEach(o => {
    const inp = el("input", {type: "radio", name: "opt", value: o.id, checked: answers[q.id] === o.id});
    inp.onchange = () => { answers[q.id] = o.id; saveProgress(); $("btn-next").disabled = false; $("bar").style.width = (answeredCount() / n * 100) + "%"; $("progress").setAttribute("aria-valuenow", answeredCount()); };
    box.append(el("label", {className: "opt"}, inp, el("span", {textContent: o.text})));
  });
  setErr("quiz-error", "");
  $("btn-prev").disabled = idx === 0;
  $("btn-next").disabled = !answers[q.id];
  $("btn-next").textContent = idx === n - 1 ? "결과 보기" : "다음";
  if (focus !== false) $("qtext").focus({preventScroll: true});
}

function loading(text){ $("loading-text").textContent = text; announce(text); show("loading"); }

async function submit(){
  loading("오늘의 나를 정리하는 중… (최대 1분)");
  try{
    const d = await api("/api/assess", {answers});
    last = d; rec = null; store.set(sessionStorage, PKEY, {answers, result: d});
    store.del(localStorage, KEY);
    if (user && lastTaste !== undefined) api("/api/assessments/last-answers").then(r => { lastTaste = r.answers; renderTopbar(); }).catch(() => {});
    showResult(d, true);
  }catch(e){ render(false); setErr("quiz-error", e.message); }
}

function meter(label, v, max){
  const bar = el("b"); bar.style.width = (v / max * 100) + "%";
  return el("div", {className: "meter"}, el("span", {}, el("em", {textContent: label}), el("em", {textContent: `${v}/${max}`})), el("i", {}, bar));
}

function showResult(d, focus){
  const p = d.profile;
  $("summary").textContent = d.summary;
  $("notice").hidden = !d.notice; $("notice").textContent = d.notice || "";
  $("meters").replaceChildren(meter("마음 에너지", p.mind.energy, 3), meter("긴장·스트레스", p.mind.tension, 3), meter("몸의 피로", p.body.fatigue, 3));
  const tags = [`원하는 것: ${WANT_KO[p.mind.want]}`, `걸음 한도 약 ${p.body.max_steps.toLocaleString()}보`,
    p.body.stairs_ok ? "계단 가능" : "평지 선호", p.body.needs_rest ? "쉼터 필요" : "쉼터 선택", `동행: ${COMP_KO[p.taste.etc.companion]}`];
  ["history", "aesthetic", "mood"].forEach(c => p.taste[c].forEach(t => tags.push(TAG_KO[t] || t)));
  $("tags").replaceChildren(...tags.map(t => el("span", {className: "tag", textContent: t})));
  $("json").textContent = JSON.stringify(d, null, 2);
  setErr("rec-error", "");
  show("result", focus ? "result-h" : null);
  if (focus) announce("진단 결과가 나왔어요");
}

/* ---------- 장소 카드 ---------- */
function photo(place, big){
  const f = el("figure", {className: "photo"});
  const ph = () => el("div", {className: "ph", textContent: `${place.palace} ${place.name}`});
  if (place.image && place.image.url){
    const img = el("img", {src: place.image.url, alt: place.image.alt || place.name, loading: big ? "eager" : "lazy", referrerPolicy: "no-referrer"});
    img.onerror = () => img.replaceWith(ph());
    f.append(img);
    const cap = el("figcaption", {}, `사진: ${[place.image.author || "작성자 미상", place.image.license].filter(Boolean).join(" · ")} · `);
    if (/^https:\/\/commons\.wikimedia\.org\//.test(place.image.page || "")){ const a = el("a", {href: place.image.page, target: "_blank", rel: "noopener noreferrer", textContent: "원본"}); a.setAttribute("aria-label", "원본 사진 페이지(새 탭)"); cap.append(a); }
    f.append(cap);
  } else f.append(ph());
  return f;
}

function placeCard(p, intro){
  const w = p.walk;
  const box = el("div", {}, photo(p, true),
    el("h3", {className: "place-name", textContent: p.name}), el("p", {className: "palace", textContent: p.palace}),
    intro && intro.why ? el("p", {className: "why", textContent: intro.why}) : "",
    el("p", {className: "facts", textContent: p.facts}));
  if (intro && intro.highlights){
    box.append(el("strong", {textContent: "감상 포인트"}), el("ul", {className: "hl"}, intro.highlights.map(h => el("li", {textContent: h}))));
  }
  box.append(el("p", {className: "walk", textContent: `산책 정보(추정): 왕복 약 ${(w.steps_from_entrance * 2).toLocaleString()}보 · ${w.duration_min}분 · ${STAIRS_KO[w.stairs]} · 쉼터 ${w.rest_spots ? "있음" : "적음"}`}));
  if (p.access_note) box.append(el("p", {className: "walk", textContent: "유의: " + p.access_note}));
  if (intro && intro.tip) box.append(el("p", {className: "muted", textContent: "팁: " + intro.tip}));
  return box;
}

function renderSaveBtn(){
  const saved = savedByPlace.has(curPlaceId);
  $("btn-save").textContent = saved ? "♥ 저장됨 (누르면 해제)" : "♡ 이 장소 저장";
  $("btn-save").setAttribute("aria-pressed", saved);
}

function showPlace(viewId){
  const all = [rec.primary, ...rec.alternatives];
  const cur = all.find(x => x.id === viewId) || rec.primary;
  curPlaceId = cur.id;
  const intro = cur.id === rec.primary.id ? rec.primary : {why: cur.one_line};
  $("place-card").replaceChildren(placeCard(cur, intro));
  $("alts").replaceChildren(...all.filter(x => x.id !== cur.id).map(a =>
    el("button", {className: "alt", onclick: () => showPlace(a.id)}, a.name + " ", el("small", {textContent: `${a.palace} · ${a.one_line || a.feel}`}))));
  $("rec-notice").hidden = !rec.notice; $("rec-notice").textContent = rec.notice || "";
  $("save-msg").hidden = true;
  renderSaveBtn();
  show("place", "place-h");
  announce(`${cur.palace} ${cur.name}을(를) 보고 있어요`);
}

async function recommend(){
  if (rec){ showPlace(); return; }  // 이미 받은 추천은 다시 요청하지 않는다(무료 모델 쿼터 보호)
  loading("어울리는 장소를 찾는 중… (최대 1분)");
  try{ rec = await api("/api/recommend", {answers}); store.set(sessionStorage, PKEY, {answers, result: last, rec}); showPlace(); }
  catch(e){ showResult(last, false); setErr("rec-error", e.message); }
}

function msg(text){ $("save-msg").hidden = false; $("save-msg").textContent = text; announce(text); }

async function toggleSave(){
  if (!user){
    pending = {type: "save", placeId: curPlaceId};
    openAuth("login", "저장하려면 로그인이 필요해요. 진단과 추천 결과는 그대로 유지돼요.");
    return;
  }
  const all = [rec.primary, ...rec.alternatives];
  const cur = all.find(x => x.id === curPlaceId);
  $("btn-save").disabled = true;
  try{
    const item = savedByPlace.get(curPlaceId);
    if (item){ await api(`/api/saved/${item.id}`, null, "DELETE"); savedByPlace.delete(curPlaceId); msg("저장을 해제했어요."); }
    else{
      const why = cur.id === rec.primary.id ? (rec.primary.why || "") : (cur.one_line || "");
      const r = await api("/api/saved", {place_id: curPlaceId, why, answers, summary: (last && last.summary) || ""});
      savedByPlace.set(curPlaceId, r.item); msg(r.created ? "내 산책 노트에 저장했어요." : "이미 저장된 장소예요.");
    }
    renderSaveBtn();
  }catch(e){
    if (e.status === 401){ user = null; renderTopbar(); pending = {type: "save", placeId: curPlaceId}; openAuth("login", "로그인이 만료됐어요. 다시 로그인해 주세요."); }
    else msg(e.message);
  }finally{ $("btn-save").disabled = false; }
}

/* ---------- 내 산책 노트 ---------- */
const fmtDate = (s) => (s || "").slice(0, 10);

function noteCard(it){
  const box = el("div", {className: "note"});
  if (it.place){
    box.append(photo(it.place, false), el("h3", {textContent: it.place.name}), el("p", {className: "palace", textContent: `${it.place.palace} · 저장 ${fmtDate(it.saved_at)}`}));
  } else {
    box.append(el("h3", {textContent: "정보가 업데이트된 장소"}), el("p", {className: "muted", textContent: "이 장소의 정보가 바뀌어 지금은 보여드릴 수 없어요. 저장 기록만 남아 있어요."}));
  }
  if (it.why) box.append(el("p", {className: "why", textContent: it.why}));
  if (it.place && it.place.access_note) box.append(el("p", {className: "muted", textContent: "유의: " + it.place.access_note}));
  const chk = el("input", {type: "checkbox", checked: it.visited});
  const lab = el("label", {className: "chk"}, chk, it.visited && it.visited_at ? `다녀왔어요 (${fmtDate(it.visited_at)})` : "다녀왔어요");
  const memo = el("textarea", {value: it.memo || "", maxLength: 500, rows: 3});
  memo.setAttribute("aria-label", `${it.place ? it.place.name : "저장한 장소"} 메모`);
  const status = el("span", {className: "muted", role: "status"});
  const patch = async (b) => {
    try{ const r = await api(`/api/saved/${it.id}`, b, "PATCH"); Object.assign(it, r.item); status.textContent = "저장했어요."; return true; }
    catch(e){ status.textContent = e.message; return false; }
  };
  chk.onchange = async () => { if (await patch({visited: chk.checked})) lab.lastChild.textContent = it.visited && it.visited_at ? `다녀왔어요 (${fmtDate(it.visited_at)})` : "다녀왔어요"; else chk.checked = !chk.checked; };
  const saveMemo = el("button", {textContent: "메모 저장", onclick: () => patch({memo: memo.value})});
  const del = el("button", {className: "danger", textContent: "저장 해제", onclick: async () => {
    if (!confirm("이 장소를 산책 노트에서 뺄까요?")) return;
    try{ await api(`/api/saved/${it.id}`, null, "DELETE"); savedByPlace.delete(it.place_id); await openNotes(false); }
    catch(e){ status.textContent = e.message; }
  }});
  box.append(lab, memo, el("div", {className: "row"}, saveMemo, del, status));
  return box;
}

async function openNotes(focus = true){
  if (!user){ pending = {back: current}; openAuth("login"); return; }
  if (current !== "notes") backTo = current;
  const palace = $("palace-filter").value;
  setErr("notes-error", "");
  show("notes", focus ? "notes-h" : null);
  $("who").textContent = `${user.nickname} · ${user.email}`;
  try{
    const d = await api("/api/saved" + (palace ? `?palace=${encodeURIComponent(palace)}` : ""));
    const sel = $("palace-filter");
    if (sel.options.length <= 1) d.palaces.forEach(p => sel.append(el("option", {value: p, textContent: p})));
    $("notes-list").replaceChildren(...d.items.map(noteCard));
    $("notes-empty").hidden = d.items.length > 0;
    const h = await api("/api/assessments");
    $("history-list").replaceChildren(...(h.items.length ? h.items.map(a => el("div", {className: "hist", textContent: `${fmtDate(a.created_at)} — ${a.summary || "요약 없음"}`})) : [el("p", {className: "muted", textContent: "아직 진단 이력이 없어요."})]));
  }catch(e){
    if (e.status === 401){ user = null; renderTopbar(); openAuth("login", "로그인이 만료됐어요."); }
    else setErr("notes-error", e.message);
  }
}

async function logout(){
  try{ await api("/api/auth/logout", {}); }catch(e){}
  user = null; savedByPlace = new Map(); lastTaste = null; renderTopbar(); announce("로그아웃했어요");
  reset();
}
async function deleteAccount(){
  if (!confirm("계정과 저장한 장소, 진단 이력이 모두 삭제돼요. 되돌릴 수 없어요. 계속할까요?")) return;
  const typed = prompt(`확인을 위해 이메일(${user.email})을 입력해 주세요.`);
  if (typed === null || typed.trim().toLowerCase() !== user.email.toLowerCase()){ setErr("notes-error", "이메일이 일치하지 않아 삭제하지 않았어요."); return; }
  try{ await api("/api/me", null, "DELETE"); user = null; savedByPlace = new Map(); lastTaste = null; renderTopbar(); announce("계정을 삭제했어요"); reset(); }
  catch(e){ setErr("notes-error", e.message); }
}

function reset(){ answers = {}; idx = 0; last = null; rec = null; curPlaceId = null; store.del(sessionStorage, PKEY); store.del(localStorage, KEY); init(false); }

$("btn-start").onclick = () => { if (!Q) return; if (Object.keys(answers).length === 0 && store.get(localStorage, KEY) && !confirm("저장된 진행 내용이 사라져요. 새로 시작할까요?")) return; answers = {}; idx = 0; setSeq(false); store.del(localStorage, KEY); render(); };
$("btn-last").onclick = () => { if (!Q || !lastTaste) return; answers = {...lastTaste}; idx = 0; setSeq(true); store.del(localStorage, KEY); render(); };
$("btn-prev").onclick = () => { if (idx > 0){ idx--; saveProgress(); render(); } };
$("btn-next").onclick = () => { if (idx < seq.length - 1){ idx++; saveProgress(); render(); } else submit(); };
$("btn-retry").onclick = reset;
$("btn-again").onclick = reset;
$("btn-back").onclick = () => showResult(last, true);
$("btn-recommend").onclick = recommend;
$("btn-save").onclick = toggleSave;
$("btn-account").onclick = () => { if (user) openNotes(); else { pending = {back: current === "auth" ? "start" : current}; openAuth("login"); } };
$("btn-notes").onclick = () => openNotes();
$("notes-back").onclick = () => show(backTo === "notes" || backTo === "auth" ? "start" : backTo, null);
$("palace-filter").onchange = () => openNotes(false);
$("btn-logout").onclick = logout;
$("btn-delete").onclick = deleteAccount;
$("auth-form").addEventListener("submit", submitAuth);
$("auth-toggle").onclick = () => openAuth(authMode === "login" ? "signup" : "login", $("auth-hint").hidden ? "" : $("auth-hint").textContent);
$("auth-cancel").onclick = () => { const p = pending; pending = null; if (p && p.type === "save" && rec) showPlace(p.placeId); else show(p && p.back && p.back !== "auth" ? p.back : "start", null); };
init();
