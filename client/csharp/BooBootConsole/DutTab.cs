using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Controls.Shapes;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using BooBoot;

namespace BooBootConsole;

/// <summary>
/// One DUT in the window, with its tab: its console view, typing, power and log.
/// The window shows the status and the buttons of the current one.
/// </summary>
public sealed class DutTab
{
    static readonly IBrush OnBrush = new SolidColorBrush(Color.Parse("#2EA043"));
    static readonly IBrush OffBrush = new SolidColorBrush(Color.Parse("#8C8C8C"));

    readonly Settings settings;
    readonly Action saveSettings;
    readonly LogFile log = new();
    readonly Ellipse dot = new()
    {
        Width = 8, Height = 8, Stroke = OffBrush, StrokeThickness = 1, VerticalAlignment = VerticalAlignment.Center,
    };
    readonly TextBlock title = new()
    {
        Margin = new Thickness(6, 0, 0, 0), MaxWidth = 260, TextTrimming = TextTrimming.CharacterEllipsis,
        VerticalAlignment = VerticalAlignment.Center,
    };
    readonly TextBlock state = new()
    {
        Margin = new Thickness(6, 0, 0, 0), Opacity = 0.7, MaxWidth = 200, TextTrimming = TextTrimming.CharacterEllipsis,
        VerticalAlignment = VerticalAlignment.Center,
    };
    readonly TextBlock mark = new()
    {
        Text = "•", FontWeight = FontWeight.Bold, Margin = new Thickness(4, 0, 0, 0), IsVisible = false,
        VerticalAlignment = VerticalAlignment.Center,
    };
    ConsoleSource? source;
    ConsoleInput? input;
    // Key of the addresses of the board in the settings.
    string board = "";
    string connection = "Not connected", power = "", session = "", note = "", logText = "";
    // Another address of the server, for where its name does not work.
    string alsoAt = "";
    // True while a power switch of the power button runs.
    bool switching;
    // True while output from before the connection is added: it is not logged.
    bool history;
    bool connectedOnce, current;

    public DutTab(string name, Settings settings, Action saveSettings)
    {
        Name = name;
        this.settings = settings;
        this.saveSettings = saveSettings;
        Buffer = new TerminalBuffer(settings.ScrollbackLines);
        Buffer.LineDone += line =>
        {
            if (!history)
                log.WriteLine(line.Text);
        };
        View = new TerminalView { Buffer = Buffer, TextSize = settings.TextSize, Fonts = settings.Fonts, IsVisible = false };
        View.FollowChanged += (_, _) => Changed?.Invoke();
        View.Input += text => input?.Send(text);
        View.ReadOnlyKey += (_, _) =>
        {
            if (source != null && input?.Token == null)
            {
                note = "read only: Take control to type";
                Update();
            }
        };
        Header = new TabStripItem
        {
            Content = new StackPanel { Orientation = Orientation.Horizontal, Children = { dot, title, state, mark } },
        };
        UpdateLogText();
        Update();
    }

    public string Name { get; private set; }

    /// <summary>Free text shown with the name, set on the server. Empty: none.</summary>
    public string Label { get; private set; } = "";

    /// <summary>The label with the name, or the name.</summary>
    public string DisplayName => Label != "" ? $"{Label} ({Name})" : Name;

    /// <summary>Details of the DUT and its server, once connected.</summary>
    public ConsoleSource.Details? Details { get; private set; }

    /// <summary>The address of the DUT, while connected.</summary>
    public string Url => source?.Url ?? "";

    /// <summary>The last note of the status bar, like why the label was not set.</summary>
    public string Note => note;

    public TerminalBuffer Buffer { get; }

    public TerminalView View { get; }

    /// <summary>The tab, in the tab strip of the window.</summary>
    public TabStripItem Header { get; }

    /// <summary>Raised when what the window shows of this DUT changes.</summary>
    public event Action? Changed;

    /// <summary>Raised with the names of the DUTs of the board, when they change.</summary>
    public event Action<IReadOnlyList<string>>? BoardChanged;

    public bool Connected => source != null;

    public bool InControl => input?.Token != null;

    /// <summary>"on", "off", or "" when not known.</summary>
    public string PowerState => power;

    /// <summary>The DUT has a relay, or is not known yet. Without one, the power state is only recorded.</summary>
    public bool HasRelay => Details?.Relay != false;

    string PowerText => power != "" && HasRelay ? "power " + power : "";

    /// <summary>True while a power switch runs.</summary>
    public bool Switching => switching;

    /// <summary>Output came while another DUT was shown.</summary>
    public bool NewOutput { get; private set; }

    /// <summary>The board has several DUTs shown in the window: log file names get the DUT name.</summary>
    public bool Several { get; set; }

    /// <summary>The DUT shown in the window.</summary>
    public bool Current
    {
        get => current;
        set
        {
            current = value;
            View.IsVisible = value;
            if (value)
                NewOutput = false;
            Update();
        }
    }

    public string Status => string.Join("    ", new[]
    {
        connection,
        alsoAt != "" ? "also at " + alsoAt : "",
        DisplayName,
        PowerText,
        session != "" ? "session " + session : "",
        $"{Buffer.Count:N0} lines",
        InControl ? (note != "" ? "in control, " + note : "in control") : note,
    }.Where(p => p != ""));

    /// <summary>The log file, or why there is none.</summary>
    public string LogText => logText;

    /// <summary>The current log file.</summary>
    public string? LogPath => log.Active ? log.Path : null;

    // Connection

    /// <summary>Shows the console of the DUT at url, from the start of its output.</summary>
    /// <param name="board">The board, under which the settings keep its addresses.</param>
    public void Connect(string url, string board)
    {
        Disconnect();
        this.board = board;
        Buffer.Clear();
        View.Refresh();
        connectedOnce = false;
        var known = settings.Addresses.TryGetValue(board, out var list) ? list : new List<string>();
        source = new ConsoleSource(url, known);
        source.Available += () => Dispatcher.UIThread.Post(Drain, DispatcherPriority.Background);
        input = new ConsoleInput(url, known);
        input.Lost += reason => SetControl(false, reason);
        input.Failed += text =>
        {
            note = text;
            Update();
        };
        connection = "Connecting to " + source.Url;
        Update();
        source.Start();
    }

    public void Disconnect()
    {
        if (source == null)
            return;
        source.Dispose();
        source = null;
        if (input != null)
        {
            _ = Release(input);
            input = null;
            SetControl(false, "");
        }
        (connection, power, session, alsoAt) = ("Not connected", "", "", "");
        Update();
    }

    /// <summary>Disconnects and stops the log, for good.</summary>
    public void Close()
    {
        Disconnect();
        StopLog();
    }

    /// <summary>Adds the queued console items to the view, a bit at a time to keep the window responsive.</summary>
    void Drain()
    {
        if (source == null)
            return;
        var watch = Stopwatch.StartNew();
        // An item can close the tab: the list of DUTs of the board.
        while (source != null && source.TryTake(out var item))
        {
            Handle(item);
            if (watch.ElapsedMilliseconds > 50)
            {
                Dispatcher.UIThread.Post(Drain, DispatcherPriority.Background);
                break;
            }
        }
        log.Flush();
        View.Refresh();
        Update();
    }

    void Handle(ConsoleSource.Item item)
    {
        switch (item)
        {
            case ConsoleSource.Connected c:
                if (c.Restarted)
                    Buffer.AddMarker(Marker("server restarted", DateTime.Now));
                (Name, power) = (c.Name, c.Power);
                connection = "Connected to " + source!.Url;
                if (!connectedOnce && settings.LogOnConnect && !log.Active)
                    StartLog();
                connectedOnce = true;
                break;
            case ConsoleSource.Disconnected d:
                connection = "No connection, trying again: " + d.Message;
                break;
            case ConsoleSource.Output o:
                history = o.History;
                Buffer.Feed(o.Text);
                history = false;
                if (!o.History && !current)
                    NewOutput = true;
                break;
            case ConsoleSource.Power p:
                if (!p.History)
                {
                    power = p.On ? "on" : "off";
                    if (p.On && settings.NewLogAtPowerOn && log.Active)
                    {
                        // The unfinished line goes to the old file, the marker to the new one.
                        Buffer.EndLine();
                        StartLog();
                    }
                }
                history = p.History;
                Buffer.AddMarker(Marker(p.On ? "power on" : "power off", p.Time));
                history = false;
                break;
            case ConsoleSource.Lost l:
                Buffer.AddMarker($"---- {l.Bytes} bytes lost ----");
                break;
            case ConsoleSource.Network n:
                var server = new Uri(source!.Url);
                alsoAt = n.Addresses.Count == 0 || n.Addresses.Contains(server.Host) ? "" : $"{n.Addresses[0]}:{server.Port}";
                // Kept for when the name of the server is not found, like a .local name over a VPN.
                if (server.HostNameType == UriHostNameType.Dns
                    && !(settings.Addresses.TryGetValue(board, out var old) && old.SequenceEqual(n.Addresses)))
                {
                    settings.Addresses[board] = n.Addresses.ToList();
                    saveSettings();
                }
                break;
            case ConsoleSource.AddressUsed a:
                note = a.Note;
                break;
            case ConsoleSource.Board b:
                BoardChanged?.Invoke(b.Duts);
                break;
            case ConsoleSource.Label l:
                Label = l.Text;
                break;
            case ConsoleSource.Details d:
                Details = d;
                break;
            case ConsoleSource.Session s:
                session = s.Text;
                // Only an answer about the current session counts.
                if (s.Token != null && s.Token == input?.Token && !s.Yours)
                {
                    input.Forget();
                    SetControl(false, s.Text == "free" ? "control ended after the idle time"
                        : "control lost: the DUT is " + s.Text);
                }
                break;
        }
    }

    static string Marker(string what, DateTime time) => $"---- {what} {time:yyyy-MM-dd HH:mm:ss.fff} ----";

    /// <summary>Updates the tab, and tells the window.</summary>
    void Update()
    {
        dot.Fill = !HasRelay ? Brushes.Transparent : power == "on" ? OnBrush : power == "off" ? OffBrush : Brushes.Transparent;
        title.Text = DisplayName;
        state.Text = InControl ? "in control" : session.StartsWith("used by ") ? session : "";
        mark.IsVisible = NewOutput;
        ToolTip.SetTip(Header, string.Join(", ", new[] { DisplayName, PowerText, state.Text,
            NewOutput ? "new output" : "" }.Where(p => p != "")));
        Changed?.Invoke();
    }

    /// <summary>Applies the settings of the view.</summary>
    public void ApplySettings()
    {
        Buffer.MaxLines = settings.ScrollbackLines;
        View.TextSize = settings.TextSize;
        View.Fonts = settings.Fonts;
    }

    // Control

    /// <summary>Opens the session to type. Returns null, or the session of the client that has the DUT.</summary>
    public async Task<JsonElement?> TakeControl(bool force = false, bool ifGone = false)
    {
        if (input == null)
            return null;
        try
        {
            if (await input.TakeAsync(force, ifGone) is JsonElement busy)
                return busy;
            SetControl(true, "");
        }
        catch (BooBootException e)
        {
            SetControl(false, "cannot take control: " + e.Message);
        }
        return null;
    }

    public async Task ReleaseControl()
    {
        if (input == null)
            return;
        await input.ReleaseAsync();
        SetControl(false, "");
        session = "free";
        Update();
    }

    static async Task Release(ConsoleInput old)
    {
        await old.ReleaseAsync();
        old.Dispose();
    }

    /// <summary>Sets the label of the DUT, in control. Returns false when it was not set: the status says why.</summary>
    public async Task<bool> SetLabel(string text)
    {
        if (input?.Token == null)
        {
            note = "take control to set the label";
            Update();
            return false;
        }
        try
        {
            if (await input.LabelAsync(text) is not string label)
                return false;
            (Label, note) = (label, "");
            return true;
        }
        catch (BooBootException e)
        {
            note = "label not set: " + e.Message;
            return false;
        }
        finally
        {
            Update();
        }
    }

    /// <summary>Switches the DUT power, in control.</summary>
    public async Task SwitchPower(bool on)
    {
        if (input?.Token == null)
            return;
        switching = true;
        Update();
        try
        {
            if (await input.PowerAsync(on) is string now)
            {
                power = now;
                note = "";
            }
        }
        catch (BooBootException e)
        {
            note = $"power {(on ? "on" : "off")} failed: {e.Message}";
        }
        finally
        {
            switching = false;
            Update();
            View.Focus();
        }
    }

    void SetControl(bool on, string text)
    {
        note = text;
        View.Typing = on;
        if (source != null)
            source.Token = on ? input?.Token : null;
        if (on)
        {
            session = "yours";
            View.Focus();
        }
        Update();
    }

    // Log

    public string Prefix()
    {
        var prefix = settings.LogPrefix.Trim();
        prefix = prefix == "" ? (Name != "" ? Name : "console") : Several ? prefix + "-" + Name : prefix;
        return string.Concat(prefix.Select(c => System.IO.Path.GetInvalidFileNameChars().Contains(c) ? '_' : c));
    }

    public void StartLog()
    {
        try
        {
            log.Start(settings.LogFolder, Prefix());
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or ArgumentException)
        {
            ShowLogProblem("Cannot write the log: " + e.Message);
            return;
        }
        UpdateLogText();
    }

    public void StopLog()
    {
        if (!log.Active)
            return;
        if (Buffer.Pending.Length > 0)
            log.WriteLine(Buffer.Pending);
        log.Stop();
        UpdateLogText();
    }

    void UpdateLogText()
    {
        logText = log.Active ? "Log: " + System.IO.Path.GetFileName(log.Path) : "No log";
        Update();
    }

    /// <summary>Shows a problem with a file where the log file is shown.</summary>
    public void ShowLogProblem(string text)
    {
        logText = text;
        Update();
    }

    public void Clear()
    {
        Buffer.Clear();
        View.ClearSelection();
        View.Follow = true;
        View.Refresh();
        Update();
    }
}
