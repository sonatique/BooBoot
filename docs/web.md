# Console web page

The BooBoot server has a web page that shows the serial console of the DUT,
live: open `http://booboot.local:8080/` (the server address) in any browser,
on a computer, a tablet or a phone. Nothing to install.

![Console web page](web.png)

Like [BooBoot Console](console.md), it only reads: it needs no session, so
agents and other clients use the DUT as usual, and the console data is not
changed in any way. Any number of people can watch at the same time.

## Features

- **Live output**, and the output kept by the server from before (8 MB by
  default).
- **Terminal text**: colors, carriage returns, backspaces and line erase.
- **Power switches**: a line shows each power on and power off, with its time.
- **Follow**: the page follows new output. Scrolling up stops it, so that the
  text stays in place. Scrolling back to the end, End or the Follow button
  starts it again. It also waits while text is being selected.
- **Copy**: select with the mouse and copy as usual. Line breaks and empty
  lines are kept.
- **Search**: the browser search (Ctrl+F) finds text in all the lines kept.
- **Save** downloads all the text as a file. **Clear** empties the page (not
  the server memory).
- **Logs** lists the log files that the server writes, one per power on, each
  line with its time since power on. A click downloads one.
- **A- and A+** change the text size. The browser remembers it.
- **Connection**: when the connection is lost, the page connects again and
  continues where it was. A line says when the server was restarted.

The page keeps the last 50,000 lines. `?lines=N` in the address changes it,
like `http://booboot.local:8080/?lines=200000`.

## Web page or desktop program

| | Web page | BooBoot Console |
|---|---|---|
| Install | nothing | a program file |
| Devices | any browser, phones too | Windows, Linux, macOS |
| Log files | the ones on the BooBoot board | on the desktop, started when you want |
| Lines kept | 50,000 by default | 200,000 by default |

## Turn it off

In the server configuration (`/etc/booboot/NAME.ini`):

```ini
[server]
web = no
```

then `sudo systemctl restart booboot@NAME`. The API, the stream included,
stays available.

## How it works

The page is three small files served by the BooBoot server itself
(`server/booboot_server/web/`), with nothing loaded from the internet. It
reads `GET /api/v1/console/stream` (see [api.md](api.md)) and lists the logs
with `GET /api/v1/logs`. The server reads the serial port all the time;
viewers only read its memory, so the DUT does not see them.

## Limits

- As in BooBoot Console, the view is line based. Programs that draw on the
  whole screen (`top`, `vi`, `menuconfig`) do not show well: use `booboot
  console attach` in a terminal for them.
- Read only: no typing.
- On a trusted network, anyone who can reach the server can watch, as with
  the API.
