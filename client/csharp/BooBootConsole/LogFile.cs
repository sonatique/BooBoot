using System;
using System.IO;
using System.Text;

namespace BooBootConsole;

/// <summary>A text log file named PREFIX-YYYYMMDD-HHMMSS.log.</summary>
public sealed class LogFile : IDisposable
{
    StreamWriter? writer;

    public string? Path { get; private set; }

    public bool Active => writer != null;

    public void Start(string folder, string prefix)
    {
        Stop();
        Directory.CreateDirectory(folder);
        var name = $"{prefix}-{DateTime.Now:yyyyMMdd-HHmmss}";
        var path = System.IO.Path.Combine(folder, name + ".log");
        for (var n = 2; File.Exists(path); n++)
            path = System.IO.Path.Combine(folder, $"{name}-{n}.log");
        writer = new StreamWriter(path, false, new UTF8Encoding(false));
        Path = path;
    }

    public void WriteLine(string text) => writer?.Write(text + Environment.NewLine);

    public void Flush() => writer?.Flush();

    public void Stop()
    {
        writer?.Dispose();
        writer = null;
    }

    public void Dispose() => Stop();
}
