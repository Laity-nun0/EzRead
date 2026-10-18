; ISCC 6.7.3；所有路径由工作区内的构建入口传入。
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef WebViewSetup
  #error WebViewSetup is required
#endif

[Setup]
AppId=EzRead.Desktop.Installation
AppName=EzRead
AppVersion={#AppVersion}
AppPublisher=EzRead contributors
DefaultDirName={localappdata}\Programs\EzRead
DefaultGroupName=EzRead
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=EzRead-{#AppVersion}-Setup-x64
SetupIconFile={#PayloadDir}\static\ezread.ico
UninstallDisplayIcon={app}\EzRead.exe
LicenseFile={#PayloadDir}\LICENSE
InfoBeforeFile={#PayloadDir}\THIRD-PARTY-NOTICES.md
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
Uninstallable=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "data\*,__pycache__\*,*.pyc"
Source: "{#WebViewSetup}"; Flags: dontcopy

[Icons]
Name: "{group}\EzRead"; Filename: "{app}\EzRead.exe"; WorkingDir: "{app}"; IconFilename: "{app}\EzRead.exe"
Name: "{autodesktop}\EzRead"; Filename: "{app}\EzRead.exe"; WorkingDir: "{app}"; IconFilename: "{app}\EzRead.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\EzRead.exe"; Description: "{cm:LaunchProgram,EzRead}"; Flags: nowait postinstall skipifsilent unchecked

[Code]
function HasWebView2: Boolean;
var
  Version: String;
  Key: String;
begin
  { 微软官方 Evergreen Runtime 检测键：当前用户与机器级 32 位视图。 }
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := (RegQueryStringValue(HKCU, Key, 'pv', Version) and
             (Version <> '') and (Version <> '0.0.0.0'));
  if not Result then
    Result := (RegQueryStringValue(HKLM32, Key, 'pv', Version) and
               (Version <> '') and (Version <> '0.0.0.0'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := '';
  if HasWebView2 then exit;
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  { 不加 --system-level；以当前用户身份安装，不申请管理员权限。 }
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
              '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    Result := 'Microsoft WebView2 Runtime installation could not start. / 无法启动 WebView2 运行时安装。'
  else if (ExitCode <> 0) or (not HasWebView2) then
    Result := Format('Microsoft WebView2 Runtime installation failed (code %d). Check your network and retry. / WebView2 运行时安装失败，请检查网络后重试。', [ExitCode]);
end;
