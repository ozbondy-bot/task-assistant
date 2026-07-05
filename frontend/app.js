/* =============================================
   Task Assistant Mini App — app.js
   ============================================= */

const tg = window.Telegram?.WebApp;
const API_BASE = '';  // Same origin as the Mini App URL
let initData = '';
let currentUser = null;
let currentPoints = 0;
let shiftTaskId = null;
let shiftTaskType = null;

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
      else if (tab === 'settings') loadSettingsTab();
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
    <div class="task-card house-task flex-between" onclick="openChoreDetails(${JSON.stringify(t).replace(/"/g, '&quot;')})" style="padding: 10px 12px; cursor: pointer;">
      <div class="task-left flex-row" style="align-items: center; gap: 8px;">
        <span style="font-size: 16px;">🏠</span>
        <span class="task-title" style="font-weight: 500; font-size: 14px;">${escHtml(stripEmoji(t.title))}</span>
      </div>
      <span class="task-badge badge-points" style="font-size: 12px; margin-left: auto;">+${t.points} ✨</span>
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
        <div class="task-card house-task flex-between" onclick="openMyTaskDetails(${JSON.stringify(t).replace(/"/g, '&quot;')}, 'household')" style="padding: 10px 12px; cursor: pointer; margin-bottom: 8px;">
          <div class="task-left flex-row" style="align-items: center; gap: 8px;">
            <span style="font-size: 16px;">🏠</span>
            <span class="task-title" style="font-weight: 500; font-size: 14px;">${escHtml(stripEmoji(t.text))}</span>
          </div>
          <span class="task-badge badge-points" style="font-size: 12px; margin-left: auto;">+${t.points} ✨</span>
        </div>`;
    } else {
      return `
        <div class="task-card personal-task flex-between" onclick="openMyTaskDetails(${JSON.stringify(t).replace(/"/g, '&quot;')}, 'personal')" style="padding: 10px 12px; cursor: pointer; margin-bottom: 8px;">
          <div class="task-left flex-row" style="align-items: center; gap: 8px;">
            <span style="font-size: 16px;">👤</span>
            <span class="task-title" style="font-weight: 500; font-size: 14px;">${escHtml(stripEmoji(t.text))}</span>
          </div>
          ${t.recurrence ? `<span class="task-badge badge-rec" style="font-size: 11px; margin-left: auto;">🔁</span>` : ''}
        </div>`;
    }
  }).join('');
}

async function completePersonalTask(id) {
  try {
    await api('POST', `/api/tasks/${id}/complete`);
    showToast('✅ Выполнено!');
    loadPersonalTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

let pendingCookingTaskId = null;

async function completeHouseTask(id, title) {
  if (title && (title.toLowerCase().includes('готов') || title.toLowerCase().includes('cook'))) {
    pendingCookingTaskId = id;
    document.getElementById('cookingDurationModal').classList.remove('hidden');
    return;
  }
  try {
    const res = await api('POST', `/api/house/tasks/${id}/done`);
    showToast(`✅ Готово! +${res.points_earned} ✨`);
    loadPersonalTab();
    loadHouseTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

async function submitCookingDone(points) {
  const id = pendingCookingTaskId;
  document.getElementById('cookingDurationModal').classList.add('hidden');
  if (!id) return;
  try {
    const res = await api('POST', `/api/house/tasks/${id}/done?points=${points}`);
    showToast(`✅ Готово! +${res.points_earned} ✨`);
    loadPersonalTab();
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
    const date = document.getElementById('newTaskDate').value || null;
    const recurrence = document.getElementById('newTaskRecurrence').value || null;
    
    // Snappy modal close
    document.getElementById('addTaskModal').classList.add('hidden');
    document.getElementById('newTaskInput').value = '';
    document.getElementById('newTaskDate').value = '';
    document.getElementById('newTaskRecurrence').value = '';
    
    try {
      await api('POST', '/api/tasks', { text, date, recurrence });
      showToast('✅ Задача добавлена!');
      loadPersonalTab();
    } catch (e) {
      showToast(`⚠️ ${e.message}`);
      loadPersonalTab();
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

  // Chores templates modal
  document.getElementById('cancelChoreBtn').addEventListener('click', () => {
    document.getElementById('addChoreModal').classList.add('hidden');
  });

  document.getElementById('saveChoreTmplBtn').addEventListener('click', async () => {
    const title = document.getElementById('choreTmplTitle').value.trim();
    const points = parseInt(document.getElementById('choreTmplPoints').value) || 1;
    const periodicity = document.getElementById('choreTmplPeriodicity').value;
    const startDate = document.getElementById('choreTmplStartDate').value;
    if (!title) return;
    try {
      const res = await api('POST', '/api/chores/templates', { title, points, periodicity, start_date: startDate || null });
      document.getElementById('addChoreModal').classList.add('hidden');
      document.getElementById('choreTmplTitle').value = '';
      document.getElementById('choreTmplPoints').value = '1';
      document.getElementById('choreTmplStartDate').value = '';
      if (res && res.pending) {
        showToast(res.message || '⏳ Запрос отправлен на согласование партнёру!');
      } else {
        showToast('✅ Шаблон добавлен!');
      }
      loadSettingsTab();
    } catch (e) {
      showToast(`⚠️ ${e.message}`);
    }
  });

  // Rewards settings modal
  document.getElementById('cancelRewardBtn').addEventListener('click', () => {
    document.getElementById('createRewardModal').classList.add('hidden');
  });

  document.getElementById('saveRewardBtn').addEventListener('click', async () => {
    const title = document.getElementById('rewardTitleInput').value.trim();
    const price = parseInt(document.getElementById('rewardPriceInput').value) || 1;
    if (!title) return;
    try {
      await api('POST', '/api/rewards', { title, price });
      document.getElementById('createRewardModal').classList.add('hidden');
      document.getElementById('rewardTitleInput').value = '';
      document.getElementById('rewardPriceInput').value = '';
      showToast('✅ Награда добавлена!');
      loadSettingsTab();
    } catch (e) {
      showToast(`⚠️ ${e.message}`);
    }
  });

  // Shift modal
  document.getElementById('cancelShiftBtn').addEventListener('click', () => {
    document.getElementById('shiftTaskModal').classList.add('hidden');
  });

  document.getElementById('saveShiftBtn').addEventListener('click', async () => {
    const newDate = document.getElementById('shiftTaskDate').value;
    if (!newDate) return;
    try {
      if (shiftTaskType === 'chore') {
        await api('POST', `/api/house/tasks/${shiftTaskId}/shift`, { new_date: newDate });
        loadHouseTab();
      } else {
        await api('POST', `/api/tasks/${shiftTaskId}/shift`, { new_date: newDate });
        loadPersonalTab();
      }
      document.getElementById('shiftTaskModal').classList.add('hidden');
      showToast('🗓 Задача перенесена!');
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

  // House tab add button
  document.getElementById('addChoreBtn').addEventListener('click', () => {
    document.getElementById('addChoreChoiceModal').classList.remove('hidden');
  });

  document.getElementById('addRewardBtn').addEventListener('click', () => {
    document.getElementById('createRewardModal').classList.remove('hidden');
  });

  // Archive buttons listeners
  document.getElementById('viewChoresArchiveBtn').addEventListener('click', openChoresArchive);
  document.getElementById('viewTasksArchiveBtn').addEventListener('click', openTasksArchive);
  document.getElementById('viewShoppingArchiveBtn').addEventListener('click', openShoppingArchive);
  document.getElementById('viewPurchasesArchiveBtnFromSettings').addEventListener('click', openPurchasesArchive);
  document.getElementById('viewPurchasesArchiveBtn').addEventListener('click', openPurchasesArchive);
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
    <div class="reward-card" style="display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; width: 100%;">
      <div class="reward-title" style="font-weight: 500; font-size: 14px; text-align: left;">${escHtml(stripEmoji(r.title))}</div>
      <button class="btn-buy" ${myPoints < r.price ? 'disabled' : ''} onclick="buyReward(${r.id}, '${escAttr(r.title)}')" style="font-size: 13px; padding: 6px 12px; font-weight: 600; border-radius: 12px; border: none; cursor: pointer; white-space: nowrap;">
        ${r.price} ✨
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
  if (!text) return '';
  return text
    .replace(/\p{Extended_Pictographic}/gu, '')
    .replace(/[\u{1F300}-\u{1F9FF}]|[\u{1F600}-\u{1F64F}]|[\u{1F680}-\u{1F6FF}]|[\u{2600}-\u{26FF}]|[\u{2700}-\u{27BF}]/gu, '')
    .trim();
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
  const m = { 
    daily: 'Ежедневно', 
    weekly: 'Еженедельно', 
    twice_weekly: '2 раза/нед', 
    monthly: 'Ежемесячно',
    twice_monthly: '2 раза/мес', 
    quarterly: 'Раз в квартал'
  };
  if (m[p]) return m[p];
  if (p && p.startsWith('every_') && p.endsWith('_days')) {
    const days = p.split('_')[1];
    return `Каждые ${days} дней`;
  }
  return p || 'Каждые 30 дней';
}

function recLabel(r) {
  const m = { daily:'Ежедневно', weekly:'Еженед.', biweekly:'Раз в 2 нед.', monthly:'Ежемес.' };
  return m[r] || r;
}

// ── Settings Tab & Actions ───────────────────────────────────────────────────
async function loadSettingsTab() {
  const choresList = document.getElementById('settingsChoresList');
  const rewardsList = document.getElementById('settingsRewardsList');
  choresList.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;
  rewardsList.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;

  try {
    const [templates, rewardsData] = await Promise.all([
      api('GET', '/api/chores/templates'),
      api('GET', '/api/rewards'),
    ]);

    // Render templates & rewards
    renderChoresTemplates(templates);
    renderRewardsTemplates(rewardsData.rewards);
  } catch (e) {
    choresList.innerHTML = `<p style="color:var(--danger)">${e.message}</p>`;
    rewardsList.innerHTML = `<p style="color:var(--danger)">${e.message}</p>`;
  }
}

function renderChoresTemplates(templates) {
  const list = document.getElementById('settingsChoresList');
  if (!templates.length) {
    list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:12px">Нет шаблонов</p>`;
    return;
  }
  list.innerHTML = templates.map(t => `
    <div class="settings-item">
      <div class="settings-item-body">
        <div class="settings-item-title">${escHtml(t.title)}</div>
        <div class="settings-item-meta">${t.points} 🍪 • ${periodLabel(t.periodicity)}</div>
      </div>
      <button class="btn-delete-icon" onclick="deleteChoreTemplate(${t.id})">🗑</button>
    </div>
  `).join('');
}

function renderRewardsTemplates(rewards) {
  const list = document.getElementById('settingsRewardsList');
  if (!rewards.length) {
    list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:12px">Нет наград</p>`;
    return;
  }
  list.innerHTML = rewards.map(r => `
    <div class="settings-item">
      <div class="settings-item-body">
        <div class="settings-item-title">${escHtml(r.title)}</div>
        <div class="settings-item-meta">${r.price} 🍪</div>
      </div>
      <button class="btn-delete-icon" onclick="deleteReward(${r.id})">🗑</button>
    </div>
  `).join('');
}

async function deleteChoreTemplate(id) {
  if (!confirm('Удалить этот шаблон дела?')) return;
  try {
    const res = await api('DELETE', `/api/chores/templates/${id}`);
    if (res && res.pending) {
      showToast(res.message || '⏳ Запрос на удаление отправлен партнёру!');
    } else {
      showToast('🗑 Шаблон удален!');
    }
    loadSettingsTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

async function deleteReward(id) {
  if (!confirm('Удалить эту награду?')) return;
  try {
    await api('DELETE', `/api/rewards/${id}`);
    showToast('🗑 Награда удалена!');
    loadSettingsTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

async function unclaimChore(id) {
  try {
    await api('POST', `/api/house/tasks/${id}/unclaim`);
    showToast('↩ Задача возвращена в свободные');
    loadPersonalTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

async function skipChore(id) {
  try {
    await api('POST', `/api/house/tasks/${id}/skip`);
    showToast('🗑 Задача пропущена на сегодня');
    loadPersonalTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

function openShiftModal(id, type) {
  shiftTaskId = id;
  shiftTaskType = type;
  document.getElementById('shiftTaskModal').classList.remove('hidden');
  document.getElementById('shiftTaskDate').value = new Date().toISOString().split('T')[0];
}


/* ── Free Chores & Nudge ── */
async function nudgeTask(instanceId) {
  try {
    await api('POST', `/api/house/tasks/${instanceId}/nudge`);
    showToast('🔔 Намек отправлен партнеру!');
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

async function skipFreeChore(instanceId) {
  if (!confirm('Удалить эту копию дела на сегодня?')) return;
  try {
    await api('POST', `/api/house/tasks/${instanceId}/skip`);
    showToast('🗑 Копия дела удалена на сегодня');
    loadHouseTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}


/* ── Archives Managers ── */
let choresArchivePage = 0;
let tasksArchivePage = 0;
let purchasesArchivePage = 0;

function closeModal(modalId) {
  document.getElementById(modalId).classList.add('hidden');
}

async function openChoresArchive() {
  choresArchivePage = 0;
  document.getElementById('choresArchiveModal').classList.remove('hidden');
  await loadChoresArchive(0);
}

async function loadChoresArchive(page) {
  choresArchivePage = page;
  const list = document.getElementById('choresArchiveList');
  const pag = document.getElementById('choresArchivePagination');
  list.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;
  pag.innerHTML = '';
  
  try {
    const items = await api('GET', `/api/archive/chores?page=${page}&limit=10`);
    if (!items.length) {
      list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:24px">Архив пуст</p>`;
      return;
    }
    
    list.innerHTML = items.map(c => {
      const dt = new Date(c.date);
      const dtStr = dt.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
      return `
        <div class="archive-item">
          <div class="archive-item-info">
            <span class="archive-item-title">${escHtml(c.title)}</span>
            <span class="archive-item-meta">${dtStr} • Выполнил: ${escHtml(c.user)}</span>
          </div>
          <div class="flex-row" style="gap: 8px; align-items: center;">
            <span class="task-badge badge-points" style="font-size:11px;">+${c.points} ✨</span>
            <button class="btn btn-primary btn-xs" style="font-size:11px;padding:3px 8px;" onclick="restoreChoreFromArchive(${c.id})">Вернуть</button>
          </div>
        </div>
      `;
    }).join('');
    
    pag.innerHTML = `
      <button class="btn btn-secondary btn-xs" ${page === 0 ? 'disabled' : ''} onclick="loadChoresArchive(${page - 1})">⏪ Назад</button>
      <span style="font-size:12px;color:var(--text2)">Страница ${page + 1}</span>
      <button class="btn btn-secondary btn-xs" ${items.length < 10 ? 'disabled' : ''} onclick="loadChoresArchive(${page + 1})">Вперед ⏩</button>
    `;
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);text-align:center;padding:24px">${e.message}</p>`;
  }
}

async function openTasksArchive() {
  tasksArchivePage = 0;
  document.getElementById('tasksArchiveModal').classList.remove('hidden');
  await loadTasksArchive(0);
}

async function loadTasksArchive(page) {
  tasksArchivePage = page;
  const list = document.getElementById('tasksArchiveList');
  const pag = document.getElementById('tasksArchivePagination');
  list.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;
  pag.innerHTML = '';
  
  try {
    const items = await api('GET', `/api/archive/tasks?page=${page}&limit=10`);
    if (!items.length) {
      list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:24px">Архив пуст</p>`;
      return;
    }
    
    list.innerHTML = items.map(t => {
      const rec = t.recurrence ? `🔁 ${recLabel(t.recurrence)}` : '';
      return `
        <div class="archive-item">
          <div class="archive-item-info">
            <span class="archive-item-title" style="text-decoration: line-through;color:var(--text3);">${escHtml(t.text)}</span>
            <span class="archive-item-meta">${escHtml(t.date)} ${rec}</span>
          </div>
          <button class="btn btn-primary btn-xs" style="font-size:11px;padding:3px 8px;" onclick="restoreTaskFromArchive(${t.id})">Вернуть</button>
        </div>
      `;
    }).join('');
    
    pag.innerHTML = `
      <button class="btn btn-secondary btn-xs" ${page === 0 ? 'disabled' : ''} onclick="loadTasksArchive(${page - 1})">⏪ Назад</button>
      <span style="font-size:12px;color:var(--text2)">Страница ${page + 1}</span>
      <button class="btn btn-secondary btn-xs" ${items.length < 10 ? 'disabled' : ''} onclick="loadTasksArchive(${page + 1})">Вперед ⏩</button>
    `;
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);text-align:center;padding:24px">${e.message}</p>`;
  }
}

async function restoreTaskFromArchive(id) {
  try {
    await api('POST', `/api/archive/tasks/${id}/restore`);
    showToast('↩ Задача вернулась в Мои дела!');
    loadTasksArchive(tasksArchivePage);
    loadPersonalTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

async function openPurchasesArchive() {
  purchasesArchivePage = 0;
  document.getElementById('purchasesArchiveModal').classList.remove('hidden');
  await loadPurchasesArchive(0);
}

async function loadPurchasesArchive(page) {
  purchasesArchivePage = page;
  const list = document.getElementById('purchasesArchiveList');
  const pag = document.getElementById('purchasesArchivePagination');
  list.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;
  pag.innerHTML = '';
  
  try {
    const items = await api('GET', `/api/archive/purchases?page=${page}&limit=10`);
    if (!items.length) {
      list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:24px">Покупок пока не было</p>`;
      return;
    }
    
    list.innerHTML = items.map(p => {
      const dt = new Date(p.date);
      const dtStr = dt.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
      return `
        <div class="archive-item">
          <div class="archive-item-info">
            <span class="archive-item-title">${escHtml(p.reward_title)}</span>
            <span class="archive-item-meta">${dtStr} • Купил: ${escHtml(p.user)}</span>
          </div>
          <span class="task-badge badge-points" style="background:rgba(251,191,36,0.15);color:var(--warning);font-size:11px;">-${p.price} 🍪</span>
        </div>
      `;
    }).join('');
    
    pag.innerHTML = `
      <button class="btn btn-secondary btn-xs" ${page === 0 ? 'disabled' : ''} onclick="loadPurchasesArchive(${page - 1})">⏪ Назад</button>
      <span style="font-size:12px;color:var(--text2)">Страница ${page + 1}</span>
      <button class="btn btn-secondary btn-xs" ${items.length < 10 ? 'disabled' : ''} onclick="loadPurchasesArchive(${page + 1})">Вперед ⏩</button>
    `;
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);text-align:center;padding:24px">${e.message}</p>`;
  }
}

async function deletePersonalTask(id) {
  if (!confirm('Удалить эту задачу?')) return;
  try {
    await api('DELETE', `/api/tasks/${id}`);
    showToast('🗑 Задача удалена');
    loadPersonalTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

// Expose to window object for inline onclick event handlers
window.loadChoresArchive = loadChoresArchive;
window.loadTasksArchive = loadTasksArchive;
window.loadPurchasesArchive = loadPurchasesArchive;
window.restoreTaskFromArchive = restoreTaskFromArchive;
window.closeModal = closeModal;
window.openChoresArchive = openChoresArchive;
window.openTasksArchive = openTasksArchive;
window.openPurchasesArchive = openPurchasesArchive;
window.nudgeTask = nudgeTask;
window.skipFreeChore = skipFreeChore;
window.deletePersonalTask = deletePersonalTask;
window.claimTask = claimTask;
window.openShiftModal = openShiftModal;
window.completePersonalTask = completePersonalTask;
window.completeHouseTask = completeHouseTask;
window.unclaimChore = unclaimChore;
window.skipChore = skipChore;

/* ── Chore Details & Tab Switching ── */
function openChoreDetails(t) {
  document.getElementById('choreDetailsTitle').textContent = stripEmoji(t.title);
  document.getElementById('choreDetailsPeriod').textContent = periodLabel(t.periodicity);
  document.getElementById('choreDetailsPoints').textContent = `${t.points} очков`;
  
  const actions = document.getElementById('choreDetailsActions');
  actions.innerHTML = `
    <button class="btn-done btn-xs" onclick="claimTask(${t.id}); closeModal('choreDetailsModal');">Взять</button>
    <button class="btn-unclaim btn-xs" onclick="nudgeTask(${t.id}); closeModal('choreDetailsModal');">Намек</button>
    <button class="btn-skip btn-xs" onclick="skipFreeChore(${t.id}); closeModal('choreDetailsModal');">Копия</button>
    <button class="btn-shift btn-xs" onclick="openShiftModal(${t.id}, 'chore'); closeModal('choreDetailsModal');">Сдвиг</button>
  `;
  document.getElementById('choreDetailsModal').classList.remove('hidden');
}

function switchTab(tabName) {
  const btn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
  if (btn) btn.click();
}

/* ── Shopping List Archive ── */
let shoppingArchivePage = 0;

async function openShoppingArchive() {
  shoppingArchivePage = 0;
  document.getElementById('shoppingArchiveModal').classList.remove('hidden');
  await loadShoppingArchive(0);
}

async function loadShoppingArchive(page) {
  shoppingArchivePage = page;
  const list = document.getElementById('shoppingArchiveList');
  const pag = document.getElementById('shoppingArchivePagination');
  list.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;
  pag.innerHTML = '';
  
  try {
    const items = await api('GET', `/api/archive/shopping?page=${page}&limit=10`);
    if (!items.length) {
      list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:24px">Архив пуст</p>`;
      return;
    }
    
    list.innerHTML = items.map(item => {
      const dt = item.date ? new Date(item.date) : null;
      const dtStr = dt ? dt.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
      return `
        <div class="archive-item">
          <div class="archive-item-info">
            <span class="archive-item-title">${escHtml(item.item_name)}</span>
            <span class="archive-item-meta">${dtStr} • Купил: ${escHtml(item.user)}</span>
          </div>
          <span class="task-badge" style="font-size:11px;">${item.price} руб</span>
        </div>
      `;
    }).join('');
    
    pag.innerHTML = `
      <button class="btn btn-secondary btn-xs" ${page === 0 ? 'disabled' : ''} onclick="loadShoppingArchive(${page - 1})">⏪ Назад</button>
      <span style="font-size:12px;color:var(--text2)">Страница ${page + 1}</span>
      <button class="btn btn-secondary btn-xs" ${items.length < 10 ? 'disabled' : ''} onclick="loadShoppingArchive(${page + 1})">Вперед ⏩</button>
    `;
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);text-align:center;padding:24px">${e.message}</p>`;
  }
}

window.openChoreDetails = openChoreDetails;
window.switchTab = switchTab;
window.openShoppingArchive = openShoppingArchive;
window.loadShoppingArchive = loadShoppingArchive;

/* ── My Task Details Modal (Personal/Claimed) ── */
function openMyTaskDetails(t, type) {
  const title = t.text;
  document.getElementById('myTaskDetailsTitle').textContent = stripEmoji(title);
  
  let body = '';
  if (type === 'household') {
    body = `
      <div style="display: flex; flex-direction: column; gap: 6px;">
        <div>🏠 <strong>Тип:</strong> Домашнее дело</div>
        <div>✨ <strong>Награда:</strong> ${t.points} очков</div>
      </div>
    `;
  } else {
    const rec = t.recurrence ? recLabel(t.recurrence) : 'Без повторения';
    body = `
      <div style="display: flex; flex-direction: column; gap: 6px;">
        <div>👤 <strong>Тип:</strong> Личная задача</div>
        <div>🔁 <strong>Повторение:</strong> ${rec}</div>
      </div>
    `;
  }
  document.getElementById('myTaskDetailsBody').innerHTML = body;
  
  const actions = document.getElementById('myTaskDetailsActions');
  if (type === 'household') {
    actions.innerHTML = `
      <button class="btn btn-primary" onclick="completeHouseTask(${t.id}, '${escAttr(title)}'); closeModal('myTaskDetailsModal');" style="margin-bottom: 4px; width: 100%; padding: 12px; font-weight: 600;">Выполнить</button>
      <div style="display: flex; gap: 6px; width: 100%;">
        <button class="btn btn-secondary" onclick="unclaimChore(${t.id}); closeModal('myTaskDetailsModal');" style="flex: 1; padding: 10px; font-weight: 600;">Вернуть</button>
        <button class="btn btn-secondary" onclick="openShiftModal(${t.id}, 'chore'); closeModal('myTaskDetailsModal');" style="flex: 1; padding: 10px; font-weight: 600;">Сдвиг</button>
      </div>
    `;
  } else {
    actions.innerHTML = `
      <button class="btn btn-primary" onclick="completePersonalTask(${t.id}); closeModal('myTaskDetailsModal');" style="margin-bottom: 4px; width: 100%; padding: 12px; font-weight: 600;">Выполнить</button>
      <div style="display: flex; gap: 6px; width: 100%;">
        <button class="btn btn-secondary" onclick="deletePersonalTask(${t.id}); closeModal('myTaskDetailsModal');" style="flex: 1; padding: 10px; font-weight: 600;">Удалить</button>
        <button class="btn btn-secondary" onclick="openShiftModal(${t.id}, 'personal'); closeModal('myTaskDetailsModal');" style="flex: 1; padding: 10px; font-weight: 600;">Сдвиг</button>
      </div>
    `;
  }
  
  document.getElementById('myTaskDetailsModal').classList.remove('hidden');
}

/* ── Restore Chore From Archive ── */
async function restoreChoreFromArchive(id) {
  try {
    await api('POST', `/api/archive/chores/${id}/restore`);
    showToast('↩ Дело вернулось в Мои дела!');
    loadChoresArchive(choresArchivePage);
    loadPersonalTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

/* ── Spawn/Database Preset Add Flow ── */
async function openAddFromDatabaseModal() {
  closeModal('addChoreChoiceModal');
  document.getElementById('addFromDbModal').classList.remove('hidden');
  const list = document.getElementById('dbTemplatesList');
  list.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;
  
  try {
    const [templates, activeTasks] = await Promise.all([
      api('GET', '/api/chores/templates'),
      api('GET', '/api/house/tasks')
    ]);
    
    const activeTemplateIds = new Set(activeTasks.map(t => t.template_id));
    const inactive = templates.filter(t => !activeTemplateIds.has(t.id));
    
    if (!inactive.length) {
      list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:24px">Все шаблоны уже активны на сегодня</p>`;
      return;
    }
    
    list.innerHTML = inactive.map(t => `
      <div class="archive-item" style="cursor:pointer;" onclick="spawnChoreFromTemplate(${t.id})">
        <div class="archive-item-info">
          <span class="archive-item-title">${escHtml(stripEmoji(t.title))}</span>
          <span class="archive-item-meta">${periodLabel(t.periodicity)}</span>
        </div>
        <button class="btn btn-primary btn-xs" style="font-size:11px;padding:3px 8px;">Активировать</button>
      </div>
    `).join('');
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);text-align:center;padding:24px">${e.message}</p>`;
  }
}

async function spawnChoreFromTemplate(tmplId) {
  try {
    await api('POST', `/api/house/tasks/spawn`, { template_id: tmplId });
    showToast('✅ Дело активировано на сегодня!');
    closeModal('addFromDbModal');
    loadHouseTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

window.openMyTaskDetails = openMyTaskDetails;
window.restoreChoreFromArchive = restoreChoreFromArchive;
window.openAddFromDatabaseModal = openAddFromDatabaseModal;
window.spawnChoreFromTemplate = spawnChoreFromTemplate;
window.submitCookingDone = submitCookingDone;
