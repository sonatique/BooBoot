using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Platform.Storage;
using Avalonia.Layout;
using Avalonia.Threading;
using BooBoot;

namespace BooBootConsole;

public partial class MainWindow : Window
{
    readonly Settings settings;
    readonly bool persist;
    readonly TerminalBuffer buffer;
    readonly LogFile log = new();
    ConsoleSource? source;
    ConsoleInput? input;
    string connection = "Not connected", dutName = "", power = "", session = "", note = "";
    // Another address of the server, for where its name does not work.
    string alsoAt = "";
    bool closing;
    // True while output from before the connection is added: it is not logged.
    bool history;
    bool connectedOnce;

    public MainWindow() : this(new Settings(), null, false)
    {
    }

    /// <param name="url">Server to connect to at start. Null: the last one.</param>
    /// <param name="persist">Save the settings when they change.</param>
    public MainWindow(Settings settings, string? url, bool persist = true)
    {
        InitializeComponent();
        this.settings = settings;
        this.persist = persist;
        buffer = new TerminalBuffer(settings.ScrollbackLines);
        buffer.LineDone += line =>
        {
            if (!history)
                log.WriteLine(line.Text);
        };
        View.Buffer = buffer;
        View.TextSize = settings.TextSize;
        View.Fonts = settings.Fonts;
        View.FollowChanged += (_, _) => FollowButton.IsChecked = View.Follow;
        FollowButton.IsCheckedChanged += (_, _) => View.Follow = FollowButton.IsChecked == true;
        UrlBox.Text = url ?? settings.Url;
        UrlBox.KeyDown += (_, e) =>
        {
            if (e.Key == Key.Enter)
                Connect();
        };
        ConnectButton.Click += (_, _) =>
        {
            if (source == null)
                Connect();
            else
                Disconnect();
        };
        ClearButton.Click += OnClear;
        CopyButton.Click += OnCopy;
        SaveButton.Click += OnSave;
        StartLogButton.Click += (_, _) => StartLog();
        StopLogButton.Click += (_, _) => StopLog();
        BrowseButton.Click += OnBrowse;
        OpenFolderButton.Click += OnOpenFolder;
        ControlButton.Click += async (_, _) =>
        {
            if (input?.Token != null)
                await ReleaseControl();
            else
                await TakeControl();
        };
        View.Input += text => input?.Send(text);
        View.ReadOnlyKey += (_, _) =>
        {
            if (source != null && input?.Token == null)
            {
                note = "read only: Take control to type";
                UpdateStatus();
            }
        };
        ShowSettings();
        UpdateLogText();
        Opened += (_, _) =>
        {
            View.Focus();
            Connect();
        };
        Closing += async (_, e) =>
        {
            // Release the session before closing.
            if (input?.Token != null && !closing)
            {
                e.Cancel = true;
                closing = true;
                await input.ReleaseAsync();
                Close();
                return;
            }
            Disconnect();
            StopLog();
        };
    }

    /// <summary>The text buffer, for tests.</summary>
    public TerminalBuffer Buffer => buffer;

    public TerminalView Terminal => View;

    /// <summary>The current log file, for tests.</summary>
    public string? LogPath => log.Active ? log.Path : null;

    public string Status => StatusText.Text ?? "";

    // Connection

    void Connect()
    {
        Disconnect();
        var url = (UrlBox.Text ?? "").Trim();
        if (url.Length == 0)
            return;
        if (!url.Contains("://"))
            url = "http://" + url;
        if (!Uri.TryCreate(url, UriKind.Absolute, out _))
        {
            connection = "Bad server address: " + url;
            UpdateStatus();
            return;
        }
        UrlBox.Text = url;
        settings.Url = url;
        SaveSettings();
        buffer.Clear();
        View.Refresh();
        connectedOnce = false;
        var known = settings.Addresses.TryGetValue(url.TrimEnd('/'), out var list) ? list : new List<string>();
        source = new ConsoleSource(url, known);
        source.Available += () => Dispatcher.UIThread.Post(Drain, DispatcherPriority.Background);
        input = new ConsoleInput(url, known);
        input.Lost += reason => SetControl(false, reason);
        input.Failed += text =>
        {
            note = text;
            UpdateStatus();
        };
        connection = "Connecting to " + url;
        ConnectButton.Content = "Disconnect";
        ControlButton.IsEnabled = true;
        UpdateStatus();
        source.Start();
    }

    void Disconnect()
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
        (connection, dutName, power, session, alsoAt) = ("Not connected", "", "", "", "");
        ConnectButton.Content = "Connect";
        ControlButton.IsEnabled = false;
        Title = "BooBoot Console";
        UpdateStatus();
    }

    /// <summary>Adds the queued console items to the view, a bit at a time to keep the window responsive.</summary>
    void Drain()
    {
        if (source == null)
            return;
        var watch = Stopwatch.StartNew();
        while (source.TryTake(out var item))
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
        UpdateStatus();
    }

    void Handle(ConsoleSource.Item item)
    {
        switch (item)
        {
            case ConsoleSource.Connected c:
                if (c.Restarted)
                    buffer.AddMarker(Marker("server restarted", DateTime.Now));
                (dutName, power) = (c.Name, c.Power);
                connection = "Connected to " + source!.Url;
                Title = $"{dutName} - BooBoot Console";
                if (!connectedOnce && settings.LogOnConnect && !log.Active)
                    StartLog();
                connectedOnce = true;
                break;
            case ConsoleSource.Disconnected d:
                connection = "No connection, trying again: " + d.Message;
                break;
            case ConsoleSource.Output o:
                history = o.History;
                buffer.Feed(o.Text);
                history = false;
                break;
            case ConsoleSource.Power p:
                if (!p.History)
                {
                    power = p.On ? "on" : "off";
                    if (p.On && settings.NewLogAtPowerOn && log.Active)
                    {
                        // The unfinished line goes to the old file, the marker to the new one.
                        buffer.EndLine();
                        StartLog();
                    }
                }
                history = p.History;
                buffer.AddMarker(Marker(p.On ? "power on" : "power off", p.Time));
                history = false;
                break;
            case ConsoleSource.Lost l:
                buffer.AddMarker($"---- {l.Bytes} bytes lost ----");
                break;
            case ConsoleSource.Network n:
                var server = new Uri(source!.Url);
                alsoAt = n.Addresses.Count == 0 || n.Addresses.Contains(server.Host) ? "" : $"{n.Addresses[0]}:{server.Port}";
                // Kept for when the name of the server is not found, like a .local name over a VPN.
                if (server.HostNameType == UriHostNameType.Dns
                    && !(settings.Addresses.TryGetValue(source.Url, out var old) && old.SequenceEqual(n.Addresses)))
                {
                    settings.Addresses[source.Url] = n.Addresses.ToList();
                    SaveSettings();
                }
                break;
            case ConsoleSource.AddressUsed a:
                note = a.Note;
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

    void UpdateStatus()
    {
        var parts = new[]
        {
            connection,
            alsoAt != "" ? "also at " + alsoAt : "",
            dutName,
            power != "" ? "power " + power : "",
            session != "" ? "session " + session : "",
            $"{buffer.Count:N0} lines",
            input?.Token != null ? "in control" : note,
        };
        StatusText.Text = string.Join("    ", parts.Where(p => p != ""));
    }

    // Control

    /// <summary>Opens the session to type. When another client has it, asks before taking it over.</summary>
    public async Task TakeControl(bool force = false, bool ifGone = false)
    {
        if (input == null)
            return;
        try
        {
            if (await input.TakeAsync(force, ifGone) is JsonElement busy)
                AskTakeOver(busy);
            else
                SetControl(true, "");
        }
        catch (BooBootException e)
        {
            SetControl(false, "cannot take control: " + e.Message);
        }
    }

    public async Task ReleaseControl()
    {
        if (input == null)
            return;
        await input.ReleaseAsync();
        SetControl(false, "");
        session = "free";
        UpdateStatus();
    }

    static async Task Release(ConsoleInput old)
    {
        await old.ReleaseAsync();
        old.Dispose();
    }

    void AskTakeOver(JsonElement busy)
    {
        var who = busy.GetProperty("client").GetString();
        var idle = busy.GetProperty("idle").GetDouble();
        var gone = ConsoleInput.IsGone(busy);
        var connected = busy.TryGetProperty("alive", out var alive) && alive.ValueKind == JsonValueKind.True;
        var text = $"The DUT is used by {who}, " + (
            gone ? $"which is gone: no heartbeat for {busy.GetProperty("heartbeat_age").GetDouble():0} s."
            : connected ? $"which is connected. Last action {idle:0} s ago.\nTaking over interrupts their work."
            : $"idle for {idle:0} s.");
        var take = new Button { Content = connected ? "Take over anyway" : "Take over" };
        var cancel = new Button { Content = "Cancel" };
        var flyout = new Flyout
        {
            Content = new StackPanel
            {
                Spacing = 8,
                Children =
                {
                    new TextBlock { Text = text },
                    new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Children = { take, cancel } },
                },
            },
        };
        take.Click += async (_, _) =>
        {
            flyout.Hide();
            // Taken only if its client is still gone, else asked again.
            await TakeControl(force: !gone, ifGone: gone);
        };
        cancel.Click += (_, _) => flyout.Hide();
        flyout.ShowAt(ControlButton);
    }

    void SetControl(bool on, string text)
    {
        note = text;
        View.Typing = on;
        if (source != null)
            source.Token = on ? input?.Token : null;
        ControlButton.Content = on ? "Release control" : "Take control";
        ControlButton.Classes.Set("accent", on);
        if (on)
        {
            session = "yours";
            View.Focus();
        }
        UpdateStatus();
    }

    // Log

    string Prefix()
    {
        var prefix = settings.LogPrefix.Trim();
        if (prefix == "")
            prefix = dutName != "" ? dutName : "console";
        return string.Concat(prefix.Select(c => Path.GetInvalidFileNameChars().Contains(c) ? '_' : c));
    }

    void StartLog()
    {
        try
        {
            log.Start(settings.LogFolder, Prefix());
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or ArgumentException)
        {
            LogText.Text = "Cannot write the log: " + e.Message;
            return;
        }
        UpdateLogText();
    }

    void StopLog()
    {
        if (!log.Active)
            return;
        if (buffer.Pending.Length > 0)
            log.WriteLine(buffer.Pending);
        log.Stop();
        UpdateLogText();
    }

    void UpdateLogText()
    {
        LogText.Text = log.Active ? "Log: " + Path.GetFileName(log.Path) : "No log";
        ToolTip.SetTip(LogText, log.Active ? log.Path : null);
        StopLogButton.IsEnabled = log.Active;
    }

    // Commands

    void OnClear(object? sender, RoutedEventArgs e)
    {
        buffer.Clear();
        View.ClearSelection();
        View.Follow = true;
        View.Refresh();
        UpdateStatus();
    }

    void OnCopy(object? sender, RoutedEventArgs e) => View.CopySelection();

    void OnPaste(object? sender, RoutedEventArgs e) => View.Paste();

    void OnCopyAll(object? sender, RoutedEventArgs e)
    {
        View.SelectAll();
        View.CopySelection();
    }

    void OnSelectAll(object? sender, RoutedEventArgs e) => View.SelectAll();

    async void OnSave(object? sender, RoutedEventArgs e)
    {
        var file = await StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions
        {
            Title = "Save the console text",
            SuggestedFileName = $"{Prefix()}-{DateTime.Now:yyyyMMdd-HHmmss}.txt",
            DefaultExtension = "txt",
            FileTypeChoices = new[]
            {
                new FilePickerFileType("Text") { Patterns = new[] { "*.txt", "*.log" } },
                FilePickerFileTypes.All,
            },
        });
        if (file == null)
            return;
        try
        {
            await using var stream = await file.OpenWriteAsync();
            await using var writer = new StreamWriter(stream, new UTF8Encoding(false));
            await writer.WriteAsync(buffer.GetAllText(Environment.NewLine));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            LogText.Text = "Cannot save: " + ex.Message;
        }
    }

    protected override void OnKeyDown(KeyEventArgs e)
    {
        base.OnKeyDown(e);
        if (e.Handled || !(e.KeyModifiers.HasFlag(KeyModifiers.Control) || e.KeyModifiers.HasFlag(KeyModifiers.Meta)))
            return;
        if (e.Key == Key.L)
            OnClear(this, e);
        else if (e.Key == Key.S)
            OnSave(this, e);
        else
            return;
        e.Handled = true;
    }

    // Settings

    void ShowSettings()
    {
        LogFolderBox.Text = settings.LogFolder;
        PrefixBox.Text = settings.LogPrefix;
        LogOnConnectBox.IsChecked = settings.LogOnConnect;
        NewLogBox.IsChecked = settings.NewLogAtPowerOn;
        ScrollbackBox.Value = settings.ScrollbackLines;
        TextSizeBox.Value = (decimal)settings.TextSize;
        FontsBox.Text = settings.Fonts;
    }

    void OnSettingsClosed(object? sender, EventArgs e)
    {
        var folder = LogFolderBox.Text?.Trim();
        if (!string.IsNullOrEmpty(folder))
            settings.LogFolder = folder;
        settings.LogPrefix = PrefixBox.Text?.Trim() ?? "";
        settings.LogOnConnect = LogOnConnectBox.IsChecked == true;
        settings.NewLogAtPowerOn = NewLogBox.IsChecked == true;
        settings.ScrollbackLines = (int)(ScrollbackBox.Value ?? settings.ScrollbackLines);
        settings.TextSize = (double)(TextSizeBox.Value ?? (decimal)settings.TextSize);
        settings.Fonts = string.IsNullOrWhiteSpace(FontsBox.Text) ? TerminalView.DefaultFonts : FontsBox.Text.Trim();
        ShowSettings();
        buffer.MaxLines = settings.ScrollbackLines;
        View.TextSize = settings.TextSize;
        View.Fonts = settings.Fonts;
        SaveSettings();
    }

    async void OnBrowse(object? sender, RoutedEventArgs e)
    {
        var folders = await StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions
        {
            Title = "Log folder",
        });
        if (folders.Count > 0 && folders[0].TryGetLocalPath() is string path)
            LogFolderBox.Text = path;
    }

    async void OnOpenFolder(object? sender, RoutedEventArgs e)
    {
        try
        {
            var folder = Directory.CreateDirectory(LogFolderBox.Text?.Trim() ?? settings.LogFolder);
            await Launcher.LaunchDirectoryInfoAsync(folder);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException)
        {
            LogText.Text = "Cannot open the folder: " + ex.Message;
        }
    }

    void SaveSettings()
    {
        if (persist)
            settings.Save();
    }
}
