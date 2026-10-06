// Checks all calls of BooBootClient against a server with a fake board:
//   cd server && python3 -m booboot_server --fake --port 8080
//   dotnet run --project client/csharp/Check -- http://127.0.0.1:8080

using System.Globalization;
using System.IO.Compression;
using BooBoot;

var url = args.Length > 0 ? args[0] : "http://127.0.0.1:8080";
var tmp = Path.Combine(Path.GetTempPath(), "booboot-check-" + Path.GetRandomFileName());
Directory.CreateDirectory(tmp);

// Numbers must be sent in the invariant format, also with a comma as decimal separator.
try
{
    CultureInfo.CurrentCulture = new CultureInfo("fr-FR");
}
catch (CultureNotFoundException)
{
}

using var dut = new BooBootClient(url);
using var other = new BooBootClient(url);

Check((await dut.StatusAsync()).GetProperty("version").GetString() != null, "status");

// When the name of the server is not found, like a .local name over a VPN: its other addresses.
var server = new Uri(url);
using (var named = new BooBootClient($"http://booboot-none.invalid:{server.Port}") { Addresses = { server.Host } })
{
    var note = "";
    named.AddressUsed += text => note = text;
    Check((await named.StatusAsync()).GetProperty("name").GetString() == "dut1"
        && named.Base == $"http://{server.Host}:{server.Port}"
        && note == $"booboot-none.invalid not found, using {server.Host}", "other address when the name is not found");
}
using (var local = new BooBootClient($"http://booboot-none.local:{server.Port}"))
    Check((await Fails(() => local.StatusAsync())).Message.Contains(".local names work only on the local network"),
        "hint when a .local name is not found");

await dut.OpenSessionAsync("csharp-check");
var busy = await Fails(() => other.OpenSessionAsync("other"));
Check(busy.Status == 423 && busy.Code == "busy", "second client is refused");
Check(busy.Info?.GetProperty("session").GetProperty("client").GetString() == "csharp-check",
    "busy error names the client");
Check((await Fails(() => other.PowerOnAsync())).Code == "busy", "calls without the session are refused");
Check((await dut.HeartbeatAsync()).GetProperty("alive").GetBoolean(), "heartbeat");
var connected = await Fails(() => other.OpenSessionAsync("other", ifGone: true));
Check(connected.Code == "busy" && connected.Info?.GetProperty("session").GetProperty("alive").GetBoolean() == true,
    "a connected client keeps the session");

// Files
await dut.PowerOffAsync();
var boot = Path.Combine(tmp, "BOOT.BIN");
File.WriteAllBytes(boot, Enumerable.Range(0, 5000).Select(i => (byte)i).ToArray());
Check((await dut.PutFileAsync(boot, "1:/BOOT.BIN")).GetProperty("size").GetInt32() == 5000, "put file");
Check((await dut.ListDirAsync("1:/")).EnumerateArray().Any(e => e.GetProperty("name").GetString() == "BOOT.BIN"),
    "list directory");
var copy = Path.Combine(tmp, "copy.bin");
Check(await dut.GetFileAsync("/BOOT.BIN", copy) == 5000, "get file");
Check(File.ReadAllBytes(copy).SequenceEqual(File.ReadAllBytes(boot)), "same content");
Check((await Fails(() => dut.ListDirAsync("1:/BOOT.BIN"))).Code == "not_a_directory", "list a file");
Check((await Fails(() => dut.GetFileAsync("1:/", copy))).Code == "is_a_directory", "get a directory");
await dut.MkdirAsync("2:/dir with space");
Check((await dut.ListDirAsync("2:/")).EnumerateArray()
    .Any(e => e.GetProperty("name").GetString() == "dir with space"), "make directory");
await dut.DeleteAsync("2:/dir with space");
Check((await Fails(() => dut.ListDirAsync("2:/dir with space"))).Status == 404, "delete directory");

// Image
var raw = new byte[3 << 20];
new Random(1).NextBytes(raw.AsSpan(0, 1 << 20));
var image = Path.Combine(tmp, "card.img.gz");
using (var f = File.Create(image))
using (var gz = new GZipStream(f, CompressionLevel.Fastest))
    gz.Write(raw);
var written = await dut.WriteImageAsync(image, verify: true);
Check(written.GetProperty("bytes").GetInt64() == raw.Length && written.GetProperty("verified").GetBoolean(),
    "write image");
Check((await dut.PartitionsAsync()).GetProperty("partitions").GetArrayLength() > 0, "partitions");

// Boot and console
Check((await dut.PowerOnAsync()).GetProperty("power").GetString() == "on", "power on");
var login = await dut.ExpectAsync("login: $", since: "boot", timeout: 10);
Check(login.Matched && login.Text.Contains("U-Boot"), "expect login prompt");
Check(login.Time > 0.1 && login.Time < 2, "time from power on to the login prompt");
Check((await dut.RunAsync("root", timeout: 5)).Text == "", "log in");
var uname = await dut.RunAsync("uname -a", timeout: 5);
Check(uname.Matched && uname.Text == "Linux fake 6.6.0-fake #1 SMP armv7l GNU/Linux\n", "run command");
Check(!(await dut.ExpectAsync("never", since: "now", timeout: 0.3)).Matched, "expect timeout");
await dut.WriteAsync("echo written", newline: true);
Check((await dut.ExpectAsync("written\r\n")).Matched, "write");
var read = await dut.ReadAsync("boot", wait: 0.5, clean: true);
Check(read.GetProperty("text").GetString()!.Contains("Linux fake"), "read console");
Check((await dut.ReadAsync("boot", timestamps: true)).GetProperty("text").GetString()!.StartsWith("["),
    "read console with timestamps");
var (data, next) = await dut.ReadRawAsync("boot");
Check(data.Length > 0 && next == read.GetProperty("cursor").GetInt64() + data.Length, "read raw console");

// Stream, without session
using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(20)))
{
    var events = other.StreamConsoleAsync("now", cts.Token).GetAsyncEnumerator(cts.Token);
    Check(await events.MoveNextAsync() && events.Current.Type == "hello"
        && events.Current.Data.GetProperty("name").GetString() == "dut1", "stream hello");
    var cycle = await dut.PowerCycleAsync(0);
    var text = "";
    ConsoleEvent? on = null;
    while (!text.EndsWith("login: ") && await events.MoveNextAsync())
    {
        if (events.Current is { Type: "power", State: "on" })
            on = events.Current;
        else if (on != null && events.Current.Type == "output")
            text += events.Current.Text;
    }
    Check(on?.Cursor == cycle.GetProperty("boot").GetInt64()
        && Math.Abs(on.Time - DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0) < 10, "stream power on");
    Check(text.Contains("U-Boot 2024.01"), "stream output");
    cts.Cancel();
    Check(await Cancelled(() => events.MoveNextAsync().AsTask()), "stream cancel");
    await events.DisposeAsync();
}

var conflict = await Fails(() => dut.SdModeAsync("host"));
Check(conflict.Status == 409 && conflict.Code == "power_on", "card refused while powered");
Check((await dut.PowerCycleAsync(0)).GetProperty("power").GetString() == "on", "power cycle");
var bootTime = await dut.BootTimeAsync("login: $", timeout: 10, offTime: 0);
Check(bootTime > 0.1 && bootTime < 2, "boot time");
Check(await dut.BootTimeAsync("never", timeout: 0.5, offTime: 0) == null, "boot time timeout");
await dut.KeepaliveAsync();

// Clean up and release
await dut.PowerOffAsync();
await dut.DeleteAsync("1:/BOOT.BIN");
await dut.SdModeAsync("dut");
await dut.CloseSessionAsync();
await other.OpenSessionAsync("other");
await other.CloseSessionAsync();
Check(true, "session released");

using var nobody = new BooBootClient("http://127.0.0.1:9", TimeSpan.FromSeconds(5));
Check((await Fails(() => nobody.StatusAsync())).Status == 0, "server not reachable");

Directory.Delete(tmp, true);
Console.WriteLine("all checks passed");
return 0;

static void Check(bool ok, string what)
{
    if (!ok)
        throw new Exception("check failed: " + what);
    Console.WriteLine("ok: " + what);
}

static async Task<bool> Cancelled(Func<Task> call)
{
    try
    {
        await call();
    }
    catch (OperationCanceledException)
    {
        return true;
    }
    return false;
}

static async Task<BooBootException> Fails(Func<Task> call)
{
    try
    {
        await call();
    }
    catch (BooBootException e)
    {
        return e;
    }
    throw new Exception("no error");
}
