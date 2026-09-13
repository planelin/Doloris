(function () {
  "use strict";

  const body = document.body;
  const toggle = document.querySelector(".nav-toggle");
  const nav = document.querySelector(".site-nav");

  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      const isOpen = body.classList.toggle("nav-open");
      toggle.setAttribute("aria-expanded", String(isOpen));
    });

    nav.addEventListener("click", function (event) {
      if (event.target.closest("a")) {
        body.classList.remove("nav-open");
        toggle.setAttribute("aria-expanded", "false");
      }
    });

    window.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        body.classList.remove("nav-open");
        toggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  const currentFile = window.location.pathname.split("/").pop() || "index.html";
  document.querySelectorAll(".site-nav a").forEach(function (link) {
    const target = link.getAttribute("href");
    if (target && target.endsWith(currentFile)) {
      link.setAttribute("aria-current", "page");
    }
  });

  const revealItems = document.querySelectorAll(".reveal");
  if (!revealItems.length) {
    return;
  }

  if (!("IntersectionObserver" in window)) {
    revealItems.forEach(function (item) {
      item.classList.add("is-visible");
    });
    return;
  }

  const observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      }
    });
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });

  revealItems.forEach(function (item) {
    observer.observe(item);
  });
})();
