# 获取固定版本的官方构建材料；所有写入都限制在项目内。
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskTools = Join-Path $taskRoot '.deps\packaging'
$taskSources = Join-Path $taskRoot 'work\release\sources'
New-Item -ItemType Directory -Path $taskTools,$taskSources -Force | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-ReleaseFile([string]$Url, [string]$Destination, [string]$Sha256) {
    if (!(Test-Path -LiteralPath $Destination) -or
        ($Sha256 -and (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ne $Sha256)) {
        $temporary = $Destination + '.download'
        Invoke-WebRequest -Uri $Url -OutFile $temporary -UseBasicParsing
        if ($Sha256 -and (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ne $Sha256) {
            throw ('官方材料 SHA-256 不匹配，停止构建：' + [IO.Path]::GetFileName($Destination))
        }
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    }
}

Get-ReleaseFile 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' (Join-Path $taskTools 'python-3.12.10-embed-amd64.zip') '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
$taskInnoSetup = Join-Path $taskTools 'innosetup-6.7.3.exe'
Get-ReleaseFile 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe' $taskInnoSetup '9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732'
$taskSignature = Get-AuthenticodeSignature -LiteralPath $taskInnoSetup
if ($taskSignature.Status -ne 'Valid' -or $taskSignature.SignerCertificate.Subject -notmatch 'O=Pyrsys B.V.') { throw 'Inno Setup 官方数字签名无效。' }
$taskInnoDirectory = Join-Path $taskTools 'inno'
if (!(Test-Path -LiteralPath (Join-Path $taskInnoDirectory 'ISCC.exe'))) {
    $taskSetupProcess = Start-Process -FilePath $taskInnoSetup -ArgumentList @('/PORTABLE=1', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', '/NOICONS', ('/DIR="' + $taskInnoDirectory + '"')) -WindowStyle Hidden -PassThru -Wait
    if ($taskSetupProcess.ExitCode -ne 0) { throw '便携构建工具准备失败。' }
}
Get-ReleaseFile 'https://raw.githubusercontent.com/jrsoftware/issrc/is-6_7_3/Files/Languages/Unofficial/ChineseSimplified.isl' (Join-Path $taskInnoDirectory 'Languages\ChineseSimplified.isl') '7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278'
$taskWebViewSetup = Join-Path $taskTools 'MicrosoftEdgeWebview2Setup.exe'
Get-ReleaseFile 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' $taskWebViewSetup ''
$taskSignature = Get-AuthenticodeSignature -LiteralPath $taskWebViewSetup
if ($taskSignature.Status -ne 'Valid' -or $taskSignature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation(?:,|$)') { throw 'WebView2 官方数字签名无效。' }

$taskSourceEntries = @(
    @{name='BabelDOC'; version='0.6.4'; sha256='dbd2a69ccaf6678c34089f8c422a38a0fa170f5fa88ee1313b4235103421a875'},
    @{name='PyMuPDF'; version='1.26.7'; sha256='71add8bdc8eb1aaa207c69a13400693f06ad9b927bea976f5d5ab9df0bb489c3'}
)
$taskSourceManifest = @()
foreach ($entry in $taskSourceEntries) {
    $metadata = Invoke-RestMethod -Uri ('https://pypi.org/pypi/' + $entry.name + '/' + $entry.version + '/json')
    $source = $metadata.urls | Where-Object { $_.packagetype -eq 'sdist' } | Select-Object -First 1
    if (!$source -or $source.digests.sha256 -ne $entry.sha256) { throw ('上游源码清单变化：' + $entry.name) }
    Get-ReleaseFile $source.url (Join-Path $taskSources $source.filename) $entry.sha256
    $taskSourceManifest += @{file=$source.filename; url=$source.url; sha256=$entry.sha256}
}
$taskMuPdfUrl = 'https://mupdf.com/downloads/archive/mupdf-1.26.12-source.tar.gz'
$taskMuPdfHash = '6baf910928f404167ba49be6340195dec340795724722b331f5a2143f5aa0d01'
Get-ReleaseFile $taskMuPdfUrl (Join-Path $taskSources 'mupdf-1.26.12-source.tar.gz') $taskMuPdfHash
$taskSourceManifest += @{file='mupdf-1.26.12-source.tar.gz'; url=$taskMuPdfUrl; sha256=$taskMuPdfHash}
$taskUtf8 = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $taskSources 'manifest.json'), ($taskSourceManifest | ConvertTo-Json -Depth 5), $taskUtf8)
Write-Output '官方构建工具、私有 Python 运行时和第三方对应源码准备完成。'
