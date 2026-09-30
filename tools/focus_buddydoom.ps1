# Bring the buddydoom window to the foreground (tlics freeze when unfocused).
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class Win32Fg {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
'@
$p = Get-Process buddydoom -ErrorAction Stop
$h = $p.MainWindowHandle
[Win32Fg]::ShowWindow($h, 9) | Out-Null   # SW_RESTORE
$r = [Win32Fg]::SetForegroundWindow($h)
$fg = [Win32Fg]::GetForegroundWindow()
"activate pid=$($p.Id) title='$($p.MainWindowTitle)' setfg=$r fg_now_matches=$($fg -eq $h)"
