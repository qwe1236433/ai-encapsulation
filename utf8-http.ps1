# UTF-8 JSON over HTTP：避免 Invoke-RestMethod / Invoke-WebRequest 在无 charset 时误解码导致中文乱码。
# Windows PowerShell 5.x / PowerShell 7+ 可用；依赖 .NET HttpClient。

Add-Type -AssemblyName System.Net.Http

function Invoke-HttpUtf8 {
    param(
        [ValidateSet("Get", "Post")]
        [string] $Method = "Get",
        [Parameter(Mandatory = $true)]
        [string] $Uri,
        [string] $JsonBody = $null,
        [int] $TimeoutSec = 120
    )

    $handler = New-Object System.Net.Http.HttpClientHandler
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
    try {
        if ($Method -eq "Get") {
            $resp = $client.GetAsync($Uri).GetAwaiter().GetResult()
        }
        else {
            $enc = New-Object System.Text.UTF8Encoding $false
            $content = New-Object System.Net.Http.StringContent($JsonBody, $enc, "application/json")
            $resp = $client.PostAsync($Uri, $content).GetAwaiter().GetResult()
        }
        $bytes = $resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        if (-not $resp.IsSuccessStatusCode) {
            throw "HTTP $([int]$resp.StatusCode): $text"
        }
        return $text
    }
    finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Write-JsonUtf8File {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,
        [Parameter(Mandatory = $true)]
        [string] $JsonText
    )
    $enc = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($Path, $JsonText, $enc)
}
