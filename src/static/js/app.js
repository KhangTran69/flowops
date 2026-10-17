(function () {
  "use strict";

  var root = document.documentElement;
  var THEME_KEY = "flowops-theme";

  function preferredTheme() {
    var stored = localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark" || stored === "auto") return stored;
    return "auto";
  }

  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    var button = document.getElementById("theme-toggle");
    if (button) button.textContent = "Theme: " + theme;
  }

  applyTheme(preferredTheme());

  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var order = ["auto", "light", "dark"];
      var next = order[(order.indexOf(preferredTheme()) + 1) % order.length];
      localStorage.setItem(THEME_KEY, next);
      applyTheme(next);
    });
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function toast(message, kind) {
    var node = document.getElementById("toast");
    if (!node) {
      node = document.createElement("div");
      node.id = "toast";
      node.setAttribute("role", "status");
      node.setAttribute("aria-live", "polite");
      document.body.appendChild(node);
    }
    node.className = "toast " + (kind || "");
    node.textContent = message;
    clearTimeout(node._timer);
    node._timer = setTimeout(function () { node.textContent = ""; }, 6000);
  }

  document.querySelectorAll("[data-copy-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.getElementById(button.getAttribute("data-copy-target"));
      if (!target) return;
      var text = target.textContent || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          toast("Copied to clipboard.", "success");
        }).catch(function () { toast("Could not copy.", "error"); });
      } else {
        toast("Clipboard is not available.", "error");
      }
    });
  });

  document.querySelectorAll("[data-action-url]").forEach(function (button) {
    button.addEventListener("click", function () {
      var url = button.getAttribute("data-action-url");
      var method = button.getAttribute("data-method") || "POST";
      var confirmText = button.getAttribute("data-confirm");
      if (confirmText && !window.confirm(confirmText)) return;

      var original = button.textContent;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = "Working…";

      var options = { method: method, headers: { "Accept": "application/json" } };
      var csrf = csrfToken();
      if (csrf) options.headers["X-CSRF-Token"] = csrf;
      var body = button.getAttribute("data-body");
      if (body) {
        options.headers["Content-Type"] = "application/json";
        options.body = body;
      } else if (method !== "GET" && method !== "HEAD") {
        options.headers["Content-Type"] = "application/json";
        options.body = "{}";
      }

      fetch(url, options).then(function (response) {
        return response.text().then(function (text) {
          var payload = null;
          try { payload = text ? JSON.parse(text) : null; } catch (error) { payload = null; }
          if (!response.ok) {
            var detail = payload && payload.detail ? payload.detail : ("HTTP " + response.status);
            throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
          }
          return payload;
        });
      }).then(function () {
        toast("Action completed.", "success");
        if (button.getAttribute("data-reload") === "true") {
          window.location.reload();
        } else {
          button.disabled = false;
          button.removeAttribute("aria-busy");
          button.textContent = original;
        }
      }).catch(function (error) {
        toast(error.message || "Action failed.", "error");
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = original;
      });
    });
  });

  var autoRefresh = document.getElementById("auto-refresh");
  if (autoRefresh) {
    var REFRESH_KEY = "flowops-auto-refresh";
    if (localStorage.getItem(REFRESH_KEY) === "true") autoRefresh.checked = true;
    autoRefresh.addEventListener("change", function () {
      localStorage.setItem(REFRESH_KEY, autoRefresh.checked ? "true" : "false");
    });

    var refreshNow = document.getElementById("refresh-now");
    if (refreshNow) refreshNow.addEventListener("click", function () { window.location.reload(); });

    window.setInterval(function () {
      if (!autoRefresh.checked || document.hidden) return;
      window.location.reload();
    }, 15000);
  }

  var status = document.getElementById("api-status");
  if (status) {
    fetch("/health", { headers: { "Accept": "application/json" } })
      .then(function (response) { if (!response.ok) throw new Error(); return response.json(); })
      .then(function (payload) {
        status.textContent = payload.status === "ok" ? "API healthy" : "API degraded";
      })
      .catch(function () { status.textContent = "API unavailable"; });
  }
  document.querySelectorAll("[data-ai-apply]").forEach(function (button) {
    button.addEventListener("click", function () {
      var url = button.getAttribute("data-ai-apply");
      var ids = Array.prototype.map.call(
        document.querySelectorAll('input[name="ai-recommendation"]:checked'),
        function (input) { return input.value; }
      );
      if (!ids.length) {
        toast("Select at least one applicable recommendation.", "error");
        return;
      }
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      var headers = { "Content-Type": "application/json", "Accept": "application/json" };
      var csrf = csrfToken();
      if (csrf) headers["X-CSRF-Token"] = csrf;
      fetch(url, {
        method: "POST",
        headers: headers,
        body: JSON.stringify({ recommendation_ids: ids })
      }).then(function (response) {
        return response.text().then(function (text) {
          var payload = null;
          try { payload = text ? JSON.parse(text) : null; } catch (error) { payload = null; }
          if (!response.ok) {
            var detail = payload && payload.detail ? payload.detail : ("HTTP " + response.status);
            throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
          }
          return payload;
        });
      }).then(function () {
        toast("AI recommendations applied.", "success");
        window.location.reload();
      }).catch(function (error) {
        toast(error.message || "Could not apply recommendations.", "error");
        button.disabled = false;
        button.removeAttribute("aria-busy");
      });
    });
  });

})();
