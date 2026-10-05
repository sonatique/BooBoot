// Console output as lines, for a line based view: the kind of terminal that
// only writes to its last line. Handles CR, backspace, tabs, colors and line
// erase. Other escape sequences, like cursor moves, are dropped.
// Same rules as client/csharp/BooBootConsole/Terminal.cs.

(function (exports) {
  "use strict";

  const MAX_LINE = 16384;
  const TEXT = 0, ESCAPE = 1, CSI = 2, OSC = 3, OSC_ESCAPE = 4, SKIP_ONE = 5;
  // Style flags
  const BOLD = 1, DIM = 2, ITALIC = 4, UNDERLINE = 8, INVERSE = 16;
  // Colors: 0 is the default, 1 to 256 a palette index plus 1, RGB + 0xRRGGBB a true color.
  const RGB = 0x1000000;

  // A style is a string "fg,bg,flags", "" for the default style.
  function styleKey(fg, bg, flags) {
    return fg || bg || flags ? fg + "," + bg + "," + flags : "";
  }

  function parseStyle(key) {
    const [fg, bg, flags] = key ? key.split(",").map(Number) : [0, 0, 0];
    return { fg, bg, flags };
  }

  class Line {
    constructor(text, runs, marker) {
      this.text = text;
      this.runs = runs || null; // [{start, style}], null when all default
      this.marker = !!marker;   // a line added by the viewer, like a power switch
    }
  }

  class Terminal {
    constructor(maxLines) {
      this.maxLines = maxLines || 50000;
      this.lines = [new Line("")];
      this.first = 0;          // absolute number of lines[0]
      this.onLine = null;      // called with each complete line
      this.cur = [];
      this.curStyles = [];
      this.seq = "";
      this.col = 0;
      this.style = "";
      this.state = TEXT;
      this.dirty = false;
    }

    get last() { return this.first + this.lines.length - 1; }

    line(n) { return this.lines[n - this.first]; }

    get pending() { return this.cur.join(""); }

    feed(text) {
      for (const c of text)
        this.feedChar(c);
      this.sync();
    }

    endLine() {
      if (this.cur.length > 0)
        this.newLine();
    }

    addMarker(text) {
      this.endLine();
      const marker = new Line(text, null, true);
      this.lines.splice(this.lines.length - 1, 0, marker);
      if (this.onLine)
        this.onLine(marker);
      this.trim();
    }

    clear() {
      this.first += this.lines.length;
      this.lines = [new Line("")];
      this.cur = [];
      this.curStyles = [];
      this.col = 0;
    }

    text(newline) {
      return this.lines.map(l => l.text).join(newline || "\n");
    }

    feedChar(c) {
      switch (this.state) {
        case TEXT:
          if (c >= " " && c !== "\x7f" && (c < "\x80" || c > "\x9f"))
            this.put(c);
          else if (c === "\n")
            this.newLine();
          else if (c === "\r")
            this.col = 0;
          else if (c === "\b")
            this.col = Math.max(0, this.col - 1);
          else if (c === "\t")
            this.col = Math.min(MAX_LINE - 1, (Math.floor(this.col / 8) + 1) * 8);
          else if (c === "\x1b")
            this.state = ESCAPE;
          break;
        case ESCAPE:
          this.seq = "";
          if (c === "[")
            this.state = CSI;
          else if ("]PX^_".includes(c))
            this.state = OSC;
          else if ("()*+#%".includes(c))
            this.state = SKIP_ONE;
          else
            this.state = TEXT;
          if (c === "c")
            this.style = "";
          break;
        case CSI:
          if (c >= "@" && c <= "~") {
            this.csi(c);
            this.state = TEXT;
          } else if (c === "\x1b") {
            this.state = ESCAPE;
          } else if (c < " " || this.seq.length > 64) {
            this.state = TEXT;
          } else {
            this.seq += c;
          }
          break;
        case OSC:
          if (c === "\x07")
            this.state = TEXT;
          else if (c === "\x1b")
            this.state = OSC_ESCAPE;
          break;
        case OSC_ESCAPE:
          this.state = c === "\\" ? TEXT : OSC;
          break;
        case SKIP_ONE:
          this.state = TEXT;
          break;
      }
    }

    put(c) {
      while (this.cur.length < this.col) {
        this.cur.push(" ");
        this.curStyles.push("");
      }
      this.cur[this.col] = c;
      this.curStyles[this.col] = this.style;
      this.col++;
      this.dirty = true;
      if (this.cur.length >= MAX_LINE)
        this.newLine();
    }

    newLine() {
      this.dirty = true;
      this.sync();
      if (this.onLine)
        this.onLine(this.lines[this.lines.length - 1]);
      this.cur = [];
      this.curStyles = [];
      this.col = 0;
      this.lines.push(new Line(""));
      this.trim();
    }

    trim() {
      // Drop in batches, as removing from the front of the array copies it.
      const extra = this.lines.length - this.maxLines;
      if (extra > Math.max(Math.floor(this.maxLines / 16), 1)) {
        this.lines.splice(0, extra);
        this.first += extra;
      }
    }

    // Updates the last line from the characters being written.
    sync() {
      if (!this.dirty)
        return;
      this.dirty = false;
      // Run starts are string offsets: a character can take two.
      let runs = null, offset = 0;
      for (let i = 0; i < this.curStyles.length; i++) {
        const prev = i === 0 ? "" : this.curStyles[i - 1];
        if (this.curStyles[i] !== prev)
          (runs = runs || []).push({ start: offset, style: this.curStyles[i] });
        offset += this.cur[i].length;
      }
      this.lines[this.lines.length - 1] = new Line(this.cur.join(""), runs);
    }

    csi(final) {
      if (/^[?<>=]/.test(this.seq))
        return;
      const ps = this.seq.split(/[;:]/).map(p => Math.min(parseInt(p, 10) || 0, 99999));
      const n = ps[0] > 0 ? ps[0] : 1;
      switch (final) {
        case "m":
          this.sgr(ps);
          break;
        case "K":
          this.eraseLine(ps[0] || 0);
          break;
        case "C":
          this.col = Math.min(MAX_LINE - 1, this.col + n);
          break;
        case "D":
          this.col = Math.max(0, this.col - n);
          break;
        case "G":
          this.col = Math.min(MAX_LINE - 1, n - 1);
          break;
        case "P":
          if (this.col < this.cur.length) {
            this.cur.splice(this.col, n);
            this.curStyles.splice(this.col, n);
            this.dirty = true;
          }
          break;
        case "@":
          if (this.col < this.cur.length) {
            const count = Math.min(n, MAX_LINE - this.cur.length);
            this.cur.splice(this.col, 0, ...Array(count).fill(" "));
            this.curStyles.splice(this.col, 0, ...Array(count).fill(""));
            this.dirty = true;
          }
          break;
      }
    }

    eraseLine(mode) {
      if (mode === 0 && this.col < this.cur.length) {
        this.cur.length = this.col;
        this.curStyles.length = this.col;
      } else if (mode === 1) {
        for (let i = 0; i < Math.min(this.col + 1, this.cur.length); i++) {
          this.cur[i] = " ";
          this.curStyles[i] = "";
        }
      } else if (mode === 2) {
        this.cur = [];
        this.curStyles = [];
      }
      this.dirty = true;
    }

    sgr(ps) {
      let { fg, bg, flags } = parseStyle(this.style);
      for (let i = 0; i < ps.length; i++) {
        const p = ps[i];
        if (p === 0) [fg, bg, flags] = [0, 0, 0];
        else if (p === 1) flags |= BOLD;
        else if (p === 2) flags |= DIM;
        else if (p === 3) flags |= ITALIC;
        else if (p === 4) flags |= UNDERLINE;
        else if (p === 7) flags |= INVERSE;
        else if (p === 22) flags &= ~(BOLD | DIM);
        else if (p === 23) flags &= ~ITALIC;
        else if (p === 24) flags &= ~UNDERLINE;
        else if (p === 27) flags &= ~INVERSE;
        else if (p >= 30 && p <= 37) fg = p - 30 + 1;
        else if (p === 39) fg = 0;
        else if (p >= 40 && p <= 47) bg = p - 40 + 1;
        else if (p === 49) bg = 0;
        else if (p >= 90 && p <= 97) fg = p - 90 + 8 + 1;
        else if (p >= 100 && p <= 107) bg = p - 100 + 8 + 1;
        else if (p === 38 || p === 48) {
          let color = 0;
          if (i + 2 < ps.length && ps[i + 1] === 5) {
            color = Math.min(ps[i + 2], 255) + 1;
            i += 2;
          } else if (i + 4 < ps.length && ps[i + 1] === 2) {
            color = RGB + Math.min(ps[i + 2], 255) * 65536 + Math.min(ps[i + 3], 255) * 256
              + Math.min(ps[i + 4], 255);
            i += 4;
          } else {
            i = ps.length;
          }
          if (p === 38)
            fg = color;
          else
            bg = color;
        }
      }
      this.style = styleKey(fg, bg, flags);
    }
  }

  // What a terminal sends for special keys.
  const KEYS = {
    Enter: "\r", Backspace: "\x7f", Tab: "\t", Escape: "\x1b",
    ArrowUp: "\x1b[A", ArrowDown: "\x1b[B", ArrowRight: "\x1b[C", ArrowLeft: "\x1b[D",
    Home: "\x1b[H", End: "\x1b[F", Insert: "\x1b[2~", Delete: "\x1b[3~",
  };

  // Text to send for a key event, or null to leave the key to the browser.
  // Ctrl+letter sends a control code, except Ctrl+C with a selection (copy) and
  // Ctrl+V (paste). Ctrl+Shift keys stay with the browser.
  function keyData(e, hasSelection) {
    if (e.metaKey || e.isComposing)
      return null;
    const altGr = e.ctrlKey && e.altKey;
    if (e.ctrlKey && !altGr) {
      const k = e.key.length === 1 ? e.key.toLowerCase() : "";
      if (e.shiftKey || (k === "c" && hasSelection) || k === "v")
        return null;
      if (k >= "a" && k <= "z")
        return String.fromCharCode(k.charCodeAt(0) - 96);
      return { "[": "\x1b", "\\": "\x1c", "]": "\x1d" }[k] || null;
    }
    if (e.key === "Tab" && e.shiftKey)
      return "\x1b[Z";
    if (KEYS[e.key])
      return KEYS[e.key];
    return [...e.key].length === 1 ? e.key : null;
  }

  exports.Terminal = Terminal;
  exports.keyData = keyData;
  exports.parseStyle = parseStyle;
  exports.MAX_LINE = MAX_LINE;
  exports.RGB = RGB;
  exports.BOLD = BOLD;
  exports.DIM = DIM;
  exports.ITALIC = ITALIC;
  exports.UNDERLINE = UNDERLINE;
  exports.INVERSE = INVERSE;
})(typeof module === "object" ? module.exports : (window.BooBootTerminal = {}));
