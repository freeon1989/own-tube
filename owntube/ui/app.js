"use strict";
/* own-tube 0.3.2. Текст из сети вставляется только через textContent и атрибуты; innerHTML не используется. */
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
    { id: "rei", name: "Рэй", theme: "violet", tone: [1.0, 0.85], persona: "Спокойная и точная, чуть загадочная. Отвечает коротко и по делу." },
    { id: "yuki", name: "Юки", theme: "minimal", tone: [0.92, 1.1], persona: "Мягкая и заботливая. Объясняет подробно, любит спокойные каналы." },
    { id: "kate", name: "Кейт", theme: "neon", tone: [1.12, 1.15], persona: "Бойкая, шутит и подначивает. Подбирает, что посмотреть вечером." },
    { id: "lila", name: "Лила", theme: "sakura", tone: [1.15, 1.35], persona: "Энергичная, обожает музыку и танцы. Всегда за новый плейлист." },
    { id: "nina", name: "Нина", theme: "light", tone: [1.05, 1.0], persona: "Любит новости и тренды. Всё проверяет и даёт источники." },
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
  let MEDIA = "";        // локальный медиасервер: картинки и видео идут через прокси программы
  let acct = { google: {}, vk: {} };
  let watchCleanup = null;
  const prefetched = new Set();   // ролики, для которых поток уже разбирается заранее
  let activePager = null;       // текущий список с «Показать ещё» (для действия помощницы [[more]])
  function img(url) { return url && MEDIA ? MEDIA + "/img?u=" + encodeURIComponent(url) : ""; }
  function ytThumb(id) { return "https://i.ytimg.com/vi/" + id + "/mqdefault.jpg"; }

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

  /* ---------- чат с помощницей ---------- */
  let chatJob = null;
  let chatLoaded = "";
  function bubble(role, text, extra) {
    const b = h("div", { class: "m " + (role === "user" ? "me" : "ai") + (extra ? " " + extra : "") }, text || "");
    $("msgs").append(b);
    $("msgs").scrollTop = $("msgs").scrollHeight;
    return b;
  }
  async function openChat() {
    $("chat").classList.remove("closed");
    if (chatLoaded !== S.assistant) {
      chatLoaded = S.assistant;
      $("msgs").replaceChildren();
      const r = await call("assistant_history");
      if (r.ok) {
        $("chSub").textContent = r.ai ? (r.ai.name + (r.ai.model ? " · " + r.ai.model : "")) : "ИИ не настроен";
        if (!r.messages.length) {
          bubble("ai", r.ai ? `Привет! Я ${assistant().name}. Спроси, что посмотреть, или попроси найти видео.`
            : `Привет! Я ${assistant().name}. Чтобы я могла отвечать, добавь конфигурацию в «Настройки → ИИ — API» и сделай её активной.`);
        }
        for (const m of r.messages) bubble(m.role, m.text);
      }
    }
    $("chatIn").focus();
  }
  const SAFE_ACTIONS = ["play", "search", "open", "more"];
  function runAction(a) {
    if (a.kind === "search") { $("q").value = a.query; setSource("yt", true); go("search", a.query, ""); return true; }
    if (a.kind === "play") {
      const known = lastItems.find((x) => x.id === a.id);
      play(known || { source: "yt", id: a.id, title: "Видео " + a.id, channel: "", channel_id: "", duration: "", views: "", live: false, thumb: ytThumb(a.id) });
      return true;
    }
    if (a.kind === "open") { go(a.section); return true; }
    if (a.kind === "more") {
      if (activePager) { activePager.loadMore(); status("Подгружаю ещё…", "busy"); return true; }
      status("Здесь нечего подгружать", "err");
      return false;
    }
    return false;
  }
  function actionButton(a) {
    if (a.kind === "search") return h("button", { type: "button", class: "ghost", onclick: () => runAction(a) }, ic("search"), " Найти: " + a.query);
    if (a.kind === "play") {
      const known = lastItems.find((x) => x.id === a.id);
      return h("button", { type: "button", class: "ghost", onclick: () => runAction(a) }, ic("play"), " Включить" + (known ? ": " + known.title.slice(0, 40) : ""));
    }
    if (a.kind === "open") return h("button", { type: "button", class: "ghost", onclick: () => runAction(a) }, "Открыть: " + a.label);
    if (a.kind === "more") return h("button", { type: "button", class: "ghost", onclick: () => runAction(a) }, "Показать ещё");
    const names = { like: "Поставить лайк", subscribe: "Подписаться", comment: "Написать комментарий" };
    return h("button", { type: "button", class: "ghost warn", onclick: () => {
      if (!confirm(`${assistant().name} предлагает действие с аккаунтом: «${names[a.kind]}» (${a.arg}). Подтвердить?`)) return;
      status("Действия с аккаунтом YouTube из чата в этой версии не выполняются — сделайте это вручную.", "err");
    } }, (names[a.kind] || a.kind) + "… (нужно подтверждение)");
  }
  /* Безопасные действия (поиск, включить, открыть раздел, «ещё») идут сразу; действия с аккаунтом — только после подтверждения. */
  function autoRun(actions, bar) {
    if (S.ai_autorun === false) return;
    const a = actions.find((x) => SAFE_ACTIONS.includes(x.kind) && (x.kind !== "play" || x.known));
    if (!a) return;
    const idx = actions.indexOf(a);
    setTimeout(() => {
      if (runAction(a) && bar && bar.children[idx]) { bar.children[idx].classList.add("done"); bar.children[idx].title = "Выполнено автоматически"; }
    }, 350);
  }
  async function sendChat(text) {
    bubble("user", text);
    const ai = bubble("ai", "", "typing");
    act("think", "…");
    stopSpeak();
    const items = (current.view === "watch" && current.arg ? [current.arg] : []).concat(lastItems).slice(0, 12);
    const r = await call("assistant_send", text, { view: current.view, title: current.title || "", items });
    if (!r.ok) { ai.classList.remove("typing"); ai.classList.add("err"); ai.textContent = r.error; act("wow", "!"); return; }
    chatJob = r.job;
    $("chatStop").hidden = false;
    const tick = async () => {
      if (chatJob !== r.job) return;
      const x = await call("assistant_poll", r.job);
      if (!x.ok) { ai.classList.add("err"); ai.textContent = x.error; chatJob = null; $("chatStop").hidden = true; return; }
      if (x.text) { ai.classList.remove("typing"); ai.textContent = x.text; }
      $("msgs").scrollTop = $("msgs").scrollHeight;
      if (!x.done) { setTimeout(tick, 120); return; }
      chatJob = null; $("chatStop").hidden = true;
      ai.classList.remove("typing");
      if (x.error) { ai.classList.add("err"); ai.textContent = (x.text ? x.text + "\n\n" : "") + "Ошибка: " + x.error; act("wow", "!"); return; }
      if (!x.text) ai.textContent = "(пустой ответ модели)";
      let bar = null;
      if (x.actions && x.actions.length) { bar = h("div", { class: "m-acts" }, ...x.actions.map(actionButton)); ai.after(bar); }
      act("joy", "♪");
      if (x.text) speak(x.text);
      if (x.actions && x.actions.length) autoRun(x.actions, bar);
      $("msgs").scrollTop = $("msgs").scrollHeight;
    };
    tick();
  }
  function wireChat() {
    $("chatClose").addEventListener("click", () => $("chat").classList.add("closed"));
    $("chatFab").addEventListener("click", openChat);
    $("hero").addEventListener("click", openChat);
    $("hero").setAttribute("title", "Поговорить с помощницей");
    $("chatClear").addEventListener("click", async () => {
      if (!confirm("Очистить переписку с " + assistant().name + "?")) return;
      await call("assistant_clear"); chatLoaded = ""; openChat();
    });
    $("chatStop").addEventListener("click", async () => { if (chatJob) await call("assistant_cancel", chatJob); });
    $("chatMic").addEventListener("click", toggleMic);
    const ttsBtn = $("chatTts");
    ttsBtn.setAttribute("aria-pressed", String(!!S.voice_tts));
    ttsBtn.addEventListener("click", async () => {
      const on = !S.voice_tts;
      if (on && !("speechSynthesis" in window)) { status("Озвучка недоступна: окно не поддерживает синтез речи.", "err"); return; }
      if (await saveSettings({ voice_tts: on })) {
        ttsBtn.setAttribute("aria-pressed", String(on));
        if (on) speak("Теперь я буду говорить вслух.", true); else stopSpeak();
      }
    });
    loadVoices();
    if ("speechSynthesis" in window) speechSynthesis.addEventListener("voiceschanged", loadVoices);
    $("chatForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const text = $("chatIn").value.trim();
      if (!text || chatJob) return;
      $("chatIn").value = "";
      sendChat(text);
    });
  }

  /* ---------- голос: озвучка ответов и микрофон ---------- */
  const tts = { voices: [], warned: false };
  function loadVoices() { if ("speechSynthesis" in window) tts.voices = speechSynthesis.getVoices() || []; }
  function pickVoice() {
    return tts.voices.find((v) => S.voice_name && (v.voiceURI === S.voice_name || v.name === S.voice_name))
      || tts.voices.find((v) => /^ru/i.test(v.lang)) || null;
  }
  function voiceNote(text) { if (!tts.warned) { tts.warned = true; status(text, "err"); } }
  function speakable(text) {
    return String(text || "").replace(/\[\[[^\]]*\]\]/g, "").replace(/https?:\/\/\S+/g, "ссылка")
      .replace(/[*_`#>~]+/g, "").replace(/\p{Extended_Pictographic}/gu, "").replace(/\s+/g, " ").trim();
  }
  function stopSpeak() {
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    $("stage").classList.remove("talking");
  }
  function speak(text, force) {
    if (!(S.voice_tts || force)) return;
    if (!("speechSynthesis" in window)) { voiceNote("Озвучка недоступна: окно не поддерживает синтез речи."); return; }
    loadVoices();
    const v = pickVoice();
    if (!v) { voiceNote("Русского голоса в Windows нет. Добавьте его: Параметры → Время и язык → Речь → Добавить голоса → русский."); return; }
    const t = speakable(text);
    if (!t) return;
    speechSynthesis.cancel();
    // Chromium обрывает длинные тексты — читаем по предложениям
    const parts = (t.match(/[^.!?…]+[.!?…]*/g) || [t]).map((x) => x.trim()).filter(Boolean);
    const [rate, pitch] = assistant().tone || [1, 1];
    parts.forEach((p, k) => {
      const u = new SpeechSynthesisUtterance(p);
      u.voice = v; u.lang = v.lang;
      u.rate = Math.max(0.5, Math.min(2, (S.voice_rate || 1) * rate));
      u.pitch = Math.max(0, Math.min(2, (S.voice_pitch == null ? 1 : S.voice_pitch) * pitch));
      if (k === 0) u.onstart = () => $("stage").classList.add("talking");
      if (k === parts.length - 1) u.onend = u.onerror = () => $("stage").classList.remove("talking");
      speechSynthesis.speak(u);
    });
  }
  const mic = { rec: null, timer: null };
  function micError(e) {
    const n = (e && e.name) || "";
    if (n === "NotAllowedError" || n === "SecurityError") return "Доступ к микрофону запрещён. В Windows: Параметры → Конфиденциальность → Микрофон — разрешите классическим приложениям, затем перезапустите own-tube.";
    if (n === "NotFoundError" || n === "OverconstrainedError") return "Микрофон не найден. Подключите его и попробуйте снова.";
    if (n === "NotReadableError") return "Микрофон занят другой программой.";
    return "Не удалось включить микрофон" + (n ? " (" + n + ")" : "") + ".";
  }
  function blobB64(blob) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result).split(",")[1] || "");
      fr.onerror = () => reject(fr.error);
      fr.readAsDataURL(blob);
    });
  }
  async function toggleMic() {
    const btn = $("chatMic");
    if (mic.rec) { mic.rec.stop(); return; }
    if (chatJob) return;
    if (!navigator.mediaDevices || !window.MediaRecorder) { status("Запись с микрофона недоступна в этом окне.", "err"); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
    catch (e) { const t = micError(e); status(t, "err"); bubble("ai", t, "err"); return; }
    stopSpeak();
    const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "";
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
    const chunks = [];
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = async () => {
      clearTimeout(mic.timer); mic.rec = null;
      stream.getTracks().forEach((t) => t.stop());
      btn.classList.remove("rec"); btn.setAttribute("aria-label", "Говорить (микрофон)");
      const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      if (blob.size < 2500) { status("Запись получилась слишком короткой — скажите ещё раз.", "err"); return; }
      const pending = bubble("me", "…распознаю речь…", "typing");
      let r;
      try { r = await call("stt_transcribe", await blobB64(blob), rec.mimeType || ""); }
      catch (e) { r = { ok: false, error: "Не удалось прочитать запись" }; }
      pending.remove();
      if (!r.ok) { status(r.error, "err"); bubble("ai", "Не получилось распознать: " + r.error, "err"); act("wow", "!"); return; }
      $("chatIn").value = r.text;
      if (S.stt_autosend !== false) { $("chatIn").value = ""; sendChat(r.text); } else $("chatIn").focus();
    };
    rec.start();
    mic.rec = rec;
    btn.classList.add("rec"); btn.setAttribute("aria-label", "Закончить запись");
    status("Говорите… (нажмите на микрофон, чтобы закончить)", "busy");
    act("think", "…");
    mic.timer = setTimeout(() => { if (mic.rec) mic.rec.stop(); }, 60000);
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
      accountButton()];
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
  function accountButton() {
    const g = acct.google || {};
    const av = g.avatar ? h("img", { class: "g", src: img(g.avatar), alt: "" }) : h("span", { class: "g" }, "G");
    return h("button", { class: "account", type: "button", title: "Аккаунты Google и VK", onclick: () => go("settings", "accounts") },
      av, h("span", { class: "t" }, h("b", {}, g.signed_in ? (g.name || "Google: вход выполнен") : "Войти"),
        h("span", {}, g.signed_in ? "подписки и история из YouTube" : "Google и VK — по желанию")));
  }
  async function refreshAccounts() {
    const r = await call("account_status");
    if (r.ok) acct = r.accounts;
    if (acct.google && acct.google.signed_in && !acct.google.name) {
      const p = await call("account_profile");
      if (p.ok && p.profile) { acct.google.name = p.profile.name; acct.google.avatar = p.profile.avatar; }
    }
    buildSide();
    return acct;
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
    const thumb = h("div", { class: "thumb" }, item.thumb && MEDIA ? h("img", { src: img(item.thumb), alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : null);
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
    let pf = null;   // наведение на карточку на 0,35 с — заранее разбираем поток, клик открывается почти мгновенно
    c.addEventListener("pointerenter", () => { if (!prefetched.has(item.id) && !item.live && S.player_mode !== "mpv") pf = setTimeout(() => { prefetched.add(item.id); call("prefetch", item, !!item.short); }, 350); });
    c.addEventListener("pointerleave", () => { clearTimeout(pf); });
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
      mk("link", "В отдельном окне (mpv)", () => playExternal(item)),
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
  function play(item, node) {
    if (S.player_mode === "mpv" || item.live) return playExternal(item, node);  // прямые эфиры — в mpv
    if (item.short) return go("shorts", item, "Shorts");
    go("watch", item, item.title);
  }
  async function playExternal(item, node) {
    if (watchCleanup) { watchCleanup(); watchCleanup = null; }
    if (node) node.setAttribute("aria-busy", "true");
    status("Открываю «" + item.title + "» в отдельном окне…", "busy");
    const r = await call("play_external", item, S.quality);
    if (node) node.removeAttribute("aria-busy");
    if (!r.ok) { status(r.error, "err"); act("wow", "!"); return; }
    status("Играет в mpv" + (r.skips ? ` · SponsorBlock пропустит отрезков: ${r.skips}` : ""));
    act("joy", "♪");
    refreshCounts();
  }
  function fmtTime(t) {
    t = Math.max(0, Math.floor(t || 0));
    const hh = Math.floor(t / 3600), mm = Math.floor((t % 3600) / 60), ss = t % 60;
    return (hh ? hh + ":" + String(mm).padStart(2, "0") : mm) + ":" + String(ss).padStart(2, "0");
  }
  async function showWatch(item) {
    if (!item) return go("home");
    const my = ++seq;
    const vid = h("video", { class: "vid", autoplay: true, playsinline: true, preload: "auto" });
    const stage = h("div", { class: "player" }, vid, h("div", { class: "p-msg", id: "pMsg" }, "Загружаю видео…"));
    const ttl = h("h1", { class: "w-title" }, item.title || "Видео");
    const chan = h("div", { class: "w-chan" });
    const related = h("div", { class: "w-related" }, h("p", { class: "note" }, "Похожие загружаются…"));
    const bar = h("div", { class: "p-bar", role: "slider", "aria-label": "Позиция", tabindex: "0" }, h("i", { class: "p-done" }), h("i", { class: "p-segs" }));
    const tm = h("span", { class: "p-time" }, "0:00 / 0:00");
    const playBtn = h("button", { type: "button", class: "p-btn", "aria-label": "Пауза" }, "❚❚");
    const vol = h("input", { type: "range", min: "0", max: "1", step: "0.05", value: String(S.volume == null ? 1 : 1), "aria-label": "Громкость" });
    const qSel = h("select", { "aria-label": "Качество" }, ...[["best", "Лучшее"], ["1080", "1080p"], ["720", "720p"], ["480", "480p"], ["360", "360p"]].map(([v, t]) => h("option", { value: v }, t)));
    qSel.value = S.quality;
    const fsBtn = h("button", { type: "button", class: "p-btn", "aria-label": "Во весь экран", title: "Во весь экран (F)" }, "⛶");
    const extBtn = h("button", { type: "button", class: "ghost" }, ic("link"), " В отдельном окне (mpv)");
    const controls = h("div", { class: "p-ctl" }, playBtn, tm, bar, vol, qSel, fsBtn);
    const wrap = h("div", { class: "watch" }, h("div", { class: "w-main" }, h("div", { class: "p-wrap" }, stage, controls), ttl, h("div", { class: "w-row" }, chan, h("span", { class: "sp" }), extBtn)), h("aside", { class: "w-side" }, h("h2", { class: "h1" }, h("small", {}, "Похожие")), related));
    view(wrap);
    status("Открываю «" + (item.title || "") + "»…", "busy");
    let info = null, offset = 0, alive = true, skipped = new Set(), full = false;
    const msg = (t) => { const m = $("pMsg"); if (m) { m.textContent = t || ""; m.hidden = !t; } };
    const now = () => offset + (vid.currentTime || 0);
    async function seek(t) {
      if (!info) return;
      t = Math.max(0, Math.min(t, (info.duration || 1e9) - 1));
      const r = await call("seek_url", info.sid, t);
      if (!r.ok || !alive) return;
      offset = t; msg("…");
      vid.src = r.url; vid.play().catch(() => {});
    }
    async function open(q) {
      const pos = info ? now() : 0;
      const r = await call("watch", item, q);
      if (my !== seq || !alive) return;
      if (!r.ok) { status(r.error, "err"); msg(r.error + " — можно открыть в отдельном окне (mpv)."); act("wow", "!"); return; }
      info = r; offset = 0; skipped = new Set();
      ttl.textContent = r.title; current.title = r.title;
      chan.replaceChildren(...[r.channel_id ? h("button", { class: "chan", type: "button", onclick: () => go("channel", r.channel_id, r.channel) }, r.channel) : (r.channel || ""),
        r.height ? h("span", { class: "pill" }, r.height + "p · " + String(r.container || "").toUpperCase()) : null,
        r.segments.length ? h("span", { class: "pill" }, "SponsorBlock: " + r.segments.length) : null].filter((x) => x));
      const segs = bar.querySelector(".p-segs"); segs.replaceChildren();
      if (r.duration) for (const [a, b] of r.segments) { const i = h("b"); i.style.left = (a / r.duration * 100) + "%"; i.style.width = ((b - a) / r.duration * 100) + "%"; segs.append(i); }
      vid.src = r.url;
      vid.play().catch(() => {});
      status(""); act("joy", "♪"); refreshCounts();
      if (pos > 1) seek(pos);
    }
    vid.addEventListener("playing", () => { msg(""); playBtn.textContent = "❚❚"; });
    vid.addEventListener("pause", () => { playBtn.textContent = "▶"; });
    vid.addEventListener("waiting", () => msg("Буферизация…"));
    vid.addEventListener("error", () => { if (alive && info) msg("Встроенный плеер не смог показать это видео. Попробуйте другое качество или «В отдельном окне (mpv)»."); });
    vid.addEventListener("timeupdate", () => {
      const t = now(), d = info ? info.duration : 0;
      tm.textContent = fmtTime(t) + " / " + fmtTime(d);
      if (d) bar.querySelector(".p-done").style.width = Math.min(100, t / d * 100) + "%";
      if (info && S.sponsorblock) for (const [a, b] of info.segments) {
        const key = a + "-" + b;
        if (t >= a && t < b - 0.5 && !skipped.has(key)) { skipped.add(key); status("SponsorBlock: пропущен отрезок " + fmtTime(a) + "–" + fmtTime(b)); seek(b); break; }
      }
    });
    vid.addEventListener("ended", () => { playBtn.textContent = "▶"; });
    playBtn.addEventListener("click", () => { if (vid.paused) vid.play().catch(() => {}); else vid.pause(); });
    vid.addEventListener("click", () => playBtn.click());
    bar.addEventListener("click", (e) => { if (!info || !info.duration) return; const r = bar.getBoundingClientRect(); seek((e.clientX - r.left) / r.width * info.duration); });
    vol.addEventListener("input", () => { vid.volume = Number(vol.value); vid.muted = false; });
    qSel.addEventListener("change", () => open(qSel.value));
    function setFull(on) {
      full = on; document.body.classList.toggle("fullplayer", on);
      call("fullscreen", on);
    }
    fsBtn.addEventListener("click", () => setFull(!full));
    vid.addEventListener("dblclick", () => setFull(!full));
    extBtn.addEventListener("click", () => { const t = now(); playExternal(item); msg("Играет в отдельном окне"); void t; });
    const keys = (e) => {
      if (["INPUT", "SELECT", "TEXTAREA"].includes((e.target.tagName || ""))) return;
      if (e.key === " " || e.key === "k") { e.preventDefault(); playBtn.click(); }
      else if (e.key === "f") setFull(!full);
      else if (e.key === "Escape" && full) setFull(false);
      else if (e.key === "ArrowRight") seek(now() + 10);
      else if (e.key === "ArrowLeft") seek(now() - 10);
      else if (e.key === "m") vid.muted = !vid.muted;
    };
    document.addEventListener("keydown", keys);
    watchCleanup = () => {
      alive = false;
      document.removeEventListener("keydown", keys);
      if (full) setFull(false);
      vid.pause(); vid.removeAttribute("src"); vid.load();
      call("stop_watch");
    };
    open(qSel.value);
    call("related", item).then((r) => { if (my === seq && alive) related.replaceChildren(r.ok ? grid(r.items, { empty: "Похожих не нашлось." }) : h("p", { class: "note" }, r.error)); });
  }
  /* ---------- Shorts: вертикальный плеер с листанием ---------- */
  async function showShorts(startItem) {
    const my = ++seq;
    let alive = true, idx = 0, items = [], cursor = null, loadingMore = false, muted = false, wantNext = false, lastStep = 0, skipTimer = null;
    const seen = new Set();
    const slots = new Map();
    const stage = h("div", { class: "sh-stage" });
    const chEl = h("b", { class: "sh-ch" });
    const ttEl = h("div", { class: "sh-ttl" });
    const msgEl = h("div", { class: "sh-msg" }, "Загружаю Shorts…");
    const countEl = h("span", { class: "sh-count" });
    const mk = (label, text, fn, extra) => h("button", { type: "button", class: "sh-btn" + (extra ? " " + extra : ""), "aria-label": label, title: label, onclick: fn }, text);
    const prevB = mk("Предыдущий (↑)", "▲", () => step(-1));
    const nextB = mk("Следующий (↓)", "▼", () => step(1));
    const muteB = mk("Звук (M)", "🔊", () => toggleMute());
    const pauseB = mk("Пауза (пробел)", "❚❚", () => togglePause());
    const extB = mk("Открыть в отдельном окне (mpv)", "mpv", () => { const s0 = items[idx]; if (s0) playExternal(s0); });
    const side = h("div", { class: "sh-side" }, prevB, nextB, pauseB, muteB, extB, countEl);
    const frame = h("div", { class: "sh-frame" }, stage, h("div", { class: "sh-info" }, chEl, ttEl), msgEl);
    const hint = h("p", { class: "note sh-hint" }, "Колесо мыши, ↑ ↓, свайп — следующий и предыдущий; пробел — пауза; M — звук.");
    view(h("div", { class: "shorts-view" }, h("div", { class: "sh-row" }, frame, side), hint));
    status("");

    const cur = () => slots.get(idx);
    const setMsg = (t) => { msgEl.textContent = t || ""; msgEl.hidden = !t; };
    function addItems(list) {
      let n = 0;
      for (const i of list || []) { if (!i || seen.has(i.id)) continue; seen.add(i.id); items.push(i); n++; }
      lastItems = items;
      return n;
    }
    function slotFor(i) {
      if (slots.has(i)) return slots.get(i);
      const item = items[i];
      if (!item) return null;
      const vid = h("video", { class: "sh-vid", playsinline: true, loop: true, preload: "auto" });
      vid.muted = muted;
      const sl = { i, item, vid, sid: null, err: "", ready: false, info: null };
      slots.set(i, sl);
      call("watch", item, null, true).then((r) => {
        if (!alive || slots.get(i) !== sl) { if (r.ok) call("drop_watch", r.sid); return; }
        if (!r.ok) { sl.err = r.error; if (i === idx) { paint(); scheduleSkip(); } return; }
        sl.sid = r.sid; sl.info = r; sl.ready = true;
        vid.src = r.url;
        if (i === idx) { paint(); playCurrent(); }
      });
      vid.addEventListener("playing", () => { if (slots.get(i) === sl && i === idx) setMsg(""); });
      vid.addEventListener("waiting", () => { if (slots.get(i) === sl && i === idx) setMsg("Буферизация…"); });
      vid.addEventListener("error", () => { if (slots.get(i) === sl && sl.ready) { sl.err = "Не удалось показать этот ролик"; if (i === idx) { paint(); scheduleSkip(); } } });
      return sl;
    }
    function paint() {
      const sl = cur();
      if (!sl) return;
      if (stage.firstChild !== sl.vid) stage.replaceChildren(sl.vid);
      const d = sl.info || {};
      chEl.textContent = d.channel || sl.item.channel || "";
      ttEl.textContent = d.title || sl.item.title || "";
      countEl.textContent = (idx + 1) + (cursor ? "" : " / " + items.length);
      prevB.disabled = idx === 0;
      setMsg(sl.err ? sl.err + " — перехожу к следующему…" : (!sl.ready ? "Загружаю…" : (sl.vid.paused ? "" : "")));
    }
    function playCurrent() {
      const sl = cur();
      if (!sl || !sl.ready || sl.err) return;
      sl.vid.muted = muted;
      sl.vid.play().then(() => { pauseB.textContent = "❚❚"; }).catch(() => setMsg("Нажмите на видео, чтобы запустить"));
    }
    function scheduleSkip() {
      clearTimeout(skipTimer);
      skipTimer = setTimeout(() => { if (alive && cur() && cur().err && idx + 1 < items.length) step(1, true); }, 1400);
    }
    function togglePause() {
      const sl = cur();
      if (!sl || !sl.ready) return;
      if (sl.vid.paused) { sl.vid.play().catch(() => {}); pauseB.textContent = "❚❚"; setMsg(""); } else { sl.vid.pause(); pauseB.textContent = "▶"; setMsg("Пауза"); }
    }
    function toggleMute() { muted = !muted; muteB.textContent = muted ? "🔇" : "🔊"; const sl = cur(); if (sl) sl.vid.muted = muted; }
    function drop(k) {
      const sl = slots.get(k);
      if (!sl) return;
      slots.delete(k);
      sl.vid.pause(); sl.vid.removeAttribute("src"); sl.vid.load();
      if (sl.sid) call("drop_watch", sl.sid);
    }
    async function ensureMore() {
      if (loadingMore || !cursor) return;
      loadingMore = true;
      let r = await call("feed", "shorts", null, cursor, "fresh");
      if (!r.ok) r = await call("feed", "shorts", null, null, "fresh");   // токен устарел — берём свежую подборку
      loadingMore = false;
      if (!alive) return;
      if (r.ok) { addItems(r.items); cursor = r.more || null; }
      else setMsg(r.error);
      countEl.textContent = (idx + 1) + (cursor ? "" : " / " + items.length);
      if (wantNext && idx + 1 < items.length) { wantNext = false; step(1, true); }
    }
    function step(dir, force) {
      const now = Date.now();
      if (!force && now - lastStep < 450) return;
      const j = idx + dir;
      if (j < 0) return;
      if (j >= items.length) { wantNext = true; setMsg("Подбираю ещё…"); ensureMore(); return; }
      lastStep = now; clearTimeout(skipTimer);
      const old = cur(); if (old) old.vid.pause();
      idx = j;
      stage.classList.remove("to-next", "to-prev"); void stage.offsetWidth; stage.classList.add(dir > 0 ? "to-next" : "to-prev");
      slotFor(idx); paint(); playCurrent();
      slotFor(idx + 1);                                          // следующий ролик грузится заранее
      if (items[idx + 2] && !prefetched.has(items[idx + 2].id)) { prefetched.add(items[idx + 2].id); call("prefetch", items[idx + 2], true); }
      for (const k of [...slots.keys()]) if (k < idx - 1 || k > idx + 1) drop(k);
      if (items.length - idx < 6) ensureMore();
    }
    // колесо, клавиши, свайп
    let wheelAt = 0;
    frame.addEventListener("wheel", (e) => { e.preventDefault(); const t = Date.now(); if (t - wheelAt < 600 || Math.abs(e.deltaY) < 4) return; wheelAt = t; step(e.deltaY > 0 ? 1 : -1); }, { passive: false });
    let down = null;
    stage.addEventListener("pointerdown", (e) => { down = { y: e.clientY, x: e.clientX, t: Date.now() }; try { stage.setPointerCapture(e.pointerId); } catch (_) { /* не критично */ } });
    stage.addEventListener("pointerup", (e) => {
      if (!down) return;
      const dy = down.y - e.clientY, dx = down.x - e.clientX; down = null;
      if (Math.abs(dy) > 50 && Math.abs(dy) > Math.abs(dx)) step(dy > 0 ? 1 : -1);
      else if (Math.abs(dy) < 8 && Math.abs(dx) < 8) togglePause();
    });
    stage.addEventListener("pointercancel", () => { down = null; });
    const keys = (e) => {
      if (["INPUT", "SELECT", "TEXTAREA"].includes((e.target.tagName || ""))) return;
      if (e.key === "ArrowDown" || e.key === "PageDown" || e.key === "j") { e.preventDefault(); step(1); }
      else if (e.key === "ArrowUp" || e.key === "PageUp" || e.key === "k") { e.preventDefault(); step(-1); }
      else if (e.key === " ") { e.preventDefault(); togglePause(); }
      else if (e.key === "m") toggleMute();
    };
    document.addEventListener("keydown", keys);
    watchCleanup = () => {
      alive = false; clearTimeout(skipTimer);
      document.removeEventListener("keydown", keys);
      for (const k of [...slots.keys()]) drop(k);
      call("stop_watch");
    };

    // список: стартовый ролик (если открыли конкретный) и свежая подборка
    if (startItem && startItem.id) addItems([startItem]);
    if (items.length) { slotFor(0); paint(); }
    const r = await call("feed", "shorts", null, null, "fresh");
    if (!alive || my !== seq) return;
    if (!r.ok) { if (!items.length) setMsg(r.error); return; }
    addItems(r.items); cursor = r.more || null;
    if (!items.length) { setMsg("Shorts не нашлись. Попробуйте позже."); return; }
    if (!cur()) { slotFor(0); paint(); }
    slotFor(1);
    if (items[2] && !prefetched.has(items[2].id)) { prefetched.add(items[2].id); call("prefetch", items[2], true); }
    countEl.textContent = "1" + (cursor ? "" : " / " + items.length);
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
  function skeletonCards(n) {
    return Array.from({ length: n }, () => h("div", { class: "card sk", "aria-hidden": "true" },
      h("div", { class: "thumb" }), h("div", { class: "meta" }, h("div", { class: "title" }), h("div", { class: "sub" }))));
  }
  /* Список с догрузкой: сначала сохранённое (мгновенно), потом свежее; дальше — по прокрутке и кнопке «Показать ещё».
     fetchPage(cursor, mode) -> ответ API {ok, items, more}; mode: "cache" (только сохранённое) или "fresh". */
  function pagedGrid(fetchPage, opts) {
    opts = opts || {};
    const my = seq;
    const gridEl = h("div", { class: "grid" + (opts.shorts ? " shorts" : "") }, ...skeletonCards(opts.skeletons || 12));
    const moreBtn = h("button", { type: "button", class: "ghost more-btn", hidden: true }, "Показать ещё");
    const note = h("p", { class: "note pg-note", hidden: true });
    const sentinel = h("div", { class: "sentinel", "aria-hidden": "true" });
    const wrap = h("div", { class: "paged" }, gridEl, h("div", { class: "pager" }, moreBtn, note), sentinel);
    let seen = new Set(), total = [], cursor = null, busy = false, io = null;
    const alive = () => my === seq;
    const setNote = (t, err) => { note.hidden = !t; note.textContent = t || ""; note.classList.toggle("err", !!err); };
    const add = (items) => {
      let n = 0;
      for (const i of items || []) {
        const k = (i.source || "yt") + ":" + i.id;
        if (seen.has(k)) continue;
        seen.add(k); total.push(i); gridEl.append(card(i, opts)); n++;
      }
      lastItems = total;
      return n;
    };
    const sync = () => { moreBtn.hidden = !cursor; };
    const nearEnd = () => cursor && !busy && sentinel.getBoundingClientRect().top < window.innerHeight + 900;
    async function loadMore() {
      if (busy || !cursor || !alive()) return;
      busy = true; moreBtn.disabled = true; moreBtn.textContent = "Загружаю…";
      let got = 0;
      for (let tries = 0; cursor && got === 0 && tries < 3; tries++) {
        const r = await fetchPage(cursor, "fresh");
        if (!alive()) return;
        if (!r.ok) { setNote(r.error + " — нажмите «Показать ещё», чтобы повторить.", true); break; }
        got += add(r.items); cursor = r.more || null; setNote("");
      }
      busy = false; moreBtn.disabled = false; moreBtn.textContent = "Показать ещё"; sync(); status("");
      if (got && nearEnd()) setTimeout(loadMore, 60);
    }
    moreBtn.addEventListener("click", loadMore);
    async function begin() {
      let shown = false;
      if (opts.cache !== false) {
        const c = await fetchPage(null, "cache");
        if (!alive()) return;
        if (c.ok && c.items && c.items.length) {
          gridEl.replaceChildren(); add(c.items); cursor = null; shown = true;
          if (opts.onMeta) opts.onMeta(c);
          setNote("Показано сохранённое, обновляю…");
        }
      }
      const r = await fetchPage(null, "fresh");
      if (!alive()) return;
      if (!r.ok) {
        if (!shown) gridEl.replaceChildren(h("p", { class: "empty" }, r.error));
        setNote(shown ? "Не удалось обновить: " + r.error : "", shown);
        status(shown ? "" : r.error, shown ? "" : "err");
        return;
      }
      const same = shown && r.items.length && r.items.slice(0, 6).every((x, i) => total[i] && total[i].id === x.id);
      if (!same) { gridEl.replaceChildren(); seen = new Set(); total = []; }
      add(r.items); cursor = r.more || null; setNote(""); status("");
      if (opts.onMeta) opts.onMeta(r);
      if (!total.length) gridEl.replaceChildren(h("p", { class: "empty" }, opts.empty || "Ничего не найдено."));
      sync();
      if ("IntersectionObserver" in window) {
        io = new IntersectionObserver((es) => { if (!alive()) { io.disconnect(); return; } if (es.some((e) => e.isIntersecting)) loadMore(); }, { rootMargin: "900px 0px" });
        io.observe(sentinel);
      }
      if (nearEnd()) setTimeout(loadMore, 60);
    }
    activePager = { loadMore: () => loadMore(), alive };
    status("Загружаю…", "busy");
    begin();
    return wrap;
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
    if (watchCleanup) { watchCleanup(); watchCleanup = null; }
    current = { view: viewName, arg: arg || null, title: label || "" };
    if (dlTimer) { clearInterval(dlTimer); dlTimer = null; }
    closeMenu(); markNav();
    setSource(viewName === "vk" ? "vk" : (viewName === "search" ? source : "yt"), true);
    window.scrollTo(0, 0);
    reload();
  }
  function reload() {
    const v = current.view;
    ++seq; activePager = null;
    if (v === "home") {
      const sub = h("small", {});
      return view(title("Главная", sub), pagedGrid((c, m) => call("feed", "home", null, c, m), {
        onMeta: (r) => { sub.textContent = r.from === "subs" ? "новое с ваших подписок" : "популярное за неделю — подпишитесь на каналы, и здесь будут они"; } }));
    }
    if (v === "shorts") return showShorts(current.arg);
    if (v === "subs") return showSubs();
    if (v.startsWith("cat:")) { const k = v.slice(4); return view(title(CAT[k]), pagedGrid((c, m) => call("feed", "category", k, c, m))); }
    if (v === "channel") { const cid = current.arg; return view(title(current.title || "Канал",
      h("span", { class: "sp" }), h("button", { class: "ghost", type: "button", onclick: () => subscribe(cid, current.title, true) }, "Подписаться")),
      pagedGrid((c, m) => call("feed", "channel", cid, c, m))); }
    if (v === "history") return load(() => call("library", "history"), (r) => view(title("История", h("span", { class: "sp" }), acctBtn("history", "История"),
      h("button", { class: "ghost", type: "button", onclick: async () => { if (confirm("Очистить всю историю просмотра?")) { await call("clear_history"); reload(); refreshCounts(); } } }, "Очистить")),
      grid(r.items, { empty: "Вы ещё ничего не смотрели. Историю из YouTube можно перенести через Google Takeout в настройках." })));
    if (v === "later" || v === "favorite") return load(() => call("library", v), (r) => view(title(v === "later" ? "Смотреть позже" : "Избранное", h("span", { class: "sp" }), v === "later" ? acctBtn("later", "Смотреть позже") : acctBtn("liked", "Понравившиеся")),
      grid(r.items, { mark: v, empty: "Добавляйте видео через меню «⋯» на карточке." })));
    if (v === "playlists") return showPlaylists();
    if (v === "playlist") return load(() => call("library", "playlist", current.arg), (r) => view(title(current.title,
      h("span", { class: "sp" }), h("button", { class: "ghost", type: "button", onclick: async () => { if (confirm("Удалить плейлист «" + current.title + "»?")) { await call("delete_playlist", current.arg); go("playlists"); refreshCounts(); } } }, "Удалить плейлист")),
      grid(r.items, { playlistId: current.arg, empty: "Плейлист пуст." })));
    if (v === "downloads") return showDownloads();
    if (v === "vk") return showVk();
    if (v === "search") return doSearch(current.arg);
    if (v === "settings") return showSettings(current.arg);
    if (v === "watch") return showWatch(current.arg);
    if (v === "acct") return showAccountFeed(current.arg, current.title);
    if (v === "acctpl") return load(() => call("account_feed", "playlist", current.arg), (r) => view(title(current.title || "Плейлист", h("small", {}, "из аккаунта YouTube")), grid(r.items)));
  }
  function acctBtn(kind, label) {
    if (!acct.google || !acct.google.signed_in) return null;
    return h("button", { class: "ghost", type: "button", onclick: () => go("acct", kind, label) }, ic("user"), " " + label + " из аккаунта");
  }
  async function showAccountFeed(kind, label) {
    if (kind === "playlists") {
      return load(() => call("account_feed", "playlists"), (r) => view(title("Плейлисты", h("small", {}, "из аккаунта YouTube")),
        r.playlists.length ? h("div", { class: "grid" }, ...r.playlists.map((p) => {
          const c = h("div", { class: "card pl-card", role: "button", tabindex: "0" },
            h("div", { class: "thumb" }, p.thumb && MEDIA ? h("img", { src: img(p.thumb), alt: "" }) : ic("list"), p.count ? h("span", { class: "count" }, String(p.count)) : null),
            h("div", { class: "meta" }, h("div", { class: "title" }, p.title)));
          c.addEventListener("click", () => go("acctpl", p.id, p.title));
          return c;
        })) : h("p", { class: "empty" }, "В аккаунте нет плейлистов.")));
    }
    return load(() => call("account_feed", kind), (r) => view(title(label || "Аккаунт", h("small", {}, "из аккаунта YouTube")), grid(r.items, { empty: "Пусто. Если вы только что вошли, YouTube мог попросить подтвердить вход — попробуйте позже." })));
  }

  async function showSubs() {
    const my0 = seq;
    const r = await call("library", "subscriptions");
    if (my0 !== seq) return;
    const chans = r.ok ? r.channels : [];
    const chips = h("div", { class: "chips" }, ...chans.map((c) => h("span", { class: "chip" },
      h("button", { class: "chan", type: "button", onclick: () => go("channel", c.channel_id, c.title) }, c.title),
      h("button", { class: "x", type: "button", "aria-label": "Отписаться от " + c.title, title: "Отписаться", onclick: () => subscribe(c.channel_id, c.title, false) }, "×"))));
    if (!chans.length) {
      view(title("Подписки", h("span", { class: "sp" }), acctBtn("subs", "Подписки")), h("p", { class: "empty" }, "Подписок пока нет.", h("br"),
        "Подпишитесь через меню «⋯» на карточке или ", h("b", {}, "перенесите подписки из YouTube"), " через Google Takeout.", h("br"), h("br"),
        h("button", { class: "go", type: "button", onclick: () => go("settings", "data") }, "Импорт из Takeout")));
      return;
    }
    view(title("Подписки", h("small", {}, `каналов: ${chans.length}`), h("span", { class: "sp" }), acctBtn("subs", "Подписки")), chips, h("div", { id: "subsFeed" }));
    const f = $("subsFeed");
    if (f) f.replaceChildren(pagedGrid((c, m) => call("feed", "subs", null, c, m), { empty: "Новых видео от подписок пока нет." }));
  }

  async function showPlaylists() {
    const r = await call("library", "playlists");
    const pls = r.ok ? r.playlists : [];
    const add = h("button", { class: "ghost", type: "button", onclick: async () => {
      const name = prompt("Название нового плейлиста"); if (!name) return;
      const x = await call("create_playlist", name); if (!x.ok) status(x.error, "err"); reload(); refreshCounts();
    } }, "Новый плейлист");
    view(title("Плейлисты", h("span", { class: "sp" }), acctBtn("playlists", "Плейлисты"), add), pls.length ? h("div", { class: "grid" }, ...pls.map((p) => {
      const thumb = h("div", { class: "thumb" }, p.thumb && MEDIA ? h("img", { src: img(p.thumb), alt: "" }) : ic("list"), h("span", { class: "count" }, String(p.count)));
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
        h("b", {}, "VK Видео"),
        h("p", {}, (acct.vk && acct.vk.signed_in ? "Вход в VK выполнен. " : "Без входа поиск работает как у гостя. ") +
          "Ищите сверху или вставьте ссылку vkvideo.ru / vk.com. Ролики 18+ — только после входа в VK и с включённым переключателем."),
        h("div", { class: "row2" },
          acct.vk && acct.vk.signed_in ? h("label", { class: "check" }, (() => { const cb = h("input", { type: "checkbox" }); cb.checked = !!S.vk_adult; cb.addEventListener("change", () => saveSettings({ vk_adult: cb.checked })); return cb; })(), "Показывать 18+ в поиске")
            : h("button", { class: "ghost", type: "button", onclick: () => go("settings", "accounts") }, "Войти в VK")))),
      h("h2", { class: "h1" }, h("small", {}, "Недавно смотрели в VK")),
      grid(vkItems, { empty: "Здесь появятся ролики VK, которые вы открывали." }));
    status("");
  }

  function doSearch(q) {
    const src = source;
    view(title((src === "vk" ? "VK: " : "Поиск: ") + q), pagedGrid((c, m) => call("search", q, src, c, m), { empty: "Ничего не нашлось. Попробуйте другой запрос." }));
  }

  function setSource(s, silent) {
    source = s;
    $("tabYt").setAttribute("aria-selected", String(s === "yt"));
    $("tabVk").setAttribute("aria-selected", String(s === "vk"));
    $("q").placeholder = s === "vk" ? "Поиск в VK Видео или ссылка vkvideo.ru/video-…" : "Поиск на YouTube или ссылка";
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


  function hostPort(url) {
    return (url || "").replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
  }

  async function showLog() {
    const pre = h("pre", { class: "log" }, "Загружаю…");
    const dlg = h("dialog", { class: "log-dlg", "aria-label": "Журнал" }, h("h2", {}, "Журнал own-tube"),
      h("p", { class: "note" }, "Пароли, ключи и логины в адресах вырезаны. Можно скопировать и прислать при проблемах."), pre,
      h("div", { class: "actions" },
        h("button", { class: "ghost", type: "button", onclick: () => fill() }, "Обновить"),
        h("button", { class: "ghost", type: "button", onclick: () => { navigator.clipboard && navigator.clipboard.writeText(pre.textContent); status("Журнал скопирован"); } }, "Копировать"),
        h("button", { class: "go", type: "button", onclick: () => { dlg.close(); dlg.remove(); } }, "Закрыть")));
    async function fill() { const r = await call("read_log", 400); pre.textContent = r.ok ? (r.lines.join("\n") || "Журнал пуст") : r.error; pre.scrollTop = pre.scrollHeight; }
    document.body.append(dlg); dlg.showModal(); fill();
  }
  function buildNetPanel(netMsg) {
    const steps = h("ol", { class: "steps" });
    let proxyType = S.proxy_type || (String(S.proxy || "").startsWith("http") ? "http" : "socks5");
    const typeSeg = h("div", { class: "seg", role: "radiogroup", "aria-label": "Тип прокси" },
      ...[["socks5", "SOCKS5"], ["http", "HTTP"]].map(([id, label]) => {
        const b = h("button", { type: "button", role: "radio", "aria-checked": String(proxyType === id), "data-type": id }, label);
        b.addEventListener("click", () => {
          proxyType = id;
          typeSeg.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
        });
        return b;
      }));
    const proxy = h("input", { type: "text", value: hostPort(S.proxy), placeholder: "127.0.0.1:1080", "aria-label": "Адрес и порт прокси", spellcheck: "false", autocomplete: "off" });
    const user = h("input", { type: "text", value: S.proxy_user || "", placeholder: "если прокси требует вход", "aria-label": "Логин прокси", spellcheck: "false", autocomplete: "off" });
    const pass = h("input", { type: "password", value: "", placeholder: S.proxy_password_set ? "сохранён, введите новый чтобы заменить" : "пароль прокси", "aria-label": "Пароль прокси", autocomplete: "new-password" });
    async function saveProxy() {
      const patch = { proxy: proxy.value, proxy_type: proxyType, proxy_user: user.value };
      if (pass.value) patch.proxy_password = pass.value;
      const r = await call("save_settings", patch);
      if (!r.ok) {
        netMsg.textContent = r.error || "Не удалось сохранить";
        netMsg.className = "note bad-text";
        status(r.error, "err");
        return false;
      }
      S = r.settings;
      pass.value = "";
      pass.placeholder = S.proxy_password_set ? "сохранён, введите новый чтобы заменить" : "пароль прокси";
      proxy.value = hostPort(S.proxy);
      const shown = hostPort(S.proxy);
      netMsg.textContent = S.proxy
        ? ("Сохранено: " + (String(S.proxy).startsWith("http") ? "HTTP " : "SOCKS5 ") + shown)
        : "Сохранено: прокси выключен, соединение напрямую";
      netMsg.className = "note ok-text";
      if (S.proxy_password_note && S.proxy_password_set && !S.proxy_password_persist) {
        netMsg.textContent += ". " + S.proxy_password_note;
      }
      updateRouteChip();
      return true;
    }
    const clearBtn = h("button", { class: "ghost", type: "button", onclick: async () => {
      const r = await call("save_settings", { clear_proxy_password: true });
      if (!r.ok) { netMsg.textContent = r.error || ""; netMsg.className = "note bad-text"; return; }
      S = r.settings;
      pass.value = "";
      pass.placeholder = "пароль прокси";
      netMsg.textContent = "Пароль удалён";
      netMsg.className = "note";
    } }, "Удалить пароль");
    if (!S.proxy_password_set) clearBtn.hidden = true;
    return panel("net", "globe", "Сеть: прокси и VPN",
      "Через прокси идёт только трафик own-tube. Адрес можно указать как хост:порт — по умолчанию это SOCKS5. Для SOCKS-прокси плеер mpv работает через встроенный мост HTTP→SOCKS. Пароль хранится отдельно и шифруется средствами Windows.",
      field("Тип", typeSeg),
      field("Адрес и порт", proxy),
      field("Логин", user),
      field("Пароль", pass),
      h("div", { class: "row2" },
        h("button", { class: "go", type: "button", onclick: saveProxy }, "Сохранить"),
        h("button", { class: "ghost", type: "button", onclick: async () => {
          if (!await saveProxy()) return;
          netMsg.textContent = "Проверяю связь…";
          netMsg.className = "note";
          const r = await call("check_route");
          netMsg.textContent = r.detail || r.error || "";
          netMsg.className = "note " + (r.ok ? "ok-text" : "bad-text");
          steps.replaceChildren(...(r.steps || []).map((x) => h("li", { class: x.ok ? "ok-text" : "bad-text" },
            (x.ok ? "✓ " : "✗ ") + x.name + " — " + x.detail + " (" + x.ms + " мс)")));
        } }, "Проверить связь"),
        h("button", { class: "ghost", type: "button", onclick: showLog }, "Журнал"),
        clearBtn),
      netMsg, steps,
      h("p", { class: "note" }, S.proxy_password_note || "Пароль не записывается в settings.json."));
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " Б";
    if (n < 1048576) return (n / 1024).toFixed(1) + " КБ";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " МБ";
    return (n / 1073741824).toFixed(2) + " ГБ";
  }

  /* Встроенный VPN AmneziaWG: туннель только для own-tube, без системного адаптера и прав администратора */
  function buildVpnPanel() {
    const msg = h("p", { class: "note", id: "vpnMsg" });
    const info = h("div", { class: "vpn-info", id: "vpnInfo" });
    const stat = h("p", { class: "note vpn-stat", id: "vpnStat" });
    const input = h("textarea", { class: "vpn-input", rows: "3", placeholder: "Вставьте ссылку vpn://… из AmneziaVPN или текст конфигурации AmneziaWG ([Interface] … [Peer] …)",
      "aria-label": "Ссылка или конфигурация AmneziaWG (скрыта при вводе)", spellcheck: "false", autocomplete: "off" });
    let state = null;
    const modeSeg = h("div", { class: "seg", role: "radiogroup", "aria-label": "Как выходить в сеть" });
    function say(text, kind) { msg.textContent = text || ""; msg.className = "note " + (kind === "ok" ? "ok-text" : kind === "bad" ? "bad-text" : ""); }
    function renderMode() {
      const on = !!(state && state.enabled);
      modeSeg.replaceChildren(...[[false, "Прокси из настроек / напрямую"], [true, "Встроенный VPN"]].map(([v, t]) => {
        const b = h("button", { type: "button", role: "radio", "aria-checked": String(on === v), "data-vpn": String(v) }, t);
        b.addEventListener("click", () => setMode(v));
        return b;
      }));
    }
    function render() {
      renderMode();
      const c = state && state.config;
      info.replaceChildren(c
        ? h("div", { class: "row asst" }, h("span", { class: "dot " + (state.status && state.status.running ? "ok" : "") }),
            h("div", { class: "nm" }, h("b", {}, c.name), h("span", {}, c.kind + " · сервер " + c.server + (c.obf.length ? " · " + c.obf.join(", ") : "") + " · DNS через туннель")),
            h("button", { class: "ghost", type: "button", onclick: restart }, "Перезапустить"),
            h("button", { class: "ghost", type: "button", onclick: forget }, "Удалить"))
        : h("p", { class: "note" }, "Конфигурация не импортирована."));
      const st = (state && state.status) || {};
      if (!state || !state.config) stat.textContent = "";
      else if (!st.running) stat.textContent = state.enabled ? ("Туннель не запущен" + (st.error ? ": " + st.error : "")) : "Выключен — трафик идёт через прокси из настроек или напрямую.";
      else {
        const hs = st.handshake_age_sec;
        stat.textContent = "Работает · рукопожатие: " + (hs == null || hs < 0 ? "ещё не было" : hs + " с назад") +
          " · ↓ " + fmtBytes(st.rx) + " ↑ " + fmtBytes(st.tx) +
          (st.latency_ms != null ? " · до YouTube " + st.latency_ms + " мс" : "") + (st.probe_error ? " · " + st.probe_error : "");
      }
      stat.className = "note vpn-stat " + (st.running ? "ok-text" : "");
      if (state && !state.helper) say("Нет tools\\awgproxy.exe рядом с программой — встроенный VPN недоступен.", "bad");
    }
    async function refresh(probe) {
      const r = await call("vpn_state", !!probe);
      if (r.ok) { state = r; render(); }
    }
    async function setMode(on) {
      say(on ? "Поднимаю туннель…" : "Выключаю…");
      const r = await call("vpn_enable", on);
      if (!r.ok) { say(r.error, "bad"); return; }
      state = r; if (r.settings) S = r.settings;
      render(); updateRouteChip();
      say(on ? "Встроенный VPN включён: весь трафик own-tube идёт через туннель." : "VPN выключен.", "ok");
    }
    async function doImport(r) {
      if (r.cancelled) return;
      if (!r.ok) { say(r.error, "bad"); return; }
      input.value = "";
      state = r; render();
      say("Конфигурация импортирована и зашифрована." + (r.persist ? "" : " Внимание: хранилище секретов недоступно — до закрытия программы."), "ok");
    }
    async function restart() { say("Перезапускаю…"); const r = await call("vpn_restart"); if (r.ok) { state = r; render(); say("Туннель перезапущен", "ok"); } else say(r.error, "bad"); }
    async function forget() {
      const r = await call("vpn_forget");
      if (!r.ok) { say(r.error, "bad"); return; }
      state = r; if (r.settings) S = r.settings; render(); updateRouteChip(); say("Конфигурация удалена", "ok");
    }
    const timer = setInterval(() => { if (!document.body.contains(stat)) { clearInterval(timer); return; } if (state && state.status && state.status.running) refresh(false); }, 5000);
    refresh(false);
    return panel("vpn", "globe", "Встроенный VPN (AmneziaWG)",
      "Туннель только для own-tube: остальные программы и браузер идут как обычно. Не нужны права администратора и отдельный клиент. Подходит та же ссылка vpn://, что и для AmneziaVPN (протокол AmneziaWG), или файл .conf.",
      field("Как выходить в сеть", modeSeg),
      field("Импорт", input),
      h("div", { class: "row2" },
        h("button", { class: "go", type: "button", onclick: async () => { const t = input.value; input.value = ""; const r = await call("vpn_import_text", t); if (!r.ok) input.value = t; doImport(r); } }, "Импортировать"),
        h("button", { class: "ghost", type: "button", onclick: (e) => { const shown = input.classList.toggle("shown"); e.currentTarget.textContent = shown ? "Скрыть" : "Показать"; } }, "Показать"),
        h("button", { class: "ghost", type: "button", onclick: async () => doImport(await call("vpn_import_file")) }, ic("upload"), " Файл .conf…"),
        h("button", { class: "ghost", type: "button", onclick: () => refresh(true) }, "Обновить состояние")),
      info, stat, msg,
      h("p", { class: "note" }, "Ключи хранятся только в зашифрованном хранилище Windows (DPAPI): их нет в settings.json и в журнале. «Проверить связь» в разделе выше работает и через VPN."));
  }

  function buildUpdatePanel(version) {
    const msg = h("p", { class: "note" });
    const box = h("div", { class: "upd-box" });
    const bar = h("progress", { class: "upd-bar", max: "100", value: "0", hidden: true });
    let info = null, poll = null;
    function say(text, kind) { msg.textContent = text || ""; msg.className = "note " + (kind === "ok" ? "ok-text" : kind === "bad" ? "bad-text" : ""); }
    function render() {
      box.replaceChildren();
      if (!info) return;
      if (!info.available) {
        box.append(h("p", { class: "note ok-text" }, "У вас последняя версия (" + info.current + ")."));
      } else {
        box.append(h("p", {}, h("b", {}, "Доступна own-tube " + info.version), info.published ? " · " + new Date(info.published).toLocaleDateString("ru-RU") : ""));
        box.append(h("div", { class: "upd-notes" }, ...(info.notes ? info.notes.split("\n").map((line) => {
          const t = line.replace(/\*\*/g, "").replace(/`/g, "");
          return /^#{1,6}\s/.test(t) ? h("b", { class: "upd-h" }, t.replace(/^#+\s*/, "")) : h("div", {}, t.replace(/^\s*[-*]\s+/, "• ") || "\u00a0");
        }) : ["Описание — на странице релиза."])));
        const row = h("div", { class: "row2" });
        if (info.frozen && info.has_assets) row.append(h("button", { class: "go", type: "button", onclick: download }, ic("download"), " Скачать и обновить"));
        row.append(h("button", { class: "ghost", type: "button", onclick: () => call("update_open_page") }, ic("link"), " Страница релиза"));
        if (!info.skipped) row.append(h("button", { class: "ghost", type: "button", onclick: skip }, "Пропустить эту версию"));
        box.append(row);
        if (!info.frozen) box.append(h("p", { class: "note" }, "Запуск из исходников: обновите через git pull."));
      }
      if (info.backup) box.append(h("p", { class: "note" }, "Есть резервная копия версии " + info.backup + ". ",
        h("button", { class: "ghost", type: "button", onclick: rollback }, "Вернуть " + info.backup)));
    }
    async function check() {
      say("Проверяю…");
      const r = await call("update_check", false);
      if (!r.ok) { say(r.error, "bad"); return; }
      info = r; say(""); render();
    }
    async function skip() {
      const r = await call("update_skip", info.version);
      if (r.ok) { S = r.settings; info.skipped = true; render(); say("Не буду напоминать о версии " + info.version, "ok"); }
    }
    async function download() {
      const r = await call("update_download");
      if (!r.ok) { say(r.error, "bad"); return; }
      bar.hidden = false;
      clearInterval(poll);
      poll = setInterval(progress, 700);
    }
    async function progress() {
      if (!document.body.contains(bar)) { clearInterval(poll); return; }
      const r = await call("update_progress");
      if (!r.ok) return;
      if (r.stage === "download") { bar.value = r.total ? Math.round(r.done * 100 / r.total) : 0; say("Скачиваю: " + fmtBytes(r.done) + (r.total ? " из " + fmtBytes(r.total) : "")); }
      else if (r.stage === "extract") say("Контрольная сумма совпала, распаковываю…");
      else if (r.stage === "error") { clearInterval(poll); bar.hidden = true; say(r.error, "bad"); }
      else if (r.stage === "ready") {
        clearInterval(poll); bar.value = 100;
        say("Версия " + r.version + " скачана и проверена (SHA-256).", "ok");
        box.append(h("div", { class: "row2" }, h("button", { class: "go", type: "button", onclick: apply }, "Обновить и перезапустить"),
          h("span", { class: "note" }, "Программа закроется, заменит файлы (старая версия останется резервной копией) и откроется снова. Настройки и данные сохраняются.")));
      }
    }
    async function apply() {
      const r = await call("update_apply");
      if (!r.ok) { say(r.error, "bad"); return; }
      say("Закрываюсь для обновления…", "ok");
    }
    async function rollback() {
      if (!confirm("Вернуть версию " + info.backup + "? Программа закроется и откроется снова.")) return;
      const r = await call("update_rollback");
      if (!r.ok) say(r.error, "bad");
    }
    return panel("about", "check", "О программе и обновления",
      "own-tube " + version + " · github.com/freeon1989/own-tube · MIT. Обновления берутся только из релизов GitHub, архив сверяется по SHA-256 и ставится только по кнопке.",
      field("Проверять при запуске", sw(S.update_check !== false, (v) => saveSettings({ update_check: v }), "Проверять обновления при запуске"),
        h("span", { class: "note" }, "один запрос к api.github.com через выбранный маршрут (прокси/VPN), без статистики")),
      h("div", { class: "row2" }, h("button", { class: "go", type: "button", onclick: check }, "Проверить обновления")),
      box, bar, msg);
  }

  async function startupUpdate() {
    const done = await call("update_result");
    if (done.ok && done.result) {
      const x = done.result;
      status(x.ok ? "Обновлено до own-tube " + x.to + "." : "Обновление не установлено: " + (x.error || "ошибка"), x.ok ? "" : "err");
    }
    const r = await call("update_check", true);
    if (!r.ok || !r.available || r.skipped) return;
    const ban = h("div", { class: "upd-banner", role: "status" },
      h("span", {}, "Доступна own-tube " + r.version),
      h("button", { class: "go", type: "button", onclick: () => { ban.remove(); go("settings", "about"); } }, "Подробнее"),
      h("button", { class: "ghost", type: "button", onclick: () => ban.remove() }, "Позже"));
    document.body.append(ban);
  }

  function buildAiSection() {
    const wrap = h("div", { class: "ai-wrap" });
    const formHost = h("div");
    const listHost = h("div", { class: "ai-list" });
    const msg = h("p", { class: "note" });
    const presets = S.ai_presets || [];

    function renderList() {
      listHost.replaceChildren();
      const configs = S.ai_configs || [];
      if (!configs.length) {
        listHost.appendChild(h("p", { class: "note" }, "Пока нет конфигураций. Добавьте сервер OpenAI, Claude, Grok, Gemini, DeepSeek, OpenRouter или свой (Ollama, LM Studio)."));
        return;
      }
      configs.forEach((c) => {
        listHost.appendChild(h("div", { class: "ai-card" + (c.active ? " active" : "") },
          h("div", { class: "nm" },
            h("b", {}, c.name),
            h("span", {}, (c.model || "модель не указана") + " · " + hostPort(c.base_url))),
          c.active ? h("span", { class: "pill local" }, "активна") : h("span"),
          c.api_key_set ? h("span", { class: "tick on", title: "Ключ сохранён" }, "✓") : h("span", { class: "note" }, "нет ключа"),
          h("button", { class: "ghost", type: "button", onclick: () => openForm(c) }, "Изменить"),
          h("button", { class: "ghost", type: "button", onclick: async () => {
            const r = await call("delete_ai_config", c.id);
            if (!r.ok) { msg.textContent = r.error || ""; msg.className = "note bad-text"; return; }
            S = r.settings;
            formHost.replaceChildren();
            renderList();
            msg.textContent = "Конфигурация удалена";
            msg.className = "note";
          } }, "Удалить")));
      });
    }

    function openForm(cfg) {
      const isNew = !cfg;
      cfg = cfg || { name: "", base_url: "", model: "", vision: false, threads: 1, context: 130000, active: false, api_key_set: false };
      let models = [];
      const name = h("input", { type: "text", value: cfg.name || "", placeholder: "например, Домашний сервер", "aria-label": "Название конфигурации", spellcheck: "false" });
      const url = h("input", { type: "text", value: cfg.base_url || "", placeholder: "https://api.openai.com/v1 или http://127.0.0.1:1234/v1", "aria-label": "URL сервера", spellcheck: "false", autocomplete: "off" });
      const key = h("input", { type: "password", value: "", placeholder: cfg.api_key_set ? "сохранён, введите новый чтобы заменить" : "API-ключ", "aria-label": "API-ключ", autocomplete: "new-password" });
      const tick = h("span", { class: "tick" + (cfg.api_key_set ? " on" : ""), title: cfg.api_key_set ? "Ключ сохранён" : "Ключ ещё не проверен" }, "✓");
      const keyMsg = h("span", { class: "note" });
      const model = h("input", { type: "text", value: cfg.model || "", placeholder: "например local/llama или gpt-4.1", "aria-label": "Название модели", spellcheck: "false", autocomplete: "off" });
      const suggest = h("ul", { class: "suggest-list", hidden: true, role: "listbox" });
      function showSuggest() {
        const q = (model.value || "").toLowerCase();
        const items = models.filter((m) => !q || m.toLowerCase().includes(q)).slice(0, 50);
        suggest.replaceChildren();
        if (!items.length) { suggest.hidden = true; return; }
        items.forEach((m) => {
          const li = h("li", { role: "option", onclick: () => { model.value = m; suggest.hidden = true; } }, m);
          suggest.appendChild(li);
        });
        suggest.hidden = false;
      }
      model.addEventListener("input", showSuggest);
      model.addEventListener("focus", showSuggest);
      model.addEventListener("blur", () => setTimeout(() => { suggest.hidden = true; }, 150));
      const vision = h("input", { type: "checkbox" }); vision.checked = !!cfg.vision;
      const threads = h("input", { type: "number", min: "1", max: "64", value: String(cfg.threads || 1), "aria-label": "Число потоков для обработки данных" });
      const context = h("input", { type: "number", min: "1024", max: "2000000", value: String(cfg.context || 130000), "aria-label": "Объём данных для анализа" });
      const active = h("input", { type: "checkbox" }); active.checked = !!cfg.active;
      const presetRow = h("div", { class: "presets" },
        ...presets.map((p) => {
          const id = p[0], label = p[1], presetUrl = p[2];
          const b = h("button", { type: "button" }, label);
          b.addEventListener("click", () => {
            if (presetUrl) url.value = presetUrl;
            if (!name.value) name.value = label;
            presetRow.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
          });
          return b;
        }));
      async function checkKey() {
        keyMsg.textContent = "Проверяю…";
        keyMsg.className = "note";
        tick.classList.remove("on");
        const payload = { base_url: url.value, api_key: key.value };
        if (cfg.id) payload.id = cfg.id;
        const r = await call("check_ai_key", payload);
        if (!r.ok) {
          keyMsg.textContent = r.error || "Не удалось проверить";
          keyMsg.className = "note bad-text";
          return;
        }
        tick.classList.add("on");
        keyMsg.textContent = r.detail || "Ключ принят";
        keyMsg.className = "note ok-text";
        models = r.models || [];
        if (models.length && !model.value) model.value = models[0];
        showSuggest();
      }
      formHost.replaceChildren(
        h("div", { class: "ai-form" },
          h("h3", {}, isNew ? "Новая конфигурация" : "Изменить конфигурацию"),
          presetRow,
          field("Название конфигурации", name),
          field("URL сервера", url),
          field("API-ключ", h("div", { class: "key-row" }, key, h("button", { class: "ghost", type: "button", onclick: checkKey }, "Проверить"), tick)),
          keyMsg,
          field("Название модели", h("div", { class: "suggest" }, model, suggest)),
          h("label", { class: "check" }, vision, "Анализировать изображения"),
          field("Число потоков для обработки данных", threads),
          field("Объём данных для анализа", context, h("span", { class: "info", title: "Сколько токенов модель может принять за раз: системный текст, история чата и содержимое экрана. 130000 подходит для большинства моделей." }, "что это?")),
          h("label", { class: "check" }, active, "Сделать модель активной"),
          h("div", { class: "actions" },
            h("button", { class: "ghost", type: "button", onclick: () => formHost.replaceChildren() }, "Отмена"),
            h("button", { class: "go", type: "button", onclick: async () => {
              const payload = {
                name: name.value, base_url: url.value, model: model.value,
                vision: vision.checked, threads: Number(threads.value), context: Number(context.value),
                active: active.checked,
              };
              if (cfg.id) payload.id = cfg.id;
              if (key.value) payload.api_key = key.value;
              const r = await call("save_ai_config", payload);
              if (!r.ok) { msg.textContent = r.error || "Не удалось сохранить"; msg.className = "note bad-text"; return; }
              S = r.settings;
              formHost.replaceChildren();
              renderList();
              msg.textContent = "Конфигурация сохранена" + ((r.settings && r.settings.secrets_persist === false && key.value) ? ". " + (r.settings.secrets_note || "") : "");
              msg.className = "note ok-text";
            } }, "Сохранить"))));
    }

    renderList();
    wrap.append(
      h("div", { class: "row2" }, h("button", { class: "go", type: "button", onclick: () => openForm(null) }, "Добавить конфигурацию")),
      listHost, formHost, msg,
      h("p", { class: "note" }, S.secrets_note || "API-ключи шифруются средствами Windows и не записываются в settings.json. Запросы к моделям идут через прокси, если он включён."),
      h("p", { class: "note" }, "Помощницы отвечают через активную конфигурацию. Запросы к моделям идут через прокси, кроме адресов локальной сети (10.x, 192.168.x, 127.x) — к ним напрямую. Голос и встроенные модели GGUF — позже."),
    );
    return panel("ai", "chip", "ИИ — API",
      "Подключите платную модель или свой сервер с API как у OpenAI (Ollama, LM Studio и другие). Можно сохранить несколько конфигураций и сделать одну активной.",
      wrap);
  }

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
      h("div", { class: "kv" }, h("span", {}, "ИИ"), h("span", { class: "pill" }, ((S.ai_configs || []).find((c) => c.active) || {}).name || "не настроен")),
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

    const netMsg = h("span", { class: "note" });
    const mpvPath = h("input", { type: "text", value: S.mpv_path, placeholder: "по умолчанию: tools\\mpv.exe рядом с программой", "aria-label": "Путь к mpv", spellcheck: "false" });
    const dlDir = h("input", { type: "text", value: S.download_dir, readonly: true, "aria-label": "Папка загрузок" });
    const tkMsg = h("p", { class: "note" });

    const cats = [["look", "Оформление"], ["play", "Воспроизведение"], ["net", "Сеть"], ["vpn", "VPN"], ["data", "Данные"], ["accounts", "Аккаунты"], ["ai", "ИИ"], ["voice", "Голос"], ["about", "О программе"]];
    view(title("Настройки", h("small", {}, "own-tube " + st.version)),
      h("div", { class: "subnav" }, ...cats.map(([id, t]) => h("button", { type: "button", onclick: () => $("set-" + id).scrollIntoView({ block: "start" }) }, t))),
      panel("look", "palette", "Оформление и помощницы", "Тема меняет цвета всего окна. «Как у помощницы» — у каждой своя.", themeOpts, h("div", { class: "row2" }), ...asstRows),
      panel("play", "play", "Воспроизведение", "По умолчанию видео играет прямо в окне программы. mpv остаётся как «отдельное окно». Качество — кнопками сверху.",
        field("Где играть", (() => {
          const seg = h("div", { class: "seg", role: "radiogroup", "aria-label": "Где играть" }, ...[["embedded", "В окне программы"], ["mpv", "Отдельное окно (mpv)"]].map(([id, t]) => {
            const b = h("button", { type: "button", role: "radio", "aria-checked": String((S.player_mode || "embedded") === id) }, t);
            b.addEventListener("click", async () => { if (await saveSettings({ player_mode: id })) seg.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x === b))); });
            return b;
          }));
          return seg;
        })()),
        field("Кодек видео", codec),
        field("SponsorBlock", sw(S.sponsorblock, (v) => saveSettings({ sponsorblock: v }), "SponsorBlock"), h("span", { class: "note" }, "пропускать рекламу внутри роликов YouTube")),
        sbChecks,
        h("p", { class: "note" }, "SponsorBlock получает только 4 символа хеша от ID видео — какой ролик вы смотрите, сервер не знает."),
        field("Путь к mpv", mpvPath, h("button", { class: "ghost", type: "button", onclick: async () => { if (await saveSettings({ mpv_path: mpvPath.value })) status("Сохранено"); } }, "Сохранить")),
        h("div", { class: "toolstate" }, toolPill("mpv", "mpv"), toolPill("ffmpeg", "ffmpeg (встроенный плеер и загрузки)"), toolPill("deno", "deno (для YouTube)"))),
      buildNetPanel(netMsg),
      buildVpnPanel(),
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
      buildAccountsPanel(),
      buildAiSection(),
      buildVoicePanel(),
      buildUpdatePanel(st.version),
    );
    status("");
    if (section) { const el = $("set-" + section); if (el) el.scrollIntoView({ block: "start" }); }
  }

  function buildVoicePanel() {
    const msg = h("p", { class: "note" });
    const rate = h("input", { type: "range", min: "0.6", max: "1.6", step: "0.05", value: String(S.voice_rate || 1), "aria-label": "Скорость речи" });
    const pitch = h("input", { type: "range", min: "0.5", max: "1.5", step: "0.05", value: String(S.voice_pitch == null ? 1 : S.voice_pitch), "aria-label": "Высота голоса" });
    rate.addEventListener("change", () => saveSettings({ voice_rate: Number(rate.value) }));
    pitch.addEventListener("change", () => saveSettings({ voice_pitch: Number(pitch.value) }));
    const voiceSel = h("select", { "aria-label": "Голос" });
    function fillVoices() {
      loadVoices();
      const ru = tts.voices.filter((v) => /^ru/i.test(v.lang)), rest = tts.voices.filter((v) => !/^ru/i.test(v.lang));
      voiceSel.replaceChildren(h("option", { value: "" }, ru.length ? "Подобрать русский автоматически" : "Русских голосов нет"),
        ...ru.concat(rest).map((v) => h("option", { value: v.voiceURI }, v.name + " (" + v.lang + ")")));
      voiceSel.value = S.voice_name || "";
      msg.textContent = !("speechSynthesis" in window) ? "В этом окне нет синтеза речи."
        : (ru.length ? "" : "В Windows не установлен русский голос: Параметры → Время и язык → Речь → Добавить голоса → русский. После установки перезапустите own-tube.");
    }
    fillVoices();
    if ("speechSynthesis" in window) speechSynthesis.addEventListener("voiceschanged", fillVoices);
    voiceSel.addEventListener("change", () => saveSettings({ voice_name: voiceSel.value }));

    const engine = h("select", { "aria-label": "Способ распознавания" },
      h("option", { value: "auto" }, "Авто: локальный whisper, если установлен, иначе API"),
      h("option", { value: "api" }, "Только API активной конфигурации ИИ"),
      h("option", { value: "local" }, "Только локальный whisper.cpp"));
    engine.value = S.stt_engine || "auto";
    engine.addEventListener("change", () => saveSettings({ stt_engine: engine.value }));
    const model = h("input", { type: "text", value: S.stt_model || "whisper-1", maxlength: "80", "aria-label": "Модель распознавания (API)", spellcheck: "false" });
    const wdir = h("input", { type: "text", value: S.whisper_dir || "", placeholder: "по умолчанию — папка данных программы (можно E:\\AI\\whisper)", "aria-label": "Папка whisper", spellcheck: "false" });
    const sttState = h("p", { class: "note" }, "Проверяю…");
    const bar = h("div", { class: "progress", hidden: true }, h("i"));
    const installBtn = h("button", { class: "go", type: "button" }, "Установить локальное распознавание");
    let timer = null;
    async function refresh() {
      const r = await call("stt_status");
      if (!r.ok) { sttState.textContent = r.error; return; }
      const ins = r.install || {};
      if (ins.running) {
        bar.hidden = false;
        const pct = ins.total ? Math.round(ins.done / ins.total * 100) : 0;
        bar.firstChild.style.width = pct + "%";
        sttState.textContent = `Скачиваю (${ins.stage}): ${pct}% — ${Math.round(ins.done / 1048576)} из ${Math.round(ins.total / 1048576)} МБ`;
        installBtn.disabled = true;
        if (!timer) timer = setInterval(refresh, 700);
        return;
      }
      if (timer) { clearInterval(timer); timer = null; }
      bar.hidden = true; installBtn.disabled = false;
      if (ins.error) sttState.textContent = "Установка не удалась: " + ins.error;
      else sttState.textContent = (r.local ? "Локальное распознавание установлено: " + r.dir
        : "Локально не установлено (около " + r.size_mb + " МБ, whisper.cpp + модель base, хватает для русского)")
        + (r.api_possible ? ". API: активная конфигурация OpenAI-совместимая — запрос /audio/transcriptions будет проверен при первой записи." : ". API: активная конфигурация не подходит для речи.")
        + (r.ffmpeg ? "" : " ffmpeg не найден — запись обработать нечем.");
      installBtn.textContent = r.local ? "Переустановить" : "Установить локальное распознавание";
    }
    installBtn.addEventListener("click", async () => {
      if (wdir.value !== (S.whisper_dir || "")) { if (!(await saveSettings({ whisper_dir: wdir.value }))) return; }
      const r = await call("stt_install");
      if (!r.ok) { sttState.textContent = r.error; return; }
      refresh();
    });
    wdir.addEventListener("change", async () => { if (await saveSettings({ whisper_dir: wdir.value })) refresh(); });
    model.addEventListener("change", () => saveSettings({ stt_model: model.value }));
    const micTest = h("button", { class: "ghost", type: "button", onclick: async () => {
      if (!navigator.mediaDevices) { micMsg.textContent = "Микрофон недоступен в этом окне."; return; }
      try {
        const st = await navigator.mediaDevices.getUserMedia({ audio: true });
        const label = (st.getAudioTracks()[0] || {}).label || "микрофон";
        st.getTracks().forEach((t) => t.stop());
        micMsg.textContent = "Микрофон доступен: " + label;
      } catch (e) { micMsg.textContent = micError(e); }
    } }, "Проверить микрофон");
    const micMsg = h("p", { class: "note" });
    refresh();
    return panel("voice", "speaker", "Голос и помощница", "Озвучка ответов — голосом Windows (у каждой помощницы свой тон), ввод голосом — с микрофона.",
      field("Помощница сама открывает", sw(S.ai_autorun !== false, (v) => saveSettings({ ai_autorun: v }), "Автовыполнение действий помощницы"),
        h("span", { class: "note" }, "поиск, «включить», раздел и «ещё» выполняются без подтверждения; лайки, подписки и комментарии — только с подтверждением")),
      field("Озвучивать ответы", sw(!!S.voice_tts, (v) => { const ok = saveSettings({ voice_tts: v }); $("chatTts").setAttribute("aria-pressed", String(v)); return ok; }, "Озвучивать ответы")),
      field("Голос", voiceSel),
      field("Скорость", rate), field("Высота", pitch),
      h("div", { class: "row2" }, h("button", { class: "ghost", type: "button", onclick: () => speak(assistant().name + ": привет! Так звучит мой голос.", true) }, "Проверить голос")),
      msg,
      h("h3", { class: "sub-h" }, "Ввод голосом (микрофон в чате)"),
      field("Распознавание", engine),
      field("Модель для API", model, h("span", { class: "note" }, "для OpenAI-совместимых серверов (/audio/transcriptions)")),
      field("Папка whisper", wdir),
      field("Отправлять сразу", sw(S.stt_autosend !== false, (v) => saveSettings({ stt_autosend: v }), "Отправлять сразу после распознавания")),
      h("div", { class: "row2" }, installBtn, micTest), bar, sttState, micMsg);
  }

  function buildAccountsPanel() {
    const box = h("div", { class: "acc-list" });
    const msg = h("p", { class: "note" });
    let poll = null;
    const NAMES = { google: ["Google / YouTube", "G", "подписки, история, «Смотреть позже», понравившиеся и плейлисты из вашего аккаунта; меньше проверок «я не робот»"],
      vk: ["VK", "VK", "поиск от вашего имени и ролики 18+ (если включите)"] };
    function render() {
      box.replaceChildren(...["google", "vk"].map((k) => {
        const a = acct[k] || {};
        const [nm, letter, what] = NAMES[k];
        const av = a.avatar ? h("img", { class: "acc-av", src: img(a.avatar), alt: "" }) : h("span", { class: "acc-av" }, letter);
        const state = a.login_window ? "Открыто окно входа…" : a.signed_in ? ("Вход выполнен" + (a.name ? ": " + a.name : "") + (a.origin === "file" ? " (cookies.txt)" : "")) : "Не выполнен";
        const btns = a.signed_in ? [h("button", { class: "ghost", type: "button", onclick: async () => {
          if (!confirm("Выйти из " + nm + "? Профиль окна входа и cookies будут удалены с этого компьютера.")) return;
          const r = await call("account_logout", k); if (r.ok) { acct = r.accounts; render(); buildSide(); msg.textContent = "Выход выполнен, данные входа удалены."; }
        } }, "Выйти")] : [
          h("button", { class: "go", type: "button", onclick: async () => {
            const r = await call("account_login", k);
            if (!r.ok) { msg.textContent = r.error; return; }
            acct = r.accounts; render();
            msg.textContent = "Откроется окно " + nm + ". Войдите как обычно; окно закроется само. Пароль вводится только на странице " + nm + ".";
            if (!poll) poll = setInterval(async () => {
              if (!document.body.contains(box)) { clearInterval(poll); poll = null; return; }
              const s = await call("account_status");
              if (!s.ok) return;
              acct = s.accounts; render();
              if (!acct.google.login_window && !acct.vk.login_window) { clearInterval(poll); poll = null; refreshAccounts(); }
            }, 1500);
          } }, "Войти"),
          h("button", { class: "ghost", type: "button", onclick: async () => {
            if (!confirm("Импорт cookies.txt — запасной путь, если окно входа заблокировано. Файл даёт полный доступ к аккаунту: берите его только из своего браузера и удалите после импорта. Продолжить?")) return;
            const r = await call("account_import", k);
            if (!r.ok) { msg.textContent = r.error; return; }
            if (r.cancelled) return;
            acct = r.accounts; render(); refreshAccounts();
            msg.textContent = "Импортировано cookies: " + r.count + ". Файл больше не нужен — удалите его.";
          } }, ic("upload"), " cookies.txt")];
        return h("div", { class: "row acc" + (a.signed_in ? " on" : "") }, av,
          h("div", { class: "nm" }, h("b", {}, nm), h("span", {}, state), h("span", { class: "note" }, what)), ...btns);
      }));
    }
    render();
    return panel("accounts", "user", "Аккаунты", "Вход необязателен. Окно входа — настоящая страница Google или VK со своим отдельным профилем; трафик идёт через тот же прокси, что и YouTube.",
      box, msg,
      h("p", { class: "note" }, "Пароли программа не видит и не хранит. После входа cookies шифруются средствами Windows (DPAPI) и не попадают в settings.json и журнал. Лайки, подписки и комментарии в аккаунте сами не делаются. Google иногда блокирует вход во встроенных окнах — тогда используйте импорт cookies.txt."));
  }
  function updateRouteChip() {
    const p = S.proxy;
    const vpn = S.net_mode === "awg";
    $("routeText").textContent = vpn ? "VPN AmneziaWG" : !p ? "Напрямую" : (p.startsWith("socks") ? "SOCKS-прокси" : "HTTP-прокси");
    $("routeDot").className = "dot" + (p || vpn ? " ok" : "");
  }
  function setQuality(q) {
    document.querySelectorAll("#qualitySeg button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.q === q)));
  }

  /* ---------- запуск ---------- */
  async function start() {
    api = window.pywebview.api;
    const st = await call("get_state");
    if (!st.ok) { status(st.error, "err"); return; }
    S = st.settings; counts = st.counts; MEDIA = st.media || "";
    buildSide(); refreshAccounts(); buildStage(); wireChat(); applyAssistant(false); setQuality(S.quality); updateRouteChip();
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
    if (!st.tools.ffmpeg && S.player_mode !== "mpv") status("ffmpeg не найден — встроенный плеер не заработает. Проверьте папку tools рядом с программой.", "err");
    else if (!st.tools.mpv && S.player_mode === "mpv") status("mpv не найден — видео не запустится. Проверьте папку tools рядом с программой.", "err");
    const first = new URLSearchParams(location.search).get("view");
    go(first || "home");
    setTimeout(startupUpdate, 4000);
  }

  if (window.pywebview && window.pywebview.api) start();
  else window.addEventListener("pywebviewready", start, { once: true });
})();
