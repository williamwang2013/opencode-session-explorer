/*
 * opencode-session-explorer i18n runtime.
 * window.__SE_I18N is injected by the Python server: { lang, dict: { en: {...}, zh: {...} } }
 */
(function () {
  var data = window.__SE_I18N || { lang: "en", dict: { en: {} } };
  var dicts = data.dict || { en: {} };
  var supported = Object.keys(dicts);

  function pick(value) {
    if (!value) return "";
    var raw = String(value).toLowerCase();
    if (supported.indexOf(raw) >= 0) return raw;
    var base = raw.split(/[-_]/)[0];
    return supported.indexOf(base) >= 0 ? base : "";
  }

  var stored = "";
  try {
    stored = localStorage.getItem("se-lang") || "";
  } catch (e) {}

  var query = "";
  try {
    query = new URLSearchParams(location.search).get("lang") || "";
  } catch (e) {}

  var lang =
    pick(query) || pick(stored) || pick(data.lang) || pick(navigator.language) || supported[0] || "en";

  function t(key, vars) {
    var table = dicts[lang] || {};
    var out = table[key];
    if (out == null) out = (dicts.en || {})[key];
    if (out == null) return key;
    if (vars) {
      out = String(out).replace(/\{(\w+)\}/g, function (m, name) {
        return vars[name] != null ? String(vars[name]) : m;
      });
    }
    return out;
  }

  function apply(root) {
    root = root || document;
    var each = function (sel, fn) {
      Array.prototype.forEach.call(root.querySelectorAll(sel), fn);
    };
    each("[data-i18n]", function (el) {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    each("[data-i18n-html]", function (el) {
      el.innerHTML = t(el.getAttribute("data-i18n-html"));
    });
    each("[data-i18n-ph]", function (el) {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-ph")));
    });
    each("[data-i18n-title]", function (el) {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    });
  }

  function setLang(next) {
    var value = pick(next);
    if (!value) return lang;
    lang = value;
    try {
      localStorage.setItem("se-lang", lang);
    } catch (e) {}
    document.documentElement.lang = lang;
    apply(document);
    document.dispatchEvent(new CustomEvent("se:lang", { detail: lang }));
    return lang;
  }

  document.documentElement.lang = lang;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      apply(document);
    });
  } else {
    apply(document);
  }

  window.t = t;
  window.SE_I18N = {
    t: t,
    apply: apply,
    setLang: setLang,
    langs: supported,
    get lang() {
      return lang;
    },
  };
})();
