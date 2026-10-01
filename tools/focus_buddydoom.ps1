# Bring the buddydoom window to the foreground (tics freeze when unfocused).
# Windows' foreground lock sometimes refuses SetForegroundWindow from a
# background caller; the Alt-tap trick (send a synthetic menu key) makes the
# system treat our thread as having input state, which reliably unlocks it.
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class Win32Fg {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
}
'@
$p = Get-Process buddydoom -ErrorAction Stop
$h = $p.MainWindowHandle

# 1) Alt-tap: unlock the foreground for our thread
[Win32Fg]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)      # Alt down
[Win32Fg]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)      # Alt up (KEYEVENTF_KEYUP)

# 2) restore if minimized, then foreground
[Win32Fg]::ShowWindow($h, 6) | Out-Null                  # SW_MINIMIZE
[Win32Fg]::ShowWindow($h, 9) | Out-Null                  # SW_RESTORE
$r = [Win32Fg]::SetForegroundWindow($h)

$fg = [Win32Fg]::GetForegroundWindow()
"activate pid=$($p.Id) title='$($p.MainWindowTitle)' setfg=$r fg_now_matches=$($fg -eq $h)"
