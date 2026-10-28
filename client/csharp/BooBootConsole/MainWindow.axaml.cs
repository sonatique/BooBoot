using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Interactivity;
using Avalonia.Platform.Storage;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using BooBoot;

namespace BooBootConsole;

public partial class MainWindow : Window
{
    static readonly Regex DutPath = new("/duts/[^/]+$");
    static readonly IBrush IdleBrush = new SolidColorBrush(Color.Parse("#808080"), 0.35);
    static readonly Color ActiveColor = Color.Parse("#2EA043");
    // Bytes per second of network traffic from which an arrow lights.
    const double Busy = 4096;

    readonly Settings settings;
    readonly bool persist;
    // The DUTs shown, in the order of the tabs.
    readonly List<DutTab> tabs = new();
    readonly TerminalBuffer noBuffer = new(1);
    DutTab? current;
    // The address given at start, like on the command line: not kept as the last address.
    readonly string? given;
    // The address given, and the one of its board: the same, unless it is the address of one DUT.
    string url = "", board = "";
    // The tabs follow the DUTs of the board, and not one DUT.
    bool wholeBoard;
    List<string> boardDuts = new();
    // Shown while no DUT is.
    string connection = "Not connected";
    // Set from Connect to Disconnect.
    CancellationTokenSource? opening;
    bool closing;
    // True while the tabs change, to ignore the selection changes it makes.
    bool changingTabs;
    // The question before a power off, with its answers, while it shows.
    (Flyout Flyout, Button Yes, Button No)? powerOffQuestion;

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
        given = url == null ? null : Address(url);
        UrlBox.Text = url ?? settings.Url;
        UrlBox.KeyDown += (_, e) =>
        {
            if (e.Key == Key.Enter)
                Connect();
        };
        ConnectButton.Click += (_, _) =>
        {
            if (opening == null)
                Connect();
            else
                Disconnect();
        };
        Tabs.SelectionChanged += (_, _) =>
        {
            if (!changingTabs && tabs.FirstOrDefault(t => t.Header == Tabs.SelectedItem) is DutTab tab)
                Select(tab, focus: true);
        };
        // Before the console view, which sends Tab to the DUT.
        AddHandler(KeyDownEvent, (_, e) =>
        {
            if (e.Key == Key.Tab && e.KeyModifiers.HasFlag(KeyModifiers.Control) && current != null && tabs.Count > 1)
            {
                var next = tabs.IndexOf(current) + (e.KeyModifiers.HasFlag(KeyModifiers.Shift) ? -1 : 1);
                Select(tabs[(next + tabs.Count) % tabs.Count], focus: true);
                e.Handled = true;
            }
        }, RoutingStrategies.Tunnel);
        TabsItem.Click += (_, _) => SetLayout("tabs");
        ColumnsItem.Click += (_, _) => SetLayout("columns");
        RowsItem.Click += (_, _) => SetLayout("rows");
        ShowLayout();
        SetLabelButton.Click += async (_, _) => await SetLabelFromPanel();
        LabelBox.KeyDown += async (_, e) =>
        {
            if (e.Key == Key.Enter && SetLabelButton.IsEnabled)
                await SetLabelFromPanel();
        };
        BrowseButton.Click += OnBrowse;
        OpenFolderButton.Click += OnOpenFolder;
        ShowSettings();
        Refresh();
        Opened += (_, _) => Connect();
        Closing += async (_, e) =>
        {
            // Release the sessions before closing.
            var held = tabs.Where(t => t.InControl).ToList();
            if (held.Count > 0 && !closing)
            {
                e.Cancel = true;
                closing = true;
                await Task.WhenAll(held.Select(t => t.ReleaseControl()));
                Close();
                return;
            }
            Disconnect();
            foreach (var tab in tabs)
                tab.StopLog();
        };
    }

    /// <summary>The text buffer of the current DUT, for tests.</summary>
    public TerminalBuffer Buffer => current?.Buffer ?? noBuffer;

    public TerminalView Terminal => current?.View ?? throw new InvalidOperationException("no DUT shown");

    /// <summary>The power button of the current DUT, for tests.</summary>
    public Button Power => current?.PowerButton ?? throw new InvalidOperationException("no DUT shown");

    /// <summary>The current log file, for tests.</summary>
    public string? LogPath => current?.LogPath;

    /// <summary>The state of the board, then the name and state of the current DUT. For tests.</summary>
    public string Status => BoardText.Text + (current != null ? "    " + current.Status : "");

    /// <summary>The DUTs shown, in the order of the tabs. For tests.</summary>
    public IReadOnlyList<DutTab> Duts => tabs.Where(t => !t.IsBoardLog).ToList();

    /// <summary>The BooBoot log, when shown. For tests.</summary>
    public DutTab? BoardLog => tabs.FirstOrDefault(t => t.IsBoardLog);

    /// <summary>The DUT shown. For tests.</summary>
    public DutTab? CurrentDut => current;

    /// <summary>True when the DUTs and Layout buttons show, for a board with several DUTs. For tests.</summary>
    public bool TabsShown => DutsButton.IsVisible;

    /// <summary>True when the tab strip shows: several DUTs, one at a time. For tests.</summary>
    public bool TabStripShown => Tabs.IsVisible;

    // Connection

    static string Address(string text)
    {
        text = text.Trim();
        return text.Length == 0 || text.Contains("://") ? text : "http://" + text;
    }

    void Connect()
    {
        Disconnect();
        var text = Address(UrlBox.Text ?? "");
        if (text.Length == 0)
            return;
        if (!Uri.TryCreate(text, UriKind.Absolute, out _))
        {
            connection = "Bad server address: " + text;
            Refresh();
            return;
        }
        UrlBox.Text = text;
        // A window for one DUT, opened with its address, leaves the address of the next start as it is.
        if (text != given)
        {
            settings.Url = text;
            SaveSettings();
        }
        url = text.TrimEnd('/');
        board = DutPath.Replace(url, "");
        foreach (var tab in tabs)
            tab.Clear();
        connection = "Connecting to " + url;
        opening = new CancellationTokenSource();
        _ = Open(opening.Token);
        _ = WatchActivity(opening.Token);
        Refresh();
    }

    /// <summary>Finds the DUTs at the address, then shows them.</summary>
    async Task Open(CancellationToken stop)
    {
        var known = settings.Addresses.TryGetValue(board, out var list) ? list : new List<string>();
        using var client = new BooBootClient(url, TimeSpan.FromSeconds(10)) { Addresses = known.ToList() };
        while (true)
        {
            try
            {
                var status = await client.StatusAsync();
                if (stop.IsCancellationRequested)
                    return;
                var name = status.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                // A server older than several DUTs per board has no list.
                var hasList = status.TryGetProperty("duts", out var duts);
                wholeBoard = url == board && hasList;
                ShowDuts(wholeBoard ? duts.EnumerateArray().Select(d => d.GetString() ?? "").ToList()
                    : new List<string> { name }, name);
                return;
            }
            catch (BooBootException e)
            {
                if (stop.IsCancellationRequested)
                    return;
                connection = "No connection, trying again: " + e.Message;
                Refresh();
            }
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(2), stop);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }

    /// <summary>Shows a tab for each DUT but the hidden ones, and connects them.</summary>
    /// <param name="first">The DUT to show first.</param>
    void ShowDuts(IReadOnlyList<string> names, string? first = null)
    {
        if (names.Count == 0)
            return;
        boardDuts = names.ToList();
        var hidden = wholeBoard && settings.HiddenDuts.TryGetValue(board, out var h) ? h : new List<string>();
        var shown = names.Where(n => !hidden.Contains(n)).ToList();
        if (shown.Count == 0)
            shown = names.ToList();
        var log = wholeBoard && settings.BoardLogs.Contains(board);
        foreach (var tab in tabs.Where(t => t.IsBoardLog ? !log : !shown.Contains(t.Name)).ToList())
        {
            tab.Close();
            tabs.Remove(tab);
        }
        var ordered = shown.Select(n => tabs.FirstOrDefault(t => !t.IsBoardLog && t.Name == n) ?? Add(n)).ToList();
        if (log)
            ordered.Add(tabs.FirstOrDefault(t => t.IsBoardLog) ?? Add(DutTab.BoardLogName, boardLog: true));
        changingTabs = true;
        tabs.Clear();
        tabs.AddRange(ordered);
        Tabs.Items.Clear();
        foreach (var tab in tabs)
        {
            Tabs.Items.Add(tab.Header);
            tab.Several = wholeBoard && names.Count > 1;
            if (!tab.Connected)
                tab.Connect(tab.IsBoardLog ? board : wholeBoard ? BooBootClient.DutUrl(board, tab.Name) : url, board);
        }
        changingTabs = false;
        DutsButton.IsVisible = wholeBoard;
        LayoutButton.IsVisible = tabs.Count > 1;
        Arrange();
        var focused = FocusManager?.GetFocusedElement();
        Select(current != null && tabs.Contains(current) ? current : tabs.FirstOrDefault(t => t.Name == first) ?? tabs[0],
            focus: focused is null or TerminalView);
    }

    DutTab Add(string name, bool boardLog = false)
    {
        var tab = new DutTab(name, settings, SaveSettings, boardLog);
        tab.View.ContextMenu = ViewMenu(!boardLog);
        if (!boardLog)
            tab.Header.ContextMenu = DutMenu(() =>
            {
                Select(tab, focus: true);
                OpenDutPanel();
            });
        tab.Changed += () =>
        {
            if (tab == current)
                Refresh();
        };
        tab.BoardChanged += names =>
        {
            if (wholeBoard && tabs.Contains(tab))
                ShowDuts(names);
        };
        // With several DUTs at once, the one clicked or typed into becomes the current one.
        tab.View.GotFocus += (_, _) =>
        {
            if (tab != current && tabs.Contains(tab))
                Select(tab, focus: false);
        };
        tab.View.ContextRequested += (_, _) =>
        {
            if (tab != current && tabs.Contains(tab))
                Select(tab, focus: true);
        };
        tab.Bar.PointerPressed += (_, _) => Select(tab, focus: true);
        if (!boardLog)
            tab.Bar.ContextMenu = DutMenu(() =>
            {
                Select(tab, focus: true);
                OpenDutPanel();
            });
        tab.ControlButton.Click += async (_, _) =>
        {
            Select(tab, focus: false);
            if (tab.InControl)
                await tab.ReleaseControl();
            else
                await TakeControl(tab, false, false);
        };
        tab.PowerButton.Click += async (_, _) =>
        {
            Select(tab, focus: false);
            if (tab.PowerState == "on")
                AskPowerOff(tab);
            else
                await tab.SwitchPower(true);
        };
        tab.PropertiesButton.Click += (_, _) =>
        {
            Select(tab, focus: false);
            OpenDutPanel();
        };
        tab.LogFolderItem.Click += OnOpenFolder;
        return tab;
    }

    void Select(DutTab tab, bool focus)
    {
        if (current != null && current != tab)
            current.Current = false;
        current = tab;
        tab.Current = true;
        ShowViews();
        changingTabs = true;
        Tabs.SelectedItem = tab.Header;
        changingTabs = false;
        Refresh();
        if (focus)
            tab.View.Focus();
    }

    // Layout

    /// <summary>True when the DUTs show all at once, side by side or top and bottom.</summary>
    bool AllAtOnce => settings.Layout is "columns" or "rows" && tabs.Count > 1;

    /// <summary>Shows the DUTs in tabs ("tabs"), side by side ("columns") or top and bottom ("rows").</summary>
    public void SetLayout(string layout)
    {
        settings.Layout = layout;
        SaveSettings();
        ShowLayout();
        Arrange();
        current?.View.Focus();
    }

    void ShowLayout()
    {
        TabsItem.IsChecked = settings.Layout is not ("columns" or "rows");
        ColumnsItem.IsChecked = settings.Layout == "columns";
        RowsItem.IsChecked = settings.Layout == "rows";
    }

    /// <summary>Places the views: in the same cell, one shown at a time, or each in its own, with splitters between.</summary>
    void Arrange()
    {
        var allAtOnce = AllAtOnce;
        var columns = settings.Layout == "columns";
        Views.Children.Clear();
        Views.ColumnDefinitions.Clear();
        Views.RowDefinitions.Clear();

        void Place(Control control, GridLength size, double min)
        {
            if (columns)
                Views.ColumnDefinitions.Add(new ColumnDefinition(size) { MinWidth = min });
            else
                Views.RowDefinitions.Add(new RowDefinition(size) { MinHeight = min });
            Grid.SetColumn(control, columns ? Views.ColumnDefinitions.Count - 1 : 0);
            Grid.SetRow(control, columns ? 0 : Views.RowDefinitions.Count - 1);
            Views.Children.Add(control);
        }

        // The panes have their bars in place of the tabs.
        Tabs.IsVisible = tabs.Count > 1 && !allAtOnce;
        Views.Classes.Set("split", allAtOnce);
        foreach (var tab in tabs)
        {
            if (!allAtOnce)
            {
                Grid.SetColumn(tab.Pane, 0);
                Grid.SetRow(tab.Pane, 0);
                Views.Children.Add(tab.Pane);
                continue;
            }
            if (tab != tabs[0])
            {
                var splitter = new GridSplitter
                {
                    ResizeDirection = columns ? GridResizeDirection.Columns : GridResizeDirection.Rows,
                };
                splitter.Classes.Add("panes");
                if (columns)
                    splitter.Width = 5;
                else
                    splitter.Height = 5;
                Place(splitter, GridLength.Auto, 0);
            }
            Place(tab.Pane, new GridLength(1, GridUnitType.Star), 60);
        }
        ShowViews();
    }

    void ShowViews()
    {
        var allAtOnce = AllAtOnce;
        foreach (var tab in tabs)
            tab.Shown = allAtOnce || tab == current;
    }

    /// <summary>Selects the tab of a DUT. For tests.</summary>
    public void SelectDut(DutTab tab) => Select(tab, focus: true);

    void Disconnect()
    {
        opening?.Cancel();
        opening = null;
        foreach (var tab in tabs)
            tab.Disconnect();
        connection = "Not connected";
        Refresh();
    }

    /// <summary>Shows the state of the board, as the current DUT reaches it, and the name of that DUT in the title.</summary>
    void Refresh()
    {
        var tab = current;
        var connected = tab?.Connected == true;
        BoardText.Text = connected ? tab!.BoardState : connection;
        Title = connected && tab!.Name != "" ? $"{tab.DisplayName} - BooBoot Console" : "BooBoot Console";
        ConnectButton.Content = opening != null ? "Disconnect" : "Connect";
        ToolTip.SetTip(ConnectButton, opening != null ? "Stop watching the DUTs of this board, in this window"
            : "Watch the DUTs of the board at this address");
        ShowDutPanel(false);
    }

    // DUT panel: the DUT on the board, the same for everyone

    void OnDutPanelOpening(object? sender, EventArgs e) => ShowDutPanel(true);

    void OnDutPanelOpened(object? sender, EventArgs e)
    {
        if (SetLabelButton.IsEnabled)
            LabelBox.Focus();
    }

    /// <summary>Shows the current DUT in the panel. opening: also its label, which is not changed while typed.</summary>
    void ShowDutPanel(bool opening)
    {
        var tab = current;
        DutNameBox.Text = tab?.Name ?? "";
        DutAddressBox.Text = tab?.Url ?? "";
        var inControl = tab?.InControl == true;
        if (opening)
        {
            LabelBox.Text = tab?.Label ?? "";
            LabelNote.Text = "";
        }
        LabelBox.IsReadOnly = !inControl;
        SetLabelButton.IsEnabled = inControl;
        if (!inControl)
            LabelNote.Text = "Take control to change it";
        else if (LabelNote.Text == "Take control to change it")
            LabelNote.Text = "";
        var details = tab?.Details;
        DutConsoleText.Text = details?.Console ?? "";
        // The power state of the stream, which is newer than the one of the details.
        DutPowerText.Text = details == null ? "" : details.Power + (details.Relay && tab!.PowerState != "" ? ", " + tab.PowerState : "");
        DutSdText.Text = details?.Sd ?? "";
        DutServerText.Text = details?.Server ?? "";
    }

    /// <summary>Sets the label of the current DUT to the text of the panel. For tests: with text.</summary>
    public async Task SetLabelFromPanel(string? text = null)
    {
        if (current is not DutTab tab)
            return;
        if (text != null)
            LabelBox.Text = text;
        var ok = await tab.SetLabel(LabelBox.Text ?? "");
        LabelNote.Text = !ok ? tab.Note : tab.Label != "" ? "Label set" : "Label removed";
    }

    /// <summary>Opens the DUT panel of the current DUT, at its Properties button.</summary>
    public void OpenDutPanel()
    {
        if (current != null)
            FlyoutBase.GetAttachedFlyout(Views)?.ShowAt(current.PropertiesButton);
    }

    public void CloseDutPanel() => FlyoutBase.GetAttachedFlyout(Views)?.Hide();

    /// <summary>True while the DUT panel shows. For tests.</summary>
    public bool DutPanelOpen => FlyoutBase.GetAttachedFlyout(Views)?.IsOpen == true;

    /// <summary>The texts of the DUT panel, one per line. For tests.</summary>
    public string DutPanelText => string.Join("\n", DutNameBox.Text, DutAddressBox.Text, LabelBox.Text, LabelNote.Text,
        DutConsoleText.Text, DutPowerText.Text, DutSdText.Text, DutServerText.Text);

    /// <summary>True when the panel can set the label: in control. For tests.</summary>
    public bool CanSetLabel => SetLabelButton.IsEnabled;

    /// <summary>The items of the Log file menu of the current DUT: start, stop, open folder. For tests.</summary>
    public IReadOnlyList<MenuItem> LogMenuItems => current is DutTab tab
        ? new[] { tab.StartLogItem, tab.StopLogItem, tab.LogFolderItem } : Array.Empty<MenuItem>();

    // DUTs of the board

    void OnDutsOpening(object? sender, EventArgs e) => FillDutsList();

    void FillDutsList()
    {
        DutsList.Children.Clear();
        var duts = Duts;
        foreach (var name in boardDuts)
        {
            var shown = duts.Any(t => t.Name == name);
            var box = new CheckBox
            {
                Content = name,
                IsChecked = shown,
                // One DUT at least stays shown.
                IsEnabled = !shown || duts.Count > 1,
            };
            box.IsCheckedChanged += (_, _) =>
            {
                ShowDut(name, box.IsChecked == true);
                FillDutsList();
            };
            DutsList.Children.Add(box);
        }
        var log = new CheckBox
        {
            Content = DutTab.BoardLogName,
            IsChecked = BoardLog != null,
            [ToolTip.TipProperty] = "The messages of the BooBoot server: what it does, and its problems",
        };
        log.IsCheckedChanged += (_, _) =>
        {
            ShowBoardLog(log.IsChecked == true);
            FillDutsList();
        };
        DutsList.Children.Add(new Separator());
        DutsList.Children.Add(log);
    }

    /// <summary>Shows or hides the BooBoot log, the messages of the server of the board, and remembers it for the board.</summary>
    public void ShowBoardLog(bool shown)
    {
        if (!wholeBoard)
            return;
        settings.BoardLogs.Remove(board);
        if (shown)
            settings.BoardLogs.Add(board);
        SaveSettings();
        ShowDuts(boardDuts);
    }

    /// <summary>Shows or hides the tab of a DUT of the board, and remembers it for the board.</summary>
    public void ShowDut(string name, bool shown)
    {
        if (!wholeBoard)
            return;
        var hidden = settings.HiddenDuts.TryGetValue(board, out var h) ? h.ToList() : new List<string>();
        hidden.Remove(name);
        if (!shown)
            hidden.Add(name);
        if (boardDuts.All(hidden.Contains))
            return;
        if (hidden.Count > 0)
            settings.HiddenDuts[board] = hidden;
        else
            settings.HiddenDuts.Remove(board);
        SaveSettings();
        ShowDuts(boardDuts);
    }

    // Activity of the board

    /// <summary>Shows the load of the board every second: traffic to and from it, CPU use, disk space.</summary>
    async Task WatchActivity(CancellationToken stop)
    {
        var known = settings.Addresses.TryGetValue(board, out var list) ? list : new List<string>();
        using var client = new BooBootClient(board, TimeSpan.FromSeconds(5)) { Addresses = known.ToList() };
        while (!stop.IsCancellationRequested)
        {
            try
            {
                var activity = await client.ActivityAsync();
                if (!stop.IsCancellationRequested)
                    ShowActivity(activity);
            }
            catch (Exception e)
            {
                // A server older than this measure does not have it.
                if (e is BooBootException { Status: 404 })
                    break;
                if (!stop.IsCancellationRequested)
                    ShowActivity(null);
            }
            try
            {
                await Task.Delay(1000, stop);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
        if (!stop.IsCancellationRequested)
            ActivityPanel.IsVisible = false;
    }

    void ShowActivity(JsonElement? activity)
    {
        static double? Number(JsonElement e, string name) => e.ValueKind == JsonValueKind.Object
            && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;
        var a = activity ?? default;
        var traffic = a.ValueKind == JsonValueKind.Object && a.TryGetProperty("traffic", out var t) ? t : default;
        var (cpu, received, sent) = (Number(a, "cpu"), Number(traffic, "in"), Number(traffic, "out"));
        var temperature = Number(a, "temperature");
        var disk = a.ValueKind == JsonValueKind.Object && a.TryGetProperty("disk", out var k) ? k : default;
        var (total, free) = (Number(disk, "total") ?? 0, Number(disk, "free") ?? -1);
        double? used = total > 0 && free >= 0 ? Math.Clamp(1 - free / total, 0, 1) : null;
        ActivityPanel.IsVisible = true;
        // A server older than the disk measure does not have it.
        if (activity != null)
            DiskPanel.IsVisible = used != null;
        CpuBar.Width = 40 * Math.Clamp((cpu ?? 0) / 100, 0, 1);
        CpuBar.Background = new SolidColorBrush(LoadColor(cpu ?? 0));
        // Green while half of the disk at least is free.
        DiskBar.Width = 40 * (used ?? 0);
        DiskBar.Background = new SolidColorBrush(LoadColor(200 * Math.Max((used ?? 0) - 0.5, 0)));
        Blink(UpArrow, received);
        Blink(DownArrow, sent);
        ToolTip.SetTip(ActivityPanel, activity == null ? "No answer from the board" : string.Join("\n", new[]
        {
            "BooBoot board",
            "CPU " + (cpu is double c ? $"{c:0}%" : "unknown") + (temperature is double d ? $", {d:0.0} \u00b0C" : ""),
            "To the board: " + Rate(received),
            "From the board: " + Rate(sent),
            used is double u ? $"Disk: {Size(free)} free of {Size(total)}, {100 * u:0}% used" : "",
        }.Where(line => line != "")));
    }

    /// <summary>Lights an arrow for a moment when the traffic is busy: brighter with more traffic.</summary>
    static void Blink(Avalonia.Controls.Shapes.Path arrow, double? rate)
    {
        if (rate is not double r || r < Busy)
        {
            arrow.Fill = IdleBrush;
            return;
        }
        var level = Math.Clamp(Math.Log10(r / Busy) / Math.Log10(256), 0, 1);
        arrow.Fill = new SolidColorBrush(ActiveColor, 0.5 + 0.5 * level);
        DispatcherTimer.RunOnce(() => arrow.Fill = IdleBrush, TimeSpan.FromMilliseconds(350));
    }

    /// <summary>Green when idle, yellow at half load, red when full.</summary>
    static Color LoadColor(double percent)
    {
        Color Mix(Color a, Color b, double f) => Color.FromRgb((byte)(a.R + (b.R - a.R) * f),
            (byte)(a.G + (b.G - a.G) * f), (byte)(a.B + (b.B - a.B) * f));
        var (green, yellow, red) = (Color.Parse("#2EA043"), Color.Parse("#D29922"), Color.Parse("#F85149"));
        var f = Math.Clamp(percent / 100, 0, 1);
        return f < 0.5 ? Mix(green, yellow, f * 2) : Mix(yellow, red, (f - 0.5) * 2);
    }

    static string Rate(double? bytes) => bytes switch
    {
        null => "unknown",
        < 1024 => $"{bytes:0} B/s",
        < 1 << 20 => $"{bytes / 1024:0.0} KB/s",
        _ => $"{bytes / (1 << 20):0.0} MB/s",
    };

    static string Size(double bytes) => bytes < 1 << 30 ? $"{bytes / (1 << 20):0} MB" : $"{bytes / (1 << 30):0.0} GB";

    /// <summary>True when the load of the board shows. For tests.</summary>
    public bool ActivityShown => ActivityPanel.IsVisible;

    /// <summary>The details of the load of the board, as its tooltip shows them. For tests.</summary>
    public string ActivityText => ToolTip.GetTip(ActivityPanel) as string ?? "";

    // Control

    /// <summary>Opens the session of the current DUT to type. When another client has it, asks before taking it over.</summary>
    public Task TakeControl(bool force = false, bool ifGone = false) =>
        current == null ? Task.CompletedTask : TakeControl(current, force, ifGone);

    async Task TakeControl(DutTab tab, bool force, bool ifGone)
    {
        if (await tab.TakeControl(force, ifGone) is JsonElement busy && tab == current)
            AskTakeOver(tab, busy);
    }

    public Task ReleaseControl() => current?.ReleaseControl() ?? Task.CompletedTask;

    void AskTakeOver(DutTab tab, JsonElement busy)
    {
        var who = busy.GetProperty("client").GetString();
        var idle = busy.GetProperty("idle").GetDouble();
        var gone = ConsoleInput.IsGone(busy);
        var connected = busy.TryGetProperty("alive", out var alive) && alive.ValueKind == JsonValueKind.True;
        var text = $"{tab.DisplayName} is used by {who}, " + (
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
            await TakeControl(tab, force: !gone, ifGone: gone);
        };
        cancel.Click += (_, _) => flyout.Hide();
        flyout.ShowAt(tab.ControlButton);
    }

    /// <summary>Switches the power of the current DUT, in control.</summary>
    public Task SwitchPower(bool on) => current?.SwitchPower(on) ?? Task.CompletedTask;

    void AskPowerOff(DutTab tab)
    {
        var yes = new Button { Content = "Power off" };
        var no = new Button { Content = "Cancel" };
        var flyout = new Flyout
        {
            Content = new StackPanel
            {
                Spacing = 8,
                Children =
                {
                    new TextBlock { Text = $"Switch {tab.DisplayName} off?" },
                    new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Children = { yes, no } },
                },
            },
        };
        yes.Click += async (_, _) =>
        {
            flyout.Hide();
            await tab.SwitchPower(false);
        };
        no.Click += (_, _) => flyout.Hide();
        powerOffQuestion = (flyout, yes, no);
        flyout.ShowAt(tab.PowerButton);
    }

    /// <summary>True while the question before a power off shows. For tests.</summary>
    public bool AskingPowerOff => powerOffQuestion?.Flyout.IsOpen == true;

    /// <summary>Answers the question before a power off. For tests.</summary>
    public void AnswerPowerOff(bool yes)
    {
        if (powerOffQuestion is var (_, yesButton, noButton))
            (yes ? yesButton : noButton).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
    }

    // Commands

    ContextMenu ViewMenu(bool dut)
    {
        var menu = new ContextMenu();
        foreach (var (header, gesture, click) in new (string, string?, EventHandler<RoutedEventArgs>?)[]
                 {
                     ("Copy", "Ctrl+C", OnCopy),
                     ("Paste", "Ctrl+V", OnPaste),
                     ("Copy all", null, OnCopyAll),
                     ("Select all", "Ctrl+A", OnSelectAll),
                     ("-", null, null),
                     ("Clear", "Ctrl+L", OnClear),
                     ("Save...", "Ctrl+S", OnSave),
                 })
        {
            if (click == null)
            {
                menu.Items.Add(new Separator());
                continue;
            }
            var item = new MenuItem { Header = header, InputGesture = gesture != null ? KeyGesture.Parse(gesture) : null };
            item.Click += click;
            menu.Items.Add(item);
        }
        if (dut)
        {
            menu.Items.Add(new Separator());
            menu.Items.Add(DutItem(OpenDutPanel));
        }
        return menu;
    }

    ContextMenu DutMenu(Action open) => new() { Items = { DutItem(open) } };

    static MenuItem DutItem(Action open)
    {
        var item = new MenuItem { Header = "Properties..." };
        item.Click += (_, _) => open();
        return item;
    }

    void OnClear(object? sender, RoutedEventArgs e) => current?.Clear();

    void OnCopy(object? sender, RoutedEventArgs e) => current?.View.CopySelection();

    void OnPaste(object? sender, RoutedEventArgs e) => current?.View.Paste();

    void OnCopyAll(object? sender, RoutedEventArgs e)
    {
        current?.View.SelectAll();
        current?.View.CopySelection();
    }

    void OnSelectAll(object? sender, RoutedEventArgs e) => current?.View.SelectAll();

    async void OnSave(object? sender, RoutedEventArgs e)
    {
        if (current is not DutTab tab)
            return;
        var file = await StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions
        {
            Title = "Save the console text",
            SuggestedFileName = $"{tab.Prefix()}-{DateTime.Now:yyyyMMdd-HHmmss}.txt",
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
            await writer.WriteAsync(tab.Buffer.GetAllText(Environment.NewLine));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            tab.ShowLogProblem("Cannot save: " + ex.Message);
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
        foreach (var tab in tabs)
            tab.ApplySettings();
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
            current?.ShowLogProblem("Cannot open the folder: " + ex.Message);
        }
    }

    void SaveSettings()
    {
        if (persist)
            settings.Save();
    }
}
