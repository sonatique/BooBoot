# BooBoot Console

A desktop program that shows the serial console of the DUT, live, as a
terminal connected to it would. Watching needs no session, so agents and
other clients use the DUT as usual, and the console data is not changed in
any way. To type into the console, take control: see [Typing](#typing). With
several DUTs on the board, it shows a tab for each: see
[Several DUTs](#several-duts).

![BooBoot Console](console.png)

To watch with nothing to install, the BooBoot server also has a
[console web page](web.md).

## Features

- **Live output.** The server sends each read from the serial port at once.
- **Output from before.** The server keeps the last 8 MB of output
  (`buffer_size` in the server configuration). The window shows it at start.
- **Terminal text.** Colors, carriage returns, backspaces and line erase work
  as in a terminal.
- **Power switches.** A line shows each power on and power off, with its time.
- **Scrollback.** 200,000 lines by default. Long lines wrap.
- **Follow.** The view follows new output. Scrolling up stops it, so that the
  text stays in place. End or the Follow button starts it again.
- **Copy and save.** Select with the mouse (double-click: word, triple-click:
  line), copy with Ctrl+C, select all with Ctrl+A. Save writes all the text
  to a file (Ctrl+S). Clear (Ctrl+L) empties the view.
- **Logs.** In the Log menu, "Start new log" writes all output from now on to
  a new file `PREFIX-YYYYMMDD-HHMMSS.log` in the log folder; "Stop log" stops
  it, and "Open log folder" opens the folder. The prefix is the DUT name
  unless set; with several DUTs, a prefix set is followed by the DUT name.
  Options: start a log when connected, and a new log file at each power on.
  Logs have the text without escape codes, one line per line.
- **Connection.** When the connection is lost, the program connects again and
  continues where it was: no output is lost unless it left the server memory
  in between (a line then says how many bytes were lost). A line also says
  when the server was restarted.

The status bar shows the connection, the DUT name, the power state, and which
client has the session. When the server address is a name, it also shows the
address of the board (`also at 10.1.2.3:8080`), for where the name does not
work, like over a VPN. The program keeps the addresses of the board, and uses
them by itself when the name is not found: see [remote.md](remote.md).

## Several DUTs

With the address of a board that has several DUTs, like
`http://booboot.local:8080`, the window shows a tab for each DUT. The buttons
and the status bar are those of the DUT of the current tab. Each DUT has its
own view, scrollback, log files and session. A tab shows:

- a dot: green when the DUT is on, gray when it is off,
- "in control" when this program has the session of the DUT, or "used by"
  and the client that has it,
- a bullet when new output came while another tab was shown.

Ctrl+Tab shows the next DUT, Ctrl+Shift+Tab the previous one. The DUTs button,
next to the tabs, shows or hides DUTs: a hidden DUT is not watched, and the
program remembers it for that board. With the address of one DUT, like
`http://booboot.local:8080/duts/dut2`, the window shows that DUT only. With
one DUT on the board, there are no tabs.

## DUT panel

The DUT button, next to "Power", shows the DUT on the board, the same for
everyone (Settings are those of this program, on this computer):

- its name and its address, like `http://booboot.local:8080/duts/dut1`, to
  copy for `booboot --dut dut1`, an agent or a colleague,
- its label: free text shown with its name in the tab, the title and the
  status bar, here and in the other viewers, like `ZCU102 rev B, bench 3`.
  In control, "Set label" (or Enter) changes it for everyone; empty removes
  it,
- its serial adapter and baud rate, its relay and power state, the side of
  its SD card, and the version and addresses of the BooBoot server.

With several DUTs, it shows the DUT of the current tab. A right-click on a
tab, then "DUT...", opens it for that DUT.

## Typing

"Take control" opens the BooBoot session, in the name of
`BooBoot Console USER@COMPUTER`. While it is open, other clients that need
the session (agents, `booboot` commands) get `busy`, as with any session.
When another client has the session, the program asks before taking it
over, and says whether that client is gone (a viewer or MCP server that
stopped its heartbeats, like a program closed abruptly: taking over is
safe) or connected (someone may be using the DUT). A session left by this
program on the same computer, after a crash, is taken over without asking.
A blue frame shows that the keys go to the DUT. The view then shows the
cursor, and follows the output at each key.

- **Keys.** Text, Enter, Backspace, Tab, Escape, the arrows, Home, End,
  Insert and Delete are sent as a terminal sends them. Ctrl+letter sends the
  control character: Ctrl+C stops a program on the DUT, Ctrl+D ends a shell.
- **Copy and paste.** Ctrl+C copies when text is selected; with no selection
  it goes to the DUT. Ctrl+Shift+C always copies. Ctrl+V pastes the clipboard
  to the DUT, with each line break sent as Enter.
- **View commands.** Page Up and Page Down still scroll. With Ctrl+Shift:
  Ctrl+Shift+A selects all, Ctrl+Shift+L clears, Ctrl+Shift+S saves.
- **Power.** Next to "Take control", "Power on" or "Power off", after the
  power state of the DUT, switches it. It works only in control. "Power off"
  asks first. Power on also gives the SD card back to the DUT.
- **Label.** The DUT panel sets the label of the DUT, in control: see
  [DUT panel](#dut-panel).
- **Release.** "Release control" closes the session. Closing the window also
  does. When no key is sent for the session timeout (300 s by default), the
  session ends and the status bar says so.
- **Busy.** If another client has the session, the program shows who and its
  idle time, and asks before taking it over. If another client takes it
  over, typing stops and the status bar says who.

## Run

**Ready-built program.** Each release has a single program file for Windows
and Linux, with nothing else to install. From the
[latest release](https://github.com/sonatique/BooBoot/releases/latest), download `BooBootConsole-win-x64.exe` or
`BooBootConsole-linux-x64`. On Linux, make the file executable first:
`chmod +x BooBootConsole-linux-x64`. CI also builds them at each push: the
artifacts of the latest CI run have the changes made since the release (a
GitHub login is needed, and they are kept 30 days).

**From the sources**, on any system with the .NET 10 SDK:

```sh
dotnet run --project client/csharp/BooBootConsole -- http://booboot.local:8080
```

To build a single program file yourself (RID: `win-x64`, `linux-x64`,
`linux-arm64`, `osx-arm64`, ...):

```sh
dotnet publish client/csharp/BooBootConsole -c Release -r win-x64 --self-contained \
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true \
    -p:EnableCompressionInSingleFile=true -p:PublishReadyToRun=true -p:PublishTrimmed=true \
    -o publish
```

`PublishTrimmed` leaves out the parts of .NET that the program does not use,
and `PublishReadyToRun` compiles it ahead of time. Together they make a file
of about 35 MB that shows its window in well under a second.

The build uses Avalonia, which sends anonymous usage data while building. Set
`AVALONIA_TELEMETRY_OPTOUT=1` to turn this off.

The server address comes from the command line, or else is the last one
used. It is the address of the board, for all its DUTs, or of one DUT, like
`http://booboot.local:8080/duts/dut2`.

## Settings

The Settings button opens them. They are kept in `settings.json`, in
`%APPDATA%\BooBootConsole` on Windows and `~/.config/BooBootConsole` on Linux
and macOS.

| Setting | Default |
|---|---|
| Log folder | `BooBoot logs` in the documents folder |
| File name prefix | the DUT name; with a prefix and several DUTs, PREFIX-NAME |
| Start a log when connected | off |
| New log file at each power on | off |
| Scrollback lines | 200,000 |
| Text size | 13 |
| Fonts | Cascadia Mono, Consolas, Menlo, DejaVu Sans Mono, Liberation Mono, monospace (the first one found) |

## How it works

The program finds the DUTs of the board in `GET /api/v1/status` (`duts`),
then reads `GET /duts/NAME/api/v1/console/stream` for each (see
[api.md](api.md)): one HTTP connection on which the server sends the output
and the power switches as they come. The server reads the serial port all the time, whether viewers
are connected or not. Viewers only read its memory, so the DUT does not see
them, and any number of them can watch. A slow viewer never slows the server:
if it falls behind by more than the server memory, it skips output.
Taking control opens a session (`POST /duts/NAME/api/v1/session`), and the
keys go through `POST /duts/NAME/api/v1/console/write`.

## Limits

- The view is line based: it only writes to the last line, like a log.
  Programs that draw on the whole screen (`top`, `vi`, `menuconfig`) do not
  show well. For them, use `booboot console attach` in a terminal.
- Characters wider than others (CJK, emoji) break the alignment of their line.
