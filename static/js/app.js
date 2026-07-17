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

// Pick a weather icon from the forecast values
function weatherIcon(r) {
  const rain = r.rain_total != null ? r.rain_total : 0;
  const prob = r.rain_prob != null ? r.rain_prob : 0;
  const wind = r.wind_avg != null ? r.wind_avg : 0;
  const sun = '<svg class="fc-weather-icon" width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5" fill="#FFD764" stroke="#FFD764"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.2" y1="4.2" x2="5.6" y2="5.6"/><line x1="18.4" y1="18.4" x2="19.8" y2="19.8"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.2" y1="19.8" x2="5.6" y2="18.4"/><line x1="18.4" y1="5.6" x2="19.8" y2="4.2"/></svg>';
  const cloud = '<svg class="fc-weather-icon" width="30" height="30" viewBox="0 0 24 24" fill="rgba(255,255,255,0.9)" stroke="rgba(255,255,255,0.9)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>';
  const rainy = '<svg class="fc-weather-icon" width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.95)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="19" x2="8" y2="21" stroke="#8FD0FF"/><line x1="12" y1="19" x2="12" y2="22" stroke="#8FD0FF"/><line x1="16" y1="19" x2="16" y2="21" stroke="#8FD0FF"/><path d="M20 16.58A5 5 0 0 0 18 7h-1.26A8 8 0 1 0 4 15.25"/></svg>';
  const windy = '<svg class="fc-weather-icon" width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.95)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.59 4.59A2 2 0 1 1 11 8H2m10.59 11.41A2 2 0 1 0 14 16H2m15.73-8.27A2.5 2.5 0 1 1 19.5 12H2"/></svg>';
  if (rain >= 1 || prob >= 50) return rainy;
  if (wind >= 8) return windy;
  if (prob >= 20) return cloud;
  return sun;
}

// Format wind in m/s, km/h and mph
function windStr(ms) {
  if (ms == null) return 'N/A';
  const kmh = Math.round(ms * 3.6);
  const mph = Math.round(ms * 2.23694);
  return `${ms} m/s · ${kmh} km/h · ${mph} mph`;
}
function windShort(ms) {
  if (ms == null) return '—';
  const kmh = Math.round(ms * 3.6);
  return `${ms} m/s (${kmh} km/h)`;
}

function buildForecastCard(r) {
  const rainStr = r.rain_total != null ? `${r.rain_total} mm` : 'N/A';
  const probStr = r.rain_prob != null ? `${Math.round(r.rain_prob)}%` : 'N/A';
  const windDisp = r.wind_avg != null ? windShort(r.wind_avg) : 'N/A';
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
    const tCompact = (r.temp_min != null && r.temp_max != null)
      ? `${r.temp_min}–${r.temp_max}°C`
      : 'N/A';
    row.innerHTML = `
      <span class="fc-row-day">${dayName}</span>
      <span class="fc-row-temp">${tCompact}</span>
      <span class="fc-row-rain">${rainStr} · ${probStr}</span>
      <span class="fc-row-wind">${windDisp}</span>`;
    return row;
  }

  // Full card for single-day view
  const cRange = (r.temp_min != null && r.temp_max != null) ? `${r.temp_min}–${r.temp_max}°` : 'N/A';

  const ic = {
    tempc: '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 14.76V4a2 2 0 0 0-4 0v10.76a4 4 0 1 0 4 0z"/></svg>',
    rain:  '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/></svg>',
    chance:'<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="16" y1="13" x2="16" y2="21"/><line x1="8" y1="13" x2="8" y2="21"/><line x1="12" y1="15" x2="12" y2="23"/><path d="M20 16.58A5 5 0 0 0 18 7h-1.26A8 8 0 1 0 4 15.25"/></svg>',
    wind:  '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.59 4.59A2 2 0 1 1 11 8H2m10.59 11.41A2 2 0 1 0 14 16H2m15.73-8.27A2.5 2.5 0 1 1 19.5 12H2"/></svg>',
    hum:   '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/></svg>',
  };

  const card = document.createElement('div');
  card.className = 'forecast-card';
  const uid = 'hourly-' + Math.random().toString(36).slice(2,8);
  card.innerHTML = `
    <div class="fc-header">
      <div class="fc-header-left">
        ${weatherIcon(r)}
        <span class="fc-location">${escHtml(r.location)}</span>
      </div>
      <span class="fc-date">${escHtml(r.date)}</span>
    </div>
    <div class="fc-metrics">
      <div class="fc-metric fc-metric--tempc"><span class="fc-metric-label">${ic.tempc} Temp °C</span><span class="fc-metric-value">${cRange}</span></div>
      <div class="fc-metric fc-metric--rain"><span class="fc-metric-label">${ic.rain} Rain total</span><span class="fc-metric-value">${rainStr}</span></div>
      <div class="fc-metric fc-metric--chance"><span class="fc-metric-label">${ic.chance} Rain chance</span><span class="fc-metric-value">${probStr}</span></div>
      <div class="fc-metric fc-metric--wind"><span class="fc-metric-label">${ic.wind} Wind avg</span><span class="fc-metric-value">${windDisp}</span></div>
      <div class="fc-metric fc-metric--humidity"><span class="fc-metric-label">${ic.hum} Humidity</span><span class="fc-metric-value">${humStr}</span></div>
    </div>
    <button class="fc-hourly-toggle" onclick="toggleHourly('${uid}', this)">Show hourly breakdown ▾</button>
    <div class="fc-hourly" id="${uid}">
      ${buildHourlyTable(r.hourly || [])}
    </div>`;
  return card;
}

function buildHourlyTable(hourly) {
  if (!hourly || hourly.length === 0) return '<p style="font-size:12px;color:#888;padding:6px">No hourly data.</p>';
  // Skip rows with no temperature (e.g. boundary entries showing only dashes)
  const usable = hourly.filter(h => h.temperature != null);
  if (usable.length === 0) return '<p style="font-size:12px;color:#888;padding:6px">No hourly data.</p>';
  const rows = usable.map(h => {
    const temp  = h.temperature  != null ? `${h.temperature}°C` : '—';
    const rain  = h.rain_mm      != null ? `${h.rain_mm}mm`        : '—';
    const prob  = h.rain_prob    != null ? `${Math.round(h.rain_prob)}%` : '—';
    const wind  = h.wind         != null ? `${h.wind}m/s (${Math.round(h.wind*3.6)}km/h)` : '—';
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

// Sidebar toggle — drawer on mobile, collapse on desktop
function toggleSidebar() {
  const layout = document.querySelector('.layout');
  if (window.matchMedia('(max-width: 760px)').matches) {
    layout.classList.toggle('sidebar-open');
    let overlay = document.querySelector('.sidebar-overlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.className = 'sidebar-overlay';
      overlay.addEventListener('click', () => {
        layout.classList.remove('sidebar-open');
        overlay.classList.remove('open');
      });
      document.body.appendChild(overlay);
    }
    overlay.classList.toggle('open', layout.classList.contains('sidebar-open'));
  } else {
    layout.classList.toggle('sidebar-collapsed');
  }
}

// Browser geolocation → reverse-geocode → set home location
function useMyLocation() {
  const status = document.getElementById('save-status');
  if (!navigator.geolocation) {
    status.style.color = '#FF9B9B';
    status.textContent = 'GPS not supported. Please type a location above instead.';
    return;
  }
  status.style.color = 'rgba(255,255,255,0.8)';
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
          status.style.color = '#FF9B9B';
          status.textContent = data.error;
        } else {
          const inp = document.getElementById('home-input');
          if (inp) inp.value = data.home;
          status.style.color = '#7FE3A8';
          status.textContent = 'Location set: ' + data.home;
          setTimeout(() => { status.textContent = ''; }, 3000);
        }
      })
      .catch(() => { status.style.color = '#FF9B9B'; status.textContent = 'Failed to set location.'; });
    },
    err => {
      status.style.color = '#FF9B9B';
      status.textContent = 'Could not get GPS location. Please type a location above instead.';
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
      status.style.color = '#FF9B9B';
      status.textContent = data.error;
    } else if (!silent) {
      status.style.color = '#7FE3A8';
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