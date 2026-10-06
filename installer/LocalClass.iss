; Inno Setup 6 — установщик LocalClass (ТЗ 17.3): ярлык, автозапуск, разрешение в Windows Firewall.
; Сборка: iscc installer\LocalClass.iss   (после pyinstaller LocalClass.spec → dist\LocalClass\)

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "LocalClass"
#define AppExe "LocalClass.exe"
#define TcpPort "45821"
#define UdpPort "45820"

[Setup]
AppId={{7B1C1B0E-5C1A-4B7E-9C21-LOCALCLASS0001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=LocalClass
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\dist
OutputBaseFilename=LocalClass-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
#if FileExists("localclass.ico")
SetupIconFile=localclass.ico
#endif

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"
Name: "autostart"; Description: "Запускать LocalClass при входе в Windows"; GroupDescription: "Автозапуск:"; Flags: unchecked
Name: "firewall"; Description: "Разрешить LocalClass в брандмауэре Windows (TCP {#TcpPort}, UDP {#UdpPort})"; GroupDescription: "Сеть:"

[Files]
; localclass-node.exe — консольный узел для разработки и автотестов, обычному пользователю не нужен
Source: "..\dist\LocalClass\*"; DestDir: "{app}"; Excludes: "localclass-node.exe"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
; Правила брандмауэра: и по программе, и по портам — этого достаточно и для частных, и для общественных сетей.
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden; Tasks: firewall
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes profile=any"; Flags: runhidden; Tasks: firewall
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""{#AppName} TCP"" dir=in action=allow protocol=TCP localport={#TcpPort} profile=any"; Flags: runhidden; Tasks: firewall
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""{#AppName} UDP"" dir=in action=allow protocol=UDP localport={#UdpPort} profile=any"; Flags: runhidden; Tasks: firewall
Filename: "{app}\{#AppExe}"; Description: "Запустить {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Flags: runhidden; RunOnceId: "fw1"
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""{#AppName} TCP"""; Flags: runhidden; RunOnceId: "fw2"
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""{#AppName} UDP"""; Flags: runhidden; RunOnceId: "fw3"
