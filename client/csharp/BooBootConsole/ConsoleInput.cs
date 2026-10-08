using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using BooBoot;

namespace BooBootConsole;

/// <summary>
/// Typing into the console: holds the session, and sends the keys in order,
/// one request at a time. Keys typed during a request go in the next one.
/// While it holds the session, it sends heartbeats.
/// Used on the UI thread only.
/// </summary>
public sealed class ConsoleInput : IDisposable
{
    static readonly string Name = $"BooBoot Console {Environment.UserName}@{Environment.MachineName}";

    readonly BooBootClient client;
    readonly StringBuilder keys = new();
    bool sending;
    CancellationTokenSource? beat;

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

    /// <summary>
    /// Opens the session. Returns null, or the session of the client that has the DUT. Takes it from a gone
    /// client of the same name, like this program before a crash.
    /// </summary>
    /// <param name="force">Take the session from any client.</param>
    /// <param name="ifGone">Take the session only from a client that is gone.</param>
    public async Task<JsonElement?> TakeAsync(bool force = false, bool ifGone = false)
    {
        try
        {
            await client.OpenSessionAsync(Name, force: force, ifGone: ifGone);
        }
        catch (BooBootException e) when (e.Code == "busy" && !force && e.Info?.TryGetProperty("session", out _) == true)
        {
            var session = e.Info.Value.GetProperty("session");
            if (ifGone || !IsGone(session) || session.GetProperty("client").GetString() != Name)
                return session;
            return await TakeAsync(ifGone: true);
        }
        beat?.Cancel();
        beat = new CancellationTokenSource();
        _ = BeatAsync(beat.Token);
        return null;
    }

    /// <summary>True if the client of the session sent heartbeats, and stopped.</summary>
    public static bool IsGone(JsonElement session) =>
        session.TryGetProperty("alive", out var alive) && alive.ValueKind == JsonValueKind.False;

    async Task BeatAsync(CancellationToken stop)
    {
        try
        {
            while (Token != null)
            {
                try
                {
                    await client.HeartbeatAsync();
                }
                catch (BooBootException e) when (e.Code == "not_found")
                {
                    return; // a server older than heartbeats
                }
                catch (BooBootException)
                {
                    // The status poll finds out when the control ends.
                }
                await Task.Delay(BooBootClient.Heartbeat, stop);
            }
        }
        catch (Exception) when (stop.IsCancellationRequested)
        {
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
                End(e);
            }
            catch (BooBootException e)
            {
                Failed?.Invoke("keys not sent: " + e.Message);
            }
        }
        sending = false;
    }

    /// <summary>Switches the DUT power with the session. Returns its state, or null when the control ended.</summary>
    public async Task<string?> PowerAsync(bool on)
    {
        try
        {
            var r = on ? await client.PowerOnAsync() : await client.PowerOffAsync();
            return r.GetProperty("power").GetString();
        }
        catch (BooBootException e) when (e.Code is "busy" or "no_session")
        {
            End(e);
            return null;
        }
    }

    /// <summary>Sets the label of the DUT with the session. Returns it, or null when the control ended.</summary>
    public async Task<string?> LabelAsync(string text)
    {
        try
        {
            return (await client.SetLabelAsync(text)).GetProperty("label").GetString();
        }
        catch (BooBootException e) when (e.Code is "busy" or "no_session")
        {
            End(e);
            return null;
        }
    }

    /// <summary>Forgets a session that another client took, or that ended, and says why.</summary>
    void End(BooBootException e)
    {
        Forget();
        var who = e.Info?.TryGetProperty("session", out var s) == true ? s.GetProperty("client").GetString() : "";
        Lost?.Invoke(e.Code == "busy" ? "control lost: the DUT is used by " + who : "control ended after the idle time");
    }

    /// <summary>Forgets the session, already ended on the server.</summary>
    public void Forget()
    {
        beat?.Cancel();
        client.Session = null;
        keys.Clear();
    }

    public async Task ReleaseAsync()
    {
        beat?.Cancel();
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

    public void Dispose()
    {
        beat?.Cancel();
        client.Dispose();
    }
}
