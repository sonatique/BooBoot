// BooBoot Console: shows the serial console of a DUT, live, without taking the session.
// Usage: BooBootConsole [URL]

using System;
using Avalonia;

namespace BooBootConsole;

static class Program
{
    [STAThread]
    public static void Main(string[] args) => BuildAvaloniaApp().StartWithClassicDesktopLifetime(args);

    public static AppBuilder BuildAvaloniaApp() => AppBuilder.Configure<App>().UsePlatformDetect().LogToTrace();
}
