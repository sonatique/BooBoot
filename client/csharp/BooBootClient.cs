// BooBoot client for .NET 6 or later. One file, no package needed:
// copy it into a project, or link it like Example/Example.csproj does.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace BooBoot;

/// <summary>An error from the server, or a connection problem.</summary>
public sealed class BooBootException : Exception
{
    public BooBootException(string message, int status = 0, string code = "error", JsonElement? info = null)
        : base(message)
    {
        Status = status;
        Code = code;
        Info = info;
    }

    /// <summary>HTTP status, 0 when the server could not be reached.</summary>
    public int Status { get; }

    /// <summary>Server error code, like "busy", "no_session" or "power_on".</summary>
    public string Code { get; }

    /// <summary>Full error answer of the server, if any.</summary>
    public JsonElement? Info { get; }
}

/// <summary>Result of ExpectAsync and RunAsync.</summary>
/// <param name="Matched">False when the timeout came first.</param>
/// <param name="Text">Expect: output up to the end of the match. Run: output of the command.</param>
/// <param name="Next">Console cursor after the match.</param>
/// <param name="Time">Seconds from power on to the match, null without match.</param>
public sealed record MatchResult(bool Matched, string Text, long Next, double? Time);

/// <summary>An event of StreamConsoleAsync.</summary>
/// <param name="Type">"hello" (first event), "output", "power" or "ping".</param>
/// <param name="Cursor">Output: cursor of the text. Power: cursor at the switch. Hello: first cursor.</param>
/// <param name="Next">Output: cursor after the text.</param>
/// <param name="Text">Output: the text, escape codes included.</param>
/// <param name="State">Power: "on" or "off". Hello: power state.</param>
/// <param name="Time">Unix time of the power switch, or of the hello.</param>
/// <param name="Data">The event as sent by the server. Hello also has "name", "version", "end" (cursor at
/// the call) and "started" (Unix time of the server start: cursors count from it).</param>
public sealed record ConsoleEvent(string Type, long Cursor, long Next, string Text, string State, double Time,
    JsonElement Data);

/// <summary>
/// Access to one BooBoot server, that is one DUT.
/// All calls but StatusAsync, OpenSessionAsync and the console reads need the session.
/// Errors throw BooBootException. Answers are the JSON of the HTTP API.
/// </summary>
public sealed class BooBootClient : IDisposable
{
    readonly HttpClient http;

    public BooBootClient(string url = "http://booboot.local:8080", TimeSpan? timeout = null)
    {
        Url = url.TrimEnd('/');
        Base = Url;
        Timeout = timeout ?? TimeSpan.FromSeconds(30);
        // No proxy: the server is on the local network.
        http = new HttpClient(new HttpClientHandler { UseProxy = false })
        {
            Timeout = System.Threading.Timeout.InfiniteTimeSpan,
        };
    }

    public string Url { get; }

    /// <summary>Where the requests go: Url, or another address of the server when its name is not found.</summary>
    public string Base { get; private set; }

    /// <summary>
    /// Other addresses of the server. When the host name of Url is not found, like a .local name over
    /// a VPN, the client tries the name without .local, then these, and goes on with the first that answers.
    /// </summary>
    public IList<string> Addresses { get; set; } = new List<string>();

    /// <summary>Raised with a note when the client goes on with another address.</summary>
    public event Action<string>? AddressUsed;

    /// <summary>Time allowed for a request, on top of any wait asked from the server.</summary>
    public TimeSpan Timeout { get; set; }

    /// <summary>Session token, set by OpenSessionAsync.</summary>
    public string? Session { get; set; }

    public void Dispose() => http.Dispose();

    // Session

    /// <summary>State of power, SD card, console and session.</summary>
    public Task<JsonElement> StatusAsync() => SendJsonAsync(HttpMethod.Get, "/status");

    /// <summary>Opens the session needed by all other calls.</summary>
    /// <param name="client">Name shown to other clients.</param>
    /// <param name="timeout">Idle seconds after which the server ends the session.</param>
    /// <param name="force">Take the session from another client.</param>
    /// <param name="ifGone">Take the session only from a client that is gone: one that sent heartbeats,
    /// and stopped. force is then not needed.</param>
    public async Task<JsonElement> OpenSessionAsync(string? client = null, double? timeout = null, bool force = false,
        bool ifGone = false)
    {
        var info = await SendJsonAsync(HttpMethod.Post, "/session", Body(
            ("client", client ?? $"{Environment.UserName}@{Environment.MachineName}"),
            ("timeout", timeout),
            ("force", ifGone ? "gone" : force ? true : null)));
        Session = info.GetProperty("session").GetString();
        return info;
    }

    public async Task CloseSessionAsync()
    {
        if (Session == null)
            return;
        try
        {
            await SendJsonAsync(HttpMethod.Delete, "/session");
        }
        finally
        {
            Session = null;
        }
    }

    public Task<JsonElement> KeepaliveAsync() => SendJsonAsync(HttpMethod.Post, "/session/keepalive");

    /// <summary>Time between heartbeats.</summary>
    public static readonly TimeSpan Heartbeat = TimeSpan.FromSeconds(10);

    /// <summary>
    /// Tells the server that this client is still there. It does not count as activity. A client that
    /// stays connected sends it every Heartbeat while it has the session, so that others see when it is gone.
    /// </summary>
    public Task<JsonElement> HeartbeatAsync() => SendJsonAsync(HttpMethod.Post, "/session/heartbeat");

    // Power

    /// <summary>Power on. The SD card goes to the DUT first. The answer has the boot cursor.</summary>
    public Task<JsonElement> PowerOnAsync() => SendJsonAsync(HttpMethod.Put, "/power", Body(("state", "on")));

    public Task<JsonElement> PowerOffAsync() => SendJsonAsync(HttpMethod.Put, "/power", Body(("state", "off")));

    public Task<JsonElement> PowerCycleAsync(double? offTime = null) =>
        SendJsonAsync(HttpMethod.Post, "/power/cycle", Body(("off_time", offTime)), TimeSpan.FromSeconds(offTime ?? 60));

    // SD card. Card paths are "N:/path", N being the partition number; "/path" means "1:/path".

    /// <summary>Connects the card to "host" (the BooBoot board), "dut", or "off".</summary>
    public Task<JsonElement> SdModeAsync(string mode) =>
        SendJsonAsync(HttpMethod.Put, "/sd", Body(("mode", mode)), TimeSpan.FromSeconds(60));

    /// <summary>Writes a disk image (raw, gz, xz or bz2) to the card. Power must be off.</summary>
    public async Task<JsonElement> WriteImageAsync(string path, bool verify = false, string compression = "auto")
    {
        using var file = File.OpenRead(path);
        var query = Query(("compression", compression), ("verify", verify ? 1 : 0));
        return await SendFileAsync(HttpMethod.Put, "/sd/image" + query, file, TimeSpan.FromHours(1));
    }

    public Task<JsonElement> PartitionsAsync() =>
        SendJsonAsync(HttpMethod.Get, "/sd/partitions", null, TimeSpan.FromMinutes(2));

    /// <summary>Entries of a card directory: name, type, size, mtime.</summary>
    public async Task<JsonElement> ListDirAsync(string remote = "1:/")
    {
        using var cts = new CancellationTokenSource(Timeout + TimeSpan.FromMinutes(2));
        using var resp = await SendAsync(new HttpRequestMessage(HttpMethod.Get, Api(FilePath(remote))), cts.Token);
        if (!IsJson(resp))
            throw new BooBootException("not a directory: " + remote, 0, "not_a_directory");
        return (await ReadJsonAsync(resp, cts.Token)).GetProperty("entries");
    }

    /// <summary>Copies a card file to a local file. Returns its size.</summary>
    public async Task<long> GetFileAsync(string remote, string localPath)
    {
        using var cts = new CancellationTokenSource(Timeout + TimeSpan.FromMinutes(10));
        using var resp = await SendAsync(new HttpRequestMessage(HttpMethod.Get, Api(FilePath(remote))), cts.Token);
        if (IsJson(resp))
            throw new BooBootException("is a directory: " + remote, 0, "is_a_directory");
        using var file = File.Create(localPath);
        await resp.Content.CopyToAsync(file, cts.Token);
        return file.Length;
    }

    /// <summary>Copies a local file to the card. Parent directories are created.</summary>
    public async Task<JsonElement> PutFileAsync(string localPath, string remote)
    {
        using var file = File.OpenRead(localPath);
        return await SendFileAsync(HttpMethod.Put, FilePath(remote), file, TimeSpan.FromMinutes(10));
    }

    public Task<JsonElement> MkdirAsync(string remote) =>
        SendJsonAsync(HttpMethod.Put, FilePath(remote) + Query(("dir", 1)), null, TimeSpan.FromMinutes(2));

    public Task<JsonElement> DeleteAsync(string remote, bool recursive = false) =>
        SendJsonAsync(HttpMethod.Delete, FilePath(remote) + Query(("recursive", recursive ? 1 : null)), null,
            TimeSpan.FromMinutes(2));

    // Console. Each byte of output has a cursor. "since" is a cursor, a negative number
    // (bytes before the end), or "start", "boot" (last power on), "last" (end of the
    // last expect or run match) or "now".

    /// <summary>Console output: text, cursor, next. Waits up to wait seconds for new output.</summary>
    /// <param name="timestamps">Start each line with its time since power on.</param>
    public Task<JsonElement> ReadAsync(string since = "boot", double wait = 0, bool clean = false,
        bool timestamps = false) =>
        SendJsonAsync(HttpMethod.Get,
            "/console" + Query(("since", since), ("wait", wait > 0 ? wait : null), ("clean", clean ? 1 : null),
                ("timestamps", timestamps ? 1 : null)),
            null, TimeSpan.FromSeconds(wait));

    /// <summary>Raw console bytes, and the cursor to read from next.</summary>
    public async Task<(byte[] Data, long Next)> ReadRawAsync(string since = "boot", double wait = 0,
        bool timestamps = false)
    {
        var path = "/console" + Query(("since", since), ("wait", wait > 0 ? wait : null), ("format", "raw"),
            ("timestamps", timestamps ? 1 : null));
        using var cts = new CancellationTokenSource(Timeout + TimeSpan.FromSeconds(wait));
        using var resp = await SendAsync(new HttpRequestMessage(HttpMethod.Get, Api(path)), cts.Token);
        var data = await resp.Content.ReadAsByteArrayAsync(cts.Token);
        var next = resp.Headers.TryGetValues("X-Next", out var values) ? long.Parse(values.First()) : 0;
        return (data, next);
    }

    /// <summary>
    /// Console events as they come, until the token is canceled. The first event is "hello".
    /// "ping" comes after 10 s without other events. Throws BooBootException when the
    /// connection is lost, or after 30 s without any event.
    /// </summary>
    public async IAsyncEnumerable<ConsoleEvent> StreamConsoleAsync(string since = "boot",
        [EnumeratorCancellation] CancellationToken token = default)
    {
        HttpResponseMessage resp;
        using (var cts = CancellationTokenSource.CreateLinkedTokenSource(token))
        {
            cts.CancelAfter(Timeout);
            var request = new HttpRequestMessage(HttpMethod.Get, Api("/console/stream" + Query(("since", since))));
            try
            {
                resp = await SendAsync(request, cts.Token);
            }
            catch (BooBootException) when (token.IsCancellationRequested)
            {
                throw new OperationCanceledException(token);
            }
        }
        using (resp)
        {
            using var reader = new StreamReader(await resp.Content.ReadAsStreamAsync(token), Encoding.UTF8);
            while (true)
            {
                string? line;
                try
                {
                    line = await reader.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(30), token);
                }
                catch (TimeoutException)
                {
                    throw new BooBootException($"no data from {Url} for 30 s");
                }
                catch (IOException e)
                {
                    throw new BooBootException($"connection to {Url} lost: {e.Message}");
                }
                if (line == null)
                    throw new BooBootException($"connection to {Url} closed");
                using var doc = JsonDocument.Parse(line);
                yield return Event(doc.RootElement.Clone());
            }
        }
    }

    static ConsoleEvent Event(JsonElement e)
    {
        string Text(string name) =>
            e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString()! : "";
        double Number(string name) =>
            e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : 0;
        var type = Text("type");
        var hello = type == "hello";
        return new ConsoleEvent(type, (long)Number("cursor"), (long)Number("next"), Text("text"),
            Text(hello ? "power" : "state"), Number("time"), e);
    }

    /// <summary>Sends text. newline adds the line ending set on the server.</summary>
    public Task<JsonElement> WriteAsync(string text, bool newline = false) =>
        SendJsonAsync(HttpMethod.Post, "/console/write", Body(("text", text), ("newline", newline)));

    /// <summary>Waits for a regex (Python syntax) in the console output.</summary>
    public async Task<MatchResult> ExpectAsync(string pattern, string since = "last", double timeout = 30,
        bool clean = false)
    {
        var r = await SendJsonAsync(HttpMethod.Post, "/console/expect",
            Body(("pattern", pattern), ("since", since), ("timeout", timeout), ("clean", clean)),
            TimeSpan.FromSeconds(timeout));
        return Match(r, "text");
    }

    /// <summary>Sends a command line and waits for the prompt regex (default set on the server).</summary>
    public async Task<MatchResult> RunAsync(string command, string? prompt = null, double timeout = 30,
        bool clean = true)
    {
        var r = await SendJsonAsync(HttpMethod.Post, "/console/run",
            Body(("command", command), ("prompt", prompt), ("timeout", timeout), ("clean", clean)),
            TimeSpan.FromSeconds(timeout));
        return Match(r, "output");
    }

    /// <summary>
    /// Power cycles the DUT and waits for a regex, like a login prompt.
    /// Returns the seconds from power on to the regex, or null on timeout.
    /// </summary>
    public async Task<double?> BootTimeAsync(string pattern, double timeout = 120, double? offTime = null)
    {
        await PowerCycleAsync(offTime);
        return (await ExpectAsync(pattern, since: "boot", timeout: timeout)).Time;
    }

    static MatchResult Match(JsonElement r, string textName) =>
        new MatchResult(r.GetProperty("matched").GetBoolean(), r.GetProperty(textName).GetString() ?? "",
            r.GetProperty("next").GetInt64(),
            r.TryGetProperty("time", out var t) && t.ValueKind == JsonValueKind.Number ? t.GetDouble() : null);

    // HTTP

    string Api(string path) => Base + "/api/v1" + path;

    static Dictionary<string, object?> Body(params (string Key, object? Value)[] items) =>
        items.Where(i => i.Value != null).ToDictionary(i => i.Key, i => i.Value);

    // Written by hand rather than with JsonSerializer, which trimmed programs cannot use.
    static string ToJson(Dictionary<string, object?> body)
    {
        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream))
        {
            writer.WriteStartObject();
            foreach (var (key, value) in body)
            {
                switch (value)
                {
                    case string s:
                        writer.WriteString(key, s);
                        break;
                    case bool b:
                        writer.WriteBoolean(key, b);
                        break;
                    case double d:
                        writer.WriteNumber(key, d);
                        break;
                    default:
                        throw new ArgumentException($"unsupported value for {key}: {value?.GetType()}");
                }
            }
            writer.WriteEndObject();
        }
        return Encoding.UTF8.GetString(stream.ToArray());
    }

    static string Query(params (string Key, object? Value)[] items)
    {
        var parts = items.Where(i => i.Value != null).Select(i =>
            i.Key + "=" + Uri.EscapeDataString(Convert.ToString(i.Value, CultureInfo.InvariantCulture) ?? ""));
        var query = string.Join("&", parts);
        return query.Length > 0 ? "?" + query : "";
    }

    static string FilePath(string remote)
    {
        var m = Regex.Match(remote, @"^(\d+):(.*)$");
        var part = m.Success ? m.Groups[1].Value : "1";
        var path = m.Success ? m.Groups[2].Value : remote;
        var segments = path.Split('/', StringSplitOptions.RemoveEmptyEntries).Select(Uri.EscapeDataString);
        return $"/sd/files/{part}/{string.Join("/", segments)}";
    }

    static bool IsJson(HttpResponseMessage resp) =>
        resp.Content.Headers.ContentType?.MediaType == "application/json";

    async Task<JsonElement> SendJsonAsync(HttpMethod method, string path, Dictionary<string, object?>? body = null,
        TimeSpan? wait = null)
    {
        var request = new HttpRequestMessage(method, Api(path));
        if (body != null)
            request.Content = new StringContent(ToJson(body), Encoding.UTF8, "application/json");
        using var cts = new CancellationTokenSource(Timeout + (wait ?? TimeSpan.Zero));
        using var resp = await SendAsync(request, cts.Token);
        return await ReadJsonAsync(resp, cts.Token);
    }

    async Task<JsonElement> SendFileAsync(HttpMethod method, string path, Stream data, TimeSpan wait)
    {
        var content = new StreamContent(data);
        content.Headers.ContentType = new MediaTypeHeaderValue("application/octet-stream");
        content.Headers.ContentLength = data.Length;
        using var cts = new CancellationTokenSource(Timeout + wait);
        using var resp = await SendAsync(new HttpRequestMessage(method, Api(path)) { Content = content }, cts.Token);
        return await ReadJsonAsync(resp, cts.Token);
    }

    async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
    {
        if (Session != null)
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", Session);
        HttpResponseMessage resp;
        try
        {
            resp = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, token);
        }
        catch (HttpRequestException e)
        {
            if (NameNotFound(e) && Base == Url && await UseOtherAddressAsync())
            {
                // Nothing was sent: the same request goes to the other address.
                var again = new HttpRequestMessage(request.Method, Base + request.RequestUri!.PathAndQuery)
                {
                    Content = request.Content,
                };
                return await SendAsync(again, token);
            }
            var hint = new Uri(Url).Host.EndsWith(".local", StringComparison.OrdinalIgnoreCase)
                ? " (.local names work only on the local network: use the name or address of the board)" : "";
            throw new BooBootException($"cannot reach {Url}: {e.Message}{hint}");
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
            throw new BooBootException($"no answer from {Url} in time");
        }
        if (!resp.IsSuccessStatusCode)
        {
            using (resp)
                throw await ErrorAsync(resp);
        }
        return resp;
    }

    async Task<JsonElement> ReadJsonAsync(HttpResponseMessage resp, CancellationToken token)
    {
        try
        {
            using var stream = await resp.Content.ReadAsStreamAsync(token);
            using var doc = await JsonDocument.ParseAsync(stream, cancellationToken: token);
            return doc.RootElement.Clone();
        }
        catch (Exception e) when (e is IOException or HttpRequestException or JsonException)
        {
            throw new BooBootException($"bad answer from {Url}: {e.Message}");
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
            throw new BooBootException($"no answer from {Url} in time");
        }
    }

    static bool NameNotFound(HttpRequestException e) =>
        e.InnerException is SocketException { SocketErrorCode: SocketError.HostNotFound or SocketError.NoData };

    async Task<bool> UseOtherAddressAsync()
    {
        var url = new Uri(Url);
        var others = new List<string>();
        if (url.Host.EndsWith(".local", StringComparison.OrdinalIgnoreCase))
            others.Add(url.Host[..^".local".Length]);
        others.AddRange(Addresses);
        foreach (var other in others)
        {
            var candidate = $"{url.Scheme}://{(other.Contains(':') ? $"[{other}]" : other)}:{url.Port}";
            try
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                using var resp = await http.GetAsync(candidate + "/api/v1/status", cts.Token);
                if (!resp.IsSuccessStatusCode)
                    continue;
            }
            catch (Exception e) when (e is HttpRequestException or OperationCanceledException)
            {
                continue;
            }
            Base = candidate;
            AddressUsed?.Invoke($"{url.Host} not found, using {other}");
            return true;
        }
        return false;
    }

    static async Task<BooBootException> ErrorAsync(HttpResponseMessage resp)
    {
        var status = (int)resp.StatusCode;
        var message = $"HTTP {status} {resp.ReasonPhrase}";
        try
        {
            using var doc = JsonDocument.Parse(await resp.Content.ReadAsStringAsync());
            var info = doc.RootElement.Clone();
            if (info.ValueKind == JsonValueKind.Object)
            {
                var code = info.TryGetProperty("error", out var c) ? c.GetString() ?? "error" : "error";
                if (info.TryGetProperty("message", out var m) && m.GetString() is string text)
                    message = text;
                return new BooBootException(message, status, code, info);
            }
        }
        catch (JsonException)
        {
        }
        return new BooBootException(message, status);
    }
}
