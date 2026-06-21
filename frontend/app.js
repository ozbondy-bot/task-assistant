/* =============================================
   Task Assistant Mini App — app.js
   ============================================= */

const tg = window.Telegram?.WebApp;
const API_BASE = '';  // Same origin as the Mini App URL
let initData = '';
let currentUser = null;
let currentPoints = 0;

const FOOD_EMOJIS = [
  '🍏','🍎','🍐','🍊','🍋','🍌','🍉','🍇','🍓','🫐','🍒','🍑','🥭','🍍','🥥','🥝',
  '🍅','🥑','🥦','🧄','🧅','🍞','🥐','🥨','🧀','🍖','🍗','🥩','🥓','🍔','🍕','🌮',
  '🥚','🥗','🍿','🥫','🍱','☕','🍵','🥤'
];

const REWARD_ICONS = ['🎉','🏖️','🛀','💆','🍽️','🎬','💄','🌹','🎁','💝','✨','🌙'];

function seededEmoji(id) {
  return FOOD_EMOJIS[id % FOOD_EMOJIS.length];
}

function rewardIcon(id) {
  return REWARD_ICONS[id % REWARD_ICONS.length];
}

// ── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  if (tg) {
    tg.ready();
    tg.expand();
    initData = tg.initData || '';
    const tgUser = tg.initDataUnsafe?.user;
    if (tgUser) {
      currentUser = tgUser;
      document.getElementById('userAvatar').textContent = (tgUser.first_name || '?')[0].toUpperCase();
      document.getElementById('userName').textContent = tgUser.first_name || 'Пользователь';
    }
  } else {
    // Dev mode — use mock initData
    console.warn('[DEV] Telegram WebApp not available. Using mock data.');
    initData = 'dev_mock';
    document.getElementById('userName').textContent = 'Шурик';
    document.getElementById('userAvatar').textContent = 'Ш';
  }

  setupTabs();
  loadHouseTab();
  setupModals();
  setupFABs();
});

// ── API Helper ───────────────────────────────────────────────────────────────
async function api(method, path, body = null) {
  const opts = {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-Init-Data': initData,
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(API_BASE + path, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Ошибка сети' }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── Toast ────────────────────────────────────────────────────────────────────
let toastTimer;
function showToast(msg, duration = 2500) {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add('hidden'), duration);
}

// ── Tabs ─────────────────────────────────────────────────────────────────────
function setupTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`pane-${tab}`).classList.add('active');

      if (tab === 'house') loadHouseTab();
      else if (tab === 'personal') loadPersonalTab();
      else if (tab === 'shopping') loadShoppingTab();
      else if (tab === 'shop') loadShopTab();
    });
  });
}

// ── House Tab ─────────────────────────────────────────────────────────────────
async function loadHouseTab() {
  const list = document.getElementById('houseTasksList');
  const mList = document.getElementById('membersList');
  list.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;

  try {
    const [tasks, members] = await Promise.all([
      api('GET', '/api/house/tasks'),
      api('GET', '/api/house/members'),
    ]);

    renderHouseTasks(tasks);
    renderMembers(members);

    const me = members.find(m => m.is_me);
    if (me) {
      document.getElementById('userPoints').textContent = `${me.points} ✨`;
      currentPoints = me.points;
    }
  } catch (e) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Не удалось загрузить</div><div class="empty-sub">${e.message}</div></div>`;
  }
}

function renderHouseTasks(tasks) {
  const list = document.getElementById('houseTasksList');
  if (!tasks.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">✨</div><div class="empty-title">Все дела на сегодня разобраны!</div><div class="empty-sub">Возвращайся завтра</div></div>`;
    return;
  }
  list.innerHTML = tasks.map(t => `
    <div class="task-card house-task" id="htask-${t.id}">
      <div class="task-icon">🏠</div>
      <div class="task-body">
        <div class="task-title">${escHtml(t.title)}</div>
        <div class="task-meta">
          <span class="task-badge badge-points">+${t.points} ✨</span>
          <span class="task-badge">${periodLabel(t.periodicity)}</span>
        </div>
      </div>
      <div class="task-action">
        <button class="btn-take" onclick="claimTask(${t.id})">Взять</button>
      </div>
    </div>
  `).join('');
}

async function claimTask(instanceId) {
  try {
    await api('POST', `/api/house/tasks/${instanceId}/claim`);
    showToast('🏠 Задача взята! Теперь она в «Мои дела»');
    loadHouseTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

function renderMembers(members) {
  const list = document.getElementById('membersList');
  const medals = ['🥇', '🥈', '🥉'];
  const sorted = [...members].sort((a, b) => b.points - a.points);
  list.innerHTML = sorted.map((m, i) => `
    <div class="member-card">
      <div class="member-avatar">${(m.display_name || '?')[0].toUpperCase()}</div>
      <div class="member-name">
        ${escHtml(m.display_name || 'Участник')}
        ${m.is_me ? '<span class="you-badge">это ты</span>' : ''}
      </div>
      <div class="member-pts">${medals[i] || ''} ${m.points} ✨</div>
    </div>
  `).join('');
}

// ── Personal Tab ──────────────────────────────────────────────────────────────
async function loadPersonalTab() {
  const list = document.getElementById('personalTasksList');
  list.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;

  try {
    const data = await api('GET', '/api/tasks/today');
    renderPersonalTasks(data.personal, data.household);
  } catch (e) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Ошибка</div><div class="empty-sub">${e.message}</div></div>`;
  }
}

function renderPersonalTasks(personal, household) {
  const list = document.getElementById('personalTasksList');
  const all = [
    ...household.map(t => ({ ...t, isHousehold: true })),
    ...personal.map(t => ({ ...t, isHousehold: false })),
  ];

  if (!all.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">🎉</div><div class="empty-title">Всё сделано!</div><div class="empty-sub">На сегодня задач нет</div></div>`;
    return;
  }

  list.innerHTML = all.map(t => {
    if (t.isHousehold) {
      return `
        <div class="task-card household-claimed" id="ptask-h-${t.id}">
          <div class="task-icon">🏠</div>
          <div class="task-body">
            <div class="task-title">${escHtml(t.text)}</div>
            <div class="task-meta">
              <span class="task-badge badge-house">Домашнее</span>
              <span class="task-badge badge-points">+${t.points} ✨</span>
            </div>
          </div>
          <div class="task-action">
            <button class="btn-done" onclick="completeHouseTask(${t.id})">Готово</button>
          </div>
        </div>`;
    } else {
      const icon = getTaskIcon(t.text);
      const cleanText = stripEmoji(t.text);
      return `
        <div class="task-card" id="ptask-${t.id}" onclick="completePersonalTask(${t.id}, this)">
          <div class="task-icon">${icon}</div>
          <div class="task-body">
            <div class="task-title">${escHtml(cleanText)}</div>
            <div class="task-meta">
              ${t.recurrence ? `<span class="task-badge badge-rec">🔁 ${recLabel(t.recurrence)}</span>` : ''}
              ${t.category === 'routine' ? '<span class="task-badge">Ежедневное</span>' : ''}
            </div>
          </div>
        </div>`;
    }
  }).join('');
}

async function completePersonalTask(id, el) {
  el.classList.add('completed-anim');
  try {
    await api('POST', `/api/tasks/${id}/complete`);
    setTimeout(() => {
      el.remove();
      checkEmptyPersonal();
    }, 400);
    showToast('✅ Выполнено!');
  } catch (e) {
    el.classList.remove('completed-anim');
    showToast(`⚠️ ${e.message}`);
  }
}

async function completeHouseTask(id) {
  try {
    const res = await api('POST', `/api/house/tasks/${id}/done`);
    showToast(`✅ Готово! +${res.points_earned} ✨`);
    document.getElementById(`ptask-h-${id}`)?.remove();
    checkEmptyPersonal();
    loadHouseTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

function checkEmptyPersonal() {
  const list = document.getElementById('personalTasksList');
  if (!list.children.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">🎉</div><div class="empty-title">Всё сделано!</div><div class="empty-sub">На сегодня задач нет</div></div>`;
  }
}

// ── Add Task Modal ────────────────────────────────────────────────────────────
function setupModals() {
  document.getElementById('cancelTaskBtn').addEventListener('click', () => {
    document.getElementById('addTaskModal').classList.add('hidden');
  });

  document.getElementById('saveTaskBtn').addEventListener('click', async () => {
    const text = document.getElementById('newTaskInput').value.trim();
    if (!text) return;
    try {
      await api('POST', '/api/tasks', { text });
      document.getElementById('addTaskModal').classList.add('hidden');
      document.getElementById('newTaskInput').value = '';
      showToast('✅ Задача добавлена!');
      loadPersonalTab();
    } catch (e) {
      showToast(`⚠️ ${e.message}`);
    }
  });

  document.getElementById('cancelShopBtn').addEventListener('click', () => {
    document.getElementById('addShopModal').classList.add('hidden');
  });

  document.getElementById('saveShopBtn').addEventListener('click', async () => {
    const name = document.getElementById('shopItemName').value.trim();
    const price = parseInt(document.getElementById('shopItemPrice').value) || 0;
    const urgent = document.getElementById('shopItemUrgent').checked;
    if (!name) return;
    try {
      await api('POST', '/api/shopping', { item_name: name, price, priority: urgent ? 'high' : 'normal' });
      document.getElementById('addShopModal').classList.add('hidden');
      document.getElementById('shopItemName').value = '';
      document.getElementById('shopItemPrice').value = '';
      document.getElementById('shopItemUrgent').checked = false;
      showToast('🛒 Добавлено в список!');
      loadShoppingTab();
    } catch (e) {
      showToast(`⚠️ ${e.message}`);
    }
  });

  // Close modals on overlay click
  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) overlay.classList.add('hidden');
    });
  });
}

function setupFABs() {
  document.getElementById('addTaskBtn').addEventListener('click', () => {
    document.getElementById('addTaskModal').classList.remove('hidden');
    document.getElementById('newTaskInput').focus();
  });

  document.getElementById('addShopBtn').addEventListener('click', () => {
    document.getElementById('addShopModal').classList.remove('hidden');
    document.getElementById('shopItemName').focus();
  });
}

// ── Shopping Tab ──────────────────────────────────────────────────────────────
async function loadShoppingTab() {
  const list = document.getElementById('shoppingList');
  list.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;

  try {
    const items = await api('GET', '/api/shopping');
    renderShoppingList(items);
  } catch (e) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Ошибка</div><div class="empty-sub">${e.message}</div></div>`;
  }
}

function renderShoppingList(items) {
  const list = document.getElementById('shoppingList');
  const total = items.reduce((s, i) => s + i.price, 0);
  document.getElementById('shoppingTotal').textContent = total > 0 ? `Сумма: ${total} ₽` : 'Список покупок';

  if (!items.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">🛒</div><div class="empty-title">Список пуст</div><div class="empty-sub">Нажми «+» чтобы добавить товар</div></div>`;
    return;
  }

  list.innerHTML = items.map(item => `
    <div class="shop-item-card ${item.priority === 'high' ? 'urgent' : ''}" id="sitem-${item.id}" onclick="markBought(${item.id}, this)">
      <div class="shop-emoji">${seededEmoji(item.id)}</div>
      <div class="shop-body">
        <div class="shop-name">${item.priority === 'high' ? '🔴 ' : ''}${escHtml(item.item_name)}</div>
        ${item.price > 0 ? `<div class="shop-price">${item.price} ₽</div>` : ''}
      </div>
      <div class="shop-check">✓</div>
    </div>
  `).join('');
}

async function markBought(id, el) {
  el.style.opacity = '0.4';
  el.style.transition = 'opacity 0.3s';
  try {
    await api('POST', `/api/shopping/${id}/bought`);
    setTimeout(() => {
      el.remove();
      loadShoppingTab();
    }, 300);
    showToast('✅ Куплено!');
  } catch (e) {
    el.style.opacity = '1';
    showToast(`⚠️ ${e.message}`);
  }
}

// ── Shop / Rewards Tab ────────────────────────────────────────────────────────
async function loadShopTab() {
  const rList = document.getElementById('rewardsList');
  const lb = document.getElementById('leaderboard');
  rList.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;

  try {
    const [rewardsData, statsData] = await Promise.all([
      api('GET', '/api/rewards'),
      api('GET', '/api/stats'),
    ]);

    document.getElementById('shopPoints').textContent = rewardsData.user_points;
    currentPoints = rewardsData.user_points;

    renderRewards(rewardsData.rewards, rewardsData.user_points);
    renderLeaderboard(statsData.leaderboard);
  } catch (e) {
    rList.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Ошибка</div><div class="empty-sub">${e.message}</div></div>`;
  }
}

function renderRewards(rewards, myPoints) {
  const list = document.getElementById('rewardsList');
  if (!rewards.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">🎁</div><div class="empty-title">Наград пока нет</div></div>`;
    return;
  }
  list.innerHTML = rewards.map(r => `
    <div class="reward-card">
      <div class="reward-icon">${rewardIcon(r.id)}</div>
      <div class="reward-body">
        <div class="reward-title">${escHtml(r.title)}</div>
        <div class="reward-price">${r.price} ✨ <span>очков</span></div>
      </div>
      <button class="btn-buy" ${myPoints < r.price ? 'disabled' : ''} onclick="buyReward(${r.id}, '${escAttr(r.title)}')">
        ${myPoints >= r.price ? 'Купить' : `-${r.price - myPoints}`}
      </button>
    </div>
  `).join('');
}

async function buyReward(id, title) {
  try {
    const res = await api('POST', `/api/rewards/${id}/buy`);
    showToast(`🎉 Куплено: ${title}!`, 3000);
    loadShopTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

function renderLeaderboard(entries) {
  const lb = document.getElementById('leaderboard');
  const medals = ['🥇', '🥈', '🥉'];
  lb.innerHTML = entries.map((e, i) => `
    <div class="lb-row ${e.is_me ? 'me' : ''}">
      <div class="lb-rank">${medals[i] || `${i + 1}.`}</div>
      <div class="lb-name">${escHtml(e.display_name)}${e.is_me ? ' <span style="color:var(--accent);font-size:11px">ты</span>' : ''}</div>
      <div class="lb-pts">${e.points} ✨</div>
    </div>
  `).join('');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function escHtml(str) {
  if (!str) return '';
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escAttr(str) {
  return (str || '').replace(/'/g, "\\'");
}

function stripEmoji(text) {
  const icons = ['🟢','🟡','🔴','❗️','⚡','⬜','💧','🤸‍♂️','🚶‍♂️','🐈','📚'];
  let t = text;
  for (const icon of icons) t = t.replaceAll(icon, '');
  return t.trim();
}

function getTaskIcon(text) {
  if (text.includes('💧')) return '💧';
  if (text.includes('🤸')) return '🤸‍♂️';
  if (text.includes('🚶')) return '🚶‍♂️';
  if (text.includes('🐈')) return '🐈';
  if (text.includes('📚')) return '📚';
  if (text.includes('🔴')) return '🔴';
  if (text.includes('🟡')) return '🟡';
  return '🟢';
}

function periodLabel(p) {
  const m = { daily:'Ежедневно', weekly:'Еженедельно', twice_weekly:'2 раза/нед', monthly:'Ежемесячно',
               twice_monthly:'2 раза/мес', quarterly:'Раз в квартал' };
  return m[p] || p;
}

function recLabel(r) {
  const m = { daily:'Ежедневно', weekly:'Еженед.', biweekly:'Раз в 2 нед.', monthly:'Ежемес.' };
  return m[r] || r;
}
