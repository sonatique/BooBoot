// BooBoot Console: shows the serial consoles of the DUTs of a board, live, without taking the session.
// Usage: BooBootConsole [URL]: the board, or one DUT at URL/duts/NAME

using System;
using Avalonia;

namespace BooBootConsole;

static class Program
{
    [STAThread]
    public static void Main(string[] args) => BuildAvaloniaApp().StartWithClassicDesktopLifetime(args);

    public static AppBuilder BuildAvaloniaApp() => AppBuilder.Configure<App>().UsePlatformDetect().LogToTrace();
}
