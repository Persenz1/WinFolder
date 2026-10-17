' WinFolder 启动器：双击本文件即可在后台启动（无控制台窗口）
Option Explicit
Dim sh, fso, root, pyw, script, config, reader
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
config = root & "\pythonw.path"
If Not fso.FileExists(config) Then
    MsgBox "Missing pythonw.path. Ask your agent to configure Python; see README.md.", 48, "WinFolder"
    WScript.Quit 1
End If
Set reader = fso.OpenTextFile(config, 1, False, -1)
If reader.AtEndOfStream Then
    pyw = ""
Else
    pyw = Trim(reader.ReadLine)
End If
reader.Close
If Not fso.FileExists(pyw) Then
    MsgBox "Python was not found. Update pythonw.path for this computer.", 48, "WinFolder"
    WScript.Quit 1
End If
script = root & "\run.pyw"
sh.CurrentDirectory = root
sh.Run """" & pyw & """ """ & script & """", 0, False
