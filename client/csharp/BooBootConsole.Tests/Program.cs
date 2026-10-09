// Checks of BooBoot Console. Without argument: the terminal buffer only.
// With a server URL: also the window, headless, against a server with a fake board, best the one of the tests,
// with three DUTs, dut3 having a console only:
//   cd server && python3 -m booboot_server --fake --config-dir ../tests/fake-board --port 8080
//   dotnet run --project client/csharp/BooBootConsole.Tests -- http://127.0.0.1:8080 [screenshot.png]

using System.Text.Json;
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
        new Settings
        {
            Url = "http://test:8081", LogPrefix = "lab", NewLogAtPowerOn = true, TextSize = 15.5,
            HiddenDuts = { ["http://test:8080"] = new() { "dut2" } },
        }.Save(path);
        var s = Settings.Load(path);
        Check(s.Url == "http://test:8081" && s.LogPrefix == "lab" && s.NewLogAtPowerOn && s.TextSize == 15.5
            && s.ScrollbackLines == 200_000 && s.HiddenDuts["http://test:8080"].SequenceEqual(new[] { "dut2" }),
            "settings saved and loaded");
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
    var windows = new List<MainWindow>();
    try
    {
        await CheckWindow(dut, tmp.FullName, url, screenshot, windows.Add);
        if ((await dut.StatusAsync()).TryGetProperty("duts", out var duts) && duts.GetArrayLength() > 1)
            await CheckDuts(url, tmp.FullName, windows.Add);
    }
    finally
    {
        foreach (var window in windows)
            window.Close();
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
    var window = new MainWindow(settings, url, persist: false) { Width = 1180, Height = 540 };
    created(window);
    window.Show();
    Check(window.Icon != null, "window icon");
    await WaitFor(() => window.Status.Contains("Connected") && window.Status.Contains("session used by console-check"),
        "connected", () => window.Status);
    // The window is at 127.0.0.1: it shows another address of the server, and keeps none for an address.
    var addresses = (await dut.StatusAsync()).GetProperty("network").GetProperty("addresses");
    if (addresses.GetArrayLength() > 0)
        await WaitFor(() => window.Status.Contains($"also at {addresses[0].GetString()}:{new Uri(url).Port}"),
            "other address of the server shown", () => window.Status);
    Check(settings.Addresses.Count == 0, "no address kept for a server given by address");
    await WaitFor(() => window.Buffer.GetAllText().Contains("U-Boot 2024.01"), "output from before the viewer");
    var firstLog = window.LogPath;
    Check(firstLog != null && firstLog.StartsWith(Path.Combine(tmp, "dut1-")), "log started on connect");

    await dut.PowerOnAsync();
    await WaitFor(() => window.Buffer.Pending.EndsWith("login: "), "live output");
    await dut.RunAsync("root", timeout: 5);
    await dut.RunAsync("uname -a", timeout: 5);
    // The whole line, then it is in the log.
    await WaitFor(() => window.Buffer.GetAllText().Contains("Linux fake 6.6.0-fake #1 SMP armv7l GNU/Linux\n"),
        "command output");
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
    // The Log menu.
    var (startLog, stopLog) = (window.LogMenuItems[0], window.LogMenuItems[1]);
    Check(stopLog.IsEnabled, "stop log enabled while logging");
    stopLog.RaiseEvent(new Avalonia.Interactivity.RoutedEventArgs(Avalonia.Controls.MenuItem.ClickEvent));
    Check(window.LogPath == null && !stopLog.IsEnabled, "log stopped from the menu");
    startLog.RaiseEvent(new Avalonia.Interactivity.RoutedEventArgs(Avalonia.Controls.MenuItem.ClickEvent));
    Check(window.LogPath != null && window.LogPath != secondLog && stopLog.IsEnabled, "new log from the menu");

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
    var alive = false;
    for (var i = 0; i < 50 && !alive; i++)
    {
        alive = (await dut.StatusAsync()).GetProperty("session").GetProperty("alive").ValueKind == JsonValueKind.True;
        await Task.Delay(100);
    }
    Check(alive, "heartbeat while in control");
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

    // The power button works in control. Power off asks first.
    var powerButton = window.Power;
    Func<Task<string?>> PowerState = async () =>
        (await dut.StatusAsync()).GetProperty("power").GetProperty("state").GetString();
    Action ClickPower = () => powerButton.RaiseEvent(new Avalonia.Interactivity.RoutedEventArgs(
        Avalonia.Controls.Button.ClickEvent));
    Check(powerButton.IsEnabled && (string?)powerButton.Content == "Power off", "power button in control, DUT on");
    ClickPower();
    await WaitFor(() => window.AskingPowerOff, "power off asks first");
    window.AnswerPowerOff(false);
    Check(!window.AskingPowerOff && await PowerState() == "on", "power off canceled");
    ClickPower();
    await WaitFor(() => window.AskingPowerOff, "power off asks again");
    window.AnswerPowerOff(true);
    await WaitFor(() => (string?)powerButton.Content == "Power on" && powerButton.IsEnabled, "button after power off");
    Check(await PowerState() == "off", "DUT off");
    ClickPower();
    await WaitFor(() => (string?)powerButton.Content == "Power off" && powerButton.IsEnabled, "button after power on");
    Check(await PowerState() == "on", "DUT on");

    // The DUT panel: name, address and details, and the label, set in control, shown with the name.
    var tab = window.CurrentDut!;
    window.OpenDutPanel();
    await WaitFor(() => window.DutPanelOpen && window.DutPanelText.Contains("BooBoot "), "DUT panel",
        () => window.DutPanelText);
    var panel = window.DutPanelText.Split('\n');
    Check(panel[0] == "dut1" && panel[1].EndsWith("/duts/dut1") && panel[4].Contains(" baud")
        && panel[5].EndsWith("relay, on") && window.CanSetLabel, "DUT panel content");
    await window.SetLabelFromPanel("ZCU102 rev B");
    await WaitFor(() => tab.Label == "ZCU102 rev B" && window.Title == "ZCU102 rev B (dut1) - BooBoot Console"
        && window.Status.Contains("ZCU102 rev B (dut1)") && window.DutPanelText.Contains("\nLabel set\n"),
        "label shown", () => window.Status);
    Check((await dut.StatusAsync()).GetProperty("label").GetString() == "ZCU102 rev B", "label on the server");
    await window.SetLabelFromPanel("");
    await WaitFor(() => tab.Label == "" && window.Title == "dut1 - BooBoot Console", "label removed");

    await window.ReleaseControl();
    Check(!powerButton.IsEnabled, "power button off without control");
    Check(!window.CanSetLabel && window.DutPanelText.Contains("Take control to change it"), "panel without control");
    Check(!await tab.SetLabel("x") && window.Status.Contains("take control to set the label"),
        "no label without control");
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

// A board with several DUTs: a tab for each.
static async Task CheckDuts(string url, string tmp, Action<MainWindow> created)
{
    var settings = new Settings { LogFolder = tmp, LogPrefix = "lab", LogOnConnect = true };
    var window = new MainWindow(settings, url, persist: false) { Width = 1180, Height = 540 };
    created(window);
    window.Show();
    using var board = new BooBootClient(url);
    var names = (await board.StatusAsync()).GetProperty("duts").EnumerateArray().Select(d => d.GetString()!).ToList();
    await WaitFor(() => window.Duts.Count == names.Count && window.Duts.All(d => d.Status.Contains("Connected")),
        "a tab for each DUT", () => window.Status);
    var (first, second) = (window.Duts[0], window.Duts[1]);
    Check(first.Name == "dut1" && second.Name == "dut2" && window.CurrentDut == first && window.TabsShown
        && window.Title == "dut1 - BooBoot Console", "tabs, the first DUT shown");
    Check(Path.GetFileName(second.LogPath)!.StartsWith("lab-dut2-"), "log file with the prefix and the DUT name");

    // A DUT without relay, like dut3 of the fake board of the tests: no power button, no power state.
    if (window.Duts.FirstOrDefault(d => d.Name == "dut3") is DutTab third)
    {
        await WaitFor(() => third.Details != null, "details of dut3");
        window.SelectDut(third);
        Check(!third.HasRelay && !window.Power.IsVisible && !window.Status.Contains("power "), "no power button");
        window.OpenDutPanel();
        await WaitFor(() => window.DutPanelOpen && window.DutPanelText.Contains("BooBoot "), "DUT panel of dut3");
        var panel = window.DutPanelText.Split('\n');
        Check(panel[4].Contains(" baud") && panel[5] == "no relay" && panel[6] == "no USB-SD-Mux",
            "DUT panel says what is missing");
        window.CloseDutPanel();
        await WaitFor(() => !window.DutPanelOpen, "DUT panel closed");
        window.SelectDut(first);
        Check(window.Power.IsVisible, "power button of dut1");
    }

    // Output of the other DUT marks its tab.
    var bootFile = Path.Combine(tmp, "BOOT.BIN");
    File.WriteAllText(bootFile, "boot");
    using var dut2 = new BooBootClient(BooBootClient.DutUrl(url, "dut2"));
    await dut2.OpenSessionAsync("console-check", force: true);
    await dut2.PutFileAsync(bootFile, "1:/BOOT.BIN");
    await dut2.PowerOnAsync();
    Check((await dut2.ExpectAsync("login: $", since: "boot", timeout: 10)).Matched, "dut2 boots");
    await dut2.RunAsync("root", timeout: 5);
    await dut2.RunAsync("echo only-on-dut2", timeout: 5);
    await WaitFor(() => second.Buffer.GetAllText().Contains("\nonly-on-dut2") && second.NewOutput,
        "new output of the other DUT");
    Check(!first.Buffer.GetAllText().Contains("only-on-dut2") && !first.NewOutput, "output in its tab only");

    // Ctrl+Tab and Ctrl+Shift+Tab go through the DUTs, the status and buttons follow.
    window.KeyPressQwerty(PhysicalKey.Tab, RawInputModifiers.Control);
    Check(window.CurrentDut == second && !second.NewOutput && window.Status.Contains("dut2    power on")
        && window.Title == "dut2 - BooBoot Console", "Ctrl+Tab shows the next DUT");
    window.KeyPressQwerty(PhysicalKey.Tab, RawInputModifiers.Control | RawInputModifiers.Shift);
    Check(window.CurrentDut == first && window.Status.Contains("dut1    power off"), "Ctrl+Shift+Tab, the previous");
    window.SelectDut(second);

    // A label set by another client shows in the tab.
    await dut2.SetLabelAsync("bench 3");
    await WaitFor(() => second.DisplayName == "bench 3 (dut2)", "label of another client", () => second.Status);
    await dut2.SetLabelAsync("");
    await WaitFor(() => second.DisplayName == "dut2", "label removed by another client");

    // Each DUT has its session.
    await dut2.CloseSessionAsync();
    await window.TakeControl();
    await WaitFor(() => second.InControl && window.Status.Contains("in control"), "control of dut2");
    Check(window.Power.IsEnabled && (string?)window.Power.Content == "Power off", "power button of dut2");
    window.SelectDut(first);
    Check(!window.Power.IsEnabled && (string?)window.Power.Content == "Power on" && !first.InControl,
        "power button of dut1, not in control");
    window.SelectDut(second);
    await window.SwitchPower(false);
    Check(second.PowerState == "off", "dut2 off from its tab");

    // A hidden DUT is released, and stays hidden for the board.
    window.ShowDut("dut2", false);
    Check(window.Duts.Count == names.Count - 1 && window.CurrentDut == first && window.TabsShown
        && settings.HiddenDuts[url].SequenceEqual(new[] { "dut2" }), "DUT hidden, the tabs still shown");
    await WaitForAsync(async () => !(await dut2.StatusAsync()).GetProperty("session").GetProperty("active").GetBoolean(),
        "session of a hidden DUT released");
    foreach (var name in names.Skip(2))
        window.ShowDut(name, false);
    window.ShowDut("dut1", false);
    Check(window.Duts.Count == 1 && window.CurrentDut == first, "one DUT at least stays shown");
    var again = new MainWindow(settings, url, persist: false) { Width = 800, Height = 300 };
    created(again);
    again.Show();
    await WaitFor(() => again.Duts.Count == 1 && again.Status.Contains("Connected"), "window after a DUT was hidden");
    Check(again.CurrentDut!.Name == "dut1" && again.TabsShown, "hidden DUT remembered");
    again.Close();
    foreach (var name in names.Skip(1))
        window.ShowDut(name, true);
    await WaitFor(() => window.Duts.Count == names.Count && window.Duts.All(d => d.Status.Contains("Connected")),
        "DUTs shown again");
    Check(!settings.HiddenDuts.ContainsKey(url), "nothing hidden");

    // The address of one DUT shows that DUT only.
    var single = new MainWindow(new Settings { LogFolder = tmp }, BooBootClient.DutUrl(url, "dut2"), persist: false);
    created(single);
    single.Show();
    await WaitFor(() => single.Status.Contains("Connected"), "window of one DUT");
    Check(single.Duts.Count == 1 && single.CurrentDut!.Name == "dut2" && !single.TabsShown,
        "the address of one DUT shows that DUT only, without tabs");
    single.Close();

    await dut2.OpenSessionAsync("console-check", force: true);
    await dut2.DeleteAsync("1:/BOOT.BIN");
    await dut2.CloseSessionAsync();
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

static async Task WaitForAsync(Func<Task<bool>> condition, string what)
{
    var deadline = DateTime.UtcNow.AddSeconds(15);
    while (!await condition())
    {
        if (DateTime.UtcNow > deadline)
            throw new Exception($"timeout: {what}");
        await Task.Delay(100);
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
