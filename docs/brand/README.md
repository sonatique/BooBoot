# BooBoot visuals

![BooBoot](booboot-banner.svg)

The icon is a ghost made of an SD card, saying "Boo!": a scarily good
system. One eye is the block cursor of a terminal, the other winks as an
underline cursor.

## Files

| File | Use |
|---|---|
| `booboot-banner.svg`, `png/booboot-banner.png`, `png/booboot-banner@2x.png` | Top of documents and pages. It has its own dark background, so it fits light and dark pages. |
| `booboot-logo.svg`, `png/booboot-logo.png` | Icon and name, on light backgrounds |
| `booboot-logo-dark.svg`, `png/booboot-logo-dark.png` | Icon and name, on dark backgrounds |
| `booboot-icon.svg`, `png/booboot-icon-SIZE.png` | The icon, 48 px and more |
| `booboot-icon-small.svg`, `png/booboot-icon-16.png` to `-32.png` | The same icon for 32 px and less: thicker eyes and mouth, and an edge that shows on dark backgrounds |
| `client/csharp/BooBootConsole/Assets/booboot.ico` | BooBoot Console program and window icon, 16 to 256 px |
| `server/booboot_server/web/favicon.svg`, `favicon.ico` | Icon of the console web page in the browser tab |

In the SVG files, the text is drawn as outlines: it looks the same
everywhere, with no font to install.

## Colors and fonts

| Name | Value | Use |
|---|---|---|
| Green | `#23D18B` | eyes; "Boot" on dark backgrounds |
| Dark green | `#0E8A50` | "Boot" on light backgrounds |
| Light | `#E6EDF3` | mouth; "Boo" on dark backgrounds |
| Ink | `#1F2328` | "Boo" on light backgrounds |
| Body | `#323A44` to `#1D2128` | ghost, top to bottom |
| Silver | `#C9D1D9` | tagline: "Scarily good DUT control" |
| Gray | `#9DA7B3` | second line: "Power, SD card and serial console, over the network" |

The name is in DejaVu Sans Mono Bold, the taglines in DejaVu Sans.

## Making the files again

`make.py` makes all the files above, the icons of BooBoot Console and of
the web page included. It needs fontTools, Pillow, the DejaVu fonts and
Chrome or Chromium:

```sh
pip install fonttools pillow
CHROME=/path/to/chrome python3 docs/brand/make.py
```
