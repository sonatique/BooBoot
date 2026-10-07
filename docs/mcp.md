# MCP server

`booboot mcp` runs a Model Context Protocol (MCP) server on standard input
and output. MCP clients then use the DUT through tools: power, SD card and
serial console. It is part of `client/booboot.py` (Python 3.9 or later,
nothing else to install) and runs on the computer of the MCP client, so that
the tools can copy local files to the SD card.

## Setup

Most MCP clients take a configuration like this one:

```json
{
  "mcpServers": {
    "booboot": {
      "command": "python3",
      "args": ["/path/to/booboot.py", "--url", "http://booboot.local:8080", "mcp"]
    }
  }
}
```

On Windows, the command is usually `python` or `py`. Options go before `mcp`:

- `--url`: the BooBoot server, or one DUT of the board at
  `http://booboot.local:8080/duts/NAME`.
- `--dut`: the DUT of the board, when it has several: the same as
  `--url URL/duts/NAME`.
- `--name`: the client name shown to other clients (default `mcp user@host`).
- `--session-timeout`: idle seconds before the server ends the session
  (default 900).

## Tools

| Tool | What it does | Changes the DUT |
|---|---|---|
| `status` | Power, SD card, console and session state | no |
| `power` | Power on, off, or cycle | yes |
| `deploy` | Power off, write an image and/or copy files, power on, wait for a regex; returns the boot output and boot time | yes |
| `sd_mode` | Connect the card to the host, the DUT, or nothing | yes |
| `sd_flash` | Write a local disk image to the card | yes |
| `sd_list` | List a card directory | card goes to the host |
| `sd_get` | Copy a card file to this computer | card goes to the host |
| `sd_put` | Copy local files to the card | yes |
| `sd_delete` | Delete a card file or directory | yes |
| `console_read` | Console output after a cursor, with times since power on if asked | no |
| `console_write` | Send text, Enter by default | yes |
| `console_expect` | Wait for a regex in the console output | no |
| `console_run` | Send a command line and return its output | yes |
| `boot_time` | Power cycle and measure the time to a regex, once or several times | yes |
| `script_run` | Run a Python script on the BooBoot board ([scripts.md](scripts.md)); it goes on if the connection drops | yes |
| `script_output` | Output of a script after a cursor, and its state | no |
| `script_stop` | Stop the running script | yes |
| `script_list` | The scripts kept on the board | no |
| `session` | Open (or take) and release the session | no |

Tools that only read are marked read-only, and the others destructive (MCP
tool annotations), so that a client can ask before it switches the power or
writes the card.

## Behavior

- **Session**: the server opens the BooBoot session at the first tool call
  that needs it and releases it when it stops. If the session expired in
  between (no call for `--session-timeout` seconds), the next call opens it
  again and its result starts with a note: another client may have used the
  DUT meanwhile. When another client has the DUT, tools fail with its name,
  the time it may still keep it, and whether it is connected or gone. The
  MCP server sends heartbeats while it has the session. When a session is
  left by a gone client of the same name, like an earlier run of the MCP
  server that was killed, it takes it over by itself and says so.
- **Paths**: card paths are `N:/path` (partition N, `/path` means `1:/path`).
  Local paths are on the computer that runs the MCP server, relative to its
  working directory.
- **Output size**: console text comes without escape codes. Tool results keep
  at most 16000 characters of it (the end). `console_read` gives the cursors
  to read what was left out.
- **Long calls**: writing a full image can take minutes, and MCP clients limit
  the time of a tool call. Raise that limit in the client if needed. Uploads
  send progress notifications when the client asks for them.
- **Protocol**: MCP versions 2024-11-05 to 2025-11-25, tools only, on standard
  input and output.
