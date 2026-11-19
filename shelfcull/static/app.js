"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const gb = b => b >= 1099511627776 ? (b / 1099511627776).toFixed(2) + " TB" : (b / 1073741824).toFixed(1) + " GB";
const day = t => t ? new Date(t * 1000).toLocaleDateString(undefined, {year: "numeric", month: "short", day: "numeric"}) : "";
const safeUrl = u => /^(https?:|data:image\/)/i.test(u || "") ? u : "";

let defaultView = "all", items = [], byKey = {}, instances = [], dec = {}, queue = [], pos = 0, undo = [], skipped = new Set(), tab = "delete",
    serverState = {}, pollTimer = 0, current = "pick", shown = 0, hasPlex = true;

function banner(text, kind) { $("banner").innerHTML = text ? `<div class="msg ${kind || ""}">${esc(text)}</div>` : ""; }

async function api(action, body) {
  const r = await fetch("api/" + action, body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)} : {cache: "no-store"});
  let j = null;
  try { j = await r.json(); } catch (e) {}
  if (!r.ok || !j || !j.ok) throw new Error(j && j.error ? j.error : "The server did not save that (HTTP " + r.status + ")");
  serverState = j; dec = j.decisions || {};
  return j;
}

// A title is "decided" when it is marked Delete or Keep (or waiting to be marked seen). A failed Seen-it counts as undecided.
function mark(f) { const d = dec[f.k]; return d && !d.done && d.d !== "failed" ? d.d : ""; }
function undecided(f) { return !dec[f.k] || dec[f.k].d === "failed"; }
function playedByAnyone(f) { return f.n > 0 || (f.oth && f.oth.length > 0); }

function rebuild(keepKey) {
  const lib = $("fLib").value, play = hasPlex ? $("fPlay").value : "all", q = $("fSearch").value.trim().toLowerCase();
  const inLib = f => !lib || (lib === "kind:movie" ? f.kind === "movie" : lib === "kind:show" ? f.kind === "show" : f.src === lib);
  // A title search looks through EVERYTHING: also titles already marked Keep or Delete, whatever the played filter says,
  // so a title can always be found again to change the decision.
  queue = items.filter(f => inLib(f) && (q
    ? (f.t.toLowerCase().includes(q) || (f.ot || "").toLowerCase().includes(q))
    : undecided(f) && !skipped.has(f.k) && (play === "all" || (play === "watched" ? playedByAnyone(f) : play === "me" ? f.n === 0 : !playedByAnyone(f)))));
  const rate = f => f.ri || f.rx || 99;
  const cmp = {added: (a, b) => a.ad.localeCompare(b.ad), watched: (a, b) => (b.last || 0) - (a.last || 0), size: (a, b) => b.sz - a.sz,
               yearOld: (a, b) => a.y - b.y, yearNew: (a, b) => b.y - a.y, rating: (a, b) => rate(a) - rate(b), title: (a, b) => a.t.localeCompare(b.t)}[$("fSort").value];
  queue.sort((a, b) => cmp(a, b) || a.t.localeCompare(b.t));
  pos = 0;
  if (keepKey) { const i = queue.findIndex(f => f.k === keepKey); if (i >= 0) pos = i; }
  show();
}

function counts() {
  let n = 0, bytes = 0;
  for (const k in dec) if (dec[k].d === "delete" && !dec[k].done) { n++; if (byKey[k]) bytes += byKey[k].sz; }
  $("cnt").textContent = n ? `${n} (${gb(bytes)})` : "0";
  return {n, bytes};
}

function playedTag(f) {
  if (!hasPlex) return "";
  if (!f.inplex) return `<span class="tag">Not found in Plex</span>`;
  if (f.n > 0) return f.kind === "show"
    ? `<span class="tag ok">You watched ${f.n} of ${f.eps} episode${f.eps === 1 ? "" : "s"}${f.last ? ", last " + esc(day(f.last)) : ""}</span>`
    : `<span class="tag ok">You played it ${f.n}&times;${f.last ? ", last " + esc(day(f.last)) : ""}</span>`;
  if (f.oth && f.oth.length) return `<span class="tag ok">Played by ${esc(f.oth.join(", "))}</span>`;
  return `<span class="tag warn">Never played</span>`;
}

function show() {
  counts();
  const f = queue[pos];
  $("card").classList.toggle("hide", !f); $("done").classList.toggle("hide", !!f);
  $("progress").textContent = f ? ($("fSearch").value.trim() ? `${pos + 1} of ${queue.length.toLocaleString()} found (search looks through everything)` : `${pos + 1} of ${queue.length.toLocaleString()} left to decide`) : "";
  ["bDel", "bSkip", "bKeep"].forEach(b => $(b).disabled = !f);
  $("bSeen").disabled = !f || !f.inplex || f.full || mark(f) === "seen";     // already watched, or Plex does not have it: nothing to mark
  $("bPrev").disabled = !f || pos === 0; $("bNext").disabled = !f || pos >= queue.length - 1;
  if (!f) return;
  const hrs = f.rt ? (f.rt >= 60 ? Math.floor(f.rt / 60) + " h " : "") + (f.rt % 60) + " min" + (f.kind === "show" ? " per episode" : "") : "";
  const votes = f.rv ? " (" + (f.rv >= 1000 ? Math.round(f.rv / 1000) + "k" : f.rv) + " votes)" : "";
  const rating = f.ri ? `IMDb ${f.ri.toFixed(1)}${votes}` : (f.rx ? `Rating ${f.rx.toFixed(1)}${f.kind === "show" ? votes : ""}` : "");
  const md = mark(f), failed = dec[f.k] && dec[f.k].d === "failed";
  const markTag = md ? `<span class="tag ${md === "delete" ? "warn" : "ok"}">${md === "seen" ? "You marked this Seen it - Plex is updated within a minute" : "You marked this " + (md === "delete" ? "Delete" : "Keep")}</span>`
    : failed ? `<span class="tag warn">Could not mark it seen: ${esc(dec[f.k].error || "unknown error")}</span>` : "";
  const tags = [markTag, playedTag(f), `<span class="tag">${f.kind === "show" ? "TV show" : "Movie"}</span>`].concat(f.g.map(g => `<span class="tag">${esc(g)}</span>`)).join("");
  const links = [f.yt ? `<a href="https://www.youtube.com/watch?v=${encodeURIComponent(f.yt)}" target="_blank" rel="noopener noreferrer">Watch trailer</a>` : "",
                 f.imdb ? `<a href="https://www.imdb.com/title/${encodeURIComponent(f.imdb)}/" target="_blank" rel="noopener noreferrer">IMDb</a>` : "",
                 f.kind === "movie" ? `<a href="https://www.themoviedb.org/movie/${encodeURIComponent(f.tmdb)}" target="_blank" rel="noopener noreferrer">More about it</a>`
                                    : `<a href="https://www.thetvdb.com/dereferrer/series/${encodeURIComponent(f.tvdb)}" target="_blank" rel="noopener noreferrer">More about it</a>`].join("");
  const sub = [f.y || "", f.cert, hrs, rating, f.lang && f.lang !== "English" ? f.lang : ""].filter(Boolean).map(esc).join(" &middot; ");
  $("card").innerHTML = `
    <div class="poster" id="poster">${f.p ? "" : "No poster"}</div>
    <div>
      <p class="title">${esc(f.t)}</p>
      ${f.ot ? `<p class="sub">Original title: ${esc(f.ot)}</p>` : ""}
      <p class="sub">${sub}</p>
      <div class="tags">${tags}</div>
      <p class="plot">${esc(f.ov || "No description available.")}</p>
      ${f.kind === "show" && f.neps ? `<div class="whole"><b>Delete removes the whole show: ${f.neps} episode${f.neps === 1 ? "" : "s"} in ${f.nseas} season${f.nseas === 1 ? "" : "s"}, ${gb(f.sz)}</b>` +
        `<ul>${(f.seas || []).map(x => `<li>${x[0] === 0 ? "Specials" : "Season " + x[0]}: ${x[1]} episode${x[1] === 1 ? "" : "s"} &middot; ${gb(x[2])}</li>`).join("")}</ul></div>` : ""}
      <p class="facts">${esc(f.srcn)} &middot; ${esc(f.q)} &middot; ${gb(f.sz)} &middot; in your library since ${esc(f.ad)}${f.st ? " &middot; " + esc(f.st) : ""}${f.also && f.also.length ? "<br>Also in " + esc(f.also.join(", ")) + ". Delete removes it there too." : ""}</p>
      <p class="links">${links}</p>
    </div>`;
  const src = safeUrl(f.p);
  if (src) { const img = new Image(); img.onload = () => { if (queue[pos] === f) $("poster").style.backgroundImage = `url("${src.replace(/"/g, "%22")}")`; }; img.onerror = () => { if (queue[pos] === f) $("poster").textContent = "No poster"; }; img.src = src; }
  const nx = queue[pos + 1]; if (nx && safeUrl(nx.p)) new Image().src = nx.p;
  window.scrollTo({top: 0});
}

// Look through the list without deciding anything: Previous / Next, the arrow keys, or a swipe on the card.
function step(n) {
  const to = pos + n;
  if (to < 0 || to >= queue.length) return;
  pos = to; banner(""); show();
}

// The poster wall: the same titles as the card view (same filters and order), 120 at a time; tap one to open its card.
function tileHtml(f, i) {
  const md = mark(f);
  const dot = md === "delete" ? `<i class="dot del" title="marked Delete"></i>` : (f.full || md === "seen") ? `<i class="dot ok" title="seen"></i>` : "";
  return `<button class="tile" data-i="${i}">${safeUrl(f.p) ? `<img loading="lazy" src="${esc(f.p)}" alt="">` : `<div class="noimg"></div>`}
    <div class="cap"><b>${dot}${esc(f.t)}</b><span>${f.y || ""} &middot; ${esc(f.srcn)} &middot; ${gb(f.sz)}</span></div></button>`;
}
function moreTiles() {
  const end = Math.min(shown + 120, queue.length); let h = "";
  for (let i = shown; i < end; i++) h += tileHtml(queue[i], i);
  $("wall").insertAdjacentHTML("beforeend", h); shown = end;
  $("more").innerHTML = shown < queue.length ? `<button class="big quiet" id="moreBtn">Show more (${(queue.length - shown).toLocaleString()} left)</button>` : "";
  if ($("moreBtn")) $("moreBtn").onclick = moreTiles;
}
function renderGrid() {
  $("wall").innerHTML = ""; shown = 0;
  $("gridInfo").textContent = queue.length ? `${queue.length.toLocaleString()} titles with these filters. Tap one to open it.` : "Nothing found with these filters.";
  moreTiles();
}

let busy = false;
async function decide(d) {
  const f = queue[pos]; if (!f || busy) return;
  if (d === "seen" && $("bSeen").disabled) return;
  if (d === "skip") { skipped.add(f.k); undo.push({k: f.k, was: "skip"}); queue.splice(pos, 1); if (pos >= queue.length) pos = 0; banner(""); show(); return; }
  busy = true;
  try {
    await api("set", {k: f.k, d, title: f.t, year: f.y});
    undo.push({k: f.k, was: d}); queue.splice(pos, 1); if (pos >= queue.length) pos = 0; banner(""); show();
  } catch (e) { banner("Not saved: " + e.message + ". Nothing was changed, try again.", "err"); }
  busy = false;
}

async function undoLast() {
  const u = undo.pop(); if (!u || busy) return;
  if (u.was === "skip") { skipped.delete(u.k); rebuild(u.k); return; }
  busy = true;
  try { await api("set", {k: u.k, d: "none"}); rebuild(u.k); banner(""); }
  catch (e) { undo.push(u); banner("Undo not saved: " + e.message, "err"); }
  busy = false;
}

async function reloadItems() {
  const r = await fetch("api/items", {cache: "no-store"});
  if (!r.ok) throw new Error("The list could not be loaded (HTTP " + r.status + ")");
  const j = await r.json();
  items = j.items || []; instances = j.instances || []; byKey = {}; items.forEach(f => byKey[f.k] = f); defaultView = j.defaultView || "all";
  return j;
}

function runBox() {
  const c = counts(), st = serverState.status, dry = serverState.dryRun;
  let h = "";
  const running = st && st.state === "running" && serverState.queued;
  if (running) h += `<div class="msg">Working${st.total ? `: ${st.n} of ${st.total} done, ${gb(st.freed || 0)} so far` : "&hellip;"}. This page updates by itself.</div>`;
  else if (serverState.queued) h += `<div class="msg">Removal is queued and starts within seconds. <button id="cancelRun">Cancel</button></div>`;
  else if (st && st.state === "done") h += `<div class="msg ${st.rc ? "err" : "good"}">Last run ${esc(new Date(st.at * 1000).toLocaleString())}: ${st.dry ? "DRY RUN, would have removed" : "removed"} ${st.removed} title(s), ${gb(st.freed || 0)}${st.failed && st.failed.length ? ". Problems: " + esc(st.failed.join("; ")) : ""}.</div>`;
  clearTimeout(pollTimer);
  if (serverState.queued) pollTimer = setTimeout(() => { if (!$("review").classList.contains("hide")) api("state").then(async s => { if (!s.queued) await reloadItems(); renderReview(); }).catch(() => {}); }, 3000);
  if (tab === "delete") h += `<button class="big" id="runBtn" ${c.n && !serverState.queued ? "" : "disabled"}>Remove the ${c.n} marked title${c.n === 1 ? "" : "s"} now (${gb(c.bytes)})</button>
    <p class="facts">${dry ? "DRY RUN is on: nothing is deleted, the log shows what would be removed." : "Removed titles go to the recycle bin of Radarr / Sonarr if you set one up there. Without one they are gone for good."}</p>`;
  $("runBox").innerHTML = h;
  if ($("runBtn")) $("runBtn").onclick = async () => {
    if (!confirm(`Remove ${c.n} title${c.n === 1 ? "" : "s"} (${gb(c.bytes)}) from the library?`)) return;
    try { await api("run", {}); banner(""); renderReview(); } catch (e) { banner("Could not queue the removal: " + e.message, "err"); }
  };
  if ($("cancelRun")) $("cancelRun").onclick = async () => { try { await api("cancel", {}); renderReview(); } catch (e) { banner("Could not cancel: " + e.message, "err"); } };
}

function renderReview() {
  $("tabDel").classList.toggle("on", tab === "delete"); $("tabKeep").classList.toggle("on", tab === "keep"); $("tabImdb").classList.toggle("on", tab === "imdb");
  $("imdbBox").classList.toggle("hide", tab !== "imdb"); $("runBox").classList.toggle("hide", tab === "imdb"); $("list").classList.toggle("hide", tab === "imdb");
  if (tab === "imdb") return;
  runBox();
  const rows = Object.keys(dec).filter(k => dec[k].d === tab && !dec[k].done).sort((a, b) => (dec[b].at || 0) - (dec[a].at || 0));
  $("list").innerHTML = rows.length ? rows.map(k => {
    const f = byKey[k], d = dec[k];
    return `<div class="item">${f && safeUrl(f.p) ? `<img loading="lazy" src="${esc(f.p)}" alt="">` : `<div></div>`}
      <div><div class="t">${esc(f ? f.t : d.title)} ${f && f.y ? "(" + f.y + ")" : ""}</div>
      <div class="d">${f ? esc(f.srcn) + " &middot; " + gb(f.sz) + " &middot; " + esc(f.q) : "no longer in the library"}</div></div>
      <button data-k="${esc(k)}">${tab === "delete" ? "Don't delete" : "Decide again"}</button></div>`;
  }).join("") : `<div class="empty">${tab === "delete" ? "Nothing is marked to delete." : "Nothing is marked keep."}</div>`;
  $("list").querySelectorAll("button[data-k]").forEach(b => b.onclick = async () => {
    try { await api("set", {k: b.dataset.k, d: "none"}); renderReview(); } catch (e) { banner("Not saved: " + e.message, "err"); }
  });
}

function view(v, keepKey) {
  current = v;
  $("pick").classList.toggle("hide", v !== "pick"); $("grid").classList.toggle("hide", v !== "grid"); $("review").classList.toggle("hide", v !== "review");
  $("bar").classList.toggle("hide", v !== "pick"); $("filters").classList.toggle("hide", v === "review");
  $("gridBtn").textContent = v === "grid" ? "One at a time" : "Browse";
  if (v === "review") { api("state").then(renderReview).catch(e => banner(e.message, "err")); return; }
  rebuild(keepKey);
  if (v === "grid") { renderGrid(); window.scrollTo({top: 0}); }
}

function setupFilters() {
  const kinds = new Set(instances.map(i => i.kind));
  let h = `<option value="">Everything</option>`;
  if (kinds.size > 1) h += `<option value="kind:movie">All movies</option><option value="kind:show">All TV shows</option>`;
  if (instances.length > 1) h += instances.map(i => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join("");
  const was = $("fLib").value, first = !$("fLib").options.length;
  $("fLib").innerHTML = h;
  const want = first ? ({movies: "kind:movie", shows: "kind:show"}[defaultView] || "") : was;      // the configured opening view, once
  if ([...$("fLib").options].some(o => o.value === want)) $("fLib").value = want;
  $("fLib").classList.toggle("hide", instances.length < 2);
  $("fPlay").classList.toggle("hide", !hasPlex);
  $("bSeen").classList.toggle("hide", !hasPlex); $("tabImdb").classList.toggle("hide", !hasPlex);
  $("barRow").classList.toggle("three", !hasPlex);
  if (!hasPlex) [...$("fSort").options].forEach(o => { if (o.value === "watched") o.remove(); });
}

$("bDel").onclick = () => decide("delete"); $("bKeep").onclick = () => decide("keep"); $("bSkip").onclick = () => decide("skip"); $("bSeen").onclick = () => decide("seen"); $("bUndo").onclick = undoLast;
$("bPrev").onclick = () => step(-1); $("bNext").onclick = () => step(1);
$("gridBtn").onclick = () => view(current === "grid" ? "pick" : "grid", queue[pos] ? queue[pos].k : null);     // keep your place both ways
$("wall").onclick = e => { const t = e.target.closest(".tile"); if (t) view("pick", queue[+t.dataset.i].k); };
let touchX = null, touchY = null;
$("card").addEventListener("touchstart", e => { touchX = e.touches[0].clientX; touchY = e.touches[0].clientY; }, {passive: true});
$("card").addEventListener("touchend", e => {
  if (touchX === null) return;
  const dx = e.changedTouches[0].clientX - touchX, dy = e.changedTouches[0].clientY - touchY; touchX = null;
  if (Math.abs(dx) > 70 && Math.abs(dx) > 2 * Math.abs(dy)) step(dx < 0 ? 1 : -1);      // swipe left = next
}, {passive: true});
$("reviewBtn").onclick = () => { tab = "delete"; view("review"); };
$("tabImdb").onclick = () => { tab = "imdb"; renderReview(); };
$("imdbFile").onchange = () => { $("imdbGo").disabled = !$("imdbFile").files.length; $("imdbResult").innerHTML = ""; };
$("imdbGo").onclick = async () => {
  const file = $("imdbFile").files[0]; if (!file) return;
  $("imdbGo").disabled = true;
  try {
    if (file.size > 6000000) throw new Error("that file is too large to be an IMDb export");
    const r = (await api("imdb", {csv: await file.text(), ratedOnly: $("imdbRated").checked})).imported;
    $("imdbResult").innerHTML = `<div class="msg good">${r.inFile.toLocaleString()} titles in the file, ${r.inLibrary.toLocaleString()} of them in your library: ` +
      `${r.queued.toLocaleString()} will be marked as played in Plex within a minute, ${r.alreadyPlayed.toLocaleString()} were already played` +
      `${r.notInPlex ? ", " + r.notInPlex.toLocaleString() + " are not in Plex" : ""}.</div>`;
  } catch (e) { $("imdbResult").innerHTML = `<div class="msg err">${esc(e.message)}</div>`; }
  $("imdbGo").disabled = false;
};
$("tabDel").onclick = () => { tab = "delete"; renderReview(); }; $("tabKeep").onclick = () => { tab = "keep"; renderReview(); }; $("back").onclick = () => view("pick");
const refilter = () => { rebuild(); if (current === "grid") renderGrid(); };
$("fPlay").onchange = () => { if ($("fPlay").value === "watched") $("fSort").value = "watched"; else if ($("fSort").value === "watched") $("fSort").value = "added"; refilter(); };
["fLib", "fSort"].forEach(id => $(id).onchange = refilter); $("fSearch").oninput = refilter;
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT" || $("pick").classList.contains("hide") || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === "arrowleft") step(-1); else if (k === "arrowright") step(1);
  else if (k === "d") decide("delete"); else if (k === "k") decide("keep"); else if (k === "w" && hasPlex) decide("seen");
  else if (k === "s") decide("skip"); else if (k === "u" || k === "z") undoLast();
});

(async function start() {
  try {
    const s = await api("state"); hasPlex = !!s.plex;
    let j = await reloadItems();
    setupFilters();
    if (!j.built) {                       // first start: the list is still being built
      $("card").classList.add("hide");
      for (let i = 0; i < 600 && !j.built; i++) {
        const st = await api("state");
        banner(st.error ? "Could not build the list yet: " + st.error + ". Trying again every minute." : "Building the list for the first time…", st.error ? "err" : "");
        await new Promise(r => setTimeout(r, 2000)); j = await reloadItems();
      }
      banner(""); setupFilters();
    } else if (s.error) banner("The list could not be refreshed: " + s.error, "err");
    else if (s.dryRun) banner("DRY RUN is on: you can mark and press Remove, nothing is deleted.");
    rebuild();
  } catch (e) { $("card").classList.add("hide"); banner("Could not load: " + e.message, "err"); }
})();
