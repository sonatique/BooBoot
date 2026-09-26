// Copies boot files to the SD card, boots the DUT, logs in and runs a command.
//   dotnet run --project client/csharp/Example -- http://booboot.local:8080 BOOT.BIN image.ub

using BooBoot;

if (args.Length < 1)
{
    Console.Error.WriteLine("usage: Example URL [FILE...]");
    return 2;
}

using var dut = new BooBootClient(args[0]);
try
{
    await dut.OpenSessionAsync("csharp-example");
}
catch (BooBootException e) when (e.Code == "busy")
{
    Console.Error.WriteLine("busy: " + e.Message);
    return 4;
}

try
{
    await dut.PowerOffAsync();
    foreach (var file in args.Skip(1))
        await dut.PutFileAsync(file, "1:/" + Path.GetFileName(file));
    await dut.PowerOnAsync();

    var boot = await dut.ExpectAsync("login: ", since: "boot", timeout: 120);
    Console.WriteLine(boot.Text);
    if (!boot.Matched)
        return 3;

    await dut.RunAsync("root");
    Console.Write((await dut.RunAsync("uname -a")).Text);
    return 0;
}
finally
{
    await dut.CloseSessionAsync();
}
