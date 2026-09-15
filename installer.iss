; Inno Setup script for WaraqStudio.
; The PyInstaller bundle under Windows\WaraqStudio already contains Python,
; Qt, FFmpeg and yt-dlp, so installing it makes the app fully self-contained.
; Version is injected by CI: ISCC /DAppVersion=1.0.0 installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Waraq Studio"
#define AppExeName "WaraqStudio.exe"

[Setup]
AppId={{8F4C2A61-5B7E-4A3E-9C1D-2E7A9B6D4C88}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=M-Jadallah
DefaultDirName={autopf}\WaraqStudio
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=WaraqStudio-{#AppVersion}-setup
SetupIconFile=waraq\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExeName}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "Windows\WaraqStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
