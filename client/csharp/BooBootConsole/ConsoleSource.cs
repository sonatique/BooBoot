using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using BooBoot;

namespace BooBootConsole;

/// <summary>
/// Reads the console stream of one DUT of a BooBoot server, and connects again when
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
    /// <param name="Token">Session token sent with the poll; Yours tells if it holds the session.</param>
    public sealed record Session(string Text, string? Token, bool Yours) : Item;

    /// <summary>The addresses of the server, from its status, when they change.</summary>
    public sealed record Network(IReadOnlyList<string> Addresses) : Item;

    /// <summary>The name of the server was not found: the client goes on with another address.</summary>
    public sealed record AddressUsed(string Note) : Item;

    /// <summary>The names of the DUTs of the board, from the status, when they change.</summary>
    public sealed record Board(IReadOnlyList<string> Duts) : Item;

    /// <summary>The label of the DUT, from the status, when it changes.</summary>
    public sealed record Label(string Text) : Item;

    /// <summary>Details of the DUT and its server, from the status, when they change.</summary>
    public sealed record Details(string Console, string Power, string Sd, string Server) : Item;

    readonly BooBootClient client;
    readonly CancellationTokenSource cts = new();
    readonly ConcurrentQueue<Item> queue = new();
    int signaled;

    public ConsoleSource(string url, IEnumerable<string> addresses)
    {
        client = new BooBootClient(url, TimeSpan.FromSeconds(10)) { Addresses = addresses.ToList() };
        client.AddressUsed += note => Post(new AddressUsed(note));
        Url = client.Url;
    }

    public string Url { get; }

    /// <summary>Session token of this program while it types, to know if it still has the session.</summary>
    public string? Token { get; set; }

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

    static Details DetailsOf(JsonElement status)
    {
        string Text(JsonElement e, string name) => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v)
            ? v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : v.ValueKind == JsonValueKind.Number ? v.ToString() : ""
            : "";
        JsonElement Part(string name) => status.TryGetProperty(name, out var v) ? v : default;
        string WithError(string text, JsonElement e) => Text(e, "error") is var error and not "" ? $"{text} ({error})" : text;

        var console = Part("console");
        var baud = Text(console, "baudrate");
        var consoleText = Text(console, "device") + (baud != "" ? $" at {baud} baud" : "");
        if (console.ValueKind == JsonValueKind.Object && console.TryGetProperty("connected", out var c)
            && c.ValueKind == JsonValueKind.False)
            consoleText = WithError(consoleText + ", not connected", console);
        var power = Part("power");
        var sd = Part("sd");
        var network = Part("network");
        var addresses = network.ValueKind == JsonValueKind.Object && network.TryGetProperty("addresses", out var a)
            && a.ValueKind == JsonValueKind.Array ? string.Join(", ", a.EnumerateArray().Select(x => x.GetString())) : "";
        var host = Text(network, "hostname");
        var server = $"BooBoot {Text(status, "version")}" + (host != "" ? " on " + host : "")
            + (addresses != "" ? $" ({addresses})" : "");
        return new Details(consoleText, WithError($"{Text(power, "backend")} relay", power),
            WithError($"on the {Text(sd, "mode")} side", sd), server);
    }

    async Task PollAsync(CancellationToken token)
    {
        var last = "";
        var lastAddresses = "";
        var lastDuts = "";
        string? lastLabel = null;
        Details? lastDetails = null;
        while (!token.IsCancellationRequested)
        {
            try
            {
                var mine = Token;
                client.Session = mine;
                var status = await client.StatusAsync();
                if (status.TryGetProperty("network", out var network))
                {
                    var addresses = network.GetProperty("addresses").EnumerateArray().Select(a => a.GetString()!).ToList();
                    if (string.Join(" ", addresses) != lastAddresses)
                    {
                        lastAddresses = string.Join(" ", addresses);
                        Post(new Network(addresses));
                    }
                }
                // A server older than several DUTs per board has no list.
                if (status.TryGetProperty("duts", out var duts))
                {
                    var names = duts.EnumerateArray().Select(d => d.GetString()!).ToList();
                    if (string.Join("\n", names) != lastDuts)
                    {
                        lastDuts = string.Join("\n", names);
                        Post(new Board(names));
                    }
                }
                var label = status.TryGetProperty("label", out var l) ? l.GetString() ?? "" : "";
                if (label != lastLabel)
                    Post(new Label(lastLabel = label));
                var details = DetailsOf(status);
                if (details != lastDetails)
                    Post(lastDetails = details);
                var session = status.GetProperty("session");
                var yours = session.TryGetProperty("yours", out var y) && y.GetBoolean();
                var gone = session.TryGetProperty("alive", out var alive) && alive.ValueKind == JsonValueKind.False;
                var text = !session.GetProperty("active").GetBoolean() ? "free"
                    : yours ? "yours"
                    : "used by " + session.GetProperty("client").GetString() + (gone ? " (gone)" : "");
                if (text != last || (mine != null && !yours))
                    Post(new Session(last = text, mine, yours));
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
