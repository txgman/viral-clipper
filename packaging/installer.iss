; Inno Setup script: builds ViralClipper-Setup-<version>.exe from dist\ViralClipper
#define AppVersion GetEnv("APP_VERSION")
#if AppVersion == ""
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6B2F3C1E-9A4D-4E7B-8C21-5F0A9D3E7B44}
AppName=Viral Clipper
AppVersion={#AppVersion}
AppPublisher=Viral Clipper
DefaultDirName={autopf}\Viral Clipper
DefaultGroupName=Viral Clipper
DisableProgramGroupPage=yes
; Installs for the current user only, so no administrator prompt is needed
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=ViralClipper-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\ViralClipper.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\ViralClipper\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Viral Clipper"; Filename: "{app}\ViralClipper.exe"
Name: "{group}\Uninstall Viral Clipper"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Viral Clipper"; Filename: "{app}\ViralClipper.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ViralClipper.exe"; Description: "Start Viral Clipper now"; Flags: nowait postinstall skipifsilent
