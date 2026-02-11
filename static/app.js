const statusText = document.getElementById("ws-status");
const statusDot = document.getElementById("ws-dot");
const pressedKeys = new Set();
let ws = null;
let wsReady = false;

const keyMap = {
  arrowup: "forward",
  w: "forward",
  arrowdown: "backward",
  s: "backward",
  arrowleft: "left",
  a: "left",
  arrowright: "right",
  d: "right",
};

function setStatus(state, text) {
  statusDot.dataset.state = state;
  statusText.textContent = text;
}

function connectWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${protocol}://${window.location.host}/ws/control`;
  ws = new WebSocket(wsUrl);

  ws.addEventListener("open", () => {
    wsReady = true;
    setStatus("on", "Live");
  });

  ws.addEventListener("close", () => {
    wsReady = false;
    setStatus("off", "Offline");
    setTimeout(connectWebSocket, 1200);
  });

  ws.addEventListener("error", () => {
    wsReady = false;
    setStatus("off", "Offline");
  });
}

function sendCommand(cmd, dir) {
  const payload = { cmd };
  if (dir) {
    payload.dir = dir;
  }

  if (wsReady && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
    return;
  }

  const params = new URLSearchParams();
  params.set("cmd", cmd);
  if (dir) {
    params.set("dir", dir);
  }

  fetch(`/control?${params.toString()}`, { method: "GET", keepalive: true }).catch(
    () => {}
  );
}

function go(dir) {
  if (!dir) return;
  sendCommand("go", dir);
}

function stop() {
  sendCommand("stop");
}

window.addEventListener("keydown", (event) => {
  const key = event.key.toLowerCase();
  const dir = keyMap[key];
  if (!dir || pressedKeys.has(key)) {
    return;
  }
  if (key.startsWith("arrow")) {
    event.preventDefault();
  }
  pressedKeys.add(key);
  go(dir);
});

window.addEventListener("keyup", (event) => {
  const key = event.key.toLowerCase();
  if (!pressedKeys.has(key)) {
    return;
  }
  pressedKeys.delete(key);
  stop();
});

window.addEventListener("blur", () => {
  pressedKeys.clear();
  stop();
});

window.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    pressedKeys.clear();
    stop();
  }
});

document.querySelectorAll("button[data-dir]").forEach((button) => {
  const dir = button.dataset.dir;
  const isBurst = button.dataset.burst === "true";

  if (dir === "stop") {
    button.addEventListener("click", stop);
    return;
  }

  if (isBurst) {
    button.addEventListener("click", () => {
      go(dir);
      setTimeout(stop, 160);
    });
    return;
  }

  button.addEventListener("pointerdown", () => go(dir));
  button.addEventListener("pointerup", stop);
  button.addEventListener("pointerleave", stop);
  button.addEventListener("pointercancel", stop);
});

setStatus("wait", "Connecting");
connectWebSocket();
