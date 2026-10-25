; Script Inno Setup pour DeeKeep - Music Downloader
; Compilation : ISCC.exe installer.iss  (après le build PyInstaller,
; qui doit avoir produit dist\DeeKeep\DeeKeep.exe)

#define MyAppName "DeeKeep"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "DeeKeep"
#define MyAppExeName "DeeKeep.exe"

[Setup]
; AppId identifie l'application de façon unique dans le registre Windows
; (Applications installées / désinstallation). Ne pas le changer entre versions.
AppId={{A950B103-5953-4FD2-98DF-431C819F2A07}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} - Music Downloader
; Installation par utilisateur possible sans droits administrateur
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=installer
OutputBaseFilename=DeeKeep-Setup-{#MyAppVersion}
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Icône sur le bureau : proposée mais optionnelle (décochable)
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Copie l'intégralité de la sortie PyInstaller, qui embarque déjà FFmpeg
; (dossier ffmpeg\) et le runtime JavaScript Deno (dossier deno\, requis
; par yt-dlp pour l'extraction YouTube). Aucune dépendance à installer sur
; le poste : tout est livré avec l'application.
Source: "dist\DeeKeep\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "DeeKeep.MusicDownloader"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; AppUserModelID: "DeeKeep.MusicDownloader"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
// Les paramètres et l'historique sont stockés dans %APPDATA%\DeeKeep,
// hors du dossier d'installation : ils sont donc conservés lors des
// réinstallations et mises à jour. À la désinstallation, on propose
// (sans l'imposer) de les supprimer.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\{#MyAppName}');
    if DirExists(DataDir) then
    begin
      if MsgBox('Voulez-vous également supprimer vos paramètres et votre historique ?'
                + #13#10 + '(' + DataDir + ')',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
