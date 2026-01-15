let consoleLastId = 0;

async function fetchJson(url, options = {}) {
  const res = await fetch(url, options);
  return res.json();
}
(function () {
  const btnStart = document.getElementById("btnStart");
  const btnStop = document.getElementById("btnStop");
  const btnRestart = document.getElementById("btnRestart");

  async function post(url) {
    const r = await fetch(url, { method: "POST" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || `Request failed: ${r.status}`);
    return j;
  }

  if (btnStart) btnStart.addEventListener("click", async () => { await post("/api/server/start"); });
  if (btnStop) btnStop.addEventListener("click", async () => { await post("/api/server/stop"); });
  if (btnRestart) btnRestart.addEventListener("click", async () => { await post("/api/server/restart"); });
})();

function initDashboard() {
  const cpuEl = document.getElementById("cpu");
  const memEl = document.getElementById("mem");
  const diskEl = document.getElementById("disk");
  const statusPill = document.querySelector("#server-status .status-pill");
  const btnStart = document.getElementById("btn-start");
  const btnStop = document.getElementById("btn-stop");

  async function updateStats() {
    try {
      const data = await fetchJson("/api/stats");
      cpuEl.textContent = data.cpu_percent.toFixed(1) + "%";
      const memUsedGB = data.mem_used / (1024**3);
      const memTotalGB = data.mem_total / (1024**3);
      memEl.textContent = memUsedGB.toFixed(2) + " / " + memTotalGB.toFixed(2) + " GB (" + data.mem_percent.toFixed(1) + "%)";
      const diskUsedGB = data.disk_used / (1024**3);
      const diskTotalGB = data.disk_total / (1024**3);
      diskEl.textContent = diskUsedGB.toFixed(2) + " / " + diskTotalGB.toFixed(2) + " GB (" + data.disk_percent.toFixed(1) + "%)";

      if (statusPill) {
        if (data.server_running) {
          statusPill.classList.add("online");
          statusPill.classList.remove("offline");
          statusPill.textContent = "Online";
        } else {
          statusPill.classList.add("offline");
          statusPill.classList.remove("online");
          statusPill.textContent = "Offline";
        }
      }
    } catch (e) {
      console.error(e);
    }
  }

  if (cpuEl) {
    updateStats();
    setInterval(updateStats, 3000);
  }

  if (btnStart) {
    btnStart.addEventListener("click", async () => {
      await fetchJson("/api/server/start", { method: "POST" });
      updateStats();
    });
  }
  if (btnStop) {
  btnStop.addEventListener("click", async () => {
    try {
      await fetchJson("/api/server/stop", { method: "POST" });
      updateStats();
    } catch (e) {
      console.error("Stop request failed", e);
    }
  });
}

}

function initConsole() {
  const output = document.getElementById("console-output");
  const form = document.getElementById("console-form");
  const input = document.getElementById("console-command");

  if (!output || !form || !input) return;

  async function pollLogs() {
    try {
      const data = await fetchJson("/api/console/logs?last_id=" + consoleLastId);
      for (const entry of data.logs) {
        consoleLastId = entry.id;
        const div = document.createElement("div");
        div.classList.add("line");
        if (entry.line.startsWith("[PANEL]")) {
          div.classList.add("system");
        } else if (entry.line.startsWith("> ")) {
          div.classList.add("command");
        }
        div.textContent = entry.line;
        output.appendChild(div);
      }
      if (data.logs.length > 0) {
        output.scrollTop = output.scrollHeight;
      }
    } catch (e) {
      console.error(e);
    }
  }

  setInterval(pollLogs, 1000);
  pollLogs();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const cmd = input.value.trim();
    if (!cmd) return;
    await fetch("/api/console/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: cmd })
    });
    input.value = "";
  });
}

function initSchedulerForm() {
  const typeSelect = document.getElementById("schedule-type");
  if (!typeSelect) return;
  const fieldOnce = document.getElementById("field-once");
  const fieldDaily = document.getElementById("field-daily");
  const fieldInterval = document.getElementById("field-interval");

  function updateVisibility() {
    const t = typeSelect.value;
    fieldOnce.classList.add("hidden");
    fieldDaily.classList.add("hidden");
    fieldInterval.classList.add("hidden");
    if (t === "once") fieldOnce.classList.remove("hidden");
    if (t === "daily") fieldDaily.classList.remove("hidden");
    if (t === "interval") fieldInterval.classList.remove("hidden");
  }

  typeSelect.addEventListener("change", updateVisibility);
  updateVisibility();
}
async function fetchPlayersOnline() {
  const res = await fetch("/api/players/online");
  if (!res.ok) throw new Error("Failed to fetch online players");
  return res.json();
}

async function fetchPlayersHistory() {
  const res = await fetch("/api/players/history");
  if (!res.ok) throw new Error("Failed to fetch player history");
  return res.json();
}

function formatLastSeen(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function renderPlayerLists() {
  const onlineEl = document.getElementById("online-players");
  const recentEl = document.getElementById("recent-players");
  if (!onlineEl && !recentEl) return;

  // Online players
  fetchPlayersOnline()
    .then(data => {
      const players = data.players || [];
      onlineEl.innerHTML = "";

      if (!players.length) {
        onlineEl.innerHTML = '<li class="placeholder">No one is online.</li>';
        return;
      }

      for (const p of players) {
        const li = document.createElement("li");
        li.className = "player-row";

        // Use a public avatar service for heads based on name
        // (you can swap this to any other skin API you like)
        const avatarUrl = `https://mc-heads.net/avatar/${encodeURIComponent(p.name)}/32`;

        li.innerHTML = `
          <img class="player-avatar" src="${avatarUrl}" alt="${p.name}">
          <div>
            <div class="player-name">${p.name}</div>
            <div class="player-meta">${p.last_seen_iso ? "Last seen: " + formatLastSeen(p.last_seen_iso) : ""}</div>
          </div>
        `;
        onlineEl.appendChild(li);
      }
    })
    .catch(err => {
      console.error(err);
      if (onlineEl) {
        onlineEl.innerHTML = '<li class="placeholder">Failed to load online players.</li>';
      }
    });

  // Recent players
  fetchPlayersHistory()
    .then(data => {
      const players = data.players || [];
      recentEl.innerHTML = "";

      if (!players.length) {
        recentEl.innerHTML = '<li class="placeholder">No history yet.</li>';
        return;
      }

      for (const p of players) {
        const li = document.createElement("li");
        li.className = "player-row";

        const avatarUrl = `https://mc-heads.net/avatar/${encodeURIComponent(p.name)}/32`;

        li.innerHTML = `
          <img class="player-avatar" src="${avatarUrl}" alt="${p.name}">
          <div>
            <div class="player-name">${p.name}</div>
            <div class="player-meta">Last seen: ${formatLastSeen(p.last_seen_iso)}</div>
          </div>
        `;
        recentEl.appendChild(li);
      }
    })
    .catch(err => {
      console.error(err);
      if (recentEl) {
        recentEl.innerHTML = '<li class="placeholder">Failed to load history.</li>';
      }
    });
}

document.addEventListener("DOMContentLoaded", () => {
  initDashboard();
  initConsole();
  initSchedulerForm();
  
  // NEW: load players and refresh every 10 seconds
  renderPlayerLists();
  setInterval(renderPlayerLists, 10000);
});
