// Checks of the web page terminal model (server/booboot_server/web/terminal.js).
// Run by test_web.py when node is found: node tests/terminal_check.js

"use strict";
const path = require("path");
const assert = require("assert");
const { Terminal, parseStyle, MAX_LINE, RGB, BOLD } = require(
  path.join(__dirname, "..", "server", "booboot_server", "web", "terminal.js"));

function check(ok, what) {
  assert.ok(ok, "check failed: " + what);
}

const t = new Terminal();
const done = [];
t.onLine = line => done.push(line.text);
t.feed("one\r\ntwo\nthr");
check(done.join("|") === "one|two" && t.pending === "thr" && t.lines.length === 3, "lines");
t.feed("ee\r\n12345\rab");
check(t.pending === "ab345", "carriage return overwrites");
t.feed("\r\nabc\b\bX");
check(t.pending === "aXc", "backspace");
t.feed("\r\nabcdef\x1b[3D\x1b[K");
check(t.pending === "abc", "erase to line end");
t.feed("\r\x1b[2K");
check(t.pending === "", "erase line");
t.feed("abcdef\x1b[4G\x1b[2P");
check(t.pending === "abcf", "delete characters");
t.feed("\x1b[2D\x1b[2@");
check(t.pending === "a  bcf", "insert characters");
t.feed("\r\na\tb");
check(t.pending === "a       b", "tab");
t.feed("\r\n\x1b]0;window title\x07after\x1b(B");
check(t.pending === "after", "title sequence dropped");

t.feed("\r\n\x1b[1;31mred\x1b[0m plain \x1b[38;5;196mX\x1b[48;2;1;2;3mY\x1b[3");
t.feed("2mG");
const runs = t.line(t.last).runs;
check(JSON.stringify(parseStyle(runs[0].style)) === JSON.stringify({ fg: 2, bg: 0, flags: BOLD })
  && runs[0].start === 0, "bold red");
check(runs[1].start === 3 && runs[1].style === "", "reset");
check(runs[2].start === 10 && parseStyle(runs[2].style).fg === 197, "256 colors");
check(parseStyle(runs[3].style).bg === RGB + 0x010203, "true color");
check(parseStyle(runs[4].style).fg === 3 && t.pending.endsWith("XYG"), "sequence split between reads");

t.feed("\x1b[0m\r\npartial");
t.addMarker("---- power on ----");
const n = t.last;
check(t.line(n - 2).text === "partial" && t.line(n - 1).marker && t.line(n).text === ""
  && done[done.length - 1] === "---- power on ----", "marker after the output");

t.feed("x".repeat(MAX_LINE + 10));
check(t.pending.length === 10, "very long lines are cut");

const small = new Terminal(100);
for (let i = 0; i < 1000; i++)
  small.feed("line " + i + "\n");
check(small.lines.length <= 107 && small.first > 800 && small.line(small.last - 1).text === "line 999",
  "scrollback limit");
check(small.text().endsWith("line 998\nline 999\n"), "all text");
small.clear();
check(small.lines.length === 1 && small.pending === "" && small.first > 1000, "clear");

console.log("all terminal checks passed");
