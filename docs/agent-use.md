# Usage runbook for agents

This file is written for an agent that works with a user, can run shell
commands on the user's computer, and is asked to use a BooBoot unit that is
already set up, often in the middle of a task: "Start using BooBoot for
testing this. Read this file and use the unit at pi@booboot.local." The agent
checks the unit, gets the client, opens a console viewer for the user, and
then uses the DUT for the task.

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
2. **Look before you overwrite.** Before the first change to the SD card in
   a session, list the directory you will write to and say which files you
   will replace. The card is readable only while the DUT is off:
   `bb power off`, then `bb sd ls 1:/`. Never write a whole image (`sd flash`,
   `deploy --image`) or delete card files unless the task needs it and the
   user agreed in this session.
3. **Never take the DUT from someone.** If another client has the session
   (`busy`, exit code 4), do not use `--force`. Tell the user who has it and
   for how long it has been idle, and wait for their answer.
4. **Do not change the unit.** No update, configuration change, service
   restart or reboot of the BooBoot board without the user's consent: other
   people may use it. Checks that only read are fine.
5. **Keep the user's project clean.** The client and BooBoot Console go to
   the tool folder (`TOOLS`, section 2), not into the user's project.
6. **No secrets.** Never ask for, type or store passwords. If SSH asks for a
   password, skip the SSH checks and say so.
7. **Release the DUT.** Close the session when the task is done, or before a
   long pause: other clients wait while you hold it.

## 2. Values

Take them from the user's message. Ask for missing ones only if the defaults
do not work.

| Name | Default | Meaning |
|---|---|---|
| `HOST` | `booboot.local` | BooBoot board hostname or IP address, from the SSH address `USER@HOST` or from the URL |
| `USER` | none | user name on the BooBoot board; without it, skip the SSH checks |
| `PORT` | `8080` | server port: 8081 for a second DUT on the same board, and so on |
| `NAME` | `dut1` | DUT name, as given by `status` |
| `URL` | `http://HOST:PORT` | server address |
| `TOOLS` | `%LOCALAPPDATA%\BooBoot` on Windows, `~/.local/share/booboot` elsewhere | tool folder on the user's computer |

Commands on the BooBoot board run through SSH, without password:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new USER@HOST '<command>'
```

Below, `pi '<command>'` means that, and `bb` means:

```sh
python3 TOOLS/booboot.py --url URL --name "agent on COMPUTER"
```

`COMPUTER` is the name of the user's computer: viewers show it as the client
that has the session. On Windows, use `python` or `py` instead of `python3`.

## 3. The steps

| Id | Title | Done when |
|---|---|---|
| U1 | Reach the unit | the status answers |
| U2 | Check the unit | no error in the status; version checked if SSH works |
| U3 | Client | `bb status` answers |
| U4 | Viewer for the user | BooBoot Console or the web page is open |
| U5 | Look at the DUT | its state is known |
| U6 | Start report | sent to the user |

### U1: Reach the unit

```sh
curl -sS -m 10 URL/api/v1/status
```

(In PowerShell: `curl.exe`.) JSON with `name` and `power`: go on.

If it fails:

- Without SSH: tell the user that nothing answers at `URL`, and ask for the
  right address.
- With SSH: `pi 'systemctl --no-pager list-units "booboot@*"; ls /etc/booboot'`
  - SSH fails: report the error. The address or the SSH key is wrong
    (agent-setup.md step A3 sets up the key).
  - No `booboot@` service and no `/etc/booboot`: BooBoot is not installed.
    Stop, and offer to set it up with [agent-setup.md](agent-setup.md).
  - The service failed: `pi 'journalctl -u booboot@NAME -n 30 --no-pager'`.
    Report the lines that matter, and ask before restarting it.

**Several DUTs.** If `/etc/booboot` holds more than one `.ini` file and the
user gave no port, ask which DUT. `pi 'grep -H "^port" /etc/booboot/*.ini'`
gives each port.

### U2: Check the unit

In the status of U1:

| Field | Good | Otherwise |
|---|---|---|
| `power.error` | empty | relay line problem |
| `sd.error` | empty | mux not found |
| `console.connected` | `true` | UART adapter not found |
| `operation` | `null` | a long operation (image write) is running |
| `session.active` | `false` | someone uses the DUT: rule 3 |

With SSH, also compare the installed version with the latest one, on the
BooBoot board:

```sh
pi 'systemctl is-active booboot@NAME; sha256sum /usr/local/bin/booboot; curl -sSfL https://raw.githubusercontent.com/sonatique/BooBoot/main/client/booboot.py | sha256sum'
```

Different sums mean that the unit runs another version than the latest (no
second sum: the board has no internet access, the version is not checked).
Tell the user, and offer the update. Run it only with their consent and
while the session is free, because it restarts the service:
`pi 'cd ~/BooBoot && git pull && sudo server/install.sh NAME'`.

For an error, use the common problems of [agent-setup.md](agent-setup.md)
(section 12) and of section 6 below, report, and ask before fixing. An error
that does not matter for the task (for example no mux, when the task only
uses the console) does not stop you: report it and go on.

### U3: Client

1. Python 3.9 or later: `python3 --version` (Windows: `python --version` or
   `py --version`). Without it, tell the user and offer to install it
   (install.md B2).
2. Create `TOOLS` if needed, and get `booboot.py` into it:
   - with SSH, from the unit, so that it matches the server version:
     `scp USER@HOST:/usr/local/bin/booboot TOOLS/booboot.py`
   - otherwise from GitHub:
     `curl -sSfL -o TOOLS/booboot.py https://raw.githubusercontent.com/sonatique/BooBoot/main/client/booboot.py`
3. Check: `bb status`.

**Command line or MCP.** Use the command line tool through your shell: it
works at once, in the middle of a session, and `--json` gives results to
parse. An MCP server works only after the agent host loads it, usually in a
new session: offer it for later (section 5).

### U4: Viewer for the user

The user watches the DUT console live while you work. Watching needs no
session and does not disturb you.

1. **Already running?** Windows:
   `powershell -NoProfile -Command "Get-Process BooBootConsole -ErrorAction SilentlyContinue"`.
   Linux and macOS: `pgrep -fl BooBootConsole`. If it runs, keep it, ask
   the user to check that it shows `URL`, and go to step 4.
2. **Get BooBoot Console**, the first way that works:
   1. Windows x64 or Linux x64, with the GitHub CLI logged in
      (`gh auth status`): the build of the latest successful CI run.

      ```sh
      RUN=$(gh run list -R sonatique/BooBoot -w CI -b main -s success -L 1 --json databaseId -q '.[0].databaseId')
      gh run download "$RUN" -R sonatique/BooBoot -n BooBootConsole-win-x64 -D TOOLS/BooBootConsole
      ```

      Use `BooBootConsole-linux-x64` on Linux, then
      `chmod +x TOOLS/BooBootConsole/BooBootConsole`. Write the run number in
      `TOOLS/BooBootConsole/run.txt`; when it is already the latest run,
      keep the program there and skip the download. CI keeps the builds for
      30 days: when the download finds none, go to the next way.
   2. With the .NET 10 SDK (`dotnet --list-sdks` lists a 10 version), on any
      system: build it from the sources (RID: `win-x64`, `linux-x64`,
      `linux-arm64`, `osx-arm64`, `osx-x64`).

      ```sh
      git clone --depth 1 https://github.com/sonatique/BooBoot.git TOOLS/src    # if there: git -C TOOLS/src pull
      AVALONIA_TELEMETRY_OPTOUT=1 dotnet publish TOOLS/src/client/csharp/BooBootConsole -c Release -r RID \
          --self-contained -p:PublishSingleFile=true -o TOOLS/BooBootConsole
      ```

   3. Otherwise: only the web page (step 4). Tell the user how to get the
      program themselves, if they want it (install.md B3).
3. **Start it**, detached from your shell, with the server address:
   - Windows: `powershell -NoProfile -Command "Start-Process -FilePath 'TOOLS\BooBootConsole\BooBootConsole.exe' -ArgumentList 'URL'"`
   - Linux and macOS: `nohup TOOLS/BooBootConsole/BooBootConsole URL >/dev/null 2>&1 &`.
     On Linux it needs a desktop session (`DISPLAY` or `WAYLAND_DISPLAY`
     set). Without one, use the web page.

   After 5 seconds, check that it runs (step 1).
4. **Web page.** `http://HOST:PORT/` shows the same in any browser, phones
   too. If BooBoot Console could not start, open the page for the user:
   Windows `powershell -NoProfile -Command "Start-Process 'URL/'"`, macOS
   `open URL/`, Linux `xdg-open URL/`.

### U5: Look at the DUT

```sh
bb status
bb console read --since boot --clean
```

Neither needs the session. Read the end of the output: the DUT may be off,
in U-Boot, at a login prompt, or in a shell.

### U6: Start report

Send the user a short report:

```
BooBoot ready: URL (DUT NAME)
Unit: power <on|off>, SD card <mode>, console <connected|error>, session <free|used by X>
Version: <latest | differs from the latest | not checked, no SSH>
Client: TOOLS/booboot.py (<from the unit | from GitHub>), used from my shell
Viewer: BooBoot Console <started | already running | not available: why>; web page http://HOST:PORT/
DUT now: <one line>
```

Then tell them, once:

- Watching in BooBoot Console or on the web page is free and does not
  disturb you.
- "Take control" in either takes the DUT from you: they should tell you
  before, and click "Release control" after.
- You hold the session while you test, so other clients get "busy"
  meanwhile. You release it when done.

Then go on with the task.

## 4. Working with the DUT

`bb COMMAND --help` gives all the options of your version.

| Goal | Command |
|---|---|
| Copy boot files to the card, power on, wait for the boot | `bb deploy BOOT.BIN image.ub --expect "login: " --timeout 120` |
| Write a whole image first (rule 2) | `bb deploy --image disk.img.xz --expect "login: " --timeout 600` |
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
4. Once, offer for later sessions the MCP server, for agent hosts that
   support it: register `python3 TOOLS/booboot.py --url URL mcp` as a stdio
   server named `booboot` ([mcp.md](mcp.md)). Its tools are marked read-only
   or destructive, so that the host can ask before a power switch or a card
   write.

## 6. Common problems

| Seen | Likely cause | What to do |
|---|---|---|
| `cannot reach URL` | wrong address or port, unit off, other network | U1 |
| `busy`, exit code 4 | another client has the session | rule 3 |
| `session expired, opening a new one` | no command for the session timeout | normal; check the DUT state before going on |
| `console run` times out, output shown | the prompt regex does not match the DUT prompt | `--prompt REGEX` |
| `deploy` times out with no output | the DUT does not boot, or the console speed is wrong | `bb console read --since boot`, `bb status`; bringup.md step 7 |
| `power_on` error on an `sd` command | the card can go to the BooBoot board only while the DUT is off | `bb power off` first (`deploy` does it) |
| `gh run download` finds no artifact | CI keeps builds for 30 days | U4, step 2, next way |
| BooBoot Console does not start on Linux | no desktop session, or missing libraries | the web page |
