(() => {
  const year = document.querySelector("[data-current-year]");
  if (year) year.textContent = new Date().getFullYear();

  const currentFile = window.location.pathname.split("/").pop() || "index.html";
  document.querySelectorAll(".nav-links a").forEach((link) => {
    const target = link.getAttribute("href")?.split("/").pop();
    if (target === currentFile) link.setAttribute("aria-current", "page");
  });
})();
