/* ============================================================
   Agent 监管系统监控台 · 前端逻辑
   纯静态: 读取 data/mechanisms.json 渲染总览页
   页面通过 <html data-root=".."> 声明站点根相对路径
   ============================================================ */
(function () {
  "use strict";

  var DOC = document.documentElement;
  var ROOT = DOC.getAttribute("data-root") || ".";
  var jsonUrl = (ROOT === "." ? "" : ROOT + "/") + "data/mechanisms.json";

  /* file:// 直接打开时浏览器会拦截本地 JSON 读取, 这里保留一份内置副本兜底,
     保证 <a href="index.html"> 双击打开也能看到完整内容。真实数据仍以 JSON 为准。 */
  var FALLBACK = {
    meta: { title: "Agent 监管系统监控台", updated: "2026-09-13", source: "内置副本" },
    mechanisms: [
      { id: "01", name: "心跳检测", nameEn: "Heartbeat", slug: "heartbeat", page: "pages/01-heartbeat.html", status: "online", statusLabel: "在线", metric: "间隔 30s / 阈值 90s", summary: "以带阶段信息的心跳判定 Agent 是存活、空转还是已经僵死。" },
      { id: "02", name: "指数退避", nameEn: "Exponential Backoff", slug: "backoff", page: "pages/02-backoff.html", status: "online", statusLabel: "在线", metric: "上限 120s / 5 次", summary: "失败后按指数加抖动重试, 把重试风暴收敛成可控节奏。" },
      { id: "03", name: "会话接管", nameEn: "Session Takeover", slug: "takeover", page: "pages/03-takeover.html", status: "online", statusLabel: "在线", metric: "静默 15s / 单写者锁", summary: "原会话失联后由无头进程续跑, 全程只允许一个写入者。" },
      { id: "04", name: "供应商轮换", nameEn: "Provider Rotation", slug: "rotation", page: "pages/04-rotation.html", status: "watch", statusLabel: "观察", metric: "连败 2 次即切换", summary: "当前端点连败即冷却并切到备用端点, 避免整任务被单点拖死。" },
      { id: "05", name: "端点探针", nameEn: "Endpoint Probe", slug: "probe", page: "pages/05-probe.html", status: "online", statusLabel: "在线", metric: "启动前 1 次最小请求", summary: "开工前用最小请求试活, 把配置错和端点故障挡在长任务之前。" },
      { id: "06", name: "出站代理", nameEn: "Egress Proxy", slug: "egress", page: "pages/06-egress.html", status: "online", statusLabel: "在线", metric: "系统代理 / 逐跳可辨", summary: "统一出站链路并保留每跳标识, 让代理故障不被误判成模型故障。" },
      { id: "07", name: "验收清单", nameEn: "Acceptance Checklist", slug: "checklist", page: "pages/07-checklist.html", status: "online", statusLabel: "在线", metric: "全勾才允许停止", summary: "把完成定义落成可勾选条目, 未勾完不许宣布结束。" },
      { id: "08", name: "干预存档", nameEn: "Intervention Archive", slug: "archive", page: "pages/08-archive.html", status: "online", statusLabel: "在线", metric: "每次干预一行 JSON", summary: "所有自动动作按时间线落盘, 事后可复现每一次接管与退避。" },
      { id: "09", name: "睡眠抑制", nameEn: "Sleep Suppression", slug: "sleep-guard", page: "pages/09-sleep-guard.html", status: "online", statusLabel: "在线", metric: "持续申请 / 可恢复", summary: "长跑期间阻止系统休眠, 结束后恢复原设置, 不留副作用。" },
      { id: "10", name: "混沌测试", nameEn: "Chaos Testing", slug: "chaos", page: "pages/10-chaos.html", status: "watch", statusLabel: "观察", metric: "kill:30 / net:30:120", summary: "主动注入确定性的进程与网络故障, 验证自愈链路真的生效。" },
      { id: "11", name: "终态报告", nameEn: "Terminal Report", slug: "report", page: "pages/11-report.html", status: "online", statusLabel: "在线", metric: "成功 / 失败 / 放弃", summary: "把一次监管收敛成一份终态结论, 让结果不再依赖口头描述。" },
      { id: "12", name: "授权边界", nameEn: "Authority Boundary", slug: "boundary", page: "pages/12-boundary.html", status: "standby", statusLabel: "待命", metric: "允许清单 / 拒绝清单", summary: "限定自动化可触及的范围, 无人值守时把可造成损害的半径压到最小。" }
    ]
  };

  var STATUS_CLASS = { online: "is-online", watch: "is-watch", standby: "is-standby" };
  var STATUS_ORDER = ["online", "watch", "standby"];

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function pageHref(page) {
    return (ROOT === "." ? "" : ROOT + "/") + page;
  }

  function two(n) { return n < 10 ? "0" + n : String(n); }

  /* ---------------------------------------------- 时钟 */

  function padClock(el, withSeconds) {
    var d = new Date();
    var t = two(d.getHours()) + ":" + two(d.getMinutes());
    if (withSeconds) { t += ":" + two(d.getSeconds()); }
    el.textContent = t;
    if (el.hasAttribute("datetime")) {
      el.setAttribute("datetime", d.toISOString());
    }
  }

  function initClocks() {
    var clock = document.getElementById("clock");
    var footerTime = document.getElementById("footer-time");
    function tick() {
      if (clock) { padClock(clock, true); }
      if (footerTime) {
        var d = new Date();
        footerTime.textContent = "本地时间 " + two(d.getHours()) + ":" + two(d.getMinutes());
      }
    }
    tick();
    if (clock) { window.setInterval(tick, 1000); }
    else if (footerTime) { window.setInterval(tick, 30000); }
  }

  /* ---------------------------------------------- 滚动显现 */

  function initReveal() {
    var items = Array.prototype.slice.call(document.querySelectorAll(".reveal"));
    if (!items.length) { return; }
    if (!("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("is-visible"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    items.forEach(function (el, i) {
      el.style.transitionDelay = (i % 6) * 60 + "ms";
      io.observe(el);
    });
  }

  /* ---------------------------------------------- 总览渲染 */

  function cardHtml(m) {
    var cls = STATUS_CLASS[m.status] || "is-standby";
    return [
      '<article class="mechanism-card reveal" data-status="', esc(m.status), '">',
        '<div class="mechanism-top">',
          '<span class="mechanism-index">', esc(m.id), '</span>',
          '<span class="status-pill ', cls, '"><i></i>', esc(m.statusLabel), '</span>',
        '</div>',
        '<h3 class="mechanism-name">', esc(m.name), '</h3>',
        '<p class="mechanism-en">', esc(m.nameEn), '</p>',
        '<p class="mechanism-summary">', esc(m.summary), '</p>',
        '<div class="mechanism-foot">',
          '<a class="mechanism-link" href="', esc(pageHref(m.page)), '">查看机制 →</a>',
          '<span class="mechanism-metric">', esc(m.metric), '</span>',
        '</div>',
      '</article>'
    ].join("");
  }

  function statusRowHtml(m) {
    var cls = STATUS_CLASS[m.status] || "is-standby";
    return [
      '<div class="status-row">',
        '<span class="status-name">', esc(m.id), ' · ', esc(m.name), '</span>',
        '<span class="status-code">', esc(m.nameEn), '</span>',
        '<span class="status-pill ', cls, '"><i></i>', esc(m.statusLabel), '</span>',
      '</div>'
    ].join("");
  }

  function render(data, source) {
    var list = (data && data.mechanisms) || [];
    var grid = document.getElementById("mechanism-grid");
    var statusList = document.getElementById("status-list");
    var note = document.getElementById("data-note");

    if (grid) {
      grid.innerHTML = list.map(cardHtml).join("");
      grid.setAttribute("aria-busy", "false");
    }
    if (statusList) {
      statusList.innerHTML = list.map(statusRowHtml).join("");
    }

    var online = list.filter(function (m) { return m.status === "online"; }).length;
    var watch = list.filter(function (m) { return m.status === "watch"; }).length;
    var standby = list.length - online - watch;

    var mTotal = document.getElementById("metric-mechanisms");
    var mOnline = document.getElementById("metric-online");
    var radarCopy = document.getElementById("radar-status-copy");
    if (mTotal) { mTotal.textContent = String(list.length); }
    if (mOnline) { mOnline.textContent = online + " / " + list.length; }
    if (radarCopy) {
      radarCopy.textContent = "在线 " + online + " · 观察 " + watch + " · 待命 " + standby;
    }

    if (note) {
      var stamp = (data && data.meta && data.meta.updated) || "-";
      if (source === "fallback") {
        note.classList.add("is-warn");
        note.textContent = "已用内置副本渲染 (file:// 下浏览器拦截本地 JSON 读取); 用本地服务器打开即可读取 data/mechanisms.json。";
      } else {
        note.classList.remove("is-warn");
        note.textContent = "数据源 data/mechanisms.json · 更新 " + stamp + " · 共 " + list.length + " 项";
      }
    }

    initReveal();
    return list.length;
  }

  function load() {
    var grid = document.getElementById("mechanism-grid");
    if (!grid) { return; }
    grid.setAttribute("aria-busy", "true");

    var done = false;
    function fallback() {
      if (done) { return; }
      done = true;
      render(FALLBACK, "fallback");
    }

    if (typeof window.fetch !== "function") { fallback(); return; }

    window.fetch(jsonUrl, { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) { throw new Error("HTTP " + res.status); }
        return res.json();
      })
      .then(function (data) {
        if (done) { return; }
        done = true;
        render(data, "json");
      })
      .catch(function () { fallback(); });
  }

  function initRefresh() {
    var btn = document.getElementById("refresh-button");
    if (!btn) { return; }
    btn.addEventListener("click", function () {
      btn.classList.add("is-spinning");
      window.setTimeout(function () { btn.classList.remove("is-spinning"); }, 500);
      load();
    });
  }

  function init() {
    initClocks();
    initRefresh();
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
