using System;
using System.IO;
using System.Text.Json;

namespace BooBootConsole;

/// <summary>User settings, kept in a JSON file.</summary>
public sealed class Settings
{
    public string Url { get; set; } = "http://booboot.local:8080";
    public string LogFolder { get; set; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "BooBoot logs");
    /// <summary>Log file names start with it. Empty: the DUT name.</summary>
    public string LogPrefix { get; set; } = "";
    public bool LogOnConnect { get; set; }
    public bool NewLogAtPowerOn { get; set; }
    public int ScrollbackLines { get; set; } = 200_000;
    public double TextSize { get; set; } = 13;
    public string Fonts { get; set; } = TerminalView.DefaultFonts;

    public static string FilePath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "BooBootConsole", "settings.json");

    static readonly JsonSerializerOptions Options = new() { WriteIndented = true };

    public static Settings Load()
    {
        try
        {
            return JsonSerializer.Deserialize<Settings>(File.ReadAllText(FilePath), Options) ?? new Settings();
        }
        catch (Exception e) when (e is IOException or JsonException or UnauthorizedAccessException)
        {
            return new Settings();
        }
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
            File.WriteAllText(FilePath, JsonSerializer.Serialize(this, Options));
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
        }
    }
}
