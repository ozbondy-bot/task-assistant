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
      const av = document.getElementById('userAvatar');
      if (av) av.textContent = (tgUser.first_name || '?')[0].toUpperCase();
      const name = document.getElementById('userName');
      if (name) name.textContent = tgUser.first_name || 'Пользователь';
    }
  } else {
    // Dev mode — use mock initData
    console.warn('[DEV] Telegram WebApp not available. Using mock data.');
    initData = 'dev_mock';
    const name = document.getElementById('userName');
    if (name) name.textContent = 'Шурик';
    const av = document.getElementById('userAvatar');
    if (av) av.textContent = 'Ш';
  }

  setupTabs();
  loadHouseTab();
  setupModals();
  setupFABs();
  loadWeeklyGoal();
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

async function loadWeeklyGoal() {
  const percentEl = document.getElementById('weeklyGoalPercent');
  const progressEl = document.getElementById('weeklyGoalProgress');
  const textEl = document.getElementById('weeklyGoalText');
  if (!percentEl || !progressEl || !textEl) return;
  
  try {
    const dateStr = getHouseActiveDateStr();
    const data = await api('GET', `/api/house/weekly_goal?date=${dateStr}`);
    percentEl.textContent = `${data.percent}%`;
    progressEl.style.width = `${data.percent}%`;
    textEl.textContent = `выполнено ${data.completed_tasks} из ${data.total_tasks} задач`;
  } catch (e) {
    console.error('Failed to load weekly goal:', e);
  }
}
window.loadWeeklyGoal = loadWeeklyGoal;

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
    });
  });
}

function showSpinnerIfNeeded(container, selector = '.task-card, .reward-card, .shopping-card, .archive-item') {
  if (!container.querySelector(selector) && !container.querySelector('.empty-state')) {
    container.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;
  }
}

// ── House Tab ─────────────────────────────────────────────────────────────────
async function loadHouseTab() {
  const list = document.getElementById('houseTasksList');
  showSpinnerIfNeeded(list, '.task-card');

  const display = document.getElementById('houseActiveDateDisplay');
  if (display) display.textContent = getHouseActiveDateLabel();

  const dateStr = getHouseActiveDateStr();
  try {
    const [tasks, members] = await Promise.all([
      api('GET', `/api/house/tasks?date=${dateStr}`),
      api('GET', `/api/house/members`),
    ]);

    renderHouseTasks(tasks);
    renderMembers(members);

    const me = members.find(m => m.is_me);
    if (me) {
      const up = document.getElementById('userPoints');
      if (up) up.textContent = `${me.points} ✨`;
      currentPoints = me.points;
    }
  } catch (e) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Не удалось загрузить</div><div class="empty-sub">${e.message}</div></div>`;
  }
}

function isPastDate(dateStr) {
  if (!dateStr) return false;
  const todayStr = new Date().toISOString().split('T')[0];
  return dateStr < todayStr;
}

function renderHouseTasks(tasks) {
  const list = document.getElementById('houseTasksList');
  if (!tasks.length) {
    list.innerHTML = `<div class="empty-state"><div class="empty-icon">✨</div><div class="empty-title">Задач нет!</div><div class="empty-sub">Нет активных или выполненных задач на выбранную дату</div></div>`;
    return;
  }
  list.innerHTML = tasks.map(t => {
    const isPast = isPastDate(t.date);
    const pastIcon = isPast ? '<span style="color: var(--danger); margin-right: 4px; font-weight: bold;" title="Просрочено">⚠️</span>' : '';
    const isCompleted = t.status === 'done' || t.status === 'skipped';
    const grayClass = isCompleted ? 'completed-gray' : '';
    const clickHandler = isCompleted
      ? `openArchivedChoreDetails(${JSON.stringify(t).replace(/"/g, '&quot;')})`
      : `openChoreDetails(${JSON.stringify(t).replace(/"/g, '&quot;')})`;

    return `
      <div class="task-card house-task flex-between ${grayClass}" onclick="${clickHandler}" style="padding: 10px 12px; cursor: pointer;">
        <div class="task-left flex-row" style="align-items: center; gap: 8px;">
          <span style="font-size: 16px;">🏠</span>
          <span class="task-title" style="font-weight: 500; font-size: 14px;">${pastIcon}${escHtml(stripEmoji(t.title))}</span>
        </div>
        <span class="task-badge badge-points" style="font-size: 12px; margin-left: auto;">${t.points} ✨</span>
      </div>
    `;
  }).join('');
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
  const container = document.getElementById('membersListHeader');
  if (!container) return;
  const sorted = [...members].sort((a, b) => b.points - a.points);
  
  const m1 = sorted[0];
  const m2 = sorted[1];
  
  let html = '';
  if (m1) {
    const isMeStyle = m1.is_me ? 'font-weight: 700; color: var(--accent);' : 'color: var(--text1);';
    html += `<span style="${isMeStyle} text-align: left; flex: 1;">${escHtml(m1.display_name || 'Участник')}: ${m1.points} ✨</span>`;
  }
  if (m2) {
    const isMeStyle = m2.is_me ? 'font-weight: 700; color: var(--accent);' : 'color: var(--text1);';
    html += `<span style="${isMeStyle} text-align: right; flex: 1;">${escHtml(m2.display_name || 'Участник')}: ${m2.points} ✨</span>`;
  } else {
    html += `<span style="flex: 1;"></span>`;
  }
  container.innerHTML = html;
}

// ── Offsets & Date Helpers ───────────────────────────────────────────────────
let personalActiveOffset = 0;
let houseActiveOffset = 0;
let shoppingActiveOffset = 0;

function formatPaginationDate(dateInput) {
  const d = new Date(dateInput);
  const day = String(d.getDate()).padStart(2, '0');
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const weekdays = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
  const wd = weekdays[d.getDay()];
  return `${day}.${month}. (${wd})`;
}

// Personal Offset functions
function getPersonalActiveDateStr() {
  const d = new Date();
  d.setDate(d.getDate() + personalActiveOffset);
  return d.toISOString().split('T')[0];
}
function getPersonalActiveDateLabel() {
  const d = new Date();
  d.setDate(d.getDate() + personalActiveOffset);
  return formatPaginationDate(d);
}
function shiftPersonalDay(diff) {
  personalActiveOffset += diff;
  loadPersonalTab();
  loadWeeklyGoal();
}
window.shiftPersonalDay = shiftPersonalDay;

// House Offset functions
function getHouseActiveDateStr() {
  const d = new Date();
  d.setDate(d.getDate() + houseActiveOffset);
  return d.toISOString().split('T')[0];
}
function getHouseActiveDateLabel() {
  const d = new Date();
  d.setDate(d.getDate() + houseActiveOffset);
  return formatPaginationDate(d);
}
function shiftHouseDay(diff) {
  houseActiveOffset += diff;
  loadHouseTab();
  loadWeeklyGoal();
}
window.shiftHouseDay = shiftHouseDay;

// Shopping Offset functions
function getShoppingActiveDateStr() {
  const d = new Date();
  d.setDate(d.getDate() + shoppingActiveOffset);
  return d.toISOString().split('T')[0];
}
function getShoppingActiveDateLabel() {
  const d = new Date();
  d.setDate(d.getDate() + shoppingActiveOffset);
  return formatPaginationDate(d);
}
function shiftShoppingDay(diff) {
  shoppingActiveOffset += diff;
  loadShoppingTab();
  loadWeeklyGoal();
}
window.shiftShoppingDay = shiftShoppingDay;

async function loadPersonalTab() {
  const list = document.getElementById('personalTasksList');
  showSpinnerIfNeeded(list, '.task-card');

  const display = document.getElementById('personalActiveDateDisplay');
  if (display) display.textContent = getPersonalActiveDateLabel();

  const dateStr = getPersonalActiveDateStr();
  try {
    const data = await api('GET', `/api/tasks/today?date=${dateStr}`);
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
    const isPast = isPastDate(t.date);
    const pastIcon = isPast ? '<span style="color: var(--danger); margin-right: 4px; font-weight: bold;" title="Просрочено">⚠️</span>' : '';
    const isCompleted = t.is_completed === true;
    const grayClass = isCompleted ? 'completed-gray' : '';
    
    if (t.isHousehold) {
      return `
        <div class="task-card house-task flex-between ${grayClass}" onclick="openMyTaskDetails(${JSON.stringify(t).replace(/"/g, '&quot;')}, 'household')" style="padding: 10px 12px; cursor: pointer; margin-bottom: 8px;">
          <div class="task-left flex-row" style="align-items: center; gap: 8px;">
            <span style="font-size: 16px;">🏠</span>
            <span class="task-title" style="font-weight: 500; font-size: 14px;">${pastIcon}${escHtml(stripEmoji(t.text))}</span>
          </div>
          <span class="task-badge badge-points" style="font-size: 12px; margin-left: auto;">${t.points} ✨</span>
        </div>`;
    } else {
      const clickHandler = isCompleted
        ? `openArchivedTaskDetails(${JSON.stringify(t).replace(/"/g, '&quot;')})`
        : `openMyTaskDetails(${JSON.stringify(t).replace(/"/g, '&quot;')}, 'personal')`;

      return `
        <div class="task-card personal-task flex-between ${grayClass}" onclick="${clickHandler}" style="padding: 10px 12px; cursor: pointer; margin-bottom: 8px;">
          <div class="task-left flex-row" style="align-items: center; gap: 8px;">
            <span style="font-size: 16px;">👤</span>
            <span class="task-title" style="font-weight: 500; font-size: 14px;">${pastIcon}${escHtml(stripEmoji(t.text))}</span>
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
    loadWeeklyGoal();
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
    showToast(`✅ Готово! ${res.points_earned} ✨`);
    loadPersonalTab();
    loadHouseTab();
    loadWeeklyGoal();
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
    showToast(`✅ Готово! ${res.points_earned} ✨`);
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
    
    // Snappy modal close
    document.getElementById('addShopModal').classList.add('hidden');
    document.getElementById('shopItemName').value = '';
    document.getElementById('shopItemPrice').value = '';
    document.getElementById('shopItemUrgent').checked = false;
    
    try {
      await api('POST', '/api/shopping', { item_name: name, price, priority: urgent ? 'high' : 'normal' });
      showToast('🛒 Добавлено в список!');
      loadShoppingTab();
    } catch (e) {
      showToast(`⚠️ ${e.message}`);
      loadShoppingTab();
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
    const periodDaysInput = document.getElementById('choreTmplPeriodDays');
    const periodDays = periodicity === 'every_x_days' ? (parseInt(periodDaysInput.value) || 30) : null;
    if (!title) return;
    try {
      if (currentEditingTemplateId) {
        await api('PUT', `/api/chores/templates/${currentEditingTemplateId}`, { title, points, periodicity, period_days: periodDays });
        showToast('✅ Шаблон сохранен!');
      } else {
        const res = await api('POST', '/api/chores/templates', { title, points, periodicity, period_days: periodDays });
        if (res && res.pending) {
          showToast(res.message || '⏳ Запрос отправлен на согласование партнёру!');
        } else {
          showToast('✅ Шаблон добавлен!');
        }
      }
      document.getElementById('addChoreModal').classList.add('hidden');
      document.getElementById('choreTmplTitle').value = '';
      document.getElementById('choreTmplPoints').value = '1';
      loadHouseTab();
      loadWeeklyGoal();
    } catch (e) {
      showToast(`⚠️ ${e.message}`);
    }
  });

  // Shift modal
  document.getElementById('cancelShiftBtn').addEventListener('click', () => {
    document.getElementById('shiftTaskModal').classList.add('hidden');
  });

  document.getElementById('shiftTaskDate').addEventListener('change', async (e) => {
    const newDate = e.target.value;
    if (!newDate) return;
    
    // Close modal and show success toast instantly (optimistic UI)
    document.getElementById('shiftTaskModal').classList.add('hidden');
    showToast('🗓 Задача перенесена!');
    
    try {
      const taskId = shiftTaskId;
      const taskType = shiftTaskType;
      if (taskType === 'chore') {
        await api('POST', `/api/house/tasks/${taskId}/shift`, { new_date: newDate });
        loadHouseTab();
        loadPersonalTab();
      } else if (taskType === 'personal') {
        await api('POST', `/api/tasks/${taskId}/shift`, { new_date: newDate });
        loadPersonalTab();
      } else if (taskType === 'template') {
        await api('POST', `/api/chores/templates/${taskId}/shift`, { new_date: newDate });
        loadHouseTab();
        loadWeeklyGoal();
      }
    } catch (err) {
      showToast(`⚠️ ${err.message}`);
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
}

// ── Shopping Tab ──────────────────────────────────────────────────────────────
async function loadShoppingTab() {
  const list = document.getElementById('shoppingList');
  showSpinnerIfNeeded(list, '.shop-item-card');

  const display = document.getElementById('shoppingActiveDateDisplay');
  if (display) display.textContent = getShoppingActiveDateLabel();

  const dateStr = getShoppingActiveDateStr();
  try {
    const items = await api('GET', `/api/shopping?date=${dateStr}`);
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

  list.innerHTML = items.map(item => {
    const isCompleted = item.is_bought === true;
    const grayClass = isCompleted ? 'completed-gray' : '';
    const clickHandler = isCompleted ? `restoreShoppingItem(${item.id})` : `markBought(${item.id}, this)`;
    
    return `
      <div class="shop-item-card ${item.priority === 'high' ? 'urgent' : ''} ${grayClass}" id="sitem-${item.id}" onclick="${clickHandler}">
        <div class="shop-emoji">${seededEmoji(item.id)}</div>
        <div class="shop-body">
          <div class="shop-name">${item.priority === 'high' ? '🔴 ' : ''}${escHtml(item.item_name)}</div>
          ${item.price > 0 ? `<div class="shop-price">${item.price} ₽</div>` : ''}
        </div>
        <div class="shop-check">✓</div>
      </div>
    `;
  }).join('');
}

async function markBought(id, el) {
  el.style.opacity = '0.4';
  el.style.transition = 'opacity 0.3s';
  try {
    await api('POST', `/api/shopping/${id}/bought`);
    setTimeout(() => {
      el.remove();
      loadShoppingTab();
      loadWeeklyGoal();
    }, 300);
    showToast('✅ Куплено!');
  } catch (e) {
    el.style.opacity = '1';
    showToast(`⚠️ ${e.message}`);
  }
}

async function restoreShoppingItem(id) {
  if (!confirm('Вернуть товар в список покупок?')) return;
  try {
    await api('POST', `/api/shopping/${id}/restore`);
    showToast('↩ Товар возвращен в список!');
    loadShoppingTab();
    loadWeeklyGoal();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}
window.restoreShoppingItem = restoreShoppingItem;

// ── Shop / Rewards Tab ────────────────────────────────────────────────────────
async function loadShopTab() {
  const rList = document.getElementById('rewardsList');
  showSpinnerIfNeeded(rList, '.reward-card');

  try {
    const rewardsData = await api('GET', '/api/rewards');
    currentPoints = rewardsData.user_points;
    renderRewards(rewardsData.rewards, rewardsData.user_points);
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

function periodLabel(p, days) {
  const m = { 
    daily: 'Ежедневно', 
    weekly: 'Еженедельно', 
    twice_weekly: '2 раза/нед', 
    monthly: 'Ежемесячно',
    twice_monthly: '2 раза/мес', 
    quarterly: 'Раз в квартал'
  };
  if (m[p]) return m[p];
  if (p === 'every_x_days' || (p && p.startsWith('every_') && p.endsWith('_days'))) {
    const d = days || (p.split('_')[1] !== 'x' ? p.split('_')[1] : 30);
    return `Каждые ${d} дней`;
  }
  return p || 'Каждые 30 дней';
}

function recLabel(r) {
  const m = { daily:'Ежедневно', weekly:'Еженед.', biweekly:'Раз в 2 нед.', monthly:'Ежемес.' };
  return m[r] || r;
}

// ── Settings Tab & Actions ───────────────────────────────────────────────────
let currentSettingsSubTab = 'chores';

async function loadSettingsTab() {
  if (currentSettingsSubTab === 'chores') {
    await loadSettingsChores();
  } else {
    await loadSettingsRewards();
  }
}

async function loadSettingsChores() {
  const choresList = document.getElementById('settingsChoresList');
  showSpinnerIfNeeded(choresList, '.task-card');
  try {
    const templates = await api('GET', '/api/chores/templates');
    renderChoresTemplates(templates);
  } catch (e) {
    choresList.innerHTML = `<p style="color:var(--danger)">${e.message}</p>`;
  }
}

async function loadSettingsRewards() {
  const rewardsList = document.getElementById('settingsRewardsList');
  showSpinnerIfNeeded(rewardsList, '.reward-card');
  try {
    const rewardsData = await api('GET', '/api/rewards');
    renderRewardsTemplates(rewardsData.rewards);
  } catch (e) {
    rewardsList.innerHTML = `<p style="color:var(--danger)">${e.message}</p>`;
  }
}

function renderChoresTemplates(templates) {
  const list = document.getElementById('settingsChoresList');
  if (!templates.length) {
    list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:12px">Шаблонов пока нет</p>`;
    return;
  }
  
  // Sort: nearest first
  templates.sort((a, b) => {
    const da = a.next_execution ? new Date(a.next_execution) : new Date(2100, 11, 31);
    const db = b.next_execution ? new Date(b.next_execution) : new Date(2100, 11, 31);
    return da - db;
  });

  list.innerHTML = templates.map(t => {
    let nextStr = 'Нет';
    if (t.next_execution) {
      const dt = new Date(t.next_execution);
      const day = String(dt.getDate()).padStart(2, '0');
      const month = String(dt.getMonth() + 1).padStart(2, '0');
      nextStr = `${day}.${month}.`;
    }
    return `
      <div class="task-card house-task flex-between" onclick="openTemplateDetailsModal(${JSON.stringify(t).replace(/"/g, '&quot;')})" style="padding: 0 10px; cursor: pointer; height: 36px; align-items: center; display: flex; box-sizing: border-box;">
        <span style="font-weight: 500; font-size: 13px;">${escHtml(stripEmoji(t.title))}</span>
        <span class="task-badge" style="font-size: 11px; font-weight: 600; background: var(--bg3); color: var(--text2); border-radius: 6px; width: 70px; height: 28px; display: flex; align-items: center; justify-content: center; box-sizing: border-box; flex-shrink: 0; margin-left: auto;">📅 ${nextStr}</span>
      </div>
    `;
  }).join('');
}

function renderRewardsTemplates(rewards) {
  const list = document.getElementById('settingsRewardsList');
  if (!rewards.length) {
    list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:12px">Нет наград</p>`;
    return;
  }
  list.innerHTML = rewards.map(r => `
    <div class="settings-item" onclick="openRewardTemplateDetails(${JSON.stringify(r).replace(/"/g, '&quot;')})" style="cursor:pointer;">
      <span class="settings-item-title" style="flex:1; font-size:13px;">${escHtml(stripEmoji(r.title))}</span>
      <span style="font-size:12px; font-weight:600; color:var(--warning); white-space:nowrap;">${r.price} ⭐</span>
    </div>
  `).join('');
}

function openRewardTemplateDetails(r) {
  document.getElementById('templateDetailsTitle').textContent = stripEmoji(r.title);
  document.getElementById('templateDetailsBody').innerHTML = `
    <div>⭐ <strong>Стоимость:</strong> <span>${r.price} ⭐</span></div>
    ${r.base_days ? `<div>📅 <strong>База (дней):</strong> <span>${r.base_days}</span></div>` : ''}
  `;
  const actions = document.getElementById('templateDetailsActions');
  actions.innerHTML = `
    <div style="display: flex; gap: 6px; width: 100%;">
      <button class="btn btn-secondary" onclick="deleteReward(${r.id}); closeModal('templateDetailsModal');" style="flex: 1; padding: 10px 4px; font-size: 13px; font-weight: 600; background: var(--danger); border-color: var(--danger);">Удалить</button>
    </div>
  `;
  document.getElementById('templateDetailsModal').classList.remove('hidden');
}
window.openRewardTemplateDetails = openRewardTemplateDetails;

async function deleteChoreTemplate(id) {
  closeModal('templateDetailsModal');
  if (!confirm('Удалить этот шаблон дела?')) return;
  try {
    const res = await api('DELETE', `/api/chores/templates/${id}`);
    if (res && res.pending) {
      showToast(res.message || '⏳ Запрос на удаление отправлен партнёру!');
    } else {
      showToast('🗑 Шаблон удален!');
    }
    loadHouseTab();
    loadWeeklyGoal();
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
  closeModal('myTaskDetailsModal');
  if (!confirm('Вернуть задачу в свободные?')) return;
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
  const input = document.getElementById('shiftTaskDate');
  input.value = new Date().toISOString().split('T')[0];
  
  setTimeout(() => {
    if (typeof input.showPicker === 'function') {
      try {
        input.showPicker();
      } catch (err) {
        input.click();
      }
    } else {
      input.click();
    }
  }, 100);
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
  closeModal('choreDetailsModal');
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
let choresArchiveDate = new Date();
let tasksArchiveDate = new Date();
let purchasesArchivePage = 0;

function closeModal(modalId) {
  document.getElementById(modalId).classList.add('hidden');
}

async function openChoresArchive() {
  choresArchiveDate = new Date();
  document.getElementById('choresArchiveModal').classList.remove('hidden');
  await loadChoresArchive();
}

async function loadChoresArchive() {
  const list = document.getElementById('choresArchiveList');
  const pag = document.getElementById('choresArchivePagination');
  list.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;
  pag.innerHTML = '';
  
  const dateStr = choresArchiveDate.toISOString().split('T')[0];
  try {
    const items = await api('GET', `/api/archive/chores?date=${dateStr}`);
    if (!items.length) {
      list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:24px">Архив пуст</p>`;
    } else {
      list.innerHTML = items.map(c => `
        <div class="archive-item" onclick="openArchivedChoreDetails(${JSON.stringify(c).replace(/"/g, '&quot;')})" style="cursor:pointer; padding:10px; display:flex; justify-content:space-between; align-items:center;">
          <div class="archive-item-info" style="text-align:left;">
            <span class="archive-item-title" style="font-weight:500;">${escHtml(stripEmoji(c.title))}</span>
            <span class="archive-item-meta" style="font-size:11px;color:var(--text3);display:block;">Выполнил: ${escHtml(c.user)}</span>
          </div>
          <span class="task-badge badge-points" style="font-size:11px;">${c.points} ✨</span>
        </div>
      `).join('');
    }
    
    const displayDate = formatPaginationDate(choresArchiveDate);
    pag.innerHTML = `
      <button class="btn btn-secondary btn-xs btn-pagination" onclick="stepChoresArchive(-1)">←</button>
      <span class="pagination-label">${displayDate}</span>
      <button class="btn btn-secondary btn-xs btn-pagination" onclick="stepChoresArchive(1)">→</button>
    `;
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);text-align:center;padding:24px">${e.message}</p>`;
  }
}

function stepChoresArchive(days) {
  choresArchiveDate.setDate(choresArchiveDate.getDate() + days);
  loadChoresArchive();
}

async function openTasksArchive() {
  tasksArchiveDate = new Date();
  document.getElementById('tasksArchiveModal').classList.remove('hidden');
  await loadTasksArchive();
}

async function loadTasksArchive() {
  const list = document.getElementById('tasksArchiveList');
  const pag = document.getElementById('tasksArchivePagination');
  list.innerHTML = `<div class="loading-spinner"><div class="spinner"></div><p>Загрузка...</p></div>`;
  pag.innerHTML = '';
  
  const dateStr = tasksArchiveDate.toISOString().split('T')[0];
  try {
    const items = await api('GET', `/api/archive/tasks?date=${dateStr}`);
    if (!items.length) {
      list.innerHTML = `<p style="color:var(--text3);text-align:center;padding:24px">Архив пуст</p>`;
    } else {
      list.innerHTML = items.map(t => {
        const rec = t.recurrence ? `🔁 ${recLabel(t.recurrence)}` : '';
        return `
          <div class="archive-item" onclick="openArchivedTaskDetails(${JSON.stringify(t).replace(/"/g, '&quot;')})" style="cursor:pointer; padding:10px; display:flex; justify-content:space-between; align-items:center;">
            <div class="archive-item-info" style="text-align:left;">
              <span class="archive-item-title" style="font-weight:500; text-decoration:line-through; color:var(--text3);">${escHtml(stripEmoji(t.text))}</span>
              ${rec ? `<span class="archive-item-meta" style="font-size:11px;color:var(--text3);display:block;">${rec}</span>` : ''}
            </div>
          </div>
        `;
      }).join('');
    }
    
    const displayDate = formatPaginationDate(tasksArchiveDate);
    pag.innerHTML = `
      <button class="btn btn-secondary btn-xs btn-pagination" onclick="stepTasksArchive(-1)">←</button>
      <span class="pagination-label">${displayDate}</span>
      <button class="btn btn-secondary btn-xs btn-pagination" onclick="stepTasksArchive(1)">→</button>
    `;
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);text-align:center;padding:24px">${e.message}</p>`;
  }
}

function stepTasksArchive(days) {
  tasksArchiveDate.setDate(tasksArchiveDate.getDate() + days);
  loadTasksArchive();
}

async function restoreTaskFromArchive(id) {
  try {
    await api('POST', `/api/archive/tasks/${id}/restore`);
    showToast('↩ Задача вернулась в Мои дела!');
    loadPersonalTab();
    loadWeeklyGoal();
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
          <span class="task-badge badge-points" style="background:rgba(251,191,36,0.15);color:var(--warning);font-size:11px;">-${p.price} ⭐</span>
        </div>
      `;
    }).join('');
    
    pag.innerHTML = `
      <button class="btn btn-secondary btn-xs btn-pagination" ${page === 0 ? 'disabled' : ''} onclick="loadPurchasesArchive(${page - 1})">←</button>
      <span class="pagination-label">Страница ${page + 1}</span>
      <button class="btn btn-secondary btn-xs btn-pagination" ${items.length < 10 ? 'disabled' : ''} onclick="loadPurchasesArchive(${page + 1})">→</button>
    `;
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);text-align:center;padding:24px">${e.message}</p>`;
  }
}

async function deletePersonalTask(id) {
  closeModal('myTaskDetailsModal');
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
  document.getElementById('choreDetailsPeriod').textContent = periodLabel(t.periodicity, t.period_days);
  document.getElementById('choreDetailsPoints').textContent = `${t.points} ✨`;
  
  const actions = document.getElementById('choreDetailsActions');
  actions.innerHTML = `
    <div style="display: flex; flex-direction: column; gap: 8px; width: 100%;">
      <div style="display: flex; gap: 6px; width: 100%;">
        <button class="btn btn-primary" onclick="claimTask(${t.id}); closeModal('choreDetailsModal');" style="flex: 1; height: 38px; font-size: 12px; font-weight: 600; background: var(--success); border-color: var(--success);">Взять</button>
        <button class="btn btn-secondary" onclick="nudgeTask(${t.id}); closeModal('choreDetailsModal');" style="flex: 1; height: 38px; font-size: 12px; font-weight: 600;">Намек</button>
        <button class="btn btn-secondary" onclick="openShiftModal(${t.id}, 'chore'); closeModal('choreDetailsModal');" style="flex: 1; height: 38px; font-size: 12px; font-weight: 600;">Сдвиг</button>
        <button class="btn btn-secondary" onclick="skipFreeChore(${t.id});" style="flex: 1; height: 38px; font-size: 12px; font-weight: 600;">Убрать</button>
      </div>
      <div style="display: flex; gap: 6px; width: 100%;">
        <button class="btn btn-secondary" onclick="editChoreTemplateDirectly(${JSON.stringify(t).replace(/"/g, '&quot;')}); closeModal('choreDetailsModal');" style="flex: 1; height: 38px; font-size: 12px; font-weight: 600;">Изменить шаблон</button>
        <button class="btn btn-secondary" onclick="deleteChoreTemplate(${t.template_id}); closeModal('choreDetailsModal');" style="flex: 1; height: 38px; font-size: 12px; font-weight: 600; background: var(--danger); border-color: var(--danger); color: white;">Удалить шаблон</button>
      </div>
    </div>
  `;
  document.getElementById('choreDetailsModal').classList.remove('hidden');
}

function editChoreTemplateDirectly(t) {
  currentEditingTemplateId = t.template_id;
  document.getElementById('addChoreModal').classList.remove('hidden');
  document.getElementById('choreTmplTitle').value = stripEmoji(t.title);
  document.getElementById('choreTmplPoints').value = t.points;
  document.getElementById('choreTmplPeriodicity').value = t.periodicity;
  toggleChorePeriodDays(t.periodicity);
  if (t.periodicity === 'every_x_days') {
    document.getElementById('choreTmplPeriodDays').value = t.period_days || 30;
  }
}
window.editChoreTemplateDirectly = editChoreTemplateDirectly;

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
      <button class="btn btn-secondary btn-xs btn-pagination" ${page === 0 ? 'disabled' : ''} onclick="loadShoppingArchive(${page - 1})">←</button>
      <span class="pagination-label">Страница ${page + 1}</span>
      <button class="btn btn-secondary btn-xs btn-pagination" ${items.length < 10 ? 'disabled' : ''} onclick="loadShoppingArchive(${page + 1})">→</button>
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
let currentEditingTemplateId = null;

function openMyTaskDetails(t, type) {
  const title = t.text;
  document.getElementById('myTaskDetailsTitle').textContent = stripEmoji(title);
  
  let body = '';
  if (type === 'household') {
    body = `
      <div style="display: flex; flex-direction: column; gap: 6px;">
        <div>🏠 <strong>Тип:</strong> Домашнее дело</div>
        <div>✨ <strong>Награда:</strong> ${t.points} ✨</div>
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
      <div style="display: flex; gap: 6px; width: 100%;">
        <button class="btn btn-primary" onclick="completeHouseTask(${t.id}, '${escAttr(title)}'); closeModal('myTaskDetailsModal');" style="flex: 1; height: 42px; box-sizing: border-box; padding: 0 4px; font-size: 13px; font-weight: 600;">Выполнить</button>
        <button class="btn btn-secondary" onclick="openShiftModal(${t.id}, 'chore'); closeModal('myTaskDetailsModal');" style="flex: 1; height: 42px; box-sizing: border-box; padding: 0 4px; font-size: 13px; font-weight: 600;">Сдвиг</button>
        <button class="btn btn-secondary" onclick="unclaimChore(${t.id});" style="flex: 1; height: 42px; box-sizing: border-box; padding: 0 4px; font-size: 13px; font-weight: 600;">Вернуть</button>
      </div>
    `;
  } else {
    actions.innerHTML = `
      <div style="display: flex; gap: 6px; width: 100%;">
        <button class="btn btn-primary" onclick="completePersonalTask(${t.id}); closeModal('myTaskDetailsModal');" style="flex: 1; height: 42px; box-sizing: border-box; padding: 0 4px; font-size: 13px; font-weight: 600;">Выполнить</button>
        <button class="btn btn-secondary" onclick="openShiftModal(${t.id}, 'personal'); closeModal('myTaskDetailsModal');" style="flex: 1; height: 42px; box-sizing: border-box; padding: 0 4px; font-size: 13px; font-weight: 600;">Сдвиг</button>
        <button class="btn btn-secondary" onclick="deletePersonalTask(${t.id});" style="flex: 1; height: 42px; box-sizing: border-box; padding: 0 4px; font-size: 13px; font-weight: 600; background: var(--danger); border-color: var(--danger);">Удалить</button>
      </div>
    `;
  }
  
  document.getElementById('myTaskDetailsModal').classList.remove('hidden');
}

/* ── Restore Chore From Archive ── */
async function restoreChoreFromArchive(id) {
  try {
    await api('POST', `/api/house/tasks/${id}/restore`);
    showToast('↩ Дело вернулось в работу!');
    loadHouseTab();
    loadPersonalTab();
    loadWeeklyGoal();
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
    
    // Sort from nearest next execution to furthest
    inactive.sort((a, b) => {
      const da = a.next_execution ? new Date(a.next_execution) : new Date(2100, 11, 31);
      const db = b.next_execution ? new Date(b.next_execution) : new Date(2100, 11, 31);
      return da - db;
    });
    
    list.innerHTML = inactive.map(t => {
      let nextStr = 'Нет';
      if (t.next_execution) {
        const dt = new Date(t.next_execution);
        const day = String(dt.getDate()).padStart(2, '0');
        const month = String(dt.getMonth() + 1).padStart(2, '0');
        nextStr = `${day}.${month}.`;
      }
      return `
        <div class="archive-item" style="display: flex; justify-content: space-between; align-items: center; padding: 10px; border-bottom: 1px solid var(--border);">
          <div class="archive-item-info" style="text-align: left; flex: 1; padding-right: 8px;">
            <span class="archive-item-title" style="font-weight: 500; font-size: 14px; display: block; margin-bottom: 2px;">${escHtml(stripEmoji(t.title))}</span>
            <span class="archive-item-meta" style="font-size: 11px; color: var(--text3);">📅 ${nextStr}</span>
          </div>
          <button class="btn btn-primary btn-xs" onclick="spawnChoreFromTemplate(${t.id})" style="width: 80px; padding: 6px 0; text-align: center; font-size: 12px; flex-shrink: 0;">Добавить</button>
        </div>
      `;
    }).join('');
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);text-align:center;padding:24px">${e.message}</p>`;
  }
}

async function spawnChoreFromTemplate(tmplId) {
  try {
    await api('POST', `/api/house/tasks/spawn`, { template_id: tmplId });
    showToast('✅ Задача активирована на сегодня!');
    closeModal('addFromDbModal');
    loadHouseTab();
  } catch (e) {
    showToast(`⚠️ ${e.message}`);
  }
}

/* ── Detail Modals for Archive and Templates ── */
function openArchivedChoreDetails(c) {
  document.getElementById('archivedChoreTitle').textContent = stripEmoji(c.title);
  const dt = new Date(c.date);
  const dtStr = dt.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  
  document.getElementById('archivedChoreBody').innerHTML = `
    <div>📅 <strong>Дата выполнения:</strong> <span>${dtStr}</span></div>
    <div>✨ <strong>Очки:</strong> <span>${c.points} ✨</span></div>
    <div>👤 <strong>Выполнил:</strong> <span>${escHtml(c.user)}</span></div>
  `;
  document.getElementById('archivedChoreActions').innerHTML = `
    <button class="btn btn-primary" onclick="restoreChoreFromArchive(${c.id}); closeModal('archivedChoreDetailsModal');" style="width: 100%; padding: 12px; font-weight: 600;">Вернуть</button>
  `;
  document.getElementById('archivedChoreDetailsModal').classList.remove('hidden');
}

function openArchivedTaskDetails(t) {
  document.getElementById('archivedTaskTitle').textContent = stripEmoji(t.text);
  const rec = t.recurrence ? `🔁 ${recLabel(t.recurrence)}` : 'Без повторения';
  
  document.getElementById('archivedTaskBody').innerHTML = `
    <div>📅 <strong>Плановая дата:</strong> <span>${escHtml(t.date)}</span></div>
    <div>🔁 <strong>Повторение:</strong> <span>${rec}</span></div>
  `;
  document.getElementById('archivedTaskActions').innerHTML = `
    <button class="btn btn-primary" onclick="restoreTaskFromArchive(${t.id}); closeModal('archivedTaskDetailsModal');" style="width: 100%; padding: 12px; font-weight: 600;">Вернуть</button>
  `;
  document.getElementById('archivedTaskDetailsModal').classList.remove('hidden');
}

function openTemplateDetailsModal(t) {
  document.getElementById('templateDetailsTitle').textContent = stripEmoji(t.title);
  const last = t.last_completed ? new Date(t.last_completed).toLocaleDateString('ru-RU') : 'Нет данных';
  const next = t.next_execution ? new Date(t.next_execution).toLocaleDateString('ru-RU') : 'Нет данных';
  
  document.getElementById('templateDetailsBody').innerHTML = `
    <div>📅 <strong>Цикл повторения:</strong> <span>${periodLabel(t.periodicity, t.period_days)}</span></div>
    <div>📅 <strong>Последнее выполнение:</strong> <span>${last}</span></div>
    <div>🗓 <strong>Следующий повтор:</strong> <span>${next}</span></div>
    <div>✨ <strong>Награда:</strong> <span>${t.points} ✨</span></div>
  `;
  
  const actions = document.getElementById('templateDetailsActions');
  actions.innerHTML = `
    <div style="display: flex; gap: 6px; width: 100%;">
      <button class="btn btn-primary" onclick="startEditTemplate(${JSON.stringify(t).replace(/"/g, '&quot;')}); closeModal('templateDetailsModal');" style="flex: 1; padding: 10px 4px; font-size: 13px; font-weight: 600;">Изменить</button>
      <button class="btn btn-secondary" onclick="openShiftModal(${t.id}, 'template'); closeModal('templateDetailsModal');" style="flex: 1; padding: 10px 4px; font-size: 13px; font-weight: 600;">Сдвиг</button>
      <button class="btn btn-secondary" onclick="deleteTemplate(${t.id}); closeModal('templateDetailsModal');" style="flex: 1; padding: 10px 4px; font-size: 13px; font-weight: 600; background: var(--danger); border-color: var(--danger);">Удалить</button>
    </div>
  `;
  document.getElementById('templateDetailsModal').classList.remove('hidden');
}

function toggleChorePeriodDays(val) {
  const group = document.getElementById('chorePeriodDaysGroup');
  if (val === 'every_x_days') {
    group.classList.remove('hidden');
  } else {
    group.classList.add('hidden');
  }
}

function startEditTemplate(t) {
  currentEditingTemplateId = t.id;
  document.getElementById('addChoreModal').classList.remove('hidden');
  document.getElementById('choreTmplTitle').value = stripEmoji(t.title);
  document.getElementById('choreTmplPoints').value = t.points;
  
  let p = t.periodicity;
  let d = t.period_days;
  if (p === 'daily') { p = 'every_x_days'; d = 1; }
  else if (p === 'weekly') { p = 'every_x_days'; d = 7; }
  else if (p === 'monthly') { p = 'every_x_days'; d = 30; }
  else if (p === 'once') { p = 'once'; d = ''; }
  else if (p === 'every_x_days') { d = t.period_days || 30; }
  else { p = 'every_x_days'; d = 30; }
  
  document.getElementById('choreTmplPeriodicity').value = p;
  document.getElementById('choreTmplPeriodDays').value = d;
  toggleChorePeriodDays(p);
  
  if (t.start_date) {
    document.getElementById('choreTmplStartDate').value = t.start_date;
  } else {
    document.getElementById('choreTmplStartDate').value = '';
  }
  document.querySelector('#addChoreModal h3').textContent = 'Изменить шаблон задачи';
  document.getElementById('saveChoreTmplBtn').textContent = 'Сохранить ✓';
}

function openNewTemplateCreator() {
  closeModal('addChoreChoiceModal');
  currentEditingTemplateId = null;
  document.querySelector('#addChoreModal h3').textContent = 'Новый шаблон задачи';
  document.getElementById('saveChoreTmplBtn').textContent = 'Создать ✓';
  document.getElementById('choreTmplTitle').value = '';
  document.getElementById('choreTmplPoints').value = '1';
  document.getElementById('choreTmplPeriodicity').value = 'once';
  document.getElementById('choreTmplPeriodDays').value = '30';
  toggleChorePeriodDays('once');
  document.getElementById('choreTmplStartDate').value = '';
  document.getElementById('addChoreModal').classList.remove('hidden');
}

function deleteTemplate(id) {
  deleteChoreTemplate(id);
}

window.openMyTaskDetails = openMyTaskDetails;
window.restoreChoreFromArchive = restoreChoreFromArchive;
window.openAddFromDatabaseModal = openAddFromDatabaseModal;
window.spawnChoreFromTemplate = spawnChoreFromTemplate;
window.submitCookingDone = submitCookingDone;
window.openArchivedChoreDetails = openArchivedChoreDetails;
window.openArchivedTaskDetails = openArchivedTaskDetails;
window.openTemplateDetailsModal = openTemplateDetailsModal;
window.startEditTemplate = startEditTemplate;
window.openNewTemplateCreator = openNewTemplateCreator;
window.deleteTemplate = deleteTemplate;
window.toggleChorePeriodDays = toggleChorePeriodDays;
