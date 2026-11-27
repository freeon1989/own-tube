"use strict";
/* own-tube, этап 1. Текст из сети вставляется только через textContent и атрибуты; innerHTML не используется. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);
  let api = null;

  /* ---------- маленькие помощники DOM ---------- */
  function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid);
    return n;
  }
  function ic(name) {
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("class", "ico");
    const u = document.createElementNS(NS, "use");
    u.setAttribute("href", "#i-" + name);
    s.appendChild(u);
    return s;
  }
  function status(text, kind) {
    const s = $("status");
    s.textContent = text || "";
    s.className = "status" + (kind ? " " + kind : "");
  }
  async function call(name, ...args) {
    try {
      const r = await api[name](...args);
      return r || { ok: false, error: "Нет ответа" };
    } catch (e) {
      return { ok: false, error: "Ошибка связи с программой" };
    }
  }

  /* ---------- помощницы и темы ---------- */
  const ASSISTANTS = [
    { id: "rei", name: "Рэй", theme: "violet", persona: "Спокойная и точная, чуть загадочная. Отвечает коротко и по делу." },
    { id: "yuki", name: "Юки", theme: "minimal", persona: "Мягкая и заботливая. Объясняет подробно, любит спокойные каналы." },
    { id: "kate", name: "Кейт", theme: "neon", persona: "Бойкая, шутит и подначивает. Подбирает, что посмотреть вечером." },
    { id: "lila", name: "Лила", theme: "sakura", persona: "Энергичная, обожает музыку и танцы. Всегда за новый плейлист." },
    { id: "nina", name: "Нина", theme: "light", persona: "Любит новости и тренды. Всё проверяет и даёт источники." },
  ];
  const THEMES = [
    ["assistant", "Как у помощницы", ["#a855f7", "#ff2bd6", "#ff7eb6"]],
    ["violet", "Фиолетовая", ["#09081a", "#a855f7", "#22d3ee"]],
    ["neon", "Неон", ["#05060f", "#ff2bd6", "#00f0ff"]],
    ["sakura", "Сакура", ["#1a0d16", "#ff7eb6", "#ffd1e3"]],
    ["minimal", "Минимализм", ["#0e0f12", "#3b82f6", "#ececf0"]],
    ["light", "Светлая", ["#f4f3fa", "#2f63e8", "#1b1830"]],
  ];
  const ACTS = [["Привет", "wave", "👋"], ["Думает", "think", "…"], ["Радуется", "joy", "♪"], ["Удивлена", "wow", "!"]];

  let S = null;          // настройки
  let counts = {};
  let source = "yt";     // вкладка источника
  let current = { view: "home", arg: null, title: "Главная" };
  let lastItems = [];
  let dlTimer = null;

  function assistant() { return ASSISTANTS.find((a) => a.id === S.assistant) || ASSISTANTS[0]; }
  function applyTheme() {
    const t = S.theme === "assistant" ? assistant().theme : S.theme;
    document.documentElement.dataset.theme = t;
  }
  function applyAssistant(animate) {
    const a = assistant();
    const hero = $("hero");
    const src = `assets/heroines/${a.id}.webp`;
    if (animate) { hero.classList.add("swap"); setTimeout(() => { hero.src = src; hero.classList.remove("swap"); act("wave", "👋"); }, 280); }
    else hero.src = src;
    $("chatAv").src = src; $("fabAv").src = src;
    $("stName").textContent = a.name; $("stPersona").textContent = a.persona; $("chName").textContent = a.name;
    document.querySelectorAll("#switcher button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.id === a.id)));
    applyTheme();
  }
  function act(kind, glyph) {
    const hero = $("hero");
    hero.className = "hero"; void hero.offsetWidth; hero.classList.add("act-" + kind);
    const m = $("mood"); m.textContent = glyph; m.classList.remove("show"); void m.offsetWidth; m.classList.add("show");
    hero.addEventListener("animationend", (e) => { if (e.animationName !== "breathe") hero.className = "hero"; }, { once: true });
  }
  function buildStage() {
    $("switcher").replaceChildren(...ASSISTANTS.map((a) => h("button", {
      type: "button", role: "radio", "data-id": a.id, "aria-label": a.name, title: a.name,
      onclick: () => saveSettings({ assistant: a.id }).then(() => applyAssistant(true)),
    }, h("img", { src: `assets/heroines/${a.id}.webp`, alt: "" }))));
    $("acts").replaceChildren(...ACTS.map(([t, k, g]) => h("button", { type: "button", onclick: () => act(k, g) }, t)));
    const box = document.querySelector(".sparks"); let r = 7;
    const rnd = () => (r = (r * 16807) % 2147483647) / 2147483647;
    box.replaceChildren(...Array.from({ length: 16 }, () => {
      const i = h("i");
      i.style.setProperty("--x", (rnd() * 100).toFixed(0) + "%"); i.style.setProperty("--s", (2 + rnd() * 3).toFixed(1) + "px");
      i.style.setProperty("--d", (8 + rnd() * 8).toFixed(1) + "s"); i.style.setProperty("--dl", "-" + (rnd() * 12).toFixed(1) + "s");
      i.style.setProperty("--dx", Math.round(rnd() * 60 - 30) + "px");
      return i;
    }));
    $("hero").addEventListener("click", () => { act("joy", "♪"); openChat(); });
  }

  /* ---------- чат (ИИ — второй этап) ---------- */
  function openChat() {
    $("chat").classList.remove("closed");
    if (!$("msgs").childElementCount) {
      $("msgs").append(h("div", { class: "m ai" }, `Привет! Я ${assistant().name}. Думать и говорить я научусь на втором этапе, когда подключим ИИ. А пока могу помахать рукой.`));
    }
    $("chatIn").focus();
  }
  function wireChat() {
    $("chatClose").addEventListener("click", () => $("chat").classList.add("closed"));
    $("chatFab").addEventListener("click", openChat);
    $("chatForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = $("chatIn").value.trim();
      if (!text) return;
      $("chatIn").value = "";
      $("msgs").append(h("div", { class: "m me" }, text));
      act("think", "…");
      const r = await call("assistant_say", text);
      $("msgs").append(h("div", { class: "m ai" }, r.ok ? r.reply : r.error));
      $("msgs").scrollTop = $("msgs").scrollHeight;
    });
  }

  /* ---------- меню слева ---------- */
  const NAV = [
    ["home", "Главная", "home"], ["shorts", "Shorts", "shorts"], ["subs", "Подписки", "subs"],
    ["sec", "Вы"], ["history", "История", "history", "history"], ["playlists", "Плейлисты", "list", "playlists"],
    ["later", "Смотреть позже", "later", "later"], ["favorite", "Избранное", "heart", "favorite"], ["downloads", "Загрузки", "download", "downloads"],
    ["sec", "Обзор"], ["cat:music", "Музыка", "music"], ["cat:games", "Игры", "game"], ["cat:news", "Новости", "news"],
    ["cat:sport", "Спорт", "sport"], ["cat:live", "Трансляции", "live"], ["cat:fashion", "Мода и красота", "fashion"], ["cat:learn", "Обучение", "learn"],
    ["sec", "Другие источники"], ["vk", "VK Видео", "vk"],
  ];
  function buildSide() {
    const side = $("side");
    const logo = h("i"); const lg = document.createElementNS(NS, "svg"); lg.setAttribute("viewBox", "0 0 24 24");
    const p = document.createElementNS(NS, "path"); p.setAttribute("d", "m8 5 12 7-12 7z"); lg.appendChild(p); logo.appendChild(lg);
    const kids = [h("div", { class: "brand" }, logo, h("b", {}, "own-tube")),
      h("button", { class: "account", type: "button", title: "Вход в Google — второй этап", onclick: () => go("settings", "accounts") },
        h("span", { class: "g" }, "G"), h("span", { class: "t" }, h("b", {}, "Войти"), h("span", {}, "Google — на втором этапе")))];
    for (const it of NAV) {
      if (it[0] === "sec") { kids.push(h("div", { class: "sec" }, it[1])); continue; }
      const n = it[3] ? counts[it[3]] : 0;
      const b = h("button", { class: "nav", type: "button", "data-view": it[0], onclick: () => go(it[0]) },
        ic(it[2]), h("span", { class: "l" }, it[1]), n ? h("span", { class: "n" }, String(n)) : null);
      kids.push(b);
    }
    kids.push(h("div", { class: "grow" }));
    kids.push(h("button", { class: "nav", type: "button", "data-view": "settings", onclick: () => go("settings") }, ic("gear"), h("span", { class: "l" }, "Настройки")));
    side.replaceChildren(...kids);
    markNav();
  }
  function markNav() {
    document.querySelectorAll("#side .nav").forEach((b) => {
      if (b.dataset.view === current.view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
  }
  async function refreshCounts() {
    const r = await call("get_state");
    if (r.ok) { counts = r.counts; buildSide(); }
  }

  /* ---------- карточки и меню ---------- */
  function card(item, opts) {
    opts = opts || {};
    const thumb = h("div", { class: "thumb" }, item.thumb ? h("img", { src: item.thumb, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : null);
    if (item.source === "vk") thumb.append(h("span", { class: "badge src" }, "VK"));
    if (item.live) thumb.append(h("span", { class: "badge live" }, "LIVE"));
    else if (item.duration) thumb.append(h("span", { class: "badge" }, item.duration));
    const sub = h("div", { class: "sub" });
    if (item.channel) {
      if (item.channel_id) sub.append(h("button", { class: "chan", type: "button", onclick: (e) => { e.stopPropagation(); go("channel", item.channel_id, item.channel); } }, item.channel));
      else sub.append(item.channel);
    }
    const extra = item.views || (item.watched ? "смотрели " + new Date(item.watched * 1000).toLocaleDateString("ru-RU") : "");
    if (extra) sub.append((item.channel ? " · " : "") + extra);
    const c = h("div", { class: "card", role: "button", tabindex: "0", "aria-label": item.title },
      thumb, h("div", { class: "meta" }, h("div", { class: "title" }, item.title), sub),
      h("button", { class: "more", type: "button", "aria-label": "Действия", onclick: (e) => { e.stopPropagation(); openMenu(e.currentTarget, item, opts); } }, ic("dots")));
    c.addEventListener("click", () => play(item, c));
    c.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === c) play(item, c); });
    return c;
  }
  function closeMenu() { $("menu").hidden = true; }
  async function openMenu(anchor, item, opts) {
    const st = await call("item_state", item);
    const marks = st.ok ? st.marks : [];
    const mk = (icon, text, fn, on) => h("button", { type: "button", role: "menuitem", onclick: async () => { closeMenu(); await fn(); } },
      ic(icon), text, on ? h("span", { class: "ok" }, ic("check")) : null);
    const list = [
      mk("play", "Смотреть", () => play(item)),
      mk("later", "Смотреть позже", () => toggleMark("later", item, !marks.includes("later")), marks.includes("later")),
      mk("heart", "Избранное", () => toggleMark("favorite", item, !marks.includes("favorite")), marks.includes("favorite")),
      mk("list", "В плейлист…", () => choosePlaylist(item)),
      mk("download", "Скачать", () => download(item)),
    ];
    if (item.channel_id) list.push(h("hr"), mk("subs", st.subscribed ? "Отписаться от канала" : "Подписаться на канал",
      () => subscribe(item.channel_id, item.channel, !st.subscribed)));
    if (opts.playlistId) list.push(h("hr"), mk("trash", "Убрать из плейлиста", async () => { await call("remove_from_playlist", opts.playlistId, item); reload(); }));
    if (opts.mark) list.push(h("hr"), mk("trash", "Убрать из списка", async () => { await toggleMark(opts.mark, item, false); reload(); }));
    const m = $("menu");
    m.replaceChildren(...list);
    m.hidden = false;
    const r = anchor.getBoundingClientRect();
    m.style.left = Math.min(window.innerWidth - m.offsetWidth - 8, Math.max(8, r.right - m.offsetWidth)) + "px";
    m.style.top = Math.min(window.innerHeight - m.offsetHeight - 8, r.bottom + 6) + "px";
    m.querySelector("button").focus();
  }
  document.addEventListener("click", (e) => { if (!$("menu").contains(e.target)) closeMenu(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });

  async function toggleMark(kind, item, on) {
    const r = await call("set_mark", kind, item, on);
    status(r.ok ? (on ? "Добавлено: " : "Убрано: ") + (kind === "later" ? "«Смотреть позже»" : "«Избранное»") : r.error, r.ok ? "" : "err");
    refreshCounts();
  }
  async function subscribe(cid, title, on) {
    const r = await call("subscribe", cid, title, on);
    status(r.ok ? (on ? "Подписка на «" + title + "» добавлена" : "Отписались от «" + title + "»") : r.error, r.ok ? "" : "err");
    refreshCounts();
    if (current.view === "subs") reload();
  }
  async function download(item) {
    const r = await call("download", item, S.quality);
    status(r.ok ? "Поставлено в загрузки: " + item.title : r.error, r.ok ? "" : "err");
    refreshCounts();
  }
  async function play(item, node) {
    if (node) node.setAttribute("aria-busy", "true");
    status("Открываю «" + item.title + "»…", "busy");
    const r = await call("play", item, S.quality);
    if (node) node.removeAttribute("aria-busy");
    if (!r.ok) { status(r.error, "err"); act("wow", "!"); return; }
    status("Играет в mpv" + (r.skips ? ` · SponsorBlock пропустит отрезков: ${r.skips}` : ""));
    act("joy", "♪");
    refreshCounts();
  }
  async function choosePlaylist(item) {
    const dlg = $("plDialog");
    const r = await call("library", "playlists");
    const pls = r.ok ? r.playlists : [];
    $("plList").replaceChildren(...(pls.length ? pls.map((p) => h("button", { type: "button", onclick: async () => {
      const x = await call("add_to_playlist", p.id, item); dlg.close();
      status(x.ok ? "Добавлено в «" + p.name + "»" : x.error, x.ok ? "" : "err"); refreshCounts();
    } }, h("span", {}, p.name), h("span", { class: "n" }, String(p.count)))) : [h("p", { class: "note" }, "Плейлистов пока нет — создайте первый.")]));
    $("plNew").value = "";
    $("plForm").onsubmit = async (e) => {
      e.preventDefault();
      const x = await call("create_playlist", $("plNew").value, item); dlg.close();
      status(x.ok ? "Плейлист создан, видео добавлено" : x.error, x.ok ? "" : "err"); refreshCounts();
    };
    $("plClose").onclick = () => dlg.close();
    dlg.showModal();
  }

  /* ---------- экраны ---------- */
  function view(...kids) { $("view").replaceChildren(...kids); }
  function title(text, ...extra) { return h("h1", { class: "h1" }, text, ...extra); }
  function grid(items, opts) {
    lastItems = items;
    if (!items.length) return h("p", { class: "empty" }, (opts && opts.empty) || "Здесь пока пусто.");
    return h("div", { class: "grid" + (opts && opts.shorts ? " shorts" : "") }, ...items.map((i) => card(i, opts)));
  }
  let seq = 0;
  async function load(fn, render) {
    const my = ++seq;
    status("Загружаю…", "busy");
    const r = await fn();
    if (my !== seq) return;           // пользователь уже ушёл на другой экран
    if (!r.ok) { status(r.error, "err"); return; }
    status("");
    render(r);
  }
  const CAT = { music: "Музыка", games: "Игры", news: "Новости", sport: "Спорт", live: "Трансляции", fashion: "Мода и красота", learn: "Обучение" };

  function go(viewName, arg, label) {
    current = { view: viewName, arg: arg || null, title: label || "" };
    if (dlTimer) { clearInterval(dlTimer); dlTimer = null; }
    closeMenu(); markNav();
    setSource(viewName === "vk" ? "vk" : (viewName === "search" ? source : "yt"), true);
    window.scrollTo(0, 0);
    reload();
  }
  function reload() {
    const v = current.view;
    if (v === "home") return load(() => call("feed", "home"), (r) => view(title("Главная", h("small", {}, r.from === "subs" ? "новое с ваших подписок" : "популярное за неделю — подпишитесь на каналы, и здесь будут они")), grid(r.items)));
    if (v === "shorts") return load(() => call("feed", "shorts"), (r) => view(title("Shorts", h("small", {}, "короткие видео")), grid(r.items, { shorts: true })));
    if (v === "subs") return showSubs();
    if (v.startsWith("cat:")) { const k = v.slice(4); return load(() => call("feed", "category", k), (r) => view(title(CAT[k]), grid(r.items))); }
    if (v === "channel") return load(() => call("feed", "channel", current.arg), (r) => view(title(current.title || "Канал",
      h("span", { class: "sp" }), h("button", { class: "ghost", type: "button", onclick: () => subscribe(current.arg, current.title, true) }, "Подписаться")), grid(r.items)));
    if (v === "history") return load(() => call("library", "history"), (r) => view(title("История", h("span", { class: "sp" }),
      h("button", { class: "ghost", type: "button", onclick: async () => { if (confirm("Очистить всю историю просмотра?")) { await call("clear_history"); reload(); refreshCounts(); } } }, "Очистить")),
      grid(r.items, { empty: "Вы ещё ничего не смотрели. Историю из YouTube можно перенести через Google Takeout в настройках." })));
    if (v === "later" || v === "favorite") return load(() => call("library", v), (r) => view(title(v === "later" ? "Смотреть позже" : "Избранное"),
      grid(r.items, { mark: v, empty: "Добавляйте видео через меню «⋯» на карточке." })));
    if (v === "playlists") return showPlaylists();
    if (v === "playlist") return load(() => call("library", "playlist", current.arg), (r) => view(title(current.title,
      h("span", { class: "sp" }), h("button", { class: "ghost", type: "button", onclick: async () => { if (confirm("Удалить плейлист «" + current.title + "»?")) { await call("delete_playlist", current.arg); go("playlists"); refreshCounts(); } } }, "Удалить плейлист")),
      grid(r.items, { playlistId: current.arg, empty: "Плейлист пуст." })));
    if (v === "downloads") return showDownloads();
    if (v === "vk") return showVk();
    if (v === "search") return doSearch(current.arg);
    if (v === "settings") return showSettings(current.arg);
  }

  async function showSubs() {
    const r = await call("library", "subscriptions");
    const chans = r.ok ? r.channels : [];
    const chips = h("div", { class: "chips" }, ...chans.map((c) => h("span", { class: "chip" },
      h("button", { class: "chan", type: "button", onclick: () => go("channel", c.channel_id, c.title) }, c.title),
      h("button", { class: "x", type: "button", "aria-label": "Отписаться от " + c.title, title: "Отписаться", onclick: () => subscribe(c.channel_id, c.title, false) }, "×"))));
    if (!chans.length) {
      view(title("Подписки"), h("p", { class: "empty" }, "Подписок пока нет.", h("br"),
        "Подпишитесь через меню «⋯» на карточке или ", h("b", {}, "перенесите подписки из YouTube"), " через Google Takeout.", h("br"), h("br"),
        h("button", { class: "go", type: "button", onclick: () => go("settings", "data") }, "Импорт из Takeout")));
      return;
    }
    view(title("Подписки", h("small", {}, `каналов: ${chans.length}`)), chips, h("div", { id: "subsFeed" }));
    load(() => call("feed", "subs"), (x) => { const f = $("subsFeed"); if (f) f.replaceChildren(grid(x.items)); });
  }

  async function showPlaylists() {
    const r = await call("library", "playlists");
    const pls = r.ok ? r.playlists : [];
    const add = h("button", { class: "ghost", type: "button", onclick: async () => {
      const name = prompt("Название нового плейлиста"); if (!name) return;
      const x = await call("create_playlist", name); if (!x.ok) status(x.error, "err"); reload(); refreshCounts();
    } }, "Новый плейлист");
    view(title("Плейлисты", h("span", { class: "sp" }), add), pls.length ? h("div", { class: "grid" }, ...pls.map((p) => {
      const thumb = h("div", { class: "thumb" }, p.thumb ? h("img", { src: p.thumb, alt: "" }) : ic("list"), h("span", { class: "count" }, String(p.count)));
      const c = h("div", { class: "card pl-card", role: "button", tabindex: "0" }, thumb, h("div", { class: "meta" }, h("div", { class: "title" }, p.name), h("div", { class: "sub" }, "видео: " + p.count)));
      c.addEventListener("click", () => go("playlist", p.id, p.name));
      return c;
    })) : h("p", { class: "empty" }, "Плейлистов пока нет. Создайте свой или перенесите из YouTube через Google Takeout."));
  }

  const DL_STATUS = { queued: "в очереди", running: "скачивается", done: "готово", error: "ошибка", cancelled: "отменено" };
  async function showDownloads() {
    const render = async () => {
      const r = await call("library", "downloads");
      if (!r.ok || current.view !== "downloads") return;
      const rows = r.items.map((d) => h("div", { class: "dl" },
        d.thumb ? h("img", { src: d.thumb, alt: "" }) : h("span"),
        h("div", {}, h("div", { class: "t" }, d.title),
          h("div", { class: "s" + (d.status === "error" ? " err" : "") }, [DL_STATUS[d.status] || d.status, d.quality === "best" ? "лучшее" : d.quality + "p",
            d.status === "running" ? Math.round(d.progress || 0) + "%" : "", d.path || "", d.error || ""].filter(Boolean).join(" · ")),
          d.status === "running" || d.status === "queued" ? h("div", { class: "prog" }, (() => { const i = h("i"); i.style.width = (d.progress || 0) + "%"; return i; })()) : null),
        h("div", { class: "btns" },
          d.status === "running" || d.status === "queued" ? h("button", { class: "ghost", type: "button", onclick: async () => { await call("cancel_download", d.dl_id); render(); } }, "Отменить")
            : h("button", { class: "ghost", type: "button", onclick: async () => { await call("remove_download", d.dl_id); render(); refreshCounts(); } }, "Убрать из списка"))));
      view(title("Загрузки", h("span", { class: "sp" }), h("button", { class: "ghost", type: "button", onclick: () => call("open_download_folder") }, ic("folder"), " Открыть папку")),
        rows.length ? h("div", {}, ...rows) : h("p", { class: "empty" }, "Загрузок пока нет. Скачать видео можно через меню «⋯» на карточке."));
    };
    status("");
    await render();
    dlTimer = setInterval(render, 1500);
  }

  async function showVk() {
    const r = await call("library", "history");
    const vkItems = r.ok ? r.items.filter((i) => i.source === "vk") : [];
    view(title("VK Видео"),
      h("div", { class: "vk-hero" }, h("div", { class: "logo" }, ic("vk")), h("div", {},
        h("b", {}, "Отдельная вкладка VK Видео"),
        h("p", {}, "Вставьте ссылку на ролик vkvideo.ru или vk.com в поиск сверху — видео откроется в mpv без рекламы. Поиск по VK и ролики 18+ требуют входа в VK — это второй этап."))),
      h("h2", { class: "h1" }, h("small", {}, "Недавно смотрели в VK")),
      grid(vkItems, { empty: "Здесь появятся ролики VK, которые вы открывали." }));
    status("");
  }

  async function doSearch(q) {
    await load(() => call("search", q, source), (r) => view(title((source === "vk" ? "VK: " : "Поиск: ") + q), grid(r.items)));
  }

  function setSource(s, silent) {
    source = s;
    $("tabYt").setAttribute("aria-selected", String(s === "yt"));
    $("tabVk").setAttribute("aria-selected", String(s === "vk"));
    $("q").placeholder = s === "vk" ? "Ссылка на видео VK (vkvideo.ru/video-…)" : "Поиск на YouTube или ссылка";
    if (!silent && s === "vk") go("vk");
    if (!silent && s === "yt" && current.view === "vk") go("home");
  }

  /* ---------- настройки ---------- */
  async function saveSettings(patch) {
    const r = await call("save_settings", patch);
    if (!r.ok) { status(r.error, "err"); return false; }
    S = r.settings;
    return true;
  }
  function sw(on, fn, label) {
    const b = h("button", { class: "sw", type: "button", role: "switch", "aria-checked": String(on), "aria-label": label });
    b.addEventListener("click", async () => { const v = b.getAttribute("aria-checked") !== "true"; if (await fn(v) !== false) b.setAttribute("aria-checked", String(v)); });
    return b;
  }
  function panel(id, icon, ttl, lead, ...kids) {
    return h("section", { class: "panel", id: "set-" + id }, h("h2", {}, ic(icon), ttl), lead ? h("p", { class: "lead" }, lead) : null, ...kids);
  }
  function field(label, ...ctl) { return h("div", { class: "field" }, h("label", {}, label), ...ctl); }
  const SB_NAMES = { sponsor: "Реклама спонсора", selfpromo: "Самореклама", interaction: "Просьбы подписаться", intro: "Заставка", outro: "Концовка",
    preview: "Анонс", music_offtopic: "Не музыка в клипах", filler: "Вода" };

  async function showSettings(section) {
    const st = await call("get_state");
    if (!st.ok) { status(st.error, "err"); return; }
    S = st.settings;
    const tools = st.tools;
    const themeOpts = h("div", { class: "themes", role: "radiogroup", "aria-label": "Тема" }, ...THEMES.map(([id, name, cols]) => h("button", {
      class: "theme-opt", type: "button", role: "radio", "aria-checked": String(S.theme === id),
      onclick: async (e) => { const opt = e.currentTarget; if (await saveSettings({ theme: id })) { applyTheme(); opt.parentElement.querySelectorAll(".theme-opt").forEach((x) => x.setAttribute("aria-checked", String(x === opt))); } },
    }, h("span", { class: "sw3" }, ...cols.map((c) => { const i = h("i"); i.style.background = c; return i; })), name)));
    const asstRows = ASSISTANTS.map((a) => h("div", { class: "row asst" + (a.id === S.assistant ? " active" : "") },
      h("img", { class: "av", src: `assets/heroines/${a.id}.webp`, alt: "" }),
      h("div", { class: "nm" }, h("b", {}, a.name), h("span", {}, a.persona)),
      h("div", { class: "kv" }, h("span", {}, "Тема"), h("b", {}, (THEMES.find((t) => t[0] === a.theme) || [, ""])[1])),
      h("div", { class: "kv" }, h("span", {}, "ИИ"), h("span", { class: "pill" }, "второй этап")),
      h("button", { class: "ghost", type: "button", onclick: async () => { if (await saveSettings({ assistant: a.id })) { applyAssistant(true); showSettings("look"); } } }, a.id === S.assistant ? "Выбрана" : "Выбрать")));

    const codec = h("select", { "aria-label": "Кодек" }, h("option", { value: "auto" }, "Авто (лучшее качество, может быть AV1/VP9)"), h("option", { value: "h264" }, "H.264 — легче для видеокарты"));
    codec.value = S.codec;
    codec.addEventListener("change", () => saveSettings({ codec: codec.value }));
    const sbChecks = h("div", { class: "checks" }, ...Object.entries(SB_NAMES).map(([k, n]) => {
      const cb = h("input", { type: "checkbox", value: k }); cb.checked = S.sb_categories.includes(k);
      cb.addEventListener("change", () => saveSettings({ sb_categories: [...sbChecks.querySelectorAll("input:checked")].map((x) => x.value) }));
      return h("label", { class: "check" }, cb, n);
    }));
    const toolPill = (n, label) => h("span", { class: "pill " + (tools[n] ? "local" : "paid") }, label + (tools[n] ? ": есть" : ": не найден"));

    const proxy = h("input", { type: "text", value: S.proxy, placeholder: "socks5://127.0.0.1:1080 или http://127.0.0.1:8080", "aria-label": "Прокси", spellcheck: "false" });
    const netMsg = h("span", { class: "note" });
    const mpvPath = h("input", { type: "text", value: S.mpv_path, placeholder: "по умолчанию: tools\\mpv.exe рядом с программой", "aria-label": "Путь к mpv", spellcheck: "false" });
    const dlDir = h("input", { type: "text", value: S.download_dir, readonly: true, "aria-label": "Папка загрузок" });
    const tkMsg = h("p", { class: "note" });

    const cats = [["look", "Оформление"], ["play", "Воспроизведение"], ["net", "Сеть"], ["data", "Данные"], ["accounts", "Аккаунты"], ["ai", "ИИ"]];
    view(title("Настройки", h("small", {}, "own-tube " + st.version)),
      h("div", { class: "subnav" }, ...cats.map(([id, t]) => h("button", { type: "button", onclick: () => $("set-" + id).scrollIntoView({ block: "start" }) }, t))),
      panel("look", "palette", "Оформление и помощницы", "Тема меняет цвета всего окна. «Как у помощницы» — у каждой своя.", themeOpts, h("div", { class: "row2" }), ...asstRows),
      panel("play", "play", "Воспроизведение", "Видео играет mpv. Качество выбирается кнопками сверху.",
        field("Кодек видео", codec),
        field("SponsorBlock", sw(S.sponsorblock, (v) => saveSettings({ sponsorblock: v }), "SponsorBlock"), h("span", { class: "note" }, "пропускать рекламу внутри роликов YouTube")),
        sbChecks,
        h("p", { class: "note" }, "SponsorBlock получает только 4 символа хеша от ID видео — какой ролик вы смотрите, сервер не знает."),
        field("Путь к mpv", mpvPath, h("button", { class: "ghost", type: "button", onclick: async () => { if (await saveSettings({ mpv_path: mpvPath.value })) status("Сохранено"); } }, "Сохранить")),
        h("div", { class: "toolstate" }, toolPill("mpv", "mpv"), toolPill("ffmpeg", "ffmpeg (для загрузок)"), toolPill("deno", "deno (для YouTube)"))),
      panel("net", "globe", "Сеть: прокси и VPN", "Через прокси идёт только трафик own-tube. Для SOCKS-прокси плеер mpv работает через встроенный мост HTTP→SOCKS.",
        field("Прокси", proxy, h("button", { class: "go", type: "button", onclick: async () => {
          if (await saveSettings({ proxy: proxy.value })) { netMsg.textContent = "Сохранено"; updateRouteChip(); }
        } }, "Сохранить"), h("button", { class: "ghost", type: "button", onclick: async () => {
          const r = await call("check_route"); netMsg.textContent = r.detail || r.error || ""; netMsg.className = "note " + (r.ok ? "ok-text" : "bad-text");
        } }, "Проверить")),
        netMsg,
        h("div", { class: "row asst stub" }, h("span"), h("div", { class: "nm" }, h("b", {}, "VPN AmneziaWG"), h("span", {}, "Выбор своего сервера и маршрут только для own-tube")), h("span"), h("span"), h("span", { class: "pill" }, "скоро"))),
      panel("data", "folder", "Данные", "Всё хранится только на этом компьютере (SQLite в папке пользователя).",
        field("Google Takeout", h("button", { class: "go", type: "button", onclick: async () => {
          tkMsg.textContent = "Читаю файл…";
          const r = await call("import_takeout");
          if (!r.ok) { tkMsg.textContent = r.error; return; }
          if (r.cancelled) { tkMsg.textContent = ""; return; }
          const s = r.summary;
          tkMsg.textContent = `Готово: подписок ${s.subscriptions}, записей истории ${s.history}, плейлистов ${s.playlists} (видео в них: ${s.playlist_videos}).`;
          refreshCounts();
        } }, ic("upload"), " Импорт архива или файла")),
        tkMsg,
        h("p", { class: "note" }, "takeout.google.com → только «YouTube и YouTube Music», формат истории — JSON. Подойдёт весь .zip или отдельные subscriptions.csv / watch-history.json / файлы плейлистов."),
        field("Папка загрузок", dlDir, h("button", { class: "ghost", type: "button", onclick: async () => {
          const r = await call("choose_download_dir"); if (r.ok && r.settings) { S = r.settings; dlDir.value = S.download_dir; } else if (!r.ok) status(r.error, "err");
        } }, "Выбрать…"))),
      panel("accounts", "user", "Аккаунты", "Вход даст настоящие подписки, историю и плейлисты с YouTube, а для VK — поиск и ролики 18+.",
        h("div", { class: "row2" }, h("button", { class: "go", type: "button", disabled: true }, "Войти через Google"), h("button", { class: "go", type: "button", disabled: true }, "Войти в VK"), h("span", { class: "pill" }, "второй этап")),
        h("p", { class: "note" }, "Пароль программа не увидит: вход через окно Google/VK, токен будет зашифрован средствами Windows.")),
      panel("ai", "chip", "ИИ-помощницы", "Чат, зрение, голос, локальные (GGUF, в том числе без цензуры) и платные модели — второй этап. Макет: owntube/ui/mockup.html.",
        h("div", { class: "row2" }, ...["Чат", "Зрение", "Распознавание речи", "Голос"].map((n) => h("span", { class: "pill" }, n + " — скоро")))),
    );
    status("");
    if (section) { const el = $("set-" + section); if (el) el.scrollIntoView({ block: "start" }); }
  }

  function updateRouteChip() {
    const p = S.proxy;
    $("routeText").textContent = !p ? "Напрямую" : (p.startsWith("socks") ? "SOCKS-прокси" : "HTTP-прокси");
    $("routeDot").className = "dot" + (p ? " ok" : "");
  }
  function setQuality(q) {
    document.querySelectorAll("#qualitySeg button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.q === q)));
  }

  /* ---------- запуск ---------- */
  async function start() {
    api = window.pywebview.api;
    const st = await call("get_state");
    if (!st.ok) { status(st.error, "err"); return; }
    S = st.settings; counts = st.counts;
    buildSide(); buildStage(); wireChat(); applyAssistant(false); setQuality(S.quality); updateRouteChip();
    $("searchForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const q = $("q").value.trim();
      if (q) { current = { view: "search", arg: q, title: "" }; markNav(); reload(); }
    });
    document.querySelectorAll("#qualitySeg button").forEach((b) => b.addEventListener("click", async () => {
      if (await saveSettings({ quality: b.dataset.q })) setQuality(b.dataset.q);
    }));
    $("tabYt").addEventListener("click", () => setSource("yt"));
    $("tabVk").addEventListener("click", () => setSource("vk"));
    $("routeChip").addEventListener("click", () => go("settings", "net"));
    if (!st.tools.mpv) status("mpv не найден — видео не запустится. Проверьте папку tools рядом с программой.", "err");
    const first = new URLSearchParams(location.search).get("view");
    go(first || "home");
  }

  if (window.pywebview && window.pywebview.api) start();
  else window.addEventListener("pywebviewready", start, { once: true });
})();
