using System;
using System.Collections.Generic;
using System.Text;

namespace BooBootConsole;

[Flags]
public enum TermFlags : byte
{
    None = 0,
    Bold = 1,
    Dim = 2,
    Italic = 4,
    Underline = 8,
    Inverse = 16,
}

/// <summary>Text attributes. Colors: 0 is the default, 1 to 256 a palette index plus 1,
/// Rgb | 0xRRGGBB a true color.</summary>
public readonly record struct TermStyle(int Fg, int Bg, TermFlags Flags)
{
    public const int Rgb = 0x1000000;
}

/// <summary>Style from Start to the next run or the line end.</summary>
public readonly record struct StyleRun(int Start, TermStyle Style);

public sealed class TermLine
{
    public TermLine(string text, StyleRun[]? runs = null, bool isMarker = false)
    {
        Text = text;
        Runs = runs;
        IsMarker = isMarker;
    }

    public string Text { get; private set; }

    /// <summary>Null when all the text has the default style.</summary>
    public StyleRun[]? Runs { get; private set; }

    /// <summary>A line added by the viewer, like a power switch.</summary>
    public bool IsMarker { get; }

    /// <summary>Changes each time the text or styles change.</summary>
    public int Version { get; private set; }

    internal void Set(string text, StyleRun[]? runs)
    {
        Text = text;
        Runs = runs;
        Version++;
    }
}

/// <summary>A position in the buffer: absolute line number and character index.</summary>
public readonly record struct TextPos(long Line, int Column) : IComparable<TextPos>
{
    public int CompareTo(TextPos other) =>
        Line != other.Line ? Line.CompareTo(other.Line) : Column.CompareTo(other.Column);

    public static bool operator <(TextPos a, TextPos b) => a.CompareTo(b) < 0;
    public static bool operator >(TextPos a, TextPos b) => a.CompareTo(b) > 0;
}

/// <summary>
/// Console output as lines, for a line based view: the kind of terminal that
/// only writes to its last line. Handles CR, backspace, tabs, colors and line
/// erase. Other escape sequences, like cursor moves, are dropped.
/// </summary>
public sealed class TerminalBuffer
{
    public const int MaxLineLength = 16384;

    readonly List<TermLine> lines = new();
    readonly StringBuilder cur = new();
    readonly List<TermStyle> curStyles = new();
    readonly StringBuilder seq = new();
    int col;
    TermStyle style;
    State state;
    bool dirty;

    enum State { Text, Escape, Csi, Osc, OscEscape, SkipOne }

    public TerminalBuffer(int maxLines = 100_000)
    {
        MaxLines = maxLines;
        lines.Add(new TermLine(""));
    }

    /// <summary>Lines kept. Older lines are dropped.</summary>
    public int MaxLines { get; set; }

    /// <summary>Absolute number of the first line kept.</summary>
    public long First { get; private set; }

    /// <summary>Number of lines kept, the last one being the line being written.</summary>
    public int Count => lines.Count;

    /// <summary>Absolute number of the line being written.</summary>
    public long Last => First + lines.Count - 1;

    /// <summary>Changes each time the content changes.</summary>
    public int Version { get; private set; }

    /// <summary>Called for each complete line.</summary>
    public event Action<TermLine>? LineDone;

    public TermLine this[long line] => lines[(int)(line - First)];

    public void Feed(string text)
    {
        foreach (var c in text)
            Feed(c);
        Sync();
    }

    /// <summary>Ends the line being written, if not empty.</summary>
    public void EndLine()
    {
        if (cur.Length > 0)
            NewLine();
    }

    /// <summary>Adds a viewer line, like "---- power on ----", after the current output.</summary>
    public void AddMarker(string text)
    {
        EndLine();
        var marker = new TermLine(text, isMarker: true);
        lines.Insert(lines.Count - 1, marker);
        LineDone?.Invoke(marker);
        Trim();
        Version++;
    }

    /// <summary>Drops all lines, also the line being written.</summary>
    public void Clear()
    {
        First += lines.Count;
        lines.Clear();
        cur.Clear();
        curStyles.Clear();
        col = 0;
        lines.Add(new TermLine(""));
        Version++;
    }

    /// <summary>The line being written, if not empty.</summary>
    public string Pending => cur.ToString();

    /// <summary>Text from a to b, lines separated by newline.</summary>
    public string GetText(TextPos a, TextPos b, string newline = "\n")
    {
        if (b < a)
            (a, b) = (b, a);
        var sb = new StringBuilder();
        for (var n = Math.Max(a.Line, First); n <= Math.Min(b.Line, Last); n++)
        {
            var text = this[n].Text;
            var from = n == a.Line ? Math.Min(a.Column, text.Length) : 0;
            var to = n == b.Line ? Math.Min(b.Column, text.Length) : text.Length;
            if (n > Math.Max(a.Line, First))
                sb.Append(newline);
            if (to > from)
                sb.Append(text, from, to - from);
        }
        return sb.ToString();
    }

    public string GetAllText(string newline = "\n") =>
        GetText(new TextPos(First, 0), new TextPos(Last, int.MaxValue), newline);

    void Feed(char c)
    {
        switch (state)
        {
            case State.Text:
                if (c >= ' ' && c != '\x7f' && (c < '\x80' || c > '\x9f'))
                    Put(c);
                else if (c == '\n')
                    NewLine();
                else if (c == '\r')
                    col = 0;
                else if (c == '\b')
                    col = Math.Max(0, col - 1);
                else if (c == '\t')
                    col = Math.Min(MaxLineLength - 1, (col / 8 + 1) * 8);
                else if (c == '\x1b')
                    state = State.Escape;
                break;
            case State.Escape:
                seq.Clear();
                state = c switch
                {
                    '[' => State.Csi,
                    ']' or 'P' or 'X' or '^' or '_' => State.Osc,
                    '(' or ')' or '*' or '+' or '#' or '%' => State.SkipOne,
                    _ => State.Text,
                };
                if (c == 'c')
                    style = default;
                break;
            case State.Csi:
                if (c >= '@' && c <= '~')
                {
                    Csi(c);
                    state = State.Text;
                }
                else if (c == '\x1b')
                    state = State.Escape;
                else if (c < ' ' || seq.Length > 64)
                    state = State.Text;
                else
                    seq.Append(c);
                break;
            case State.Osc:
                if (c == '\x07')
                    state = State.Text;
                else if (c == '\x1b')
                    state = State.OscEscape;
                break;
            case State.OscEscape:
                state = c == '\\' ? State.Text : State.Osc;
                break;
            case State.SkipOne:
                state = State.Text;
                break;
        }
    }

    void Put(char c)
    {
        while (cur.Length < col)
        {
            cur.Append(' ');
            curStyles.Add(default);
        }
        if (col == cur.Length)
        {
            cur.Append(c);
            curStyles.Add(style);
        }
        else
        {
            cur[col] = c;
            curStyles[col] = style;
        }
        col++;
        dirty = true;
        if (cur.Length >= MaxLineLength)
            NewLine();
    }

    void NewLine()
    {
        dirty = true;
        Sync();
        LineDone?.Invoke(lines[^1]);
        cur.Clear();
        curStyles.Clear();
        col = 0;
        lines.Add(new TermLine(""));
        Trim();
    }

    void Trim()
    {
        // Drop in batches, as removing from the front of the list copies it.
        var extra = lines.Count - MaxLines;
        if (extra > Math.Max(MaxLines / 16, 1))
        {
            lines.RemoveRange(0, extra);
            First += extra;
        }
    }

    /// <summary>Updates the last line from the characters being written.</summary>
    void Sync()
    {
        if (!dirty)
            return;
        dirty = false;
        List<StyleRun>? runs = null;
        for (var i = 0; i < curStyles.Count; i++)
        {
            var prev = i == 0 ? default : curStyles[i - 1];
            if (curStyles[i] != prev)
                (runs ??= new List<StyleRun>()).Add(new StyleRun(i, curStyles[i]));
        }
        lines[^1].Set(cur.ToString(), runs?.ToArray());
        Version++;
    }

    int Param(List<int> ps, int i, int fallback) => i < ps.Count && ps[i] > 0 ? ps[i] : fallback;

    void Csi(char final)
    {
        if (seq.Length > 0 && (seq[0] == '?' || seq[0] == '>' || seq[0] == '<' || seq[0] == '='))
            return;
        var ps = new List<int>();
        foreach (var part in seq.ToString().Split(';', ':'))
            ps.Add(int.TryParse(part, out var n) ? Math.Min(n, 99999) : 0);
        switch (final)
        {
            case 'm':
                Sgr(ps);
                break;
            case 'K':
                EraseLine(ps.Count > 0 ? ps[0] : 0);
                break;
            case 'C':
                col = Math.Min(MaxLineLength - 1, col + Param(ps, 0, 1));
                break;
            case 'D':
                col = Math.Max(0, col - Param(ps, 0, 1));
                break;
            case 'G':
                col = Math.Min(MaxLineLength - 1, Param(ps, 0, 1) - 1);
                break;
            case 'P':
                if (col < cur.Length)
                {
                    var n = Math.Min(Param(ps, 0, 1), cur.Length - col);
                    cur.Remove(col, n);
                    curStyles.RemoveRange(col, n);
                    dirty = true;
                }
                break;
            case '@':
                if (col < cur.Length)
                {
                    var n = Math.Min(Param(ps, 0, 1), MaxLineLength - cur.Length);
                    cur.Insert(col, new string(' ', n));
                    curStyles.InsertRange(col, new TermStyle[n]);
                    dirty = true;
                }
                break;
        }
    }

    void EraseLine(int mode)
    {
        if (mode == 0 && col < cur.Length)
        {
            cur.Length = col;
            curStyles.RemoveRange(col, curStyles.Count - col);
        }
        else if (mode == 1)
        {
            for (var i = 0; i < Math.Min(col + 1, cur.Length); i++)
            {
                cur[i] = ' ';
                curStyles[i] = default;
            }
        }
        else if (mode == 2)
        {
            cur.Clear();
            curStyles.Clear();
        }
        dirty = true;
    }

    void Sgr(List<int> ps)
    {
        var (fg, bg, flags) = style;
        for (var i = 0; i < ps.Count; i++)
        {
            var p = ps[i];
            switch (p)
            {
                case 0: (fg, bg, flags) = (0, 0, TermFlags.None); break;
                case 1: flags |= TermFlags.Bold; break;
                case 2: flags |= TermFlags.Dim; break;
                case 3: flags |= TermFlags.Italic; break;
                case 4: flags |= TermFlags.Underline; break;
                case 7: flags |= TermFlags.Inverse; break;
                case 22: flags &= ~(TermFlags.Bold | TermFlags.Dim); break;
                case 23: flags &= ~TermFlags.Italic; break;
                case 24: flags &= ~TermFlags.Underline; break;
                case 27: flags &= ~TermFlags.Inverse; break;
                case >= 30 and <= 37: fg = p - 30 + 1; break;
                case 39: fg = 0; break;
                case >= 40 and <= 47: bg = p - 40 + 1; break;
                case 49: bg = 0; break;
                case >= 90 and <= 97: fg = p - 90 + 8 + 1; break;
                case >= 100 and <= 107: bg = p - 100 + 8 + 1; break;
                case 38 or 48:
                    var color = 0;
                    if (i + 2 < ps.Count && ps[i + 1] == 5)
                    {
                        color = Math.Min(ps[i + 2], 255) + 1;
                        i += 2;
                    }
                    else if (i + 4 < ps.Count && ps[i + 1] == 2)
                    {
                        color = TermStyle.Rgb | Math.Min(ps[i + 2], 255) << 16 | Math.Min(ps[i + 3], 255) << 8
                            | Math.Min(ps[i + 4], 255);
                        i += 4;
                    }
                    else
                        i = ps.Count;
                    if (p == 38)
                        fg = color;
                    else
                        bg = color;
                    break;
            }
        }
        style = new TermStyle(fg, bg, flags);
    }
}
