$TargetFile = "C:\Users\msk07\Desktop\Main\31_Scripts\Python\win2ios_fileshare\dist\win2ios_fileshare.exe"
$ShortcutFile = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\win2ios_fileshare.lnk"
$WScriptShell = New-Object -ComObject WScript.Shell
$Shortcut = $WScriptShell.CreateShortcut($ShortcutFile)
$Shortcut.TargetPath = $TargetFile
$Shortcut.WorkingDirectory = "C:\Users\msk07\Desktop\Main\31_Scripts\Python\win2ios_fileshare\dist"
$Shortcut.Description = "Win2iOS File Share Server"
$Shortcut.Save()
Write-Host "Shortcut created at $ShortcutFile"
