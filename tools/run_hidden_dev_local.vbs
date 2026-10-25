' Hidden launcher for dev_local.ps1 (used by the game to start services).
' Usage: wscript run_hidden_dev_local.vbs <dev_local.ps1> [-QualityTts] [-NoWaitTts]
' Always appends -NoGame: this entry only starts services, never a second game window.
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count = 0 Then WScript.Quit 1
scriptPath = WScript.Arguments(0)
Set fso = CreateObject("Scripting.FileSystemObject")
sh.CurrentDirectory = fso.GetParentFolderName(scriptPath)
ps = sh.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
cmd = """" & ps & """ -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptPath & """ -NoGame"
For i = 1 To WScript.Arguments.Count - 1
    cmd = cmd & " " & WScript.Arguments(i)
Next
sh.Run cmd, 0, False
