# biting-lobster-bot

#Comando para iniciar Chrome
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\BitingLobsterProfile"

cd "C:\Users\Charly Kano\PythonProjects\biting-lobster-bot"
$p = ".\data\SessionManager.py"
$b = [System.IO.File]::ReadAllBytes((Resolve-Path $p))
if ($b.Length -ge 2 -and $b[0] -eq 0x66 -and $b[1] -eq 0x00) {
  $t = [System.Text.Encoding]::Unicode.GetString($b)
} else { $t = [System.Text.Encoding]::UTF8.GetString($b) }
[System.IO.File]::WriteAllText((Resolve-Path $p), $t, (New-Object System.Text.UTF8Encoding $false))

