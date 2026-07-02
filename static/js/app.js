// MetGPT app.js

const chatWindow = document.getElementById('chat-window');
const userInput  = document.getElementById('user-input');
const sendBtn    = document.getElementById('send-btn');

// Auto-resize textarea
userInput.addEventListener('input', () => {
  userInput.style.height = 'auto';
  userInput.style.height = Math.min(userInput.scrollHeight, 140) + 'px';
});

// Send on Enter (Shift+Enter = newline)
userInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
sendBtn.addEventListener('click', sendMessage);

function sendSuggestion(btn) {
  userInput.value = btn.textContent;
  sendMessage();
}

function sendMessage() {
  const text = userInput.value.trim();
  if (!text) return;

  hideWelcome();
  appendUserMsg(text);
  userInput.value = '';
  userInput.style.height = 'auto';
  sendBtn.disabled = true;

  const typingEl = appendTyping();

  fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text }),
  })
  .then(r => r.json())
  .then(data => {
    typingEl.remove();
    if (data.error) {
      appendErrorMsg(data.error);
    } else {
      renderResponses(data.responses);
    }
    sendBtn.disabled = false;
    scrollBottom();
  })
  .catch(err => {
    typingEl.remove();
    appendErrorMsg('Network error — please try again.');
    sendBtn.disabled = false;
  });
}

function hideWelcome() {
  const w = chatWindow.querySelector('.welcome-card');
  if (w) w.remove();
}

function scrollBottom() {
  chatWindow.scrollTop = chatWindow.scrollHeight;
}

function appendUserMsg(text) {
  const msg = document.createElement('div');
  msg.className = 'msg user';
  msg.innerHTML = `
    <div class="msg-avatar">You</div>
    <div class="msg-content">
      <div class="bubble">${escHtml(text)}</div>
    </div>`;
  chatWindow.appendChild(msg);
  scrollBottom();
}

function appendTyping() {
  const msg = document.createElement('div');
  msg.className = 'msg assistant';
  msg.innerHTML = `
    <div class="msg-avatar">M</div>
    <div class="msg-content">
      <div class="typing"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
    </div>`;
  chatWindow.appendChild(msg);
  scrollBottom();
  return msg;
}

function appendErrorMsg(text) {
  const msg = document.createElement('div');
  msg.className = 'msg assistant';
  msg.innerHTML = `
    <div class="msg-avatar">M</div>
    <div class="msg-content">
      <div class="resp-error">${escHtml(text)}</div>
    </div>`;
  chatWindow.appendChild(msg);
}

function renderResponses(responses) {
  const wrapper = document.createElement('div');
  wrapper.className = 'msg assistant';
  wrapper.innerHTML = `<div class="msg-avatar">M</div>`;
  const content = document.createElement('div');
  content.className = 'msg-content';
  wrapper.appendChild(content);

  let hasContent = false;

  // Detect whether this batch is a multi-day (compact) forecast set,
  // so we can wrap the slim rows in a single week-summary container.
  const forecastResps = responses.filter(r => r.type === 'forecast');
  const isWeek = forecastResps.length > 1 && forecastResps.every(r => r.compact);
  let weekBox = null;
  if (isWeek) {
    weekBox = document.createElement('div');
    weekBox.className = 'week-summary';
    const loc = forecastResps[0].location.split(',')[0];
    weekBox.innerHTML = `
      <div class="week-head">
        <span class="week-loc">${escHtml(loc)}</span>
        <span class="week-cols"><span>Temp</span><span>Rain</span><span>Wind</span></span>
      </div>`;
  }

  responses.forEach(r => {
    if (r.type === 'info') {
      const el = document.createElement('p');
      el.className = 'resp-info';
      el.textContent = r.text;
      content.appendChild(el);
      hasContent = true;

    } else if (r.type === 'warning') {
      const el = document.createElement('div');
      el.className = 'resp-warning';
      el.textContent = r.text;
      content.appendChild(el);
      hasContent = true;

    } else if (r.type === 'error') {
      const el = document.createElement('div');
      el.className = 'resp-error';
      el.textContent = r.text;
      content.appendChild(el);
      hasContent = true;

    } else if (r.type === 'forecast') {
      if (isWeek && weekBox) {
        weekBox.appendChild(buildForecastCard(r));
      } else {
        content.appendChild(buildForecastCard(r));
      }
      hasContent = true;

    } else if (r.type === 'advice') {
      const el = document.createElement('div');
      el.className = 'advice-bubble';
      el.innerHTML = renderMarkdown(r.text);
      content.appendChild(el);
      hasContent = true;
    }
  });

  // Insert the week box before the advice bubble if present
  if (weekBox && weekBox.children.length > 1) {
    const advice = content.querySelector('.advice-bubble');
    if (advice) content.insertBefore(weekBox, advice);
    else content.appendChild(weekBox);
  }

  if (hasContent) {
    chatWindow.appendChild(wrapper);
    scrollBottom();
  }
}

function buildForecastCard(r) {
  const tempStr = (r.temp_min != null && r.temp_max != null) ? `${r.temp_min}–${r.temp_max}°C` : 'N/A';
  const rainStr = r.rain_total != null ? `${r.rain_total} mm` : 'N/A';
  const probStr = r.rain_prob != null ? `${Math.round(r.rain_prob)}%` : 'N/A';
  const windStr = r.wind_avg != null ? `${r.wind_avg} m/s` : 'N/A';
  const humStr  = r.humidity != null ? `${Math.round(r.humidity)}%` : 'N/A';

  // Compact row for multi-day (week) view
  if (r.compact) {
    const row = document.createElement('div');
    row.className = 'fc-row';
    let dayName = r.date;
    try {
      const d = new Date(r.date + 'T00:00:00');
      dayName = d.toLocaleDateString('en-IE', { weekday: 'short', day: 'numeric', month: 'short' });
    } catch (e) {}
    row.innerHTML = `
      <span class="fc-row-day">${dayName}</span>
      <span class="fc-row-temp">${tempStr}</span>
      <span class="fc-row-rain">${rainStr} · ${probStr}</span>
      <span class="fc-row-wind">${windStr}</span>`;
    return row;
  }

  // Full card for single-day view
  const card = document.createElement('div');
  card.className = 'forecast-card';
  const uid = 'hourly-' + Math.random().toString(36).slice(2,8);
  card.innerHTML = `
    <div class="fc-header">
      <span class="fc-location">${escHtml(r.location)}</span>
      <span class="fc-date">${escHtml(r.date)}</span>
    </div>
    <div class="fc-metrics">
      <div class="fc-metric"><span class="fc-metric-label">Temperature</span><span class="fc-metric-value">${tempStr}</span></div>
      <div class="fc-metric"><span class="fc-metric-label">Rain total</span><span class="fc-metric-value">${rainStr}</span></div>
      <div class="fc-metric"><span class="fc-metric-label">Rain chance</span><span class="fc-metric-value">${probStr}</span></div>
      <div class="fc-metric"><span class="fc-metric-label">Wind avg</span><span class="fc-metric-value">${windStr}</span></div>
      <div class="fc-metric"><span class="fc-metric-label">Humidity</span><span class="fc-metric-value">${humStr}</span></div>
    </div>
    <button class="fc-hourly-toggle" onclick="toggleHourly('${uid}', this)">Show hourly breakdown ▾</button>
    <div class="fc-hourly" id="${uid}">
      ${buildHourlyTable(r.hourly || [])}
    </div>`;
  return card;
}

function buildHourlyTable(hourly) {
  if (!hourly || hourly.length === 0) return '<p style="font-size:12px;color:#888;padding:6px">No hourly data.</p>';
  const rows = hourly.map(h => {
    const temp  = h.temperature  != null ? `${h.temperature}°C`    : '—';
    const rain  = h.rain_mm      != null ? `${h.rain_mm}mm`        : '—';
    const prob  = h.rain_prob    != null ? `${Math.round(h.rain_prob)}%` : '—';
    const wind  = h.wind         != null ? `${h.wind}m/s`          : '—';
    const dir   = h.wind_direction || '—';
    const cloud = h.cloudiness   != null ? `${Math.round(h.cloudiness)}%` : '—';
    return `<tr>
      <td>${escHtml(h.time_str || '')}</td>
      <td>${temp}</td>
      <td>${rain}</td>
      <td>${prob}</td>
      <td>${wind} ${dir}</td>
      <td>${cloud}</td>
    </tr>`;
  }).join('');
  return `<table class="fc-hourly-table">
    <thead><tr><th>Time</th><th>Temp</th><th>Rain</th><th>Chance</th><th>Wind</th><th>Cloud</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function toggleHourly(uid, btn) {
  const el = document.getElementById(uid);
  if (!el) return;
  const open = el.classList.toggle('open');
  btn.textContent = open ? 'Hide hourly breakdown ▴' : 'Show hourly breakdown ▾';
}

// Minimal safe markdown: **bold**, *italic*, line breaks
function renderMarkdown(str) {
  return escHtml(str)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/\n/g, '<br>');
}

// Answer length preference (short | detailed)
let answerLength = (window.__ANSWER_LENGTH__ || 'detailed');

function setAnswerLength(val, save) {
  answerLength = val;
  document.querySelectorAll('.len-opt').forEach(b => {
    b.classList.toggle('active', b.dataset.len === val);
  });
  if (save) saveSettings(true);
}

// Sidebar collapse toggle
function toggleSidebar() {
  document.querySelector('.layout').classList.toggle('sidebar-collapsed');
}

// Browser geolocation → reverse-geocode → set home location
function useMyLocation() {
  const status = document.getElementById('save-status');
  if (!navigator.geolocation) {
    status.style.color = '#A32D2D';
    status.textContent = 'Geolocation not supported by your browser.';
    return;
  }
  status.style.color = '#5F5E5A';
  status.textContent = 'Detecting your location…';
  navigator.geolocation.getCurrentPosition(
    pos => {
      fetch('/geolocate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
      })
      .then(r => r.json())
      .then(data => {
        if (data.error) {
          status.style.color = '#A32D2D';
          status.textContent = data.error;
        } else {
          const disp = document.getElementById('home-display');
          if (disp) disp.textContent = data.home;
          status.style.color = '#1D9E75';
          status.textContent = 'Location set: ' + data.home;
          setTimeout(() => { status.textContent = ''; }, 3000);
        }
      })
      .catch(() => { status.style.color = '#A32D2D'; status.textContent = 'Failed to set location.'; });
    },
    err => {
      status.style.color = '#A32D2D';
      status.textContent = 'Could not get location: ' + err.message;
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
}

// Settings
function saveSettings(silent) {
  const homeEl = document.getElementById('home-input');
  const home = homeEl ? homeEl.value.trim() : '';
  const ctx  = document.getElementById('ctx-input').value.trim();
  const status = document.getElementById('save-status');
  if (!silent) status.textContent = 'Saving…';
  fetch('/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ home_location: home, user_context: ctx, answer_length: answerLength }),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) {
      status.style.color = '#A32D2D';
      status.textContent = data.error;
    } else if (!silent) {
      status.style.color = '#1D9E75';
      status.textContent = 'Saved!';
      setTimeout(() => { status.textContent = ''; }, 2500);
    }
  })
  .catch(() => { if (!silent) status.textContent = 'Save failed.'; });
}

document.getElementById('save-settings').addEventListener('click', () => saveSettings(false));

// Clear
document.getElementById('clear-btn').addEventListener('click', () => {
  fetch('/clear', { method: 'POST' }).then(() => {
    chatWindow.innerHTML = `
      <div class="welcome-card">
        <p class="welcome-title">Hello! I'm MetGPT.</p>
        <p class="welcome-body">Ask me about the weather anywhere in Ireland — for hiking, travel, commuting, or anything else.</p>
        <div class="suggestions">
          <button class="suggestion" onclick="sendSuggestion(this)">Will it rain in Galway this weekend?</button>
          <button class="suggestion" onclick="sendSuggestion(this)">Is it a good day for hiking in Wicklow?</button>
          <button class="suggestion" onclick="sendSuggestion(this)">What should I wear in Cork tomorrow?</button>
        </div>
      </div>`;
  });
});

function escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}