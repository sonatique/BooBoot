// The console web page of a DUT: reads api/v1/console/stream and shows it. Its paths are relative:
// the page of a DUT of the board is at /duts/NAME/.
// Watching needs no session. "Take control" opens the session, to type into the console.
// ?lines=N sets the number of lines kept (default 50000).

(function () {
  "use strict";

  const T = window.BooBootTerminal;
  const params = new URLSearchParams(location.search);
  const term = new T.Terminal(Number(params.get("lines")) || 50000);
  const $ = id => document.getElementById(id);
  const screen = $("screen"), panel = $("panel"), followButton = $("follow"), controlButton = $("control");
  const powerButton = $("power");
  const BLOCK = 100;  // lines per block of the screen
  const PALETTE = [
    "#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
    "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#ffffff",
  ];

  let name = "", connection = "Connecting...", power = "", session = "", address = "";
  // Names of the DUTs of the board, as last shown in the list.
  let dutList = "";
  // Free text shown with the name of the DUT.
  let label = "";
  // The last status, for the DUT panel.
  let status = null;
  // What the panel shows: "logs" or "dut".
  let panelKind = "";
  let follow = true, holding = false, scheduled = false;
  // Control: the session token, and the keys waiting to be sent.
  let token = "", note = "", keys = "", sending = false;
  // True while a power switch of the power button runs.
  let switching = false;

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

  // Shows the cursor on the character at offset, or after the text.
  function addCursor(div, offset) {
    const cursor = document.createElement("span");
    cursor.className = "cursor";
    const walker = document.createTreeWalker(div, NodeFilter.SHOW_TEXT);
    const texts = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode())
      texts.push(node);
    let pos = 0;
    for (const node of texts) {
      const i = offset - pos;
      if (i < node.data.length && node.data[i] !== "\n") {
        const at = node.splitText(i);
        at.splitText(1);
        at.replaceWith(cursor);
        cursor.append(at);
        return;
      }
      pos += node.data.length;
    }
    const last = texts[texts.length - 1];
    cursor.className = "cursor end";
    last.splitText(last.data.length - 1).before(cursor);
  }

  function renderLine(n, cursor) {
    const div = render(term.line(n));
    if (cursor >= 0)
      addCursor(div, cursor);
    div.cursor = cursor;
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
      const cursor = token && n === term.last ? term.col : -1;
      if (node && node.line === line && node.cursor === cursor)
        continue;
      const div = renderLine(n, cursor);
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
    const parts = [connection, address && "also at " + address, power && "power " + power, session && "session " + session,
      term.lines.length.toLocaleString("en") + " lines", token ? (note ? "in control, " + note : "in control") : note];
    $("info").textContent = parts.filter(Boolean).join("   ");
    // The power button follows the power state, and works only in control.
    powerButton.textContent = power === "on" ? "Power off" : "Power on";
    powerButton.disabled = !token || switching;
    const shown = label ? label + " (" + name + ")" : name;
    $("name").textContent = shown || "BooBoot";
    document.title = shown ? shown + " - BooBoot" : "BooBoot";
    fillDut();
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
        const resp = await fetch("api/v1/console/stream?since=" + encodeURIComponent(since),
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
        const t = token;
        const s = status = await (await fetch("api/v1/status", { cache: "no-store", headers: auth(t) })).json();
        session = !s.session.active ? "free" : s.session.yours ? "yours"
          : "used by " + s.session.client + (s.session.alive === false ? " (gone)" : "");
        // Only an answer about the current session counts.
        if (t && t === token && !s.session.yours)
          setControl("", s.session.active ? "control lost: the DUT is used by " + s.session.client
            : "control ended after the idle time");
        power = s.power.state;
        name = s.name;
        label = s.label || "";
        if (s.duts) {
          // The other DUTs of the board with their labels, when there are any.
          const duts = s.duts.length > 1 ? (await (await fetch("api/v1/duts", { cache: "no-store" })).json()).duts : [];
          const list = JSON.stringify(duts.map(d => [d.name, d.label]));
          if (list !== dutList) {
            dutList = list;
            showDuts(duts);
          }
        }
        // Where the page uses a name, the address of the board, for where the name does not work.
        const addresses = (s.network && s.network.addresses) || [];
        address = addresses.length && !addresses.includes(location.hostname)
          ? addresses[0] + (location.port ? ":" + location.port : "") : "";
        schedule();
      } catch (err) {
        // The stream shows the connection state.
      }
      await sleep(3000);
    }
  }

  // With several DUTs on the board, a list in place of the name goes to the page of another one.
  function showDuts(duts) {
    const select = $("duts");
    select.replaceChildren(...duts.map(d => new Option(d.label ? d.label + " (" + d.name + ")" : d.name,
      "/duts/" + encodeURIComponent(d.name) + "/", false, d.name === name)));
    select.hidden = duts.length < 2;
    $("name").hidden = !select.hidden;
  }

  // Control

  function auth(t) {
    return t ? { Authorization: "Bearer " + t } : {};
  }

  async function api(method, path, body) {
    const resp = await fetch("api/v1" + path, {
      method, cache: "no-store", body: body && JSON.stringify(body),
      headers: Object.assign({ "Content-Type": "application/json" }, auth(token)),
    });
    const r = await resp.json().catch(() => ({}));
    if (!resp.ok)
      throw Object.assign(new Error(r.message || "HTTP " + resp.status), { code: r.error, info: r });
    return r;
  }

  function setControl(t, text) {
    if (t)
      session = "yours";
    token = t;
    note = text;
    keys = "";
    controlButton.classList.toggle("on", !!t);
    controlButton.textContent = t ? "Release control" : "Take control";
    document.body.classList.toggle("control", !!t);
    // Draw or remove the cursor.
    from = Math.min(from, term.last);
    schedule();
  }

  function takeOverText(s) {
    if (s.alive === false)
      return `The DUT is used by ${s.client}, which is gone: no heartbeat for ${Math.round(s.heartbeat_age)} s.`
        + " Take it over?";
    if (s.alive)
      return `The DUT is used by ${s.client}, which is connected. Last action ${Math.round(s.idle)} s ago.`
        + " Taking over interrupts their work. Take it over anyway?";
    return `The DUT is used by ${s.client}, idle for ${Math.round(s.idle)} s. Take it over?`;
  }

  async function takeControl() {
    try {
      let r, force = false;
      while (!r) {
        try {
          r = await api("POST", "/session", force ? { force } : {});
        } catch (err) {
          const s = err.info && err.info.session;
          if (err.code !== "busy" || !s || force === true)
            throw err;
          if (!confirm(takeOverText(s)))
            return;
          // "gone" takes it only if its client is still gone, else the page asks again.
          force = s.alive === false ? "gone" : true;
        }
      }
      setControl(r.session, "");
      heartbeat();
      screen.focus();
    } catch (err) {
      setControl("", "cannot take control: " + err.message);
    }
  }

  // Tells the server that the page is still there, so that others see when it is gone.
  async function heartbeat() {
    try {
      await api("POST", "/session/heartbeat");
    } catch (err) {
      // The status poll finds out when the control ends.
    }
  }

  // The control ended on the server: another client took the session, or it ended after the idle time.
  function lost(err) {
    setControl("", err.code === "busy" ? "control lost: the DUT is used by " + err.info.session.client
      : "control ended after the idle time");
  }

  // DUT panel: the DUT on the board, the same for everyone.

  function showDut() {
    if (!panel.hidden && panelKind === "dut") {
      panel.hidden = true;
      return;
    }
    panelKind = "dut";
    const grid = document.createElement("div");
    grid.className = "grid";
    const field = (label, id, input) => {
      const name = document.createElement("span");
      name.textContent = label;
      const value = document.createElement(input ? "input" : "span");
      value.id = id;
      grid.append(name, value);
      return value;
    };
    field("Name", "dut-name", true).readOnly = true;
    field("Address", "dut-address", true).readOnly = true;
    const box = field("Label", "dut-label", true);
    box.placeholder = "like ZCU102 rev B, bench 3";
    box.maxLength = 100;
    box.value = label;
    box.onkeydown = e => {
      if (e.key === "Enter" && token)
        setLabel();
    };
    const set = document.createElement("button");
    set.id = "dut-set";
    set.textContent = "Set label";
    set.title = "Shown with the name, to everyone. Empty: no label";
    set.onclick = setLabel;
    const note = document.createElement("span");
    note.id = "dut-note";
    const row = document.createElement("span");
    row.append(set, " ", note);
    grid.append(document.createElement("span"), row);
    for (const [label, id] of [["Console", "dut-console"], ["Power", "dut-power"], ["SD card", "dut-sd"],
      ["Server", "dut-server"]])
      field(label, id);
    panel.replaceChildren(grid);
    panel.hidden = false;
    fillDut();
    if (token)
      box.focus();
  }

  // The DUT panel, from the last status. The label box keeps what is typed.
  function fillDut() {
    if (panel.hidden || panelKind !== "dut")
      return;
    $("dut-name").value = name;
    $("dut-address").value = location.origin + location.pathname.replace(/\/[^/]*$/, "");
    $("dut-label").readOnly = !token;
    $("dut-set").disabled = !token;
    const note = $("dut-note");
    if (!token)
      note.textContent = "Take control to change it";
    else if (note.textContent === "Take control to change it")
      note.textContent = "";
    if (!status)
      return;
    const c = status.console || {}, p = status.power || {}, sd = status.sd || {}, net = status.network || {};
    const withError = (text, e) => e.error ? text + " (" + e.error + ")" : text;
    $("dut-console").textContent = c.connected === false ? withError(`${c.device} at ${c.baudrate} baud, not connected`, c)
      : `${c.device} at ${c.baudrate} baud`;
    $("dut-power").textContent = withError(`${p.backend} relay, ${power}`, p);
    $("dut-sd").textContent = withError(`on the ${sd.mode} side`, sd);
    $("dut-server").textContent = `BooBoot ${status.version}` + (net.hostname ? " on " + net.hostname : "")
      + (net.addresses && net.addresses.length ? " (" + net.addresses.join(", ") + ")" : "");
  }

  async function setLabel() {
    const note = $("dut-note");
    try {
      label = (await api("PUT", "/label", { label: $("dut-label").value })).label;
      note.textContent = label ? "Label set" : "Label removed";
      dutList = "";
    } catch (err) {
      if (err.code === "busy" || err.code === "no_session")
        lost(err);
      note.textContent = "Not set: " + err.message;
    }
    schedule();
  }

  async function switchPower() {
    const on = power !== "on";
    if (!on && !confirm("Switch the DUT off?"))
      return;
    switching = true;
    schedule();
    try {
      power = (await api("PUT", "/power", { state: on ? "on" : "off" })).power;
      note = "";
    } catch (err) {
      if (err.code === "busy" || err.code === "no_session")
        lost(err);
      else
        note = `power ${on ? "on" : "off"} failed: ${err.message}`;
    }
    switching = false;
    schedule();
    screen.focus();
  }

  async function releaseControl() {
    const t = token;
    setControl("", "");
    session = "free";
    try {
      await fetch("api/v1/session", { method: "DELETE", headers: auth(t) });
    } catch (err) {
      // The session ends by itself after the idle time.
    }
  }

  // Keys are sent in order, one request at a time; keys typed meanwhile go together in the next one.
  function type(data) {
    if (!token || !data)
      return;
    keys += data;
    setFollow(true);
    if (!sending)
      sendKeys();
  }

  async function sendKeys() {
    sending = true;
    while (keys && token) {
      const text = keys;
      keys = "";
      try {
        await api("POST", "/console/write", { text });
      } catch (err) {
        if (err.code === "busy" || err.code === "no_session")
          lost(err);
        else
          note = "keys not sent: " + err.message;
        schedule();
      }
    }
    sending = false;
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
    if (!panel.hidden && panelKind === "logs") {
      panel.hidden = true;
      return;
    }
    panelKind = "logs";
    panel.textContent = "Loading...";
    panel.hidden = false;
    try {
      const resp = await fetch("api/v1/logs", { cache: "no-store" });
      const r = await resp.json();
      panel.textContent = "";
      const p = document.createElement("p");
      p.textContent = r.files.length ? "Console logs on the BooBoot board, one file per power on:" : "No log files.";
      panel.append(p);
      const table = document.createElement("table");
      for (const f of r.files) {
        const row = table.insertRow();
        const a = document.createElement("a");
        a.href = "api/v1/logs/" + encodeURIComponent(f.name);
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
  controlButton.onclick = () => (token ? releaseControl() : takeControl());
  $("duts").onchange = e => { location.href = e.target.value; };
  powerButton.onclick = switchPower;
  $("dut").onclick = showDut;
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
    // Keys typed in the panel, like a label, stay there.
    if (panel.contains(e.target)) {
      if (e.key === "Escape") {
        panel.hidden = true;
        screen.focus();
      }
      return;
    }
    if (token) {
      // Page Up and Page Down still scroll the page.
      const data = e.key.startsWith("Page") ? null : T.keyData(e, !getSelection().isCollapsed);
      if (data !== null) {
        e.preventDefault();
        type(data);
      }
      return;
    }
    if (e.key === "End") {
      setFollow(true);
      e.preventDefault();
    } else if (e.key === "Escape") {
      panel.hidden = true;
    }
  });
  document.addEventListener("paste", e => {
    if (token && !panel.contains(e.target)) {
      e.preventDefault();
      type(e.clipboardData.getData("text").replace(/\r?\n/g, "\r"));
    }
  });
  // Release the session when the page is closed or reloaded.
  window.addEventListener("pagehide", () => {
    if (token)
      fetch("api/v1/session", { method: "DELETE", headers: auth(token), keepalive: true });
  });
  document.addEventListener("click", e => {
    if (!panel.hidden && !panel.contains(e.target) && e.target !== $("logs") && e.target !== $("dut"))
      panel.hidden = true;
  });

  setSize(size);
  screen.focus();
  showInfo();
  stream();
  poll();
  setInterval(() => {
    if (token)
      heartbeat();
  }, 10000);
})();
