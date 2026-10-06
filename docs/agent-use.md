# Usage runbook for agents

This file is written for an agent that works with a user, can run shell
commands on the user's computer, and is asked to use a BooBoot unit that is
already set up, often in the middle of a task: "Start using BooBoot for
testing this. Read this file and use the unit at booboot.local."

Expect two things:

- **The unit is shared.** Colleagues use the same DUT, watch its console, and
  may be in the middle of a test. Someone else looks after the unit. You use
  it as a guest: you check it, but you never change it.
- **The user's computer may have nothing.** The user may use BooBoot for the
  first time, on a computer with no BooBoot software. You install what is
  needed on it, in a tool folder, and explain the basics.

To set up a new unit, use [agent-setup.md](agent-setup.md) instead. If you
only have this file, the other guides are at
`https://raw.githubusercontent.com/sonatique/BooBoot/main/docs/`. An agent
without a shell uses the MCP server instead, set up by the user
([mcp.md](mcp.md)).

Read the whole file. Then do steps U1 to U6 in one go, stopping only where a
rule says to ask, and go on with the task.

## 1. Rules

1. **The task decides.** The user's message allows you to use the DUT for
   the current task: switch its power, copy the build to the SD card, and run
   commands on its console. Do only what the task needs.
2. **Never take the DUT from someone.** If another client has the session
   (`busy`, exit code 4), do not use `--force`. Tell the user who has it and
   for how long it has been idle, and wait for their answer.
3. **Ask before you disturb.** A colleague may use the DUT without holding
   the session: a test that runs on its own, or someone watching. Before the
   first power switch or card change of your work, if the DUT is on, tell
   the user what it shows (U5) and ask whether you may take it.
4. **Hold the session only while you use it.** Close it when the task is
   done, and before any pause of more than a few minutes (a long build,
   waiting for the user): colleagues get `busy` while you hold it.
5. **Do not change the unit.** No update, configuration change, service
   restart, reboot or log deletion. When something on the unit is wrong or
   old, tell the user, so that they tell whoever looks after it. Only if the
   user says that they look after the unit themselves, follow
   [install.md](install.md) for the change, with their consent for each step.
6. **Look before you overwrite.** The card may hold a colleague's files.
   Before the first change to the SD card in a session, list the directory
   you will write to and say which files you will replace. The card is
   readable only while the DUT is off: `bb power off`, then `bb sd ls 1:/`.
   Never write a whole image (`sd flash`, `deploy --image`) or delete card
   files unless the task needs it and the user agreed in this session.
7. **Change the user's computer only in `TOOLS`.** The client and BooBoot
   Console go to the tool folder (section 2), not into the user's project or
   system folders. Anything else on their computer (Python, the `PATH`, an
   MCP server) only with their consent.
8. **No secrets.** Never ask for, type or store passwords. SSH is optional:
   if SSH asks for a password, skip the SSH checks and say so.

## 2. Values

Take them from the user's message. Ask for missing ones only if the defaults
do not work.

| Name | Default | Meaning |
|---|---|---|
| `HOST` | `booboot.local` | BooBoot board hostname or IP address, from the user's message (a name, a URL, or an SSH address `USER@HOST`) |
| `PORT` | `8080` | server port: 8081 for a second DUT on the same board, and so on |
| `URL` | `http://HOST:PORT` | server address |
| `NAME` | from `status` | DUT name |
| `USER` | none | SSH user on the BooBoot board, only when the user gave an SSH address; most users of a shared unit have none, and it is not needed |
| `TOOLS` | `%LOCALAPPDATA%\BooBoot` on Windows, `~/.local/share/booboot` elsewhere | tool folder on the user's computer |
| `ME` | the user's login name | for the session name |
| `COMPUTER` | the computer name | for the session name |

With `USER`, read-only checks on the BooBoot board run through SSH, without
password:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new USER@HOST '<command>'
```

Below, `pi '<command>'` means that, and `bb` means:

```sh
python3 TOOLS/booboot.py --url URL --name "agent of ME on COMPUTER"
```

Viewers and `busy` errors show this name to colleagues, so that they know
whom to ask. On Windows, use `python` or `py` instead of `python3`.

## 3. The steps

| Id | Title | Where | Done when |
|---|---|---|---|
| U1 | Reach the unit | unit | the status answers |
| U2 | Check the unit | unit, read only | state and version known |
| U3 | Client | user's computer | `bb status` answers |
| U4 | Viewer | user's computer | BooBoot Console or the web page is open |
| U5 | Look at the DUT | unit | its state and use are known |
| U6 | Start report | | sent to the user |

### U1: Reach the unit

```sh
curl -sS -m 10 URL/api/v1/status
```

(In PowerShell: `curl.exe`.) JSON with `name` and `power`: go on.

If it fails, tell the user that nothing answers at `URL`, and ask for the
right address: whoever looks after the unit knows it. With `USER`, also look
on the board, read only:

- `pi 'systemctl --no-pager list-units "booboot@*"; ls /etc/booboot'`
- SSH fails: report the error, and go on without SSH.
- No `booboot@` service and no `/etc/booboot`: BooBoot is not installed
  there. Stop, and tell the user ([agent-setup.md](agent-setup.md) sets up a
  unit).
- The service failed: `pi 'journalctl -u booboot@NAME -n 30 --no-pager'`.
  Report the lines that matter (rule 5).

**Several DUTs.** One board can serve several DUTs, one port each, from
8080 up. If the user names a DUT, try the ports in turn until the `name` in
the status is that DUT; stop at the first port that does not answer.

### U2: Check the unit

In the status of U1:

| Field | Good | Otherwise |
|---|---|---|
| `power.error` | empty | relay line problem |
| `sd.error` | empty | mux not found |
| `console.connected` | `true` | UART adapter not found |
| `operation` | `null` | a long operation (image write) is running: someone uses the DUT |
| `session.active` | `false` | someone uses the DUT: rule 2 |
| `version` | the latest release (its tag, without the `v`: U4, step 2) | an older version: tell the user (rule 5) |

Report the errors (with the common problems of section 6), and do not fix
them (rule 5). An error that does not matter for the task (for example no
mux, when the task only uses the console) does not stop you.

### U3: Client

On the user's computer:

1. Python 3.9 or later: `python3 --version` (Windows: `python --version` or
   `py --version`). Without it, tell the user and offer to install it
   (install.md B2).
2. Create `TOOLS` if needed. When `TOOLS/booboot.py` exists and
   `python3 TOOLS/booboot.py --version` gives the unit `version` (from the
   status), keep it. Otherwise get the client of that version, from its
   release:

   ```sh
   curl -sSfL -o TOOLS/booboot.py https://github.com/sonatique/BooBoot/releases/download/vVERSION/booboot.py
   ```

   If there is no such release (404), take the latest release
   (`.../releases/latest/download/booboot.py`), then the main branch
   (`https://raw.githubusercontent.com/sonatique/BooBoot/main/client/booboot.py`).
3. Check: `bb status`.

**Command line or MCP.** Use the command line tool through your shell: it
works at once, in the middle of a session, and `--json` gives results to
parse. An MCP server works only after the agent host loads it, usually in a
new session: offer it for later (section 5).

### U4: Viewer for the user

The user watches the DUT console live while you work. Watching needs no
session and does not disturb you. Unless the user already runs the latest
BooBoot Console for `URL`, start it or offer it: do not end this step with
only "check your viewer".

1. **What runs.** List the BooBoot Console programs running, with their file
   and command line:
   - Windows: `powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object Name -like 'BooBootConsole*' | Format-List ExecutablePath, CommandLine"`
   - Linux and macOS: `for p in $(pgrep BooBootConsole); do ps -o command= -p "$p"; done`

   The command line ends with the server address when one was given.
   Without one, the program shows the last address it used.
2. **Latest release.** Its tag is the end of the address that this command
   prints (like `.../tag/v0.1.0`). An address ending with `/releases` means
   that there is no release yet.

   ```sh
   curl -sSfLI -o /dev/null -w '%{url_effective}' https://github.com/sonatique/BooBoot/releases/latest
   ```

   The version of the copy in `TOOLS` is in `TOOLS/BooBootConsole/version.txt`.
   The version of other copies is not known.
3. **Decide.**

   | What runs | What to do |
   |---|---|
   | Nothing | steps 4 and 5, without asking |
   | The `TOOLS` copy, at the latest release, with `URL` in its command line | keep it, go to step 6 |
   | The `TOOLS` copy, at the latest release, without `URL` in its command line | ask the user whether it shows `URL`; if not, step 5 |
   | Another copy, or an older version | tell the user what runs and offer the latest release, started for `URL`. If they agree: when the old one is the `TOOLS` copy, ask them to close it first (a running program cannot be replaced), then steps 4 and 5 |

4. **Get BooBoot Console**, the first way that works:
   1. Windows x64 or Linux x64, when there is a release (step 2): the latest
      release, no login needed. When `TOOLS/BooBootConsole/version.txt` holds
      its tag, keep the program there. Otherwise download it, and write the
      tag in `version.txt`:

      ```sh
      curl -sSfL --create-dirs -o TOOLS/BooBootConsole/BooBootConsole.exe https://github.com/sonatique/BooBoot/releases/latest/download/BooBootConsole-win-x64.exe
      ```

      On Linux, download `BooBootConsole-linux-x64` as
      `TOOLS/BooBootConsole/BooBootConsole`, then `chmod +x` it.
   2. Windows x64 or Linux x64, when there is no release yet, with the GitHub
      CLI logged in (`gh auth status`): the build of the latest successful CI
      run (kept 30 days). On Linux, use `BooBootConsole-linux-x64`, then
      `chmod +x TOOLS/BooBootConsole/BooBootConsole`.

      ```sh
      RUN=$(gh run list -R sonatique/BooBoot -w CI -b main -s success -L 1 --json databaseId -q '.[0].databaseId')
      gh run download "$RUN" -R sonatique/BooBoot -n BooBootConsole-win-x64 -D TOOLS/BooBootConsole
      ```
   3. With the .NET 10 SDK (`dotnet --list-sdks` lists a 10 version), on any
      system: build it from the sources (RID: `win-x64`, `linux-x64`,
      `linux-arm64`, `osx-arm64`, `osx-x64`).

      ```sh
      git clone --depth 1 https://github.com/sonatique/BooBoot.git TOOLS/src    # if there: git -C TOOLS/src pull
      AVALONIA_TELEMETRY_OPTOUT=1 dotnet publish TOOLS/src/client/csharp/BooBootConsole -c Release -r RID \
          --self-contained -p:PublishSingleFile=true -o TOOLS/BooBootConsole
      ```

   4. Otherwise: only the web page (step 6). Tell the user how to get the
      program themselves, if they want it (install.md B3).
5. **Start it**, detached from your shell, with the server address:
   - Windows: `powershell -NoProfile -Command "Start-Process -FilePath 'TOOLS\BooBootConsole\BooBootConsole.exe' -ArgumentList 'URL'"`
   - Linux and macOS: `nohup TOOLS/BooBootConsole/BooBootConsole URL >/dev/null 2>&1 &`.
     On Linux it needs a desktop session (`DISPLAY` or `WAYLAND_DISPLAY`
     set). Without one, use the web page.

   After 5 seconds, check that it runs (step 1).
6. **Web page.** `http://HOST:PORT/` shows the same in any browser, phones
   too. If BooBoot Console could not start, open the page for the user:
   Windows `powershell -NoProfile -Command "Start-Process 'URL/'"`, macOS
   `open URL/`, Linux `xdg-open URL/`.

### U5: Look at the DUT

```sh
bb status
bb console read --since boot --clean
```

Neither needs the session. Read the end of the output: the DUT may be off,
in U-Boot, at a login prompt, or in a shell. To see whether something runs
on it now, compare `console.cursor` in two `bb --json status` taken 10 s
apart: a change means new output.

### U6: Start report

Send the user a short report:

```
BooBoot ready: URL (DUT NAME, BooBoot VERSION)
Unit: power <on|off>, SD card <mode>, console <connected|error>, session <free|used by X>
Unit notes: <none | errors, older version: tell whoever looks after the unit>
Client: TOOLS/booboot.py, version <V>, used from my shell
Viewer: BooBoot Console <version> <started | already running | user kept their copy | not available: why>; web page http://HOST:PORT/
DUT now: <one line; and "output is coming now" if U5 saw it>
```

Then tell them, once, in a few lines:

- BooBoot Console (or the web page) shows the DUT serial console live, with
  the output from before. Watching is free: colleagues may watch too, and it
  disturbs nobody.
- One client at a time uses the DUT, through the session. While you test,
  you hold it as "agent of ME on COMPUTER", and others get `busy`. You
  release it when you are done or pause.
- "Take control" in BooBoot Console or on the web page opens the session to
  type into the console: it takes the DUT from you, or from a colleague. They
  should tell you first, and click "Release control" after.
- If the DUT is on (rule 3), ask whether you may take it.

Then go on with the task.

## 4. Working with the DUT

`bb COMMAND --help` gives all the options of your version.

| Goal | Command |
|---|---|
| Copy boot files to the card, power on, wait for the boot | `bb deploy BOOT.BIN image.ub --expect "login: " --timeout 120` |
| Write a whole image first (rule 6) | `bb deploy --image disk.img.xz --expect "login: " --timeout 600` |
| Log in, then run a command | `bb console run root`, then `bb console run "uname -a" --timeout 30` |
| Wait for a text | `bb console expect "REGEX" --timeout 60` |
| Send text without waiting | `bb console write "text"` |
| Read the output | `bb console read --since boot --clean` (`-t`: time since power on on each line) |
| Power | `bb power on`, `bb power off`, `bb power cycle` |
| Boot time | `bb boottime "login: " --runs 5` |
| Card files | `bb sd ls 1:/`, `bb sd get 1:/FILE LOCAL`, `bb sd put FILES 1:/` |
| State | `bb status` |

- **Results.** `--json` before the command gives JSON. Exit codes: 0 ok,
  1 error, 3 timeout (the text was not seen), 4 busy.
- **Session.** The first command that needs it opens the session, and the
  client keeps the token in a local file for the next commands. After no
  command for the session timeout (300 s by default), the session ends; the
  next command opens a new one and warns that another client may have used
  the DUT meanwhile. `bb session close` releases it.
- **Prompt.** `console run` waits for the prompt regex of the server
  configuration (`[#$>] $` by default), or `--prompt REGEX`.
- **Timeouts.** On exit code 3, read the output
  (`bb console read --since boot --clean`) before trying again: the cause is
  usually there. Writing a whole image takes minutes: give your shell
  command a long enough time limit.
- **The user sees it too.** The boot and your console commands show in their
  viewer as they happen: you can refer to them.
- Do not use `bb console attach`: it is an interactive terminal for humans.

## 5. End

1. `bb session close`.
2. Say whether the DUT is on or off. Switch it off only if the user asks.
3. Leave BooBoot Console running: it is the user's now.
4. Once, offer for later, on the user's computer:
   - the `booboot` command, with the address set once (install.md B2:
     `TOOLS/booboot.py` and `BOOBOOT_URL=URL`),
   - the MCP server, for agent hosts that support it: register
     `python3 TOOLS/booboot.py --url URL mcp` as a stdio server named
     `booboot` ([mcp.md](mcp.md)). Its tools are marked read-only or
     destructive, so that the host can ask before a power switch or a card
     write.

## 6. Common problems

| Seen | Likely cause | What to do |
|---|---|---|
| `cannot reach URL` | wrong address or port, unit off, other network | U1 |
| `busy`, exit code 4 | another client has the session | rule 2 |
| `session expired, opening a new one` | no command for the session timeout | another client may have used the DUT meanwhile: check its state (U5) before going on |
| DUT on, session free, output coming | a colleague's test runs without the session | rule 3 |
| `console run` times out, output shown | the prompt regex does not match the DUT prompt | `--prompt REGEX` |
| `deploy` times out with no output | the DUT does not boot, or the console speed is wrong | `bb console read --since boot`, `bb status`; bringup.md step 7 |
| `power_on` error on an `sd` command | the card can go to the BooBoot board only while the DUT is off | `bb power off` first (`deploy` does it) |
| The release download fails with 404 | no release yet, or none for that version | the next way of the step |
| BooBoot Console does not start on Linux | no desktop session, or missing libraries | the web page |
| `status` errors on power, mux or console | hardware or configuration of the unit | report (rule 5); [bringup.md](bringup.md) helps whoever looks after the unit |
