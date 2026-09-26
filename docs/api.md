# BooBoot HTTP API

Base URL: `http://HOST:PORT/api/v1` (default port 8080).

## Conventions

- **Parameters** can be sent as a JSON object body, as a query string, or as a
  form body. Names are the same. `true`/`false`, `1`/`0` and `yes`/`no` all
  work for booleans.
- **Uploads** (image, files) send the data as the request body, with
  `Content-Length` or chunked transfer encoding. Their parameters go in the
  query string.
- **Session**: all endpoints except `GET /status` and `POST /session` need
  the header `Authorization: Bearer TOKEN`.
- **Answers** are JSON objects, except file and raw console downloads.
- **Errors** have an HTTP status and a JSON body:
  `{"error": "code", "message": "text", ...}`.

| Status | error | When |
|---|---|---|
| 400 | `bad_request` | Bad parameter, bad regex, bad image data |
| 401 | `no_session` | No session, or it expired: open one |
| 404 | `not_found` | Unknown endpoint, file or partition |
| 405 | `method_not_allowed` | Wrong HTTP method |
| 409 | `power_on` | The operation needs the power off |
| 409 | `operation_in_progress` | Another hardware operation runs (`operation` says which) |
| 423 | `busy` | Another client has the session (`session` says who, and when it expires) |
| 500 | `hardware_error` | Relay, mux, card or serial port problem |
| 503 | `unavailable` | Hardware missing or not configured |

Some calls last long (image writes, `expect`, `run`): set the client timeout
accordingly.

## Status and session

### GET /status

No session needed.

```json
{
  "name": "dut1",
  "version": "0.1.0",
  "power": {"state": "off", "backend": "gpio", "error": ""},
  "sd": {"mode": "dut", "error": "", "card": {"state": "written", "sha256": "...", "bytes": 123, "time": 1790000000.0}},
  "console": {"connected": true, "device": "/dev/serial/by-id/...", "baudrate": 921600,
              "cursor": 5120, "boot": 1024, "last": 4800, "error": ""},
  "operation": null,
  "session": {"active": true, "client": "me@desk", "opened": "2026-09-26T10:00:00",
              "timeout": 300, "idle": 12.5, "expires_in": 287.5, "yours": false}
}
```

`power.state`: `on`, `off` or `unknown`. `sd.mode`: `host`, `dut`, `off` or
`unknown`. `sd.card.state`: `unknown`, `writing`, `written`, `incomplete` or
`modified`. `operation`: running hardware operation or null.

### POST /session

Parameters: `client` (name shown to others), `timeout` (idle seconds before
the session ends, default set on the server), `force` (take the session from
another client).

Answer: `{"session": "TOKEN", "client": ..., "timeout": ..., ...}`.
HTTP 423 `busy` if another client has the session.

### POST /session/keepalive

Keeps the session alive without doing anything. Answer: session state.

### DELETE /session

Ends the session.

## Power

### PUT /power

Parameter: `state`, `on` or `off`. Power on first switches the SD card to the
DUT if it was on the host side.

Answer: `{"power": "on", "boot": 1024}`. `boot` is the console cursor at
power on.

### POST /power/cycle

Power off, wait, power on. Parameter: `off_time` (seconds, default set on the
server). Answer as for power on.

## SD card

All SD card operations need the power off. They switch the card to the host
side when needed and leave it there.

### PUT /sd

Parameter: `mode`: `host` (card on the BooBoot board), `dut` or `off`.

### PUT /sd/image

Body: disk image, raw or compressed. Query parameters: `compression` (`auto`
by default, `none`, `gz`, `xz`, `bz2`, `zst`), `verify` (read back and
compare).

Answer: `{"bytes": 1073741824, "sha256": "...", "compression": "xz", "verified": true, "seconds": 61.2}`.

### GET /sd/partitions

Answer: `{"device": "/dev/sda", "size": 31914983424, "partitions": [{"number": 1, "device": "/dev/sda1", "start": 4194304, "size": 268435456, "type": "vfat", "label": "BOOT"}, ...]}`.

### GET /sd/files/N/PATH

N is the partition number. For a directory, the answer is
`{"path": "/", "entries": [{"name": "BOOT.BIN", "type": "file", "size": 1234, "mtime": 1790000000}, ...]}`
(`type`: `file`, `dir`, `link` or `other`). For a file, the answer is the
file content (`application/octet-stream`).

### PUT /sd/files/N/PATH

Body: file content. Parent directories are created. With `dir=1` and an empty
body, creates a directory. Answer: `{"path": "/BOOT.BIN", "size": 1234}`.

### DELETE /sd/files/N/PATH

Deletes a file, or an empty directory. `recursive=1` deletes a directory and
its content.

## Console

Every received byte has a cursor. A `since` parameter is a cursor number, a
negative number (bytes before the end), or `start`, `boot`, `last`, `now`
(see the architecture document).

Regexes use Python syntax and apply to the raw bytes. `$` matches at the end
of the output received so far, which suits prompts (but also matches a line
that is still arriving). Inline flags work:
`(?i)` ignore case, `(?m)` multi-line, `(?s)` dot matches line breaks.

`clean=1` removes terminal escape codes and carriage returns from the
returned text.

### GET /console

Parameters: `since` (default `boot`), `wait` (seconds to wait for new output
when there is none, up to 60), `max` (bytes, default 1 MiB), `clean`,
`format=raw`.

Answer: `{"cursor": 1024, "next": 5120, "lost": false, "boot": 1024, "last": 4800, "text": "..."}`.
Read again from `next` to get what follows. `lost` is true when part of the
asked output is no longer in memory. With `format=raw`, the answer is the raw
bytes, and the cursors are in the `X-Cursor` and `X-Next` headers.

### POST /console/write

Parameters: `text`, `newline` (add the line ending set on the server).
Answer: `{"written": 5, "cursor": 5120}`.

### POST /console/expect

Waits for a regex in the output after a cursor.

Parameters: `pattern`, `since` (default `last`), `timeout` (seconds, default
30), `clean`.

Answer: `{"matched": true, "match": "login: ", "cursor": 1024, "next": 4800, "text": "..."}`.
`text` is the output from `cursor` to the end of the match. After a match,
`last` is `next`. Without a match (timeout), `matched` is false and `text` has
all the output since `cursor`.

### POST /console/run

Sends a command line and waits for the prompt. The prompt is looked for
after the first line received, which is the echo of the command.

Parameters: `command`, `prompt` (regex, default set on the server:
`[#$>] $`), `timeout` (default 30), `clean` (default true).

Answer: `{"matched": true, "next": 5120, "output": "..."}`. `output` has
neither the echoed command nor the prompt line.

## Examples

### bash (curl and jq)

```bash
U=http://booboot.local:8080/api/v1
S=$(curl -s -X POST "$U/session?client=bash" | jq -r .session)
A="Authorization: Bearer $S"

curl -s -H "$A" -X PUT "$U/power?state=off"
curl -s -H "$A" -T BOOT.BIN "$U/sd/files/1/"      # curl adds the file name
curl -s -H "$A" -T image.ub "$U/sd/files/1/"
curl -s -H "$A" -X PUT "$U/power?state=on"
curl -s -H "$A" -X POST "$U/console/expect?since=boot&timeout=120" \
     --data-urlencode "pattern=login: " | jq -r .text
curl -s -H "$A" -X POST "$U/console/run" --data-urlencode "command=root" | jq -r .output
curl -s -H "$A" -X POST "$U/console/run" --data-urlencode "command=uname -a" | jq -r .output
curl -s -H "$A" -X DELETE "$U/session"
```

### PowerShell

```powershell
$u = "http://booboot.local:8080/api/v1"
$s = Invoke-RestMethod -Method Post "$u/session?client=powershell"
$h = @{ Authorization = "Bearer $($s.session)" }

Invoke-RestMethod -Method Put "$u/power?state=off" -Headers $h
Invoke-RestMethod -Method Put "$u/sd/files/1/BOOT.BIN" -Headers $h -InFile BOOT.BIN
Invoke-RestMethod -Method Put "$u/power?state=on" -Headers $h
$body = @{ pattern = "login: "; since = "boot"; timeout = 120 } | ConvertTo-Json
$r = Invoke-RestMethod -Method Post "$u/console/expect" -Headers $h -Body $body `
     -ContentType application/json -TimeoutSec 150
$r.text
Invoke-RestMethod -Method Delete "$u/session" -Headers $h
```

### C#

`client/csharp/BooBootClient.cs` is a small client class for .NET 6 or
later, in one file with no package needed. Copy it into a project.
`client/csharp/Example` is a complete program.

```csharp
using BooBoot;

using var dut = new BooBootClient("http://booboot.local:8080");
await dut.OpenSessionAsync("csharp");
await dut.PowerOffAsync();
await dut.PutFileAsync("BOOT.BIN", "1:/BOOT.BIN");
await dut.PowerOnAsync();
var boot = await dut.ExpectAsync("login: ", since: "boot", timeout: 120);
Console.WriteLine(boot.Text);
await dut.RunAsync("root");
Console.WriteLine((await dut.RunAsync("uname -a")).Text);
await dut.CloseSessionAsync();
```

Errors throw `BooBootException`, with `Status` (HTTP status) and `Code` (like
`busy`). Without the class, `HttpClient` alone is enough:

```csharp
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;

var http = new HttpClient {
    BaseAddress = new Uri("http://booboot.local:8080/api/v1/"),
    Timeout = TimeSpan.FromMinutes(10) };

var s = await (await http.PostAsJsonAsync("session", new { client = "csharp" }))
    .EnsureSuccessStatusCode().Content.ReadFromJsonAsync<JsonElement>();
http.DefaultRequestHeaders.Authorization =
    new AuthenticationHeaderValue("Bearer", s.GetProperty("session").GetString());

(await http.PutAsJsonAsync("power", new { state = "off" })).EnsureSuccessStatusCode();
using (var f = File.OpenRead("BOOT.BIN"))
    (await http.PutAsync("sd/files/1/BOOT.BIN", new StreamContent(f))).EnsureSuccessStatusCode();
(await http.PutAsJsonAsync("power", new { state = "on" })).EnsureSuccessStatusCode();

var r = await (await http.PostAsJsonAsync("console/expect",
        new { pattern = "login: ", since = "boot", timeout = 120 }))
    .EnsureSuccessStatusCode().Content.ReadFromJsonAsync<JsonElement>();
Console.WriteLine(r.GetProperty("text").GetString());
(await http.DeleteAsync("session")).EnsureSuccessStatusCode();
```

### Python

```python
import booboot  # client/booboot.py

dut = booboot.Client("http://booboot.local:8080")
dut.open_session("python")
dut.power_off()
dut.put_file("BOOT.BIN", "1:/BOOT.BIN")
dut.put_file("image.ub", "1:/image.ub")
dut.power_on()
print(dut.expect("login: ", since="boot", timeout=120)["text"])
dut.run("root")
print(dut.run("uname -a")["output"])
dut.close_session()
```
