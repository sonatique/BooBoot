using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using BooBoot;

namespace BooBootConsole;

/// <summary>
/// Typing into the console: holds the session, and sends the keys in order,
/// one request at a time. Keys typed during a request go in the next one.
/// Used on the UI thread only.
/// </summary>
public sealed class ConsoleInput : IDisposable
{
    readonly BooBootClient client;
    readonly StringBuilder keys = new();
    bool sending;

    public ConsoleInput(string url, IEnumerable<string> addresses)
    {
        client = new BooBootClient(url, TimeSpan.FromSeconds(10)) { Addresses = addresses.ToList() };
    }

    /// <summary>The session token while in control, else null.</summary>
    public string? Token => client.Session;

    /// <summary>Raised with the reason when the control ends without Release.</summary>
    public event Action<string>? Lost;

    /// <summary>Raised when keys could not be sent, the control going on.</summary>
    public event Action<string>? Failed;

    /// <summary>Opens the session. Returns null, or the session of the client that has the DUT.</summary>
    public async Task<JsonElement?> TakeAsync(bool force)
    {
        try
        {
            await client.OpenSessionAsync($"BooBoot Console {Environment.UserName}@{Environment.MachineName}",
                force: force);
            return null;
        }
        catch (BooBootException e) when (e.Code == "busy" && !force && e.Info?.TryGetProperty("session", out _) == true)
        {
            return e.Info.Value.GetProperty("session");
        }
    }

    public void Send(string text)
    {
        if (Token == null || text.Length == 0)
            return;
        keys.Append(text);
        if (!sending)
            _ = SendAsync();
    }

    async Task SendAsync()
    {
        sending = true;
        while (keys.Length > 0 && Token != null)
        {
            var text = keys.ToString();
            keys.Clear();
            try
            {
                await client.WriteAsync(text);
            }
            catch (BooBootException e) when (e.Code is "busy" or "no_session")
            {
                client.Session = null;
                keys.Clear();
                var who = e.Info?.TryGetProperty("session", out var s) == true ? s.GetProperty("client").GetString() : "";
                Lost?.Invoke(e.Code == "busy" ? "control lost: the DUT is used by " + who
                    : "control ended after the idle time");
            }
            catch (BooBootException e)
            {
                Failed?.Invoke("keys not sent: " + e.Message);
            }
        }
        sending = false;
    }

    /// <summary>Forgets the session, already ended on the server.</summary>
    public void Forget()
    {
        client.Session = null;
        keys.Clear();
    }

    public async Task ReleaseAsync()
    {
        keys.Clear();
        try
        {
            await client.CloseSessionAsync();
        }
        catch (BooBootException)
        {
            // The session ends by itself after the idle time.
        }
    }

    public void Dispose() => client.Dispose();
}
