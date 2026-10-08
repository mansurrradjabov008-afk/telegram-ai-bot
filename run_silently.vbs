Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
BatchFile = ScriptDir & "\start_butler.bat"

' Run hidden (0 = invisible window, False = don't wait)
WshShell.Run """" & BatchFile & """", 0, False
