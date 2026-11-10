; =====================================================================================
;  cesium_setup.iss  -  CESIUM AI Race Engineer installer (Inno Setup 6.3+)
; =====================================================================================

#define AppName        "CESIUM AI Race Engineer"
#define AppPublisher   "CESIUM"
#define AppExeName     "CesiumAIRaceEngineer.exe"
#define AppURL         "https://github.com/CESIUM-CO/CESIUM-ai-race-engineer"
#ifndef AppVersion
  #define AppVersion   "0.1.0"
#endif
#define SourceDir      "dist"

#define SelfUpdateByStandardUsers 0
#if SelfUpdateByStandardUsers
  #define UsersRight "M"
#else
  #define UsersRight "RX"
#endif

[Setup]
AppId={{8F3B2C1E-5A47-4D9B-9E62-C3A7D1F04B58}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}

DefaultDirName={autopf}\CESIUM\AIRaceEngineer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline

AppMutex=CesiumAIRaceEngineer_SingleInstance
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no

OutputDir=installer_output
OutputBaseFilename=CesiumAIRaceEngineer_Setup_{#AppVersion}
SetupIconFile=cesium.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern

ShowLanguageDialog=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "installer\post_install.bat"; DestDir: "{tmp}"; Flags: deleteafterinstall

#if FileExists("redist\vc_redist.x64.exe")
Source: "redist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
#endif

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\cesium.ico"
Name: "{autoprograms}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\cesium.ico"; Tasks: desktopicon

[Run]
Filename: "{cmd}"; Parameters: "/C """"{tmp}\post_install.bat"" ""{app}"""""; \
  StatusMsg: "Configuring environment..."; Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM {#AppExeName}"; Flags: runhidden; RunOnceId: "KillCesium"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Security: hardening permissions on {app}
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Params: String;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := 'Securing the installation folder...';
    Params := '"' + ExpandConstant('{app}') + '" /inheritance:r' +
              ' /grant:r *S-1-5-18:(OI)(CI)F' +
              ' /grant:r *S-1-5-32-544:(OI)(CI)F' +
              ' /grant:r *S-1-5-32-545:(OI)(CI){#UsersRight}' +
              ' /T /C /Q';
    if Exec(ExpandConstant('{sys}\icacls.exe'), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
       and (ResultCode = 0) then
      Log('ACL hardening applied to ' + ExpandConstant('{app}'))
    else
      Log('WARNING: icacls returned ' + IntToStr(ResultCode));
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if (not UninstallSilent) and
       (MsgBox('Also delete your settings, logs, voice cache, downloaded AI models and memory?' + #13#10 +
               '(' + ExpandConstant('{userappdata}\CesiumAIRaceEngineer') + ')',
               mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      DelTree(ExpandConstant('{userappdata}\CesiumAIRaceEngineer'), True, True, True);
  end;
end;
