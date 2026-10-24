; ---------------------------------------------------------------------------
;  Cesium AI Race Engineer - Inno Setup script (Inno Setup 6.3+)
;  Build the app first:  build_nuitka.bat   ->  dist_release\main_pitwall.dist\
;  Then compile this script. Output: installer_out\CesiumAIRaceEngineer_Setup_<ver>.exe
; ---------------------------------------------------------------------------

#define MyAppName      "Cesium AI Race Engineer"
#define MyAppVersion   "0.1.0"            ; keep in step with config.APP_VERSION (updater compares tags)
#define MyAppPublisher "Cesium"
#define MyAppExeName   "CesiumAIRaceEngineer.exe"
#define MyAppURL       "https://github.com/CESIUM-CO/CESIUM-ai-race-engineer"
#define BuildDir       "dist_release\main_pitwall.dist"
#define ModelFile      "models\qwen2.5-1.5b-instruct-q4_k_m.gguf"

[Setup]
AppId={{B7C3E5A2-6D14-4F0B-9A57-CE51UM000001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases

; Per-user install: the in-app updater must be able to overwrite its own files.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; The app holds this mutex while running (gui.acquire_single_instance)
AppMutex=Local\CesiumAIRaceEngineer_SingleInstance
CloseApplications=yes
RestartApplications=no

SetupIconFile=cesium.ico
UninstallDisplayIcon={app}\cesium.ico
WizardStyle=modern
WizardImageFile=wizard_side.bmp
WizardSmallImageFile=wizard_small.bmp

OutputDir=installer_out
OutputBaseFilename=CesiumAIRaceEngineer_Setup_{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Components]
Name: "app";   Description: "Cesium AI Race Engineer"; Types: full compact custom; Flags: fixed
#ifexist ModelFile
Name: "model"; Description: "Bundled AI model (about 1 GB, skips the first-run download)"; Types: full
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The whole Nuitka standalone folder: exe + DLLs + espeakng_loader + llama_cpp + ac_app + cesium.ico ...
Source: "{#BuildDir}\*"; DestDir: "{app}"; Components: app; \
    Flags: ignoreversion recursesubdirs createallsubdirs

; Safety net: make sure the icon and the in-game spotter app exist even if the build changes
Source: "cesium.ico"; DestDir: "{app}"; Components: app; Flags: ignoreversion
Source: "ac_app\PitwallSpotter\*"; DestDir: "{app}\ac_app\PitwallSpotter"; Components: app; \
    Excludes: "__pycache__,*.pyc,spotter_path.txt"; Flags: ignoreversion recursesubdirs createallsubdirs

#ifexist ModelFile
; Optional bundled model. local_llm.find_model() also searches <exe folder>\models
Source: "{#ModelFile}"; DestDir: "{app}\models"; Components: model; Flags: ignoreversion
#endif

; NOT shipped on purpose: data\, logs\, voice_cache\, config.py, *.db, credentials.json.
; All of that is created per user in %APPDATA%\CesiumAIRaceEngineer at first run.

[Dirs]
; models\ is preserved by the updater (UPDATE_PRESERVE) and used by find_model()
Name: "{app}\models"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\cesium.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\cesium.ico"; Tasks: desktopicon

[Run]
; Opens the app's own wizard (installer_gui.py via --installer). It asks for admin only if the game folder needs it.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--installer"; \
    Description: "Install the in-game spotter app for Assetto Corsa"; \
    Flags: postinstall nowait skipifsilent unchecked
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
    Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Files added by the self-updater are not in Inno's uninstall log
Type: filesandordirs; Name: "{app}\_update"
Type: filesandordirs; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  UserData: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    UserData := ExpandConstant('{userappdata}\CesiumAIRaceEngineer');
    if DirExists(UserData) and (not UninstallSilent) then
      if MsgBox('Also delete your settings, saved keys, memory, logs and downloaded AI / voice models?' + #13#10#13#10 +
                UserData, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(UserData, True, True, True);
  end;
end;