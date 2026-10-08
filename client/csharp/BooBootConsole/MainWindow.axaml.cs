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
using BooBoot;

namespace BooBootConsole;

public partial class MainWindow : Window
{
    static readonly Regex DutPath = new("/duts/[^/]+$");

    readonly Settings settings;
    readonly bool persist;
    // The DUTs shown, in the order of the tabs.
    readonly List<DutTab> tabs = new();
    readonly TerminalBuffer noBuffer = new(1);
    DutTab? current;
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
    // The label editor, while it shows.
    (Flyout Flyout, TextBox Text, Button Set, Button Cancel)? labelQuestion;

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
        FollowButton.IsCheckedChanged += (_, _) =>
        {
            if (current != null)
                current.View.Follow = FollowButton.IsChecked == true;
        };
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
        ClearButton.Click += OnClear;
        CopyButton.Click += OnCopy;
        SaveButton.Click += OnSave;
        StartLogButton.Click += (_, _) => current?.StartLog();
        StopLogButton.Click += (_, _) => current?.StopLog();
        BrowseButton.Click += OnBrowse;
        OpenFolderButton.Click += OnOpenFolder;
        ControlButton.Click += async (_, _) =>
        {
            if (current?.InControl == true)
                await current.ReleaseControl();
            else
                await TakeControl();
        };
        PowerButton.Click += async (_, _) =>
        {
            if (current == null)
                return;
            if (current.PowerState == "on")
                AskPowerOff(current);
            else
                await current.SwitchPower(true);
        };
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

    /// <summary>The power button, for tests.</summary>
    public Button Power => PowerButton;

    /// <summary>The current log file, for tests.</summary>
    public string? LogPath => current?.LogPath;

    public string Status => StatusText.Text ?? "";

    /// <summary>The DUTs shown, in the order of the tabs. For tests.</summary>
    public IReadOnlyList<DutTab> Duts => tabs;

    /// <summary>The DUT shown. For tests.</summary>
    public DutTab? CurrentDut => current;

    /// <summary>True when the tabs and the DUTs button show, for a board with several DUTs. For tests.</summary>
    public bool TabsShown => TabRow.IsVisible;

    // Connection

    void Connect()
    {
        Disconnect();
        var text = (UrlBox.Text ?? "").Trim();
        if (text.Length == 0)
            return;
        if (!text.Contains("://"))
            text = "http://" + text;
        if (!Uri.TryCreate(text, UriKind.Absolute, out _))
        {
            connection = "Bad server address: " + text;
            Refresh();
            return;
        }
        UrlBox.Text = text;
        settings.Url = text;
        SaveSettings();
        url = text.TrimEnd('/');
        board = DutPath.Replace(url, "");
        foreach (var tab in tabs)
            tab.Clear();
        connection = "Connecting to " + url;
        opening = new CancellationTokenSource();
        _ = Open(opening.Token);
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
        foreach (var tab in tabs.Where(t => !shown.Contains(t.Name)).ToList())
        {
            tab.Close();
            tabs.Remove(tab);
            Views.Children.Remove(tab.View);
        }
        var ordered = shown.Select(n => tabs.FirstOrDefault(t => t.Name == n) ?? Add(n)).ToList();
        changingTabs = true;
        tabs.Clear();
        tabs.AddRange(ordered);
        Tabs.Items.Clear();
        foreach (var tab in tabs)
        {
            Tabs.Items.Add(tab.Header);
            tab.Several = wholeBoard && names.Count > 1;
            if (!tab.Connected)
                tab.Connect(wholeBoard ? BooBootClient.DutUrl(board, tab.Name) : url, board);
        }
        changingTabs = false;
        TabRow.IsVisible = wholeBoard && names.Count > 1;
        var focused = FocusManager?.GetFocusedElement();
        Select(current != null && tabs.Contains(current) ? current : tabs.FirstOrDefault(t => t.Name == first) ?? tabs[0],
            focus: focused is null or TerminalView);
    }

    DutTab Add(string name)
    {
        var tab = new DutTab(name, settings, SaveSettings);
        tab.View.ContextMenu = ViewMenu();
        tab.Header.ContextMenu = LabelMenu(() => tab);
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
        Views.Children.Add(tab.View);
        return tab;
    }

    void Select(DutTab tab, bool focus)
    {
        if (current != null && current != tab)
            current.Current = false;
        current = tab;
        tab.Current = true;
        changingTabs = true;
        Tabs.SelectedItem = tab.Header;
        changingTabs = false;
        Refresh();
        if (focus)
            tab.View.Focus();
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

    /// <summary>Shows the status and the buttons of the current DUT.</summary>
    void Refresh()
    {
        var tab = current;
        var connected = tab?.Connected == true;
        StatusText.Text = connected ? tab!.Status : connection;
        Title = connected && tab!.Name != "" ? $"{tab.DisplayName} - BooBoot Console" : "BooBoot Console";
        ConnectButton.Content = opening != null ? "Disconnect" : "Connect";
        ControlButton.IsEnabled = connected;
        ControlButton.Content = tab?.InControl == true ? "Release control" : "Take control";
        ControlButton.Classes.Set("accent", tab?.InControl == true);
        // The power button follows the power state, and works only in control.
        PowerButton.Content = tab?.PowerState == "on" ? "Power off" : "Power on";
        PowerButton.IsEnabled = tab?.InControl == true && !tab.Switching;
        foreach (var button in new Control[] { ClearButton, CopyButton, SaveButton, FollowButton, StartLogButton })
            button.IsEnabled = tab != null;
        FollowButton.IsChecked = tab?.View.Follow ?? true;
        StopLogButton.IsEnabled = tab?.LogPath != null;
        LogText.Text = tab?.LogText ?? "No log";
        ToolTip.SetTip(LogText, tab?.LogPath);
    }

    // DUTs of the board

    void OnDutsOpening(object? sender, EventArgs e) => FillDutsList();

    void FillDutsList()
    {
        DutsList.Children.Clear();
        foreach (var name in boardDuts)
        {
            var shown = tabs.Any(t => t.Name == name);
            var box = new CheckBox
            {
                Content = name,
                IsChecked = shown,
                // One DUT at least stays shown.
                IsEnabled = !shown || tabs.Count > 1,
            };
            box.IsCheckedChanged += (_, _) =>
            {
                ShowDut(name, box.IsChecked == true);
                FillDutsList();
            };
            DutsList.Children.Add(box);
        }
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
        flyout.ShowAt(ControlButton);
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
        flyout.ShowAt(PowerButton);
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

    ContextMenu ViewMenu()
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
        menu.Items.Add(new Separator());
        AddLabelItem(menu, () => current);
        return menu;
    }

    ContextMenu LabelMenu(Func<DutTab?> tab)
    {
        var menu = new ContextMenu();
        AddLabelItem(menu, tab);
        return menu;
    }

    /// <summary>Adds "Set label..." to a menu, for the DUT that tab gives. It works only in control.</summary>
    void AddLabelItem(ContextMenu menu, Func<DutTab?> tab)
    {
        var item = new MenuItem();
        item.Click += (_, _) =>
        {
            if (tab() is DutTab t)
                AskLabel(t);
        };
        menu.Opening += (_, _) =>
        {
            var inControl = tab()?.InControl == true;
            item.Header = inControl ? "Set label..." : "Set label... (take control first)";
            item.IsEnabled = inControl;
        };
        menu.Items.Add(item);
    }

    /// <summary>Asks for the label of the DUT: free text shown with its name, kept on the server.</summary>
    public void AskLabel(DutTab tab)
    {
        var text = new TextBox { Text = tab.Label, Width = 320, PlaceholderText = "like ZCU102 rev B, bench 3" };
        var set = new Button { Content = "Set" };
        var cancel = new Button { Content = "Cancel" };
        var flyout = new Flyout
        {
            Placement = TabRow.IsVisible ? PlacementMode.BottomEdgeAlignedLeft : PlacementMode.TopEdgeAlignedLeft,
            Content = new StackPanel
            {
                Spacing = 8,
                Children =
                {
                    new TextBlock { Text = $"Label of {tab.Name}, shown with its name (empty: none)" },
                    text,
                    new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, Children = { set, cancel } },
                },
            },
        };
        set.Click += async (_, _) =>
        {
            flyout.Hide();
            await tab.SetLabel(text.Text ?? "");
        };
        cancel.Click += (_, _) => flyout.Hide();
        text.KeyDown += (_, e) =>
        {
            if (e.Key == Key.Enter)
                set.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        };
        labelQuestion = (flyout, text, set, cancel);
        flyout.ShowAt(TabRow.IsVisible ? tab.Header : StatusText);
        text.Focus();
    }

    /// <summary>True while the label editor shows. For tests.</summary>
    public bool AskingLabel => labelQuestion?.Flyout.IsOpen == true;

    /// <summary>Sets the label in the editor, or cancels with null. For tests.</summary>
    public void AnswerLabel(string? label)
    {
        if (labelQuestion is not var (_, text, set, cancel))
            return;
        if (label != null)
            text.Text = label;
        (label != null ? set : cancel).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
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
            if (current != null)
                current.ShowLogProblem("Cannot open the folder: " + ex.Message);
            else
                LogText.Text = "Cannot open the folder: " + ex.Message;
        }
    }

    void SaveSettings()
    {
        if (persist)
            settings.Save();
    }
}
