' Lance l'interface web Alternance Auto sans fenetre console
Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(baseDir, ".venv\Scripts\pythonw.exe")
appScript = fso.BuildPath(baseDir, "app.py")
pidFile = fso.BuildPath(baseDir, "app_server.pid")
url = "http://127.0.0.1:5001"
quote = Chr(34)

WshShell.CurrentDirectory = baseDir

Function ProcessExists(pid)
  On Error Resume Next
  Dim exec, output
  Set exec = WshShell.Exec("cmd /c tasklist /FI ""PID eq " & pid & """ /FO CSV /NH")
  output = LCase(exec.StdOut.ReadAll())
  ProcessExists = (InStr(output, """" & pid & """") > 0)
  On Error GoTo 0
End Function

If fso.FileExists(pidFile) Then
  On Error Resume Next
  pid = Trim(fso.OpenTextFile(pidFile, 1).ReadAll)
  On Error GoTo 0
  If pid <> "" Then
    If ProcessExists(pid) Then
      WshShell.Run "cmd /c start " & quote & quote & " " & quote & url & quote, 0, False
      WScript.Quit 0
    Else
      On Error Resume Next
      fso.DeleteFile pidFile, True
      On Error GoTo 0
    End If
  End If
End If

' 0 = fenetre cachee, False = ne pas attendre la fin
WshShell.Run quote & pythonw & quote & " " & quote & appScript & quote, 0, False
