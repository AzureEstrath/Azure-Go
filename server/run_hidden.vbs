' 隐藏窗口启动一个命令行程序（游戏自动拉起本地语音服务用）。
' pythonw.exe 加载 CosyVoice 时会静默崩溃，所以改用 python.exe + 本脚本隐藏窗口。
' 用法: wscript run_hidden.vbs <程序路径> [参数...]
Set sh = CreateObject("WScript.Shell")
If WScript.Arguments.Count = 0 Then WScript.Quit 1
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
  cmd = cmd & """" & WScript.Arguments(i) & """ "
Next
' 工作目录 = 本脚本所在目录（随包移动也正确，模型用相对路径加载）
Set fso = CreateObject("Scripting.FileSystemObject")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run cmd, 0, False