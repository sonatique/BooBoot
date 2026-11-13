using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;

namespace BooBootConsole;

/// <summary>A window with the view of one DUT, and its bar, out of the main window.</summary>
public sealed class DutWindow : Window
{
    // Set once made: the base class changes the size before.
    readonly bool made;

    public DutWindow(DutTab tab, WindowPlace place)
    {
        Tab = tab;
        Place = place;
        MinWidth = 300;
        MinHeight = 150;
        WindowStartupLocation = WindowStartupLocation.Manual;
        Position = new PixelPoint(place.X, place.Y);
        Width = place.Width;
        Height = place.Height;
        if (place.Maximized)
            WindowState = WindowState.Maximized;
        Content = tab.Pane;
        ShowTitle();
        tab.Changed += ShowTitle;
        Closed += (_, _) => tab.Changed -= ShowTitle;
        PositionChanged += (_, e) =>
        {
            if (WindowState != WindowState.Normal)
                return;
            (Place.X, Place.Y) = (e.Point.X, e.Point.Y);
            Moved?.Invoke();
        };
        made = true;
    }

    public DutTab Tab { get; }

    /// <summary>Where the window is. Its size is the one while not maximized.</summary>
    public WindowPlace Place { get; }

    /// <summary>Raised when the window moves, changes size, or is maximized or restored.</summary>
    public event Action? Moved;

    /// <summary>Raised with Ctrl or Cmd and a key not handled by the view, for the commands of the main window.</summary>
    public event Action<KeyEventArgs>? CommandKey;

    /// <summary>Shows the window in front, also when minimized.</summary>
    public void Bring()
    {
        if (WindowState == WindowState.Minimized)
            WindowState = Place.Maximized ? WindowState.Maximized : WindowState.Normal;
        Activate();
    }

    void ShowTitle() => Title = $"{Tab.DisplayName} - {MainWindow.ProgramName}";

    protected override void OnPropertyChanged(AvaloniaPropertyChangedEventArgs change)
    {
        base.OnPropertyChanged(change);
        if (!made)
            return;
        if (change.Property == WindowStateProperty && WindowState != WindowState.Minimized)
        {
            Place.Maximized = WindowState == WindowState.Maximized;
            Moved?.Invoke();
        }
        else if (change.Property == ClientSizeProperty && WindowState == WindowState.Normal)
        {
            (Place.Width, Place.Height) = (ClientSize.Width, ClientSize.Height);
            Moved?.Invoke();
        }
    }

    protected override void OnKeyDown(KeyEventArgs e)
    {
        base.OnKeyDown(e);
        if (!e.Handled && (e.KeyModifiers.HasFlag(KeyModifiers.Control) || e.KeyModifiers.HasFlag(KeyModifiers.Meta)))
            CommandKey?.Invoke(e);
    }
}
