using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Input.Platform;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Immutable;

namespace BooBootConsole;

/// <summary>
/// Shows a TerminalBuffer. Long lines wrap. Only the visible rows are drawn,
/// so the buffer can be large. The view follows new output unless the user
/// scrolls up. While Typing is on, keys are raised as Input, as a terminal
/// would send them, and the cursor is shown.
/// </summary>
public sealed class TerminalView : Control
{
    const double Pad = 4;
    public const string DefaultFonts = "Cascadia Mono, Consolas, Menlo, DejaVu Sans Mono, Liberation Mono, monospace";

    static readonly Color Background = Color.Parse("#1E1E1E");
    static readonly Color Foreground = Color.Parse("#CCCCCC");
    static readonly IBrush BackgroundBrush = new ImmutableSolidColorBrush(Background);
    static readonly IBrush SelectionBrush = new ImmutableSolidColorBrush(Color.Parse("#264F78"));
    static readonly IBrush MarkerBrush = new ImmutableSolidColorBrush(Color.Parse("#569CD6"));
    static readonly IBrush MarkerBackground = new ImmutableSolidColorBrush(Color.Parse("#252A33"));
    static readonly IBrush CursorBrush = new ImmutableSolidColorBrush(Color.Parse("#99CCCCCC"));
    static readonly IPen CursorPen = new ImmutablePen(new ImmutableSolidColorBrush(Color.Parse("#CCCCCC")));
    static readonly IPen TypingPen = new ImmutablePen(new ImmutableSolidColorBrush(Color.Parse("#0E639C")), 2);
    static readonly Color[] Basic = Array.ConvertAll(new[]
    {
        "#000000", "#CD3131", "#0DBC79", "#E5E510", "#2472C8", "#BC3FBC", "#11A8CD", "#E5E5E5",
        "#666666", "#F14C4C", "#23D18B", "#F5F543", "#3B8EEA", "#D670D6", "#29B8DB", "#FFFFFF",
    }, Color.Parse);

    readonly ScrollBar bar = new() { Orientation = Orientation.Vertical, AllowAutoHide = false };
    readonly Dictionary<int, IBrush> brushes = new();
    Dictionary<(TermLine, int), RowText> rowCache = new();
    TerminalBuffer buffer = new();
    Typeface typeface;
    FontFamily fontFamily = new(DefaultFonts);
    double fontSize = 13;
    double cellWidth, rowHeight;
    long topLine;
    int topRow;
    bool follow = true;
    TextPos? anchor, caret;
    bool selecting;
    bool typing;
    double wheel;

    record RowText(int Version, int Columns, FormattedText Text);

    public TerminalView()
    {
        Focusable = true;
        ClipToBounds = true;
        Cursor = new Cursor(StandardCursorType.Ibeam);
        VisualChildren.Add(bar);
        LogicalChildren.Add(bar);
        bar.Scroll += (_, e) => ScrollToLine((long)Math.Round(e.NewValue), e.NewValue >= bar.Maximum);
        UpdateMetrics();
    }

    public TerminalBuffer Buffer
    {
        get => buffer;
        set
        {
            buffer = value;
            anchor = caret = null;
            rowCache.Clear();
            (topLine, topRow) = (buffer.First, 0);
            Follow = true;
            Refresh();
        }
    }

    public double TextSize
    {
        get => fontSize;
        set
        {
            fontSize = Math.Clamp(value, 6, 48);
            UpdateMetrics();
            Refresh();
        }
    }

    public string Fonts
    {
        get => fontFamily.ToString();
        set
        {
            fontFamily = new FontFamily(string.IsNullOrWhiteSpace(value) ? DefaultFonts : value);
            UpdateMetrics();
            Refresh();
        }
    }

    /// <summary>True while the view shows the end of the output and follows it.</summary>
    public bool Follow
    {
        get => follow;
        set
        {
            if (follow == value)
                return;
            follow = value;
            FollowChanged?.Invoke(this, EventArgs.Empty);
            Refresh();
        }
    }

    public event EventHandler? FollowChanged;

    /// <summary>Keys go to Input, and the cursor is shown.</summary>
    public bool Typing
    {
        get => typing;
        set
        {
            typing = value;
            InvalidateVisual();
        }
    }

    /// <summary>Text to send to the console, while Typing.</summary>
    public event Action<string>? Input;

    /// <summary>Text was typed while not Typing.</summary>
    public event EventHandler? ReadOnlyKey;

    public bool HasSelection => anchor != null && caret != null && anchor != caret;

    /// <summary>Call after the buffer changed.</summary>
    public void Refresh()
    {
        if (topLine < buffer.First)
            (topLine, topRow) = (buffer.First, 0);
        topRow = Math.Min(topRow, Rows(topLine) - 1);
        if (follow && !selecting)
            (topLine, topRow) = BottomTop();
        UpdateBar();
        InvalidateVisual();
    }

    public void SelectAll()
    {
        anchor = new TextPos(buffer.First, 0);
        caret = new TextPos(buffer.Last, int.MaxValue);
        InvalidateVisual();
    }

    public void ClearSelection()
    {
        anchor = caret = null;
        InvalidateVisual();
    }

    public string SelectedText =>
        HasSelection ? buffer.GetText(anchor!.Value, caret!.Value, Environment.NewLine) : "";

    public async void CopySelection()
    {
        var clipboard = TopLevel.GetTopLevel(this)?.Clipboard;
        if (clipboard != null && HasSelection)
            await clipboard.SetTextAsync(SelectedText);
    }

    /// <summary>Sends the clipboard text, with line breaks as Enter.</summary>
    public async void Paste()
    {
        var clipboard = TopLevel.GetTopLevel(this)?.Clipboard;
        if (clipboard == null)
            return;
        var text = await clipboard.TryGetTextAsync();
        if (string.IsNullOrEmpty(text))
            return;
        if (typing)
            Send(text.Replace("\r\n", "\r").Replace('\n', '\r'));
        else
            ReadOnlyKey?.Invoke(this, EventArgs.Empty);
    }

    void Send(string text)
    {
        Follow = true;
        Input?.Invoke(text);
    }
    // Layout

    int Columns => Math.Max(10, (int)((Bounds.Width - bar.Bounds.Width - 2 * Pad) / cellWidth));

    int VisibleRows => Math.Max(1, (int)((Bounds.Height - 2 * Pad) / rowHeight));

    int Rows(long line) => Math.Max(1, (buffer[line].Text.Length + Columns - 1) / Columns);

    void UpdateMetrics()
    {
        typeface = new Typeface(fontFamily);
        var sample = new FormattedText(new string('M', 20), CultureInfo.InvariantCulture, FlowDirection.LeftToRight,
            typeface, fontSize, Brushes.White);
        cellWidth = sample.WidthIncludingTrailingWhitespace / 20;
        rowHeight = Math.Ceiling(sample.Height);
        rowCache.Clear();
    }

    protected override Size MeasureOverride(Size availableSize)
    {
        bar.Measure(availableSize);
        return default;
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        var width = bar.DesiredSize.Width;
        bar.Arrange(new Rect(finalSize.Width - width, 0, width, finalSize.Height));
        return finalSize;
    }

    protected override void OnSizeChanged(SizeChangedEventArgs e)
    {
        base.OnSizeChanged(e);
        Refresh();
    }

    // Scrolling. The top of the view is a line and a row of it.

    int Forward(ref long line, ref int row, int n)
    {
        var moved = 0;
        for (; moved < n; moved++)
        {
            if (row + 1 < Rows(line))
                row++;
            else if (line < buffer.Last)
                (line, row) = (line + 1, 0);
            else
                break;
        }
        return moved;
    }

    void Backward(ref long line, ref int row, int n)
    {
        for (var i = 0; i < n; i++)
        {
            if (row > 0)
                row--;
            else if (line > buffer.First)
                (line, row) = (line - 1, Rows(line - 1) - 1);
            else
                break;
        }
    }

    (long, int) BottomTop()
    {
        var line = buffer.Last;
        var row = Rows(line) - 1;
        Backward(ref line, ref row, VisibleRows - 1);
        return (line, row);
    }

    void SetTop(long line, int row)
    {
        var bottom = BottomTop();
        if (line > bottom.Item1 || (line == bottom.Item1 && row >= bottom.Item2))
        {
            (topLine, topRow) = bottom;
            Follow = true;
        }
        else
        {
            (topLine, topRow) = (line, row);
            Follow = false;
        }
        UpdateBar();
        InvalidateVisual();
    }

    public void ScrollRows(int n)
    {
        var (line, row) = (topLine, topRow);
        if (n > 0)
            Forward(ref line, ref row, n);
        else
            Backward(ref line, ref row, -n);
        SetTop(line, row);
    }

    void ScrollToLine(long index, bool end)
    {
        if (end)
            SetTop(buffer.Last, int.MaxValue);
        else
            SetTop(Math.Clamp(buffer.First + index, buffer.First, buffer.Last), 0);
    }

    void UpdateBar()
    {
        bar.Minimum = 0;
        bar.Maximum = BottomTop().Item1 - buffer.First;
        bar.ViewportSize = VisibleRows;
        bar.Value = topLine - buffer.First;
    }

    // Drawing

    public override void Render(DrawingContext context)
    {
        context.FillRectangle(BackgroundBrush, new Rect(Bounds.Size));
        var cols = Columns;
        var next = new Dictionary<(TermLine, int), RowText>();
        var (sa, sb) = HasSelection ? Order(anchor!.Value, caret!.Value) : (default, default);
        var y = Pad;
        var (line, row) = (topLine, topRow);
        while (y < Bounds.Height && line <= buffer.Last)
        {
            var l = buffer[line];
            var rows = Rows(line);
            if (typing && line == buffer.Last)
                rows = Math.Max(rows, buffer.CursorColumn / cols + 1);
            for (; row < rows && y < Bounds.Height; row++, y += rowHeight)
            {
                var start = row * cols;
                var end = Math.Min(l.Text.Length, start + cols);
                if (l.IsMarker)
                    context.FillRectangle(MarkerBackground, new Rect(0, y, Bounds.Width, rowHeight));
                else
                    DrawBackgrounds(context, l, start, end, y);
                if (HasSelection && line >= sa.Line && line <= sb.Line)
                {
                    var from = Math.Max(line == sa.Line ? sa.Column : 0, start);
                    // A selected line break shows as one more cell.
                    var to = Math.Min(line == sb.Line ? sb.Column : end + 1, row == rows - 1 ? end + 1 : start + cols);
                    if (to > from)
                        context.FillRectangle(SelectionBrush,
                            new Rect(Pad + (from - start) * cellWidth, y, (to - from) * cellWidth, rowHeight));
                }
                if (end > start)
                {
                    if (!rowCache.TryGetValue((l, row), out var text) || text.Version != l.Version || text.Columns != cols)
                        text = new RowText(l.Version, cols, RowFormattedText(l, start, end));
                    next[(l, row)] = text;
                    context.DrawText(text.Text, new Point(Pad, y));
                }
                if (typing && line == buffer.Last && row == buffer.CursorColumn / cols)
                {
                    var cell = new Rect(Pad + buffer.CursorColumn % cols * cellWidth, y, cellWidth, rowHeight);
                    if (IsFocused)
                        context.FillRectangle(CursorBrush, cell);
                    else
                        context.DrawRectangle(CursorPen, cell.Deflate(0.5));
                }
            }
            (line, row) = (line + 1, 0);
        }
        rowCache = next;
        if (typing)
            context.DrawRectangle(TypingPen, new Rect(Bounds.Size).Deflate(1));
    }

    void DrawBackgrounds(DrawingContext context, TermLine line, int start, int end, double y)
    {
        var runs = line.Runs;
        if (runs == null)
            return;
        for (var i = 0; i < runs.Length; i++)
        {
            var (fg, bg) = ColorsOf(runs[i].Style);
            if (bg == null)
                continue;
            var s = Math.Max(runs[i].Start, start);
            var e = Math.Min(i + 1 < runs.Length ? runs[i + 1].Start : end, end);
            if (e > s)
                context.FillRectangle(Brush(bg.Value), new Rect(Pad + (s - start) * cellWidth, y, (e - s) * cellWidth, rowHeight));
        }
    }

    FormattedText RowFormattedText(TermLine line, int start, int end)
    {
        var text = new FormattedText(line.Text.Substring(start, end - start), CultureInfo.InvariantCulture,
            FlowDirection.LeftToRight, typeface, fontSize, line.IsMarker ? MarkerBrush : Brush(Foreground));
        var runs = line.Runs;
        if (runs == null)
            return text;
        for (var i = 0; i < runs.Length; i++)
        {
            var s = Math.Max(runs[i].Start, start);
            var e = Math.Min(i + 1 < runs.Length ? runs[i + 1].Start : end, end);
            if (e <= s)
                continue;
            var style = runs[i].Style;
            text.SetForegroundBrush(Brush(ColorsOf(style).Fg), s - start, e - s);
            if (style.Flags.HasFlag(TermFlags.Bold))
                text.SetFontWeight(FontWeight.Bold, s - start, e - s);
            if (style.Flags.HasFlag(TermFlags.Italic))
                text.SetFontStyle(FontStyle.Italic, s - start, e - s);
            if (style.Flags.HasFlag(TermFlags.Underline))
                text.SetTextDecorations(TextDecorations.Underline, s - start, e - s);
        }
        return text;
    }

    /// <summary>Text color, and background color if not the default.</summary>
    static (Color Fg, Color? Bg) ColorsOf(TermStyle style)
    {
        var fg = style.Fg == 0 ? Foreground : ToColor(style.Fg);
        Color? bg = style.Bg == 0 ? null : ToColor(style.Bg);
        if (style.Flags.HasFlag(TermFlags.Inverse))
            (fg, bg) = (bg ?? Background, fg);
        if (style.Flags.HasFlag(TermFlags.Dim))
            fg = Color.FromArgb(0xA0, fg.R, fg.G, fg.B);
        return (fg, bg);
    }

    static Color ToColor(int code)
    {
        if ((code & TermStyle.Rgb) != 0)
            return Color.FromRgb((byte)(code >> 16), (byte)(code >> 8), (byte)code);
        var i = code - 1;
        if (i < 16)
            return Basic[i];
        if (i < 232)
        {
            i -= 16;
            static byte Level(int n) => (byte)(n == 0 ? 0 : 55 + n * 40);
            return Color.FromRgb(Level(i / 36), Level(i / 6 % 6), Level(i % 6));
        }
        var gray = (byte)(8 + (i - 232) * 10);
        return Color.FromRgb(gray, gray, gray);
    }

    IBrush Brush(Color color)
    {
        var key = (int)color.ToUInt32();
        if (!brushes.TryGetValue(key, out var brush))
            brushes[key] = brush = new ImmutableSolidColorBrush(color);
        return brush;
    }

    // Input

    static (TextPos, TextPos) Order(TextPos a, TextPos b) => a < b ? (a, b) : (b, a);

    TextPos HitTest(Point p)
    {
        var (line, row) = (topLine, topRow);
        var index = (int)Math.Floor((p.Y - Pad) / rowHeight);
        if (index < 0)
            return new TextPos(line, row * Columns);
        if (Forward(ref line, ref row, index) < index)
            return new TextPos(buffer.Last, buffer[buffer.Last].Text.Length);
        var col = Math.Clamp((int)Math.Round((p.X - Pad) / cellWidth), 0, Columns);
        return new TextPos(line, Math.Min(row * Columns + col, buffer[line].Text.Length));
    }

    protected override void OnPointerPressed(PointerPressedEventArgs e)
    {
        base.OnPointerPressed(e);
        var point = e.GetCurrentPoint(this);
        if (!point.Properties.IsLeftButtonPressed || point.Position.X >= Bounds.Width - bar.Bounds.Width)
            return;
        Focus();
        var pos = HitTest(point.Position);
        if (e.ClickCount == 2)
            SelectWord(pos);
        else if (e.ClickCount >= 3)
            (anchor, caret) = (new TextPos(pos.Line, 0), new TextPos(pos.Line, int.MaxValue));
        else if (e.KeyModifiers.HasFlag(KeyModifiers.Shift) && anchor != null)
            caret = pos;
        else
            anchor = caret = pos;
        selecting = true;
        e.Pointer.Capture(this);
        e.Handled = true;
        InvalidateVisual();
    }

    protected override void OnPointerMoved(PointerEventArgs e)
    {
        base.OnPointerMoved(e);
        if (!selecting)
            return;
        var p = e.GetPosition(this);
        if (p.Y < 0)
            ScrollRows(-1);
        else if (p.Y > Bounds.Height)
            ScrollRows(1);
        caret = HitTest(p);
        InvalidateVisual();
    }

    protected override void OnPointerReleased(PointerReleasedEventArgs e)
    {
        base.OnPointerReleased(e);
        if (!selecting)
            return;
        selecting = false;
        e.Pointer.Capture(null);
        Refresh();
    }

    protected override void OnPointerWheelChanged(PointerWheelEventArgs e)
    {
        base.OnPointerWheelChanged(e);
        wheel -= e.Delta.Y * 3;
        var n = (int)wheel;
        wheel -= n;
        if (n != 0)
            ScrollRows(n);
        e.Handled = true;
    }

    protected override void OnKeyDown(KeyEventArgs e)
    {
        base.OnKeyDown(e);
        var mods = e.KeyModifiers;
        var ctrl = mods.HasFlag(KeyModifiers.Control) || mods.HasFlag(KeyModifiers.Meta);
        var shift = mods.HasFlag(KeyModifiers.Shift);
        e.Handled = true;
        // While typing, Ctrl+key goes to the console. Ctrl+C copies when text
        // is selected; Ctrl+Shift+key and Cmd+key are the view commands.
        if (typing)
        {
            if (e.Key == Key.C && ctrl && (shift || HasSelection || mods.HasFlag(KeyModifiers.Meta)))
                CopySelection();
            else if (e.Key == Key.V && ctrl)
                Paste();
            else if (e.Key == Key.A && ctrl && (shift || mods.HasFlag(KeyModifiers.Meta)))
                SelectAll();
            else if (e.Key is Key.PageUp or Key.PageDown)
                ScrollRows(e.Key == Key.PageUp ? -(VisibleRows - 1) : VisibleRows - 1);
            else if (KeyText(e) is string text)
                Send(text);
            else
                e.Handled = false;
            return;
        }
        switch (e.Key)
        {
            case Key.C or Key.Insert when ctrl:
                CopySelection();
                break;
            case Key.A when ctrl:
                SelectAll();
                break;
            case Key.V when ctrl:
                Paste();
                break;
            case Key.PageUp:
                ScrollRows(-(VisibleRows - 1));
                break;
            case Key.PageDown:
                ScrollRows(VisibleRows - 1);
                break;
            case Key.Up:
                ScrollRows(-1);
                break;
            case Key.Down:
                ScrollRows(1);
                break;
            case Key.Home when ctrl:
                SetTop(buffer.First, 0);
                break;
            case Key.End:
                Follow = true;
                break;
            case Key.Escape:
                ClearSelection();
                break;
            default:
                e.Handled = false;
                break;
        }
    }

    /// <summary>What a terminal sends for a key that is not plain text, or null.</summary>
    static string? KeyText(KeyEventArgs e)
    {
        var mods = e.KeyModifiers;
        if (mods.HasFlag(KeyModifiers.Meta))
            return null;
        // Ctrl+Alt is AltGr on Windows: its characters come as text input.
        if (mods.HasFlag(KeyModifiers.Control) && !mods.HasFlag(KeyModifiers.Alt))
        {
            if (mods.HasFlag(KeyModifiers.Shift))
                return null;
            if (e.Key >= Key.A && e.Key <= Key.Z)
                return ((char)(e.Key - Key.A + 1)).ToString();
            return e.Key switch
            {
                Key.OemOpenBrackets => "\x1b",
                Key.OemPipe or Key.OemBackslash => "\x1c",
                Key.OemCloseBrackets => "\x1d",
                _ => null,
            };
        }
        return e.Key switch
        {
            Key.Enter => "\r",
            Key.Back => "\x7f",
            Key.Tab => mods.HasFlag(KeyModifiers.Shift) ? "\x1b[Z" : "\t",
            Key.Escape => "\x1b",
            Key.Up => "\x1b[A",
            Key.Down => "\x1b[B",
            Key.Right => "\x1b[C",
            Key.Left => "\x1b[D",
            Key.Home => "\x1b[H",
            Key.End => "\x1b[F",
            Key.Insert => "\x1b[2~",
            Key.Delete => "\x1b[3~",
            _ => null,
        };
    }

    protected override void OnTextInput(TextInputEventArgs e)
    {
        base.OnTextInput(e);
        // Control characters come as keys.
        var text = e.Text;
        if (string.IsNullOrEmpty(text) || text.Any(char.IsControl))
            return;
        e.Handled = true;
        if (typing)
            Send(text);
        else
            ReadOnlyKey?.Invoke(this, EventArgs.Empty);
    }

    protected override void OnGotFocus(FocusChangedEventArgs e)
    {
        base.OnGotFocus(e);
        InvalidateVisual();
    }

    protected override void OnLostFocus(FocusChangedEventArgs e)
    {
        base.OnLostFocus(e);
        InvalidateVisual();
    }

    void SelectWord(TextPos pos)
    {
        var text = buffer[pos.Line].Text;
        int from = pos.Column, to = pos.Column;
        while (from > 0 && !char.IsWhiteSpace(text[from - 1]))
            from--;
        while (to < text.Length && !char.IsWhiteSpace(text[to]))
            to++;
        (anchor, caret) = (new TextPos(pos.Line, from), new TextPos(pos.Line, to));
    }
}
