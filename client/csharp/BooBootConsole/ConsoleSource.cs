using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using BooBoot;

namespace BooBootConsole;

/// <summary>
/// Reads the console stream of one BooBoot server, and connects again when
/// the connection is lost. Needs no session: other clients are not disturbed.
/// Items are queued for the UI thread.
/// </summary>
public sealed class ConsoleSource : IDisposable
{
    public abstract record Item;

    /// <summary>Connected. Restarted: the server restarted since the last connection.</summary>
    public sealed record Connected(string Name, string Power, bool Restarted) : Item;

    public sealed record Disconnected(string Message) : Item;

    /// <summary>History: the output came before the connection.</summary>
    public sealed record Output(string Text, bool History) : Item;

    public sealed record Power(bool On, DateTime Time, bool History) : Item;

    public sealed record Lost(long Bytes) : Item;

    /// <summary>Who has the session, from the status polled every few seconds.</summary>
    public sealed record Session(string Text) : Item;

    readonly BooBootClient client;
    readonly CancellationTokenSource cts = new();
    readonly ConcurrentQueue<Item> queue = new();
    int signaled;

    public ConsoleSource(string url)
    {
        client = new BooBootClient(url, TimeSpan.FromSeconds(10));
        Url = client.Url;
    }

    public string Url { get; }

    /// <summary>Raised on a background thread when items come after the queue was emptied.</summary>
    public event Action? Available;

    public void Start()
    {
        _ = Task.Run(() => StreamAsync(cts.Token));
        _ = Task.Run(() => PollAsync(cts.Token));
    }

    /// <summary>Takes the next item. Call until false, then Available is raised again.</summary>
    public bool TryTake(out Item item)
    {
        if (queue.TryDequeue(out item!))
            return true;
        Volatile.Write(ref signaled, 0);
        // An item may have come just before the reset.
        return queue.TryDequeue(out item!);
    }

    public void Dispose()
    {
        Available = null;
        cts.Cancel();
        client.Dispose();
    }

    void Post(Item item)
    {
        queue.Enqueue(item);
        if (Interlocked.Exchange(ref signaled, 1) == 0)
            Available?.Invoke();
    }

    async Task StreamAsync(CancellationToken token)
    {
        var since = "start";
        long next = -1, historyEnd = -1;
        double started = 0, lastPower = 0, connectTime = 0;
        var restarted = false;
        while (!token.IsCancellationRequested)
        {
            try
            {
                await foreach (var e in client.StreamConsoleAsync(since, token))
                {
                    if (e.Type == "hello")
                    {
                        var serverStart = e.Data.GetProperty("started").GetDouble();
                        if (started != 0 && serverStart != started)
                        {
                            // Cursors start again from 0: read all the new output.
                            (since, next, historyEnd, started, restarted) = ("start", -1, 0, 0, true);
                            break;
                        }
                        started = serverStart;
                        connectTime = e.Time;
                        if (historyEnd < 0)
                            historyEnd = e.Data.GetProperty("end").GetInt64();
                        Post(new Connected(e.Data.GetProperty("name").GetString() ?? "", e.State, restarted));
                        restarted = false;
                    }
                    else if (e.Type == "output")
                    {
                        if (next >= 0 && e.Cursor > next)
                            Post(new Lost(e.Cursor - next));
                        next = e.Next;
                        since = next.ToString(CultureInfo.InvariantCulture);
                        Post(new Output(e.Text, e.Next <= historyEnd));
                    }
                    else if (e.Type == "power" && e.Time > lastPower)
                    {
                        // After a reconnection, the last switch may come again.
                        lastPower = e.Time;
                        var time = DateTimeOffset.FromUnixTimeMilliseconds((long)(e.Time * 1000)).LocalDateTime;
                        Post(new Power(e.State == "on", time, e.Time <= connectTime));
                    }
                }
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested)
            {
                return;
            }
            catch (Exception e) when (e is BooBootException or JsonException or InvalidOperationException
                                          or KeyNotFoundException)
            {
                Post(new Disconnected(e.Message));
            }
            if (restarted)
                continue;
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(2), token);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }

    async Task PollAsync(CancellationToken token)
    {
        var last = "";
        while (!token.IsCancellationRequested)
        {
            try
            {
                var session = (await client.StatusAsync()).GetProperty("session");
                var text = session.GetProperty("active").GetBoolean()
                    ? "used by " + session.GetProperty("client").GetString()
                    : "free";
                if (text != last)
                    Post(new Session(last = text));
            }
            catch (BooBootException)
            {
            }
            catch (Exception) when (token.IsCancellationRequested)
            {
                return;
            }
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(3), token);
            }
            catch (OperationCanceledException)
            {
                return;
            }
        }
    }
}
