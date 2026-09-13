/* 决策实验站：基础交互脚本 */

(function () {
  "use strict";

  /* 1. 根据当前文件名标记导航中的当前页 */
  var current = window.location.pathname.split("/").pop() || "index.html";

  document.querySelectorAll(".site-nav a").forEach(function (link) {
    var href = link.getAttribute("href") || "";
    if (href.split("/").pop() === current) {
      link.setAttribute("aria-current", "page");
    }
  });

  /* 2. 首页卡片入场：依次淡入，避免一次性全部出现 */
  var cards = document.querySelectorAll(".card");

  if (cards.length && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    cards.forEach(function (card, index) {
      card.style.opacity = "0";
      card.style.transform = "translateY(12px)";
      card.style.transition = "opacity 420ms ease, transform 420ms ease";

      window.setTimeout(function () {
        card.style.opacity = "1";
        card.style.transform = "translateY(0)";
      }, 80 * index);
    });
  }
})();