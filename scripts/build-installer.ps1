param(
    [string]$PayloadDirectory = 'work/release/payload',
    [string]$OutputDirectory = 'output/releases',
    [string]$Compiler = '.deps/packaging/inno/ISCC.exe'
)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
function WorkspacePath([string]$Value) {
    $candidate = if ([IO.Path]::IsPathRooted($Value)) { $Value } else { Join-Path $taskRoot $Value }
    $absolute = [IO.Path]::GetFullPath($candidate)
    if (!$absolute.StartsWith(($taskRoot + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase)) {
        throw '构建路径必须位于当前工作区内，且不能是工作区根目录。'
    }
    # 拒绝链接或 junction，防止输出逃逸工作区。
    $walk = $absolute
    while ($walk -and $walk -ne $taskRoot) {
        if ((Test-Path -LiteralPath $walk) -and ((Get-Item -LiteralPath $walk -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw ('构建路径不能经过符号链接或 junction：' + $walk)
        }
        $walk = Split-Path -Parent $walk
    }
    return $absolute
}
$payload = WorkspacePath $PayloadDirectory
$output = WorkspacePath $OutputDirectory
$iscc = WorkspacePath $Compiler
$webviewSetup = WorkspacePath '.deps/packaging/MicrosoftEdgeWebview2Setup.exe'
$csc = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$python = Join-Path $payload 'runtime/python.exe'
foreach ($required in @($iscc, $csc, $python, $webviewSetup,
    (Join-Path $payload 'runtime/pythonw.exe'), (Join-Path $payload 'launch.pyw'),
    (Join-Path $payload 'static/ezread.ico'), (Join-Path $payload 'LICENSE'),
    (Join-Path $payload 'THIRD-PARTY-NOTICES.md'), (Join-Path $payload 'release-manifest.json'))) {
    if (!(Test-Path -LiteralPath $required -PathType Leaf)) { throw ('缺少构建输入：' + $required) }
}
if (Test-Path -LiteralPath (Join-Path $payload 'data')) { throw '安装 payload 不得包含 data 文献库。' }
if ($output.StartsWith(($payload + [IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase) -or $output -eq $payload) {
    throw '安装包输出目录不能位于 payload 内。'
}
$signature = Get-AuthenticodeSignature -LiteralPath $webviewSetup
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation(?:,|$)') {
    throw 'WebView2 安装器必须具有有效的微软数字签名。'
}
$versionMatch = [regex]::Match([IO.File]::ReadAllText((Join-Path $taskRoot 'ezread/config.py')), '(?m)^VERSION\s*=\s*[''"](\d+\.\d+\.\d+)[''"]\s*$')
if (!$versionMatch.Success) { throw '无法读取源码版本。' }
$version = $versionMatch.Groups[1].Value
$payloadVersion = (Get-Content -LiteralPath (Join-Path $payload 'release-manifest.json') -Raw | ConvertFrom-Json).version
if ($payloadVersion -ne $version) { throw 'payload 版本与当前源码版本不一致，请重新构建 payload。' }
[void](WorkspacePath (Join-Path $payload 'EzRead.exe'))
[void](WorkspacePath (Join-Path $payload 'EzRead.exe.config'))
$arguments = @(
    '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/utf8output',
    ('/out:' + (Join-Path $payload 'EzRead.exe')),
    ('/win32icon:' + (Join-Path $payload 'static/ezread.ico')),
    ('/win32manifest:' + (Join-Path $taskRoot 'desktop/app.manifest')),
    '/reference:System.dll', '/reference:System.Windows.Forms.dll',
    (Join-Path $taskRoot 'desktop/EzRead.Launcher.cs')
)
& $csc @arguments
if ($LASTEXITCODE -ne 0) { throw '安装入口编译失败。' }
[IO.File]::WriteAllText((Join-Path $payload 'EzRead.exe.config'), '<configuration><startup useLegacyV2RuntimeActivationPolicy="true"><supportedRuntime version="v4.0" sku=".NETFramework,Version=v4.8" /></startup></configuration>')
& $python -B (Join-Path $PSScriptRoot 'build-release.py') --output $payload --refresh-manifest
if ($LASTEXITCODE -ne 0) { throw '发布文件清单更新失败。' }
New-Item -ItemType Directory -Path $output -Force | Out-Null
& $iscc ('/DAppVersion=' + $version) ('/DPayloadDir=' + $payload) ('/DOutputDir=' + $output) ('/DWebViewSetup=' + $webviewSetup) (Join-Path $taskRoot 'installer/EzRead.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup 安装包编译失败。' }
$artifact = Join-Path $output ('EzRead-' + $version + '-Setup-x64.exe')
if (!(Test-Path -LiteralPath $artifact -PathType Leaf)) { throw '编译器未生成预期的安装包。' }
[PSCustomObject]@{
    FullName = $artifact
    Length = (Get-Item -LiteralPath $artifact).Length
    SHA256 = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
}
