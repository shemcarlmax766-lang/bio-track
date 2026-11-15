/* BioTrack client: navigation, offline fault queue, chart helpers, BioAssist. */
(function () {
  "use strict";

  var csrf = (
    document.querySelector('meta[name="csrf-token"]') ||
    document.querySelector('input[name="_csrf"]') ||
    {}
  ).value || "";

  /* ---------------- navigation ---------------- */
  var navToggle = document.querySelector("[data-nav-toggle]");
  var topnav = document.querySelector(".topnav");
  if (navToggle && topnav) {
    navToggle.addEventListener("click", function () {
      var open = topnav.classList.toggle("open");
      navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  var menuToggle = document.querySelector("[data-menu-toggle]");
  if (menuToggle) {
    var menu = menuToggle.parentElement.querySelector(".menu");
    menuToggle.addEventListener("click", function (e) {
      e.stopPropagation();
      var hidden = menu.hasAttribute("hidden");
      if (hidden) {
        menu.removeAttribute("hidden");
      } else {
        menu.setAttribute("hidden", "");
      }
      menuToggle.setAttribute("aria-expanded", hidden ? "true" : "false");
    });
    document.addEventListener("click", function () {
      menu.setAttribute("hidden", "");
      menuToggle.setAttribute("aria-expanded", "false");
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        menu.setAttribute("hidden", "");
        menuToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  /* ---------------- tabs ---------------- */
  document.querySelectorAll("[data-tabs]").forEach(function (group) {
    var buttons = group.querySelectorAll(".tabs button");
    var panels = group.querySelectorAll(".tab-panel");
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        buttons.forEach(function (b) { b.classList.remove("active"); });
        btn.classList.add("active");
        panels.forEach(function (p) {
          p.hidden = p.getAttribute("data-panel") !== btn.getAttribute("data-tab");
        });
      });
    });
    if (buttons.length) { buttons[0].click(); }
  });

  /* ---------------- confirmations ---------------- */
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        e.preventDefault();
      }
    });
  });

  /* ---------------- dashboard charts ---------------- */
  document.querySelectorAll("[data-chart='trend']").forEach(function (el) {
    var data = JSON.parse(el.getAttribute("data-values"));
    var series = [
      { key: "faults", cls: "" },
      { key: "repairs", cls: "alt" }
    ];
    var all = [];
    series.forEach(function (s) { all = all.concat(data[s.key] || []); });
    var max = Math.max.apply(null, all.concat([1]));
    var html = "";
    data.months.forEach(function (label, i) {
      var bars = series.map(function (s) {
        var value = (data[s.key] || [])[i] || 0;
        var h = Math.round((value / max) * 100);
        return '<div class="bar ' + s.cls + '" style="height:' + h +
          '%" title="' + label + " " + s.key + ": " + value + '"></div>';
      }).join("");
      html += '<div class="bar-group"><div class="bars">' + bars +
        '</div><div class="bar-label">' + label + "</div></div>";
    });
    el.innerHTML = html;
  });

  var statusColors = {
    operational: "#1c6b3c",
    faulty: "#b3261e",
    under_repair: "#d79b2c",
    awaiting_parts: "#e0a33a",
    out_of_service: "#7a1c15",
    retired: "#8d9aa6"
  };

  document.querySelectorAll("[data-chart='status']").forEach(function (el) {
    var data = JSON.parse(el.getAttribute("data-values"));
    var total = 0;
    Object.keys(data).forEach(function (k) { total += data[k]; });
    if (!total) {
      el.innerHTML = '<p class="empty">No equipment registered yet.</p>';
      return;
    }
    var bar = '<div class="status-bar">';
    var legend = '<div class="status-legend">';
    Object.keys(data).forEach(function (key) {
      var pct = (data[key] / total) * 100;
      var color = statusColors[key] || "#8d9aa6";
      var label = key.replace(/_/g, " ");
      bar += '<span style="width:' + pct + "%;background:" + color +
        '" title="' + label + ": " + data[key] + '"></span>';
      legend += '<div><i style="background:' + color + '"></i>' + label +
        "<b>" + data[key] + "</b></div>";
    });
    bar += "</div>";
    legend += "</div>";
    el.innerHTML = bar + legend;
  });

  /* ---------------- BioAssist ---------------- */
  var chat = document.querySelector("[data-bioassist]");
  if (chat) {
    var log = chat.querySelector(".chat-log");
    var form = chat.querySelector("form");
    var input = form.querySelector("textarea[name='question']");
    var submit = form.querySelector("button[type='submit']");

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var question = input.value.trim();
      if (question.length < 5) { return; }
      addMessage("user", "You", question);
      input.value = "";
      submit.disabled = true;
      submit.textContent = "BioAssist is thinking…";

      var body = {
        question: question,
        equipment_id: (form.querySelector("[name='equipment_id']") || {}).value || null,
        document_id: (form.querySelector("[name='document_id']") || {}).value || null
      };
      fetch("/api/bioassist/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify(body)
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.error) {
            addMessage("assistant", "BioAssist", data.error);
          } else {
            addMessage("assistant", modeLabel(data.mode), data.answer, data.excerpts);
          }
        })
        .catch(function () {
          addMessage("assistant", "BioAssist",
            "BioAssist could not be reached. Check the connection and try again.");
        })
        .finally(function () {
          submit.disabled = false;
          submit.textContent = "Ask BioAssist";
        });
    });

    function modeLabel(mode) {
      return {
        manual_cited: "AI answer citing your manuals",
        manual_only: "Manual excerpts only (no AI provider)",
        general_ai: "General AI explanation (no manual match)",
        provider_error: "AI unavailable"
      }[mode] || "BioAssist";
    }

    function addMessage(role, who, text, excerpts) {
      var wrap = document.createElement("div");
      wrap.className = "msg " + role;
      var meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = who;
      wrap.appendChild(meta);
      var pre = document.createElement("pre");
      pre.textContent = text;
      wrap.appendChild(pre);
      (excerpts || []).forEach(function (ex) {
        var cite = document.createElement("div");
        cite.className = "citation";
        var strong = document.createElement("b");
        strong.textContent = ex.document + ", page " + ex.page;
        cite.appendChild(strong);
        var p = document.createElement("div");
        p.textContent = ex.text;
        cite.appendChild(p);
        wrap.appendChild(cite);
      });
      log.appendChild(wrap);
      log.scrollTop = log.scrollHeight;
    }
  }

  /* ---------------- offline fault queue (IndexedDB) ---------------- */
  var faultForm = document.querySelector("[data-offline-fault]");
  if (faultForm) {
    var DB_NAME = "biotrack-offline";
    var STORE = "pending-faults";

    function openDb() {
      return new Promise(function (resolve, reject) {
        var req = indexedDB.open(DB_NAME, 1);
        req.onupgradeneeded = function () {
          req.result.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
        };
        req.onsuccess = function () { resolve(req.result); };
        req.onerror = function () { reject(req.error); };
      });
    }

    function queueFault(record) {
      return openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
          var tx = db.transaction(STORE, "readwrite");
          tx.objectStore(STORE).add(record);
          tx.oncomplete = function () { resolve(true); };
          tx.onerror = function () { reject(tx.error); };
        });
      });
    }

    function pendingCount() {
      return openDb().then(function (db) {
        return new Promise(function (resolve) {
          var req = db.transaction(STORE, "readonly").objectStore(STORE).count();
          req.onsuccess = function () { resolve(req.result); };
          req.onerror = function () { resolve(0); };
        });
      });
    }

    function flushQueue() {
      if (!navigator.onLine) { return; }
      openDb().then(function (db) {
        var tx = db.transaction(STORE, "readwrite");
        var store = tx.objectStore(STORE);
        var all = store.getAll();
        all.onsuccess = function () {
          all.result.forEach(function (record) {
            var data = new FormData();
            data.append("_csrf", csrf);
            data.append("equipment_id", record.equipment_id);
            data.append("description", record.description);
            data.append("severity", record.severity);
            fetch("/faults/new", { method: "POST", body: data, credentials: "same-origin" })
              .then(function (res) {
                if (res.ok || res.redirected) {
                  var del = db.transaction(STORE, "readwrite");
                  del.objectStore(STORE).delete(record.id);
                }
              })
              .catch(function () { /* keep it queued */ });
          });
        };
      });
    }

    var banner = document.createElement("div");
    banner.className = "notice notice-warn";
    banner.hidden = true;
    banner.setAttribute("data-offline-banner", "");
    faultForm.parentElement.insertBefore(banner, faultForm);

    function refreshBanner() {
      pendingCount().then(function (n) {
        if (n > 0) {
          banner.hidden = false;
          banner.textContent = n + " fault report(s) saved on this device and waiting " +
            "to upload. They will be sent automatically when the connection returns.";
        } else {
          banner.hidden = true;
        }
      });
    }

    faultForm.addEventListener("submit", function (e) {
      if (navigator.onLine) { return; }
      e.preventDefault();
      var record = {
        equipment_id: faultForm.querySelector("[name='equipment_id']").value,
        description: faultForm.querySelector("[name='description']").value,
        severity: faultForm.querySelector("[name='severity']").value,
        queued_at: new Date().toISOString()
      };
      if (!record.equipment_id || record.description.length < 10) { return; }
      queueFault(record).then(function () {
        faultForm.reset();
        refreshBanner();
      });
    });

    window.addEventListener("online", function () { flushQueue(); refreshBanner(); });
    refreshBanner();
    flushQueue();
  }

  /* ---------------- service worker ---------------- */
  if ("serviceWorker" in navigator && location.protocol === "https:") {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/static/sw.js").catch(function () {});
    });
  }
})();
