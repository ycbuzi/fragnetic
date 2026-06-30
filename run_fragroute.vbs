' ============================================================
'  FRAGROUTE -- no-build launcher (runs the app with NO console window).
'  Double-click this file to start FRAGROUTE without building the .exe.
'  Requires Python 3 installed and on PATH.
'  Tip: right-click -> Send to -> Desktop (create shortcut) for easy access.
' ============================================================
Option Explicit
Dim sh, fso, appDir, cmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = appDir

' Prefer pythonw.exe (no console window); fall back to python.exe.
cmd = "pythonw"
On Error Resume Next
sh.Run "cmd /c where pythonw >nul 2>nul", 0, True
If Err.Number <> 0 Then cmd = "python"
On Error GoTo 0

' 0 = hidden window, False = don't wait. The app shows its own window / tray.
sh.Run cmd & " """ & appDir & "\fragroute_app.py""", 0, False
