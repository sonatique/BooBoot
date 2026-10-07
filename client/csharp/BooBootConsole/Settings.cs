using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace BooBootConsole;

/// <summary>User settings, kept in a JSON file.</summary>
public sealed class Settings
{
    public string Url { get; set; } = "http://booboot.local:8080";
    public string LogFolder { get; set; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "BooBoot logs");
    /// <summary>Log file names start with it, then the DUT name with several DUTs. Empty: the DUT name.</summary>
    public string LogPrefix { get; set; } = "";
    public bool LogOnConnect { get; set; }
    public bool NewLogAtPowerOn { get; set; }
    public int ScrollbackLines { get; set; } = 200_000;
    public double TextSize { get; set; } = 13;
    public string Fonts { get; set; } = TerminalView.DefaultFonts;
    /// <summary>The addresses that each server reported, for when its name is not found.</summary>
    public Dictionary<string, List<string>> Addresses { get; set; } = new();
    /// <summary>The DUTs not shown, for each board with several.</summary>
    public Dictionary<string, List<string>> HiddenDuts { get; set; } = new();

    public static string FilePath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "BooBootConsole", "settings.json");

    public static Settings Load(string? path = null)
    {
        try
        {
            return JsonSerializer.Deserialize(File.ReadAllText(path ?? FilePath), SettingsJson.Default.Settings)
                ?? new Settings();
        }
        catch (Exception e) when (e is IOException or JsonException or UnauthorizedAccessException)
        {
            return new Settings();
        }
    }

    public void Save(string? path = null)
    {
        path ??= FilePath;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, JsonSerializer.Serialize(this, SettingsJson.Default.Settings));
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
        }
    }
}

// JSON code made at build time, as trimmed programs cannot use reflection.
[JsonSourceGenerationOptions(WriteIndented = true)]
[JsonSerializable(typeof(Settings))]
partial class SettingsJson : JsonSerializerContext
{
}
