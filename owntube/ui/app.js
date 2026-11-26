"use strict";
/* Весь текст из сети вставляется только через textContent / атрибуты, innerHTML не используется. */
(function () {
  const $ = (id) => document.getElementById(id);
  const grid = $("grid"), status = $("status"), empty = $("empty");
  let quality = "best";
  let api = null;

  function setStatus(text, kind) {
    status.textContent = text || "";
    status.className = "status" + (kind ? " " + kind : "");
  }

  function setQuality(q) {
    quality = q;
    document.querySelectorAll(".seg button").forEach((b) =>
      b.setAttribute("aria-checked", String(b.dataset.q === q)));
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  function card(item) {
    const c = el("button", "card");
    c.type = "button";
    c.dataset.id = item.id;
    c.setAttribute("aria-label", item.title + (item.channel ? ", " + item.channel : ""));
    const thumb = el("div", "thumb");
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.referrerPolicy = "no-referrer";
    img.src = item.thumb;
    thumb.appendChild(img);
    if (item.live) thumb.appendChild(el("span", "badge live", "LIVE"));
    else if (item.duration) thumb.appendChild(el("span", "badge", item.duration));
    const meta = el("div", "meta");
    meta.appendChild(el("div", "title", item.title));
    const sub = el("div", "sub");
    sub.appendChild(document.createTextNode(item.channel || ""));
    if (item.channel && item.views) sub.appendChild(el("span", "dot", "·"));
    if (item.views) sub.appendChild(document.createTextNode(item.views));
    meta.appendChild(sub);
    c.append(thumb, meta);
    c.addEventListener("click", () => play(item, c));
    return c;
  }

  function render(items) {
    grid.replaceChildren(...items.map(card));
    empty.hidden = items.length > 0;
    if (!items.length) empty.textContent = "Ничего не нашлось. Попробуйте другой запрос.";
  }

  async function doSearch(q) {
    setStatus("Ищу…", "busy");
    const r = await api.search(q);
    if (!r.ok) { setStatus(r.error, "err"); return; }
    setStatus(r.items.length ? "Найдено: " + r.items.length : "");
    render(r.items);
  }

  async function play(item, node) {
    node.setAttribute("aria-busy", "true");
    setStatus("Готовлю «" + item.title + "»…", "busy");
    const r = await api.play(item.id, quality);
    node.removeAttribute("aria-busy");
    if (!r.ok) setStatus(r.error, "err");
    else setStatus("Играет в mpv: " + item.title);
  }

  async function changeQuality(q) {
    setQuality(q);
    const r = await api.save_settings({ quality: q });
    if (!r.ok) setStatus(r.error, "err");
  }

  function wireUi() {
    $("searchForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const q = $("q").value.trim();
      if (q) doSearch(q);
    });
    document.querySelectorAll(".seg button").forEach((b) =>
      b.addEventListener("click", () => changeQuality(b.dataset.q)));

    const dlg = $("settings"), err = $("settingsError");
    $("openSettings").addEventListener("click", async () => {
      const r = await api.get_settings();
      if (r.ok) { $("proxy").value = r.settings.proxy; $("mpv").value = r.settings.mpv_path; }
      err.textContent = "";
      dlg.showModal();
    });
    $("cancelSettings").addEventListener("click", () => dlg.close());
    $("settingsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const r = await api.save_settings({ proxy: $("proxy").value, mpv_path: $("mpv").value });
      if (!r.ok) { err.textContent = r.error; return; }
      dlg.close();
      setStatus(r.settings.proxy ? "Прокси включён: " + r.settings.proxy : "Прокси выключен");
    });
  }

  async function start() {
    api = window.pywebview.api;
    wireUi();
    const r = await api.get_settings();
    if (r.ok) setQuality(r.settings.quality);
    $("q").focus();
  }

  if (window.pywebview && window.pywebview.api) start();
  else window.addEventListener("pywebviewready", start, { once: true });
})();
