// The console web page: reads /api/v1/console/stream and shows it.
// Read only, no session needed. ?lines=N sets the number of lines kept (default 50000).

(function () {
  "use strict";

  const T = window.BooBootTerminal;
  const params = new URLSearchParams(location.search);
  const term = new T.Terminal(Number(params.get("lines")) || 50000);
  const $ = id => document.getElementById(id);
  const screen = $("screen"), panel = $("panel"), followButton = $("follow");
  const BLOCK = 100;  // lines per block of the screen
  const PALETTE = [
    "#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
    "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff",
  ];

  let name = "", connection = "Connecting...", power = "", session = "";
  let follow = true, holding = false, scheduled = false;

  // Screen: line divs in blocks. nodes[i] shows line domFirst + i.
  let nodes = [], domFirst = 0, from = 0;
  const styles = new Map();

  function color(code) {
    if (code >= T.RGB)
      return "#" + (code - T.RGB).toString(16).padStart(6, "0");
    const i = code - 1;
    if (i < 16)
      return PALETTE[i];
    if (i < 232) {
      const level = n => (n ? 55 + n * 40 : 0);
      const j = i - 16;
      return `rgb(${level(Math.floor(j / 36))},${level(Math.floor(j / 6) % 6)},${level(j % 6)})`;
    }
    const gray = 8 + (i - 232) * 10;
    return `rgb(${gray},${gray},${gray})`;
  }

  function css(key) {
    let text = styles.get(key);
    if (text !== undefined)
      return text;
    const { fg, bg, flags } = T.parseStyle(key);
    let f = fg ? color(fg) : "", b = bg ? color(bg) : "";
    if (flags & T.INVERSE)
      [f, b] = [b || "#1e1e1e", f || "#cccccc"];
    text = (f ? "color:" + f + ";" : "") + (b ? "background:" + b + ";" : "")
      + (flags & T.BOLD ? "font-weight:bold;" : "") + (flags & T.DIM ? "opacity:.65;" : "")
      + (flags & T.ITALIC ? "font-style:italic;" : "") + (flags & T.UNDERLINE ? "text-decoration:underline;" : "");
    styles.set(key, text);
    return text;
  }

  // Each line ends with "\n", so that copied text has its line breaks, empty lines too.
  function render(line) {
    const div = document.createElement("div");
    if (line.marker)
      div.className = "m";
    const runs = line.runs;
    if (!runs) {
      div.textContent = line.text + "\n";
    } else {
      if (runs[0].start > 0)
        div.append(line.text.slice(0, runs[0].start));
      for (let i = 0; i < runs.length; i++) {
        const text = line.text.slice(runs[i].start, i + 1 < runs.length ? runs[i + 1].start : line.text.length);
        if (!runs[i].style) {
          div.append(text);
        } else if (text) {
          const span = document.createElement("span");
          span.style.cssText = css(runs[i].style);
          span.textContent = text;
          div.append(span);
        }
      }
      div.append("\n");
    }
    div.line = line;
    return div;
  }

  function append(div) {
    let block = screen.lastElementChild;
    if (!block || block.childElementCount >= BLOCK) {
      block = document.createElement("div");
      screen.append(block);
    }
    block.append(div);
  }

  function update() {
    // Lines dropped by the terminal, or all of them after a clear.
    const drop = Math.min(term.first - domFirst, nodes.length);
    if (drop >= nodes.length) {
      screen.textContent = "";
      nodes = [];
    } else if (drop > 0) {
      for (let i = 0; i < drop; i++) {
        const block = nodes[i].parentNode;
        nodes[i].remove();
        if (!block.firstChild)
          block.remove();
      }
      nodes.splice(0, drop);
    }
    domFirst = term.first;
    // Lines before the last do not change once written, but a marker can come before the last one.
    for (let n = Math.max(from, domFirst); n <= term.last; n++) {
      const line = term.line(n), node = nodes[n - domFirst];
      if (node && node.line === line)
        continue;
      const div = render(line);
      if (node)
        node.replaceWith(div);
      else
        append(div);
      nodes[n - domFirst] = div;
    }
    from = term.last;
  }

  function schedule() {
    if (scheduled)
      return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      update();
      if (follow && !holding)
        screen.scrollTop = screen.scrollHeight;
      showInfo();
    });
  }

  function showInfo() {
    const parts = [connection, power && "power " + power, session && "session " + session,
      term.lines.length.toLocaleString("en") + " lines"];
    $("info").textContent = parts.filter(Boolean).join("   ");
    $("name").textContent = name || "BooBoot";
    document.title = name ? name + " - BooBoot" : "BooBoot";
  }

  function setFollow(on) {
    follow = on;
    followButton.classList.toggle("on", on);
    if (on)
      screen.scrollTop = screen.scrollHeight;
  }

  function pad(n, width) {
    return String(n).padStart(width || 2, "0");
  }

  function timeText(date) {
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
      + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
  }

  function marker(what, date) {
    term.addMarker(`---- ${what} ${timeText(date)} ----`);
  }

  // Stream

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  async function stream() {
    let since = "start", next = -1, started = 0, lastPower = 0, connectTime = 0, restarted = false;
    for (;;) {
      const abort = new AbortController();
      let timer = 0;
      // The server sends a ping after 10 s without output.
      const watch = () => {
        clearTimeout(timer);
        timer = setTimeout(() => abort.abort(), 30000);
      };
      let restart = false;
      try {
        watch();
        const resp = await fetch("/api/v1/console/stream?since=" + encodeURIComponent(since),
          { signal: abort.signal, cache: "no-store" });
        if (!resp.ok)
          throw new Error("HTTP " + resp.status);
        const reader = resp.body.getReader(), decoder = new TextDecoder();
        let text = "";
        while (!restart) {
          const { value, done } = await reader.read();
          if (done)
            throw new Error("connection closed");
          watch();
          text += decoder.decode(value, { stream: true });
          let end;
          while (!restart && (end = text.indexOf("\n")) >= 0) {
            const e = JSON.parse(text.slice(0, end));
            text = text.slice(end + 1);
            if (e.type === "hello") {
              if (started && e.started !== started) {
                // The server was restarted: its cursors start again from 0.
                [since, next, started, restart, restarted] = ["start", -1, 0, true, true];
                break;
              }
              started = e.started;
              connectTime = e.time;
              name = e.name;
              power = e.power;
              connection = "Connected";
              if (restarted)
                marker("server restarted", new Date());
              restarted = false;
            } else if (e.type === "output") {
              if (next >= 0 && e.cursor > next)
                term.addMarker(`---- ${e.cursor - next} bytes lost ----`);
              next = e.next;
              since = String(next);
              term.feed(e.text);
            } else if (e.type === "power" && e.time > lastPower) {
              // After a reconnection, the last switch may come again.
              lastPower = e.time;
              if (e.time > connectTime)
                power = e.state;
              marker("power " + e.state, new Date(e.time * 1000));
            }
            schedule();
          }
        }
      } catch (err) {
        connection = "No connection, trying again: " + (abort.signal.aborted ? "no data for 30 s" : err.message);
        schedule();
      } finally {
        clearTimeout(timer);
        abort.abort();
      }
      if (!restart)
        await sleep(2000);
    }
  }

  async function poll() {
    for (;;) {
      try {
        const s = await (await fetch("/api/v1/status", { cache: "no-store" })).json();
        session = s.session.active ? "used by " + s.session.client : "free";
        power = s.power.state;
        name = s.name;
        schedule();
      } catch (err) {
        // The stream shows the connection state.
      }
      await sleep(3000);
    }
  }

  // Commands

  function save() {
    const url = URL.createObjectURL(new Blob([term.text("\n")], { type: "text/plain" }));
    const a = document.createElement("a");
    const now = new Date();
    a.href = url;
    a.download = `${name || "console"}-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-`
      + `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  function sizeText(n) {
    return n < 1024 ? n + " B" : n < 1 << 20 ? (n / 1024).toFixed(1) + " KB" : (n / (1 << 20)).toFixed(1) + " MB";
  }

  async function showLogs() {
    if (!panel.hidden) {
      panel.hidden = true;
      return;
    }
    panel.textContent = "Loading...";
    panel.hidden = false;
    try {
      const resp = await fetch("/api/v1/logs", { cache: "no-store" });
      const r = await resp.json();
      panel.textContent = "";
      const p = document.createElement("p");
      p.textContent = r.files.length ? "Console logs on the BooBoot board, one file per power on:" : "No log files.";
      panel.append(p);
      const table = document.createElement("table");
      for (const f of r.files) {
        const row = table.insertRow();
        const a = document.createElement("a");
        a.href = "/api/v1/logs/" + encodeURIComponent(f.name);
        a.download = f.name;
        a.textContent = f.name;
        row.insertCell().append(a);
        row.insertCell().textContent = sizeText(f.size);
        row.insertCell().textContent = timeText(new Date(f.mtime * 1000)).slice(0, 19)
          + (f.name === r.current ? "  (current)" : "");
      }
      panel.append(table);
    } catch (err) {
      panel.textContent = "Cannot list the log files: " + err.message;
    }
  }

  let size = 13;
  try {
    size = Number(localStorage.getItem("booboot-size")) || 13;
  } catch (err) {
    // No storage: the default size.
  }

  function setSize(value) {
    size = Math.min(32, Math.max(8, value));
    document.documentElement.style.setProperty("--size", size + "px");
    try {
      localStorage.setItem("booboot-size", String(size));
    } catch (err) {
      // No storage: the size is not kept.
    }
  }

  followButton.onclick = () => setFollow(!follow);
  $("clear").onclick = () => {
    term.clear();
    setFollow(true);
    schedule();
  };
  $("save").onclick = save;
  $("logs").onclick = showLogs;
  $("smaller").onclick = () => setSize(size - 1);
  $("bigger").onclick = () => setSize(size + 1);
  screen.addEventListener("scroll", () => {
    if (!holding)
      setFollow(screen.scrollTop + screen.clientHeight >= screen.scrollHeight - 4);
  });
  // No scrolling to new output while text is being selected.
  screen.addEventListener("mousedown", () => { holding = true; });
  window.addEventListener("mouseup", () => {
    holding = false;
    if (follow)
      screen.scrollTop = screen.scrollHeight;
  });
  document.addEventListener("keydown", e => {
    if (e.key === "End") {
      setFollow(true);
      e.preventDefault();
    } else if (e.key === "Escape") {
      panel.hidden = true;
    }
  });
  document.addEventListener("click", e => {
    if (!panel.hidden && !panel.contains(e.target) && e.target !== $("logs"))
      panel.hidden = true;
  });

  setSize(size);
  screen.focus();
  showInfo();
  stream();
  poll();
})();
