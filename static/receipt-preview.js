/* Hover preview for receipts. Any element with data-doc="/doc/<id>" shows a small floating
   popup on hover: the image, the Amazon order text, or a "PDF — click to open" note. The
   kind is sniffed from the server's Content-Type (the /doc route serves receipts inline), so
   templates only need the data-doc attribute. Clicking still opens the full receipt in a tab. */
(function () {
  var pop, cache = {}, current = null;

  function ensurePop() {
    if (!pop) {
      pop = document.createElement("div");
      pop.style.cssText =
        "position:fixed;z-index:9999;max-width:340px;max-height:420px;overflow:auto;" +
        "background:#fff;border:1px solid #c9c6bd;border-radius:8px;padding:8px;display:none;" +
        "box-shadow:0 6px 24px rgba(0,0,0,.18);font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:#222;";
      // keep it open while the pointer is over the popup itself (so text is scrollable)
      pop.addEventListener("mouseenter", function () { current = pop; });
      pop.addEventListener("mouseleave", hide);
      document.body.appendChild(pop);
    }
    return pop;
  }

  function hide() { if (pop) pop.style.display = "none"; current = null; }

  function place(el) {
    var r = el.getBoundingClientRect(), p = ensurePop();
    p.style.display = "block";
    var left = r.right + 10, top = r.top;
    if (left + 350 > window.innerWidth) left = Math.max(8, r.left - 360);
    if (top + p.offsetHeight > window.innerHeight) top = Math.max(8, window.innerHeight - p.offsetHeight - 8);
    p.style.left = left + "px";
    p.style.top = top + "px";
  }

  function esc(s) {
    return s.replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; });
  }

  function render(url) {
    var p = ensurePop(), c = cache[url];
    if (!c) { p.innerHTML = '<span style="color:#888">Loading…</span>'; return; }
    if (c.kind === "image") p.innerHTML = '<img src="' + url + '" style="max-width:320px;max-height:400px;display:block">';
    else if (c.kind === "text") p.innerHTML = '<pre style="margin:0;white-space:pre-wrap;word-break:break-word">' + c.text + "</pre>";
    else p.innerHTML = '<span>📄 PDF receipt — click to open</span>';
  }

  function load(url, cb) {
    if (cache[url]) { cb(); return; }
    fetch(url).then(function (res) {
      var ct = res.headers.get("content-type") || "";
      if (!res.ok) {  // missing file etc. — show the server's message instead of an error
        res.text().then(function (t) {
          cache[url] = { kind: "text", text: esc(t || ("Couldn't load receipt (" + res.status + ")")) };
          cb();
        });
      } else if (ct.indexOf("image/") === 0) { cache[url] = { kind: "image" }; cb(); }
      else if (ct.indexOf("text/") === 0) {
        res.text().then(function (t) { cache[url] = { kind: "text", text: esc(t.slice(0, 4000)) }; cb(); });
      } else { cache[url] = { kind: "pdf" }; cb(); }
    }).catch(function () { cache[url] = { kind: "text", text: "Couldn't load receipt." }; cb(); });
  }

  function attach(el) {
    var url = el.getAttribute("data-doc");
    if (!url) return;
    el.addEventListener("mouseenter", function () {
      current = el;
      place(el);
      render(url);                                   // shows "Loading…" until fetched
      load(url, function () { if (current) { render(url); place(el); } });
    });
    el.addEventListener("mouseleave", function () {
      // let the popup's own mouseenter cancel the hide if the pointer moved onto it
      setTimeout(function () { if (current !== pop) hide(); }, 80);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-doc]").forEach(attach);
  });
})();
