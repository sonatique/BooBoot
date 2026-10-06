// Checks of BooBoot Console. Without argument: the terminal buffer only.
// With a server URL: also the window, headless, against a server with a fake board:
//   cd server && python3 -m booboot_server --fake --port 8080
//   dotnet run --project client/csharp/BooBootConsole.Tests -- http://127.0.0.1:8080 [screenshot.png]

using System.Text.RegularExpressions;
using Avalonia;
using Avalonia.Headless;
using Avalonia.Input;
using Avalonia.Input.Platform;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using BooBoot;
using BooBootConsole;

CheckBuffer();
CheckSettings();
if (args.Length > 0)
{
    TestApp.BuildAvaloniaApp().SetupWithoutStarting();
    using var done = new CancellationTokenSource();
    Exception? failure = null;
    Dispatcher.UIThread.Post(async () =>
    {
        try
        {
            await CheckAll(args[0], args.Length > 1 ? args[1] : null);
        }
        catch (Exception e)
        {
            failure = e;
        }
        done.Cancel();
    });
    Dispatcher.UIThread.MainLoop(done.Token);
    if (failure != null)
    {
        Console.WriteLine(failure);
        return 1;
    }
}
Console.WriteLine("all checks passed");
return 0;

static void CheckBuffer()
{
    var b = new TerminalBuffer();
    var done = new List<string>();
    b.LineDone += line => done.Add(line.Text);
    b.Feed("one\r\ntwo\nthr");
    Check(done.SequenceEqual(new[] { "one", "two" }) && b.Pending == "thr" && b.Count == 3, "lines");
    b.Feed("ee\r\n12345\rab");
    Check(b.Pending == "ab345", "carriage return overwrites");
    b.Feed("\r\nabc\b\bX");
    Check(b.Pending == "aXc", "backspace");
    b.Feed("\r\nabcdef\u001b[3D\u001b[K");
    Check(b.Pending == "abc", "erase to line end");
    b.Feed("\r\u001b[2K");
    Check(b.Pending == "", "erase line");
    b.Feed("abcdef\u001b[4G\u001b[2P");
    Check(b.Pending == "abcf", "delete characters");
    b.Feed("\u001b[2D\u001b[2@");
    Check(b.Pending == "a  bcf", "insert characters");
    b.Feed("\r\na\tb");
    Check(b.Pending == "a       b", "tab");
    b.Feed("\r\n\u001b]0;window title\aafter\u001b(B");
    Check(b.Pending == "after", "title sequence dropped");

    b.Feed("\r\n\u001b[1;31mred\u001b[0m plain \u001b[38;5;196mX\u001b[48;2;1;2;3mY\u001b[3");
    b.Feed("2mG");
    var runs = b[b.Last].Runs!;
    Check(runs[0] == new StyleRun(0, new TermStyle(2, 0, TermFlags.Bold)), "bold red");
    Check(runs[1] == new StyleRun(3, default), "reset");
    Check(runs[2] == new StyleRun(10, new TermStyle(197, 0, TermFlags.None)), "256 colors");
    Check(runs[3].Style.Bg == (TermStyle.Rgb | 0x010203), "true color");
    Check(runs[4].Style.Fg == 3 && b.Pending.EndsWith("XYG"), "sequence split between reads");

    b.Feed("\u001b[0m\r\npartial");
    b.AddMarker("---- power on ----");
    var n = b.Last;
    Check(b[n - 2].Text == "partial" && b[n - 1].IsMarker && b[n].Text == "" && done[^1] == "---- power on ----",
        "marker after the output");
    Check(b.GetText(new TextPos(n - 2, 2), new TextPos(n - 1, 4)) == "rtial\n----", "text between positions");

    b.Feed(new string('x', TerminalBuffer.MaxLineLength + 10));
    Check(b.Pending.Length == 10, "very long lines are cut");

    // 8 MB, the output kept by the server, colored like a kernel log.
    var big = new TerminalBuffer(200_000);
    var chunk = string.Concat(Enumerable.Range(0, 1000).Select(i =>
        $"[{i,5}.123456] \u001b[0;32mOK\u001b[0m Started unit number {i} of the system.\r\n"));
    var watch = System.Diagnostics.Stopwatch.StartNew();
    for (var i = 0; i < 8 << 20; i += chunk.Length)
        big.Feed(chunk);
    Console.WriteLine($"   8 MB in {watch.ElapsedMilliseconds} ms, {big.Count} lines");
    Check(watch.ElapsedMilliseconds < 20000 && big.Count > 100_000, "large output");

    var small = new TerminalBuffer(100);
    for (var i = 0; i < 1000; i++)
        small.Feed($"line {i}\n");
    Check(small.Count <= 107 && small.First > 800 && small[small.Last - 1].Text == "line 999", "scrollback limit");
    Check(small.GetAllText().EndsWith("line 998\nline 999\n"), "all text");
    small.Clear();
    Check(small.Count == 1 && small.Pending == "" && small.First > 1000, "clear");
}

static void CheckSettings()
{
    var dir = Directory.CreateTempSubdirectory("booboot-settings-");
    try
    {
        var path = Path.Combine(dir.FullName, "settings.json");
        new Settings { Url = "http://test:8081", LogPrefix = "lab", NewLogAtPowerOn = true, TextSize = 15.5 }.Save(path);
        var s = Settings.Load(path);
        Check(s.Url == "http://test:8081" && s.LogPrefix == "lab" && s.NewLogAtPowerOn && s.TextSize == 15.5
            && s.ScrollbackLines == 200_000, "settings saved and loaded");
        Check(File.ReadAllText(path).Contains("\n  \"LogPrefix\": \"lab\","), "settings file indented");
        Check(Settings.Load(Path.Combine(dir.FullName, "none.json")).Url == new Settings().Url, "default settings");
    }
    finally
    {
        dir.Delete(true);
    }
}

static async Task CheckAll(string url, string? screenshot)
{
    var tmp = Directory.CreateTempSubdirectory("booboot-console-");
    using var dut = new BooBootClient(url);
    await dut.OpenSessionAsync("console-check", force: true);
    MainWindow? window = null;
    try
    {
        await CheckWindow(dut, tmp.FullName, url, screenshot, w => window = w);
    }
    finally
    {
        window?.Close();
        await dut.CloseSessionAsync();
        tmp.Delete(true);
    }
}

static async Task CheckWindow(BooBootClient dut, string tmp, string url, string? screenshot,
    Action<MainWindow> created)
{
    var bootFile = Path.Combine(tmp, "BOOT.BIN");
    File.WriteAllText(bootFile, "boot");

    // Output from before the viewer starts: shown, but not logged.
    await dut.PowerOffAsync();
    await dut.PutFileAsync(bootFile, "1:/BOOT.BIN");
    await dut.PowerOnAsync();
    Check((await dut.ExpectAsync("login: $", since: "boot", timeout: 10)).Matched, "boot before the viewer");
    await dut.PowerOffAsync();

    var settings = new Settings { LogFolder = tmp, LogOnConnect = true, NewLogAtPowerOn = true };
    var window = new MainWindow(settings, url, persist: false) { Width = 1000, Height = 540 };
    created(window);
    window.Show();
    Check(window.Icon != null, "window icon");
    await WaitFor(() => window.Status.Contains("Connected") && window.Status.Contains("session used by console-check"),
        "connected", () => window.Status);
    await WaitFor(() => window.Buffer.GetAllText().Contains("U-Boot 2024.01"), "output from before the viewer");
    var firstLog = window.LogPath;
    Check(firstLog != null && firstLog.StartsWith(Path.Combine(tmp, "dut1-")), "log started on connect");

    await dut.PowerOnAsync();
    await WaitFor(() => window.Buffer.Pending.EndsWith("login: "), "live output");
    await dut.RunAsync("root", timeout: 5);
    await dut.RunAsync("uname -a", timeout: 5);
    await WaitFor(() => window.Buffer.GetAllText().Contains("Linux fake 6.6.0-fake"), "command output");
    var text = window.Buffer.GetAllText();
    Check(text.Contains("---- power off ") && text.Contains("---- power on "), "power markers");
    var ok = Enumerable.Range(0, window.Buffer.Count).Select(i => window.Buffer[window.Buffer.First + i])
        .First(l => l.Text.Contains("OK Reached target"));
    Check(ok.Runs != null && ok.Runs.Any(r => r.Style.Fg == 3), "colors");

    var secondLog = window.LogPath!;
    Check(secondLog != firstLog, "new log at power on");
    var first = File.ReadAllText(firstLog!);
    Check(!first.Contains("U-Boot") && !first.Contains("power on"), "first log without the earlier output");
    var second = File.ReadAllLines(secondLog);
    Check(second[0].StartsWith("---- power on ") && second.Contains("U-Boot 2024.01 (fake)")
        && second.Contains("Linux fake 6.6.0-fake #1 SMP armv7l GNU/Linux"), "second log");

    var frame = window.CaptureRenderedFrame()!;
    if (screenshot != null)
        frame.Save(screenshot, new PngBitmapEncoderOptions());
    Check(CountPixels(frame, 0x0D, 0xBC, 0x79) > 20, "green text drawn");

    // Scrolled up, the view stays where it is when output comes.
    window.Height = 250;
    await Task.Delay(100);
    window.KeyPressQwerty(Avalonia.Input.PhysicalKey.PageUp, Avalonia.Input.RawInputModifiers.None);
    Check(!window.Terminal.Follow, "page up stops following");
    var origin = window.Terminal.TranslatePoint(default, window)!.Value;
    var area = new PixelRect((int)origin.X, (int)origin.Y, (int)window.Terminal.Bounds.Width - 30,
        (int)window.Terminal.Bounds.Height);
    var before = Pixels(window.CaptureRenderedFrame()!, area);
    await dut.WriteAsync("echo more", newline: true);
    await WaitFor(() => window.Buffer.GetAllText().Contains("\nmore"), "more output");
    Check(before.SequenceEqual(Pixels(window.CaptureRenderedFrame()!, area)), "view kept while scrolled up");
    window.KeyPressQwerty(Avalonia.Input.PhysicalKey.End, Avalonia.Input.RawInputModifiers.None);
    Check(window.Terminal.Follow, "end follows again");
    window.Terminal.SelectAll();
    Check(window.Terminal.SelectedText.Contains("U-Boot 2024.01") && window.Terminal.SelectedText.EndsWith("# "),
        "select all");
    window.Terminal.ClearSelection();

    // Typing: read only until Take control.
    window.KeyTextInput("x");
    await WaitFor(() => window.Status.Contains("read only"), "keys refused while read only");
    await dut.CloseSessionAsync();
    await window.TakeControl();
    await WaitFor(() => window.Status.Contains("session yours") && window.Status.Contains("in control"), "control taken");
    Check((await dut.StatusAsync()).GetProperty("session").GetProperty("client").GetString()!
        .StartsWith("BooBoot Console "), "session in the name of the program");
    window.KeyTextInput("echo typedd");
    window.KeyPressQwerty(PhysicalKey.Backspace, RawInputModifiers.None);
    window.KeyPressQwerty(PhysicalKey.Enter, RawInputModifiers.None);
    await WaitFor(() => Regex.IsMatch(window.Buffer.GetAllText(), @"echo typed *\ntyped\n"), "typed command");
    window.KeyPressQwerty(PhysicalKey.C, RawInputModifiers.Control);
    await WaitFor(() => window.Buffer.GetAllText().Contains("^C"), "Ctrl+C sent");
    var clipboard = window.Clipboard;
    if (clipboard != null)
    {
        await clipboard.SetTextAsync("echo pasted\n");
        window.KeyPressQwerty(PhysicalKey.V, RawInputModifiers.Control);
        await WaitFor(() => Regex.IsMatch(window.Buffer.GetAllText(), @"echo pasted *\npasted\n"), "pasted command");
    }

    // Another client takes the DUT: the next key ends the control.
    using var other = new BooBootClient(url);
    await other.OpenSessionAsync("colleague", force: true);
    window.KeyTextInput("y");
    await WaitFor(() => window.Status.Contains("control lost: the DUT is used by colleague"), "control lost",
        () => window.Status);
    Check(!window.Terminal.Typing, "read only again");
    await window.TakeControl(force: true);
    await WaitFor(() => window.Status.Contains("in control"), "control taken over");
    await window.ReleaseControl();
    Check(!(await dut.StatusAsync()).GetProperty("session").GetProperty("active").GetBoolean(), "control released");

    await dut.OpenSessionAsync("console-check");
    await dut.PowerOffAsync();
    await dut.DeleteAsync("1:/BOOT.BIN");
    await dut.CloseSessionAsync();

    // Closing the window releases the session.
    await window.TakeControl();
    await WaitFor(() => window.Status.Contains("in control"), "control taken before closing");
    window.Close();
    await WaitFor(() => window.LogPath == null, "log closed with the window");
    Check(!(await dut.StatusAsync()).GetProperty("session").GetProperty("active").GetBoolean(),
        "session released with the window");
}

static async Task WaitFor(Func<bool> condition, string what, Func<string>? state = null)
{
    var deadline = DateTime.UtcNow.AddSeconds(15);
    while (!condition())
    {
        if (DateTime.UtcNow > deadline)
            throw new Exception($"timeout: {what} {state?.Invoke()}");
        await Task.Delay(20);
    }
    Check(true, what);
}

static unsafe int CountPixels(WriteableBitmap bitmap, byte r, byte g, byte b)
{
    using var fb = bitmap.Lock();
    var count = 0;
    for (var y = 0; y < fb.Size.Height; y++)
    {
        var row = (uint*)(fb.Address + y * fb.RowBytes);
        for (var x = 0; x < fb.Size.Width; x++)
        {
            var p = row[x];
            // BGRA or RGBA: accept both orders. Text edges are blended with the background.
            int c0 = (byte)p, c1 = (byte)(p >> 8), c2 = (byte)(p >> 16);
            if (Math.Abs(c1 - g) < 24 && ((Math.Abs(c0 - b) < 24 && Math.Abs(c2 - r) < 24)
                                          || (Math.Abs(c0 - r) < 24 && Math.Abs(c2 - b) < 24)))
                count++;
        }
    }
    return count;
}

static unsafe uint[] Pixels(WriteableBitmap bitmap, PixelRect area)
{
    using var fb = bitmap.Lock();
    var pixels = new uint[area.Width * area.Height];
    for (var y = 0; y < area.Height; y++)
        new ReadOnlySpan<uint>((uint*)(fb.Address + (area.Y + y) * fb.RowBytes) + area.X, area.Width)
            .CopyTo(pixels.AsSpan(y * area.Width));
    return pixels;
}

static void Check(bool ok, string what)
{
    if (!ok)
        throw new Exception("check failed: " + what);
    Console.WriteLine("ok: " + what);
}

class TestApp
{
    public static AppBuilder BuildAvaloniaApp() => AppBuilder.Configure<App>().UseSkia()
        .UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false });
}
