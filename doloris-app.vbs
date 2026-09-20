' Doloris Desktop Companion - Silent Background Launcher (No Console Window)
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "pythonw.exe -m doloris_app.main", 0, False
