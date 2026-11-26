"use strict";
/* Макет интерфейса: данные примерные, ничего не сохраняется и не уходит в сеть. innerHTML не используется. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);
  function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null) n.append(kid);
    return n;
  }
  function ic(name, cls) {
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("class", "ico" + (cls ? " " + cls : ""));
    const u = document.createElementNS(NS, "use");
    u.setAttribute("href", "#i-" + name);
    s.appendChild(u);
    return s;
  }

  /* ---------- помощницы (картинки из AmneziaMonitor, примеры персонажей) ---------- */
  const ASSISTANTS = [
    { id: "default", name: "Рэй", accent: "#a855f7", accent2: "#22d3ee", glow: "rgba(168,85,247,.55)",
      persona: "Спокойная и точная, чуть загадочная. Отвечает коротко и по делу.",
      voice: "Silero · «Ксения»", model: "Qwen2.5 7B · локально", paid: false },
    { id: "se", name: "Юки", accent: "#1e8fe0", accent2: "#fecc02", glow: "rgba(30,143,224,.55)",
      persona: "Мягкая и заботливая. Объясняет подробно, любит спокойные каналы.",
      voice: "Piper · «Ирина»", model: "Qwen2.5 7B · локально", paid: false },
    { id: "gb", name: "Кейт", accent: "#e0263f", accent2: "#3a5bd9", glow: "rgba(224,38,63,.5)",
      persona: "Бойкая, шутит и подначивает. Подбирает, что посмотреть вечером.",
      voice: "Silero · «Байя»", model: "Grok · платная", paid: true },
    { id: "in", name: "Лила", accent: "#ff9933", accent2: "#1fae3a", glow: "rgba(255,153,51,.5)",
      persona: "Энергичная, обожает музыку и танцы. Всегда за новый плейлист.",
      voice: "Silero · «Ксения»", model: "Gemini · платная", paid: true },
    { id: "il", name: "Нина", accent: "#2f63e8", accent2: "#e8f0ff", glow: "rgba(47,99,232,.55)",
      persona: "Любит новости и тренды. Всё проверяет в интернете и даёт источники.",
      voice: "Облачный голос", model: "Claude · платная", paid: true },
  ];
  let cur = ASSISTANTS[0];

  /* ---------- меню слева ---------- */
  const NAV = [
    ["Главная", "home"], ["Shorts", "shorts"], ["Подписки", "subs"],
    ["sec", "Вы"], ["История", "history"], ["Плейлисты", "list"], ["Смотреть позже", "later"],
    ["Избранное", "heart"], ["Загрузки", "download", "3"],
    ["sec", "Обзор"], ["Музыка", "music"], ["Игры", "game"], ["Новости", "news"], ["Спорт", "sport"],
    ["Трансляции", "live"], ["Мода и красота", "fashion"], ["Обучение", "learn"],
  ];
  let signedIn = false;
  function buildSide() {
    const side = $("side");
    side.replaceChildren();
    const logo = h("i"); const lg = document.createElementNS(NS, "svg"); lg.setAttribute("viewBox", "0 0 24 24");
    const p = document.createElementNS(NS, "path"); p.setAttribute("d", "m8 5 12 7-12 7z"); lg.appendChild(p); logo.appendChild(lg);
    side.append(h("div", { class: "brand" }, logo, h("b", {}, "own-tube")));
    side.append(h("button", { class: "account" + (signedIn ? " in" : ""), type: "button", onclick: () => { signedIn = !signedIn; buildSide(); } },
      h("span", { class: "g" }, signedIn ? "Д" : "G"),
      h("span", { class: "t" }, h("b", {}, signedIn ? "Дмитрий" : "Войти"), h("span", {}, signedIn ? "аккаунт Google · выйти" : "через аккаунт Google"))));
    for (const it of NAV) {
      if (it[0] === "sec") { side.append(h("div", { class: "sec" }, it[1])); continue; }
      const b = h("button", { class: "nav", type: "button", onclick: () => go(it[0], b) }, ic(it[1]), h("span", { class: "l" }, it[0]), it[2] ? h("span", { class: "n" }, it[2]) : null);
      if (it[0] === current) b.setAttribute("aria-current", "page");
      side.append(b);
    }
    side.append(h("div", { class: "grow" }));
    const st = h("button", { class: "nav", type: "button", onclick: () => go("Настройки") }, ic("gear"), h("span", { class: "l" }, "Настройки"));
    if (current === "Настройки") st.setAttribute("aria-current", "page");
    side.append(st);
  }
  let current = "Главная";
  function go(name) {
    current = name;
    const settings = name === "Настройки";
    $("viewHome").hidden = settings;
    $("viewSettings").hidden = !settings;
    $("viewTitle").textContent = name;
    if (settings) closeChat();
    buildSide();
    window.scrollTo(0, 0);
  }

  /* ---------- лента ---------- */
  function buildGrid() {
    const feed = window.MOCK_FEED || [];
    $("grid").replaceChildren(...feed.map((it) => {
      const thumb = h("div", { class: "thumb" }, h("img", { src: it.thumb, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }),
        it.duration ? h("span", { class: "badge" }, it.duration) : null);
      return h("button", { class: "card", type: "button" }, thumb,
        h("div", { class: "meta" }, h("div", { class: "title" }, it.title),
          h("div", { class: "sub" }, [it.channel, it.views].filter(Boolean).join(" · "))));
    }));
  }

  /* ---------- помощница, сцена, чат ---------- */
  const ACTS = [["Привет", "wave", "👋"], ["Думает", "think", "…"], ["Радуется", "joy", "♪"], ["Удивлена", "wow", "!"]];
  function buildSparks() {
    const box = document.querySelector(".sparks"); let r = 7;
    const rnd = () => (r = (r * 16807) % 2147483647) / 2147483647;
    box.replaceChildren(...Array.from({ length: 16 }, () => {
      const i = h("i"); i.style.cssText = `--x:${(rnd() * 100).toFixed(0)}%;--s:${(2 + rnd() * 3).toFixed(1)}px;--d:${(8 + rnd() * 8).toFixed(1)}s;--dl:-${(rnd() * 12).toFixed(1)}s;--dx:${Math.round(rnd() * 60 - 30)}px`;
      return i;
    }));
  }
  function act(kind, glyph) {
    const hero = $("hero");
    hero.className = "hero"; void hero.offsetWidth; hero.classList.add("act-" + kind);
    const m = $("mood"); m.textContent = glyph; m.classList.remove("show"); void m.offsetWidth; m.classList.add("show");
    hero.addEventListener("animationend", (e) => { if (e.animationName !== "breathe") hero.className = "hero"; }, { once: true });
  }
  function msgs() {
    const a = cur.name;
    const box = $("msgs");
    box.replaceChildren(
      h("div", { class: "m me" }, "Кто поёт этот опенинг? Хочу такое же на целый вечер."),
      h("div", { class: "step" }, ic("eye"), "смотрю на кадр: титры «OP»"),
      h("div", { class: "step" }, ic("search"), "ищу в интернете: «опенинг исполнитель»"),
      h("div", { class: "m ai" }, `Это опенинг сериала на экране, пример ответа от помощницы ${a}: исполнителя я нашла по титрам и подтвердила двумя источниками. Могу собрать плейлист из похожих треков.`,
        h("div", { class: "srcs" }, h("span", { class: "src" }, ic("link"), "ru.wikipedia.org"), h("span", { class: "src" }, ic("link"), "myanimelist.net"), h("span", { class: "src" }, ic("search"), "поиск YouTube")),
        h("div", { class: "speak", "aria-label": "говорит" }, h("i"), h("i"), h("i"), h("i"), h("i"))),
      h("div", { class: "m me" }, "Да, собери «Опенинги на вечер» и поставь на загрузку первые пять."),
    );
  }
  function setAssistant(a, fromUser) {
    cur = a;
    const root = document.documentElement.style;
    root.setProperty("--accent", a.accent); root.setProperty("--accent2", a.accent2); root.setProperty("--glow", a.glow);
    root.setProperty("--accent-a", a.glow.replace(/[\d.]+\)$/, ".16)")); root.setProperty("--accent-b", a.glow.replace(/[\d.]+\)$/, ".5)"));
    const hero = $("hero"); hero.classList.add("swap");
    setTimeout(() => { hero.src = `assets/heroines/${a.id}.webp`; hero.classList.remove("swap"); }, fromUser ? 300 : 0);
    ["chatAv", "fabAv"].forEach((id) => { $(id).src = `assets/heroines/${a.id}.webp`; });
    $("stName").textContent = a.name; $("stPersona").textContent = a.persona;
    $("chName").textContent = a.name; $("chModel").textContent = a.model;
    document.querySelectorAll(".switcher button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.id === a.id)));
    msgs();
    if (document.getElementById("asstList")) buildAssistants();
  }
  function buildStage() {
    $("switcher").replaceChildren(...ASSISTANTS.map((a) => {
      const b = h("button", { type: "button", role: "radio", "data-id": a.id, "aria-label": a.name, title: a.name, onclick: () => { setAssistant(a, true); act("wave", "👋"); } },
        h("img", { src: `assets/heroines/${a.id}.webp`, alt: "" }));
      return b;
    }));
    $("acts").replaceChildren(...ACTS.map(([t, k, g]) => h("button", { type: "button", onclick: () => act(k, g) }, t)));
    $("stage").classList.add("speaking");
    buildSparks();
  }
  function closeChat() { $("chat").classList.add("closed"); }
  function openChat() { $("chat").classList.remove("closed"); }

  /* ---------- настройки ---------- */
  const MODELS = [
    { key: "chat", t: "Чат", d: "Qwen2.5-7B-Instruct · GGUF Q4_K_M", gb: 3.9, on: true },
    { key: "vision", t: "Зрение", d: "Qwen2-VL-7B · проектор кадров", gb: 0.9, on: true },
    { key: "stt", t: "Распознавание речи", d: "Whisper small · микрофон", gb: 0.3, on: true },
    { key: "tts", t: "Голос", d: "Silero ru · работает на процессоре", gb: 0, on: true },
  ];
  const TOTAL = 8;
  let onlyVideo = false, saved = null;
  function vram() {
    const used = MODELS.reduce((s, m) => s + (m.on ? m.gb : 0), 0);
    const box = $("vramBox"); if (!box) return;
    box.classList.toggle("hot", used / TOTAL >= 0.8);
    $("vramNum").textContent = `${used.toFixed(1).replace(".", ",")} / ${TOTAL} ГБ`;
    $("vramFill").style.width = Math.min(100, used / TOTAL * 100) + "%";
    $("vramNote").textContent = used === 0 ? "ИИ выгружен, вся видеопамять свободна для видео" : "пример: видеокарта с 8 ГБ";
  }
  function sw(on, fn, label) {
    const b = h("button", { class: "sw", type: "button", role: "switch", "aria-checked": String(on), "aria-label": label });
    b.addEventListener("click", () => { const v = b.getAttribute("aria-checked") !== "true"; b.setAttribute("aria-checked", String(v)); fn(v); });
    return b;
  }
  function buildAssistants() {
    const list = $("asstList"); list.replaceChildren(...ASSISTANTS.map((a) =>
      h("div", { class: "row asst" + (a === cur ? " active" : "") },
        h("img", { class: "av", src: `assets/heroines/${a.id}.webp`, alt: "" }),
        h("div", { class: "nm" }, h("b", {}, a.name), h("span", {}, a.persona)),
        h("div", { class: "kv" }, h("span", {}, "Голос"), h("b", {}, a.voice)),
        h("div", { class: "kv" }, h("span", {}, "Модель"), h("span", { class: "pill " + (a.paid ? "paid" : "local") }, a.model)),
        h("button", { class: "ghost", type: "button", onclick: () => setAssistant(a, true) }, a === cur ? "Выбрана" : "Выбрать"))));
    list.append(h("button", { class: "ghost wide", type: "button" }, ic("plus"), "Добавить свою помощницу: имя, характер, голос, модель"));
  }
  function panel(id, icon, title, lead, ...kids) {
    return h("section", { class: "panel", id }, h("h2", {}, ic(icon), title), lead ? h("p", { class: "lead" }, lead) : null, ...kids);
  }
  function field(label, input) { return h("div", { class: "field" }, h("label", {}, label), input); }
  function buildSettings() {
    const body = $("settingsBody"); body.replaceChildren();
    const cats = [["ai", "ИИ"], ["models", "Источники моделей"], ["net", "VPN и прокси"], ["acc", "Аккаунт"]];
    $("subnav").replaceChildren(...cats.map(([id, t], i) => h("button", { type: "button", "aria-current": i === 0, onclick: (e) => {
      document.querySelectorAll(".subnav button").forEach((b) => b.removeAttribute("aria-current")); e.currentTarget.setAttribute("aria-current", "true");
      $(id).scrollIntoView({ block: "start" }); } }, t)));

    const modelRows = MODELS.map((m) => h("div", { class: "row model" },
      h("div", { class: "nm" }, h("b", {}, m.t)), h("div", {}, h("span", {}, m.d), h("small", {}, m.gb ? `занимает ${m.gb.toFixed(1).replace(".", ",")} ГБ видеопамяти` : "не занимает видеопамять")),
      h("div", { class: "vr" }, m.gb ? m.gb.toFixed(1).replace(".", ",") + " ГБ" : "ЦП"),
      (m.sw = sw(m.on, (v) => { m.on = v; vram(); }, m.t))));
    const only = h("div", { class: "only" }, ic("play" in {} ? "play" : "live"),
      h("div", { class: "nm" }, h("b", {}, "Только видео"), h("span", {}, "Одним нажатием выгружает все модели из видеокарты. Нужна вся мощность для просмотра.")),
      sw(false, (v) => {
        onlyVideo = v;
        if (v) { saved = MODELS.map((m) => m.on); MODELS.forEach((m) => { m.on = false; m.sw.setAttribute("aria-checked", "false"); }); }
        else if (saved) MODELS.forEach((m, i) => { m.on = saved[i]; m.sw.setAttribute("aria-checked", String(m.on)); });
        vram();
      }, "Только видео"));
    const vramBox = h("div", { class: "vram", id: "vramBox" }, ic("chip"), h("div", { class: "vcol" }, h("b", { id: "vramNum" }), h("span", { class: "t", id: "vramNote" })),
      h("div", { class: "bar2" }, h("i", { id: "vramFill" })));
    body.append(panel("ai", "chat", "ИИ-помощницы", "Каждая помощница: своя картинка, характер, голос и модель. Выбор меняет героиню справа.",
      h("div", { id: "asstList" }), only, vramBox, ...modelRows, h("p", { class: "note" }, "Выключенная модель полностью выгружается из видеокарты. Объём памяти здесь пример.")));

    const tabs = h("div", { class: "tabs", role: "tablist" });
    const local = h("div", {}, field("Файл модели", h("input", { type: "text", value: "E:\\AI\\models\\qwen2.5-7b-instruct-q4_k_m.gguf", "aria-label": "Файл GGUF" })),
      h("div", { class: "field" }, h("label", {}, ""), h("button", { class: "ghost", type: "button" }, "Выбрать файл…")),
      field("Ссылка Hugging Face", h("input", { type: "text", placeholder: "https://huggingface.co/…/model.gguf", "aria-label": "Ссылка на модель" })),
      h("div", { class: "field" }, h("label", {}, ""), h("button", { class: "go", type: "button" }, "Скачать в E:\\AI")),
      h("p", { class: "note" }, "Подойдёт любая модель в формате GGUF, в том числе uncensored и abliterated. Что подключать, решаешь ты."));
    const PROVS = [["OpenAI", "sk-…", true], ["Claude", "sk-ant-…", false], ["Grok", "xai-…", true], ["Gemini", "AIza…", false], ["DeepSeek", "sk-…", false]];
    const paid = h("div", { class: "hide" }, ...PROVS.map(([n, ph, has]) => h("div", { class: "prov" }, h("b", {}, n),
      h("input", { type: "password", placeholder: ph, value: has ? "••••••••••••••••" : null, autocomplete: "off", "aria-label": "Ключ " + n }),
      h("span", { class: "pill " + (has ? "local" : "") }, has ? "ключ задан" : "нет ключа"))),
      h("p", { class: "note" }, "Ключи хранятся зашифрованными только на этом компьютере и не показываются после ввода."));
    const mk = (t, el, sel) => { const b = h("button", { type: "button", role: "tab", "aria-selected": String(sel), onclick: () => {
      tabs.querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", "false")); b.setAttribute("aria-selected", "true");
      local.classList.toggle("hide", el !== local); paid.classList.toggle("hide", el !== paid); } }, t); return b; };
    tabs.append(mk("Локальная модель (GGUF)", local, true), mk("Платная модель (API)", paid, false));
    body.append(panel("models", "chip", "Источники моделей", "Локальная модель работает без интернета. Платная подключается по ключу, и для каждой помощницы можно выбрать свою.", tabs, local, paid));

    body.append(panel("net", "globe", "VPN и прокси", "Через это соединение идёт только трафик клиента, остальной компьютер остаётся как есть.",
      h("div", { class: "grid2" },
        h("div", {}, field("VPN-профиль", h("select", {}, h("option", {}, "AmneziaWG · профиль «Швеция»"), h("option", {}, "AmneziaWG · профиль «Германия»"), h("option", {}, "Без VPN"))),
          h("div", { class: "field" }, h("label", {}, "Только этот клиент"), sw(true, () => {}, "Только трафик клиента")),
          h("div", { class: "statusline" }, h("span", { class: "dot ok" }), "связь проверена · 42 мс (пример)"),
          h("div", { class: "field" }, h("label", {}, ""), h("button", { class: "ghost", type: "button" }, "Проверить связь"))),
        h("div", {}, field("Прокси", h("input", { type: "text", placeholder: "http://127.0.0.1:8080", "aria-label": "Прокси" })),
          h("p", { class: "note" }, "Без логина и пароля. Для воспроизведения нужен HTTP-прокси. Профили VPN хранятся вне кода, на этом компьютере."))),
    ));
    body.append(panel("acc", "user", "Аккаунт Google", "Подписки, история, «Смотреть позже», лайки и плейлисты с YouTube.",
      h("div", { class: "field" }, h("button", { class: "go", type: "button" }, "Войти через Google"), h("button", { class: "ghost", type: "button" }, "Выйти")),
      h("p", { class: "note" }, "Пароль приложение не видит. Токен доступа шифруется средствами Windows. Лучше входить отдельным аккаунтом.")));
    buildAssistants(); vram();
  }

  /* ---------- запуск ---------- */
  buildSide(); buildGrid(); buildStage(); buildSettings(); setAssistant(ASSISTANTS[0]);
  $("chatClose").addEventListener("click", closeChat);
  $("chatFab").addEventListener("click", openChat);
  $("hero").addEventListener("click", () => { act("joy", "♪"); openChat(); });
  $("micBtn").addEventListener("click", (e) => e.currentTarget.setAttribute("aria-pressed", String(e.currentTarget.getAttribute("aria-pressed") !== "true")));
  $("voiceBtn").addEventListener("click", (e) => {
    const on = e.currentTarget.getAttribute("aria-pressed") !== "true";
    e.currentTarget.setAttribute("aria-pressed", String(on)); $("stage").classList.toggle("speaking", on);
    document.querySelectorAll(".speak").forEach((s) => { s.style.display = on ? "" : "none"; });
  });
  $("searchForm").addEventListener("submit", (e) => e.preventDefault());
  const params = new URLSearchParams(location.search);
  if (params.get("closed") || window.matchMedia("(max-width:1100px)").matches) closeChat();
})();
