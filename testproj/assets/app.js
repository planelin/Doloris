(() => {
  const grid = document.querySelector('#mechanism-grid');
  if (!grid) return;

  const filters = [...document.querySelectorAll('.filter')];
  const statusText = {
    normal: '正常',
    watch: '观察中',
    active: '已介入'
  };
  let mechanisms = [];

  function escapeHtml(value) {
    return String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;');
  }

  function cardTemplate(item, position) {
    const status = statusText[item.status] ? item.status : 'normal';
    return `
      <article class="mechanism-card" data-status="${escapeHtml(status)}">
        <div class="card-top">
          <span class="card-number">${String(position + 1).padStart(2, '0')} / ${escapeHtml(item.name)}</span>
          <span class="status-badge">${escapeHtml(statusText[status])}</span>
        </div>
        <h3>${escapeHtml(item.title || item.name)}</h3>
        <p>${escapeHtml(item.summary)}</p>
        <a class="card-link" href="pages/${escapeHtml(item.file)}">查看机制详情 <span aria-hidden="true">→</span></a>
      </article>`;
  }

  function render(filter = 'all') {
    const visible = filter === 'all'
      ? mechanisms
      : mechanisms.filter((item) => item.status === filter);

    if (!visible.length) {
      grid.innerHTML = '<div class="loading-card">当前筛选条件下没有监管机制。</div>';
      return;
    }

    grid.innerHTML = visible.map((item) => {
      const originalIndex = mechanisms.indexOf(item);
      return cardTemplate(item, originalIndex);
    }).join('');
  }

  function wireFilters() {
    filters.forEach((button) => {
      button.addEventListener('click', () => {
        filters.forEach((item) => item.classList.toggle('active', item === button));
        render(button.dataset.filter || 'all');
      });
    });
  }

  async function loadMechanisms() {
    try {
      const response = await fetch('data/mechanisms.json', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (!Array.isArray(payload.mechanisms)) throw new Error('mechanisms 必须是数组');
      mechanisms = payload.mechanisms;
      render();
    } catch (error) {
      grid.innerHTML = `
        <div class="loading-card error-card">
          无法读取机制数据。请使用本地 HTTP 服务打开本站。<br>
          <small>${escapeHtml(error.message)}</small>
        </div>`;
      console.error('Unable to load mechanisms:', error);
    }
  }

  wireFilters();
  loadMechanisms();
})();
