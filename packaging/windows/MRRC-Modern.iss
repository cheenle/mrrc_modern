#define MyAppName "MRRC Modern"
#define MyAppVersion "1.8.0"
#define MyAppPublisher "cheenle"
#define MyAppURL "https://github.com/cheenle/mrrc_modern"
#define MyAppServerName "MRRC-Modern-Server.exe"
#define MyAppLauncherName "MRRC-Modern-Launcher.exe"

[Setup]
AppId={{5E7E7ED7-3EA2-414E-B549-14F9F656D2F3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\MRRC Modern
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\windows
OutputBaseFilename=MRRC-Modern-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\..\dist\windows\MRRC-Modern\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppLauncherName}"
Name: "{group}\{#MyAppName} Server"; Filename: "{app}\{#MyAppServerName}"
Name: "{group}\Edit Configuration"; Filename: "notepad.exe"; Parameters: """{localappdata}\MRRC-Modern\ft710.env"""
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppLauncherName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#MyAppLauncherName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Messages]
FinishedHeadingLabel=Completing {#MyAppName} Setup
FinishedLabel=Setup has finished installing {#MyAppName} on your computer.%n%nLaunch "{#MyAppName}" from the Start Menu to start the server and open the web UI.%n%nSelect your radio model by setting MRRC_RADIO_MODEL in %LOCALAPPDATA%\MRRC-Modern\ft710.env (choices: ft710, ic7300, ic7300mk2; default: ft710).
