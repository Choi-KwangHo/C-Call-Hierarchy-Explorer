using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("C Call Hierarchy Explorer Setup")]
[assembly: AssemblyDescription("Installer for C Call Hierarchy Explorer")]
[assembly: AssemblyCompany("Call Hierarchy Tools")]
[assembly: AssemblyProduct("C Call Hierarchy Explorer")]
[assembly: AssemblyVersion("1.1.14.0")]
[assembly: AssemblyFileVersion("1.1.14.0")]

namespace CCallHierarchyExplorerSetup
{
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            int waitForProcessId = ParseWaitProcessId(args);
            if (Array.Exists(args, delegate(string value) { return string.Equals(value, "/S", StringComparison.OrdinalIgnoreCase); }))
            {
                try
                {
                    using (InstallerForm installer = new InstallerForm(waitForProcessId)) installer.InstallSilently();
                    Environment.ExitCode = 0;
                }
                catch
                {
                    Environment.ExitCode = 1;
                }
                return;
            }
            using (InstallerForm installer = new InstallerForm(waitForProcessId))
            {
                Application.Run(installer);
                if (installer.LaunchAfterClose)
                {
                    // Give Windows Defender and the filesystem time to release the freshly
                    // written one-file executable before its Python runtime is extracted.
                    Thread.Sleep(1500);
                    installer.LaunchInstalledApplication();
                }
            }
        }

        private static int ParseWaitProcessId(string[] args)
        {
            for (int index = 0; index + 1 < args.Length; index++)
            {
                if (string.Equals(args[index], "--wait-pid", StringComparison.OrdinalIgnoreCase))
                {
                    int processId;
                    if (int.TryParse(args[index + 1], out processId) && processId > 0) return processId;
                }
            }
            return 0;
        }
    }

    internal sealed class InstallerForm : Form
    {
        private const string AppName = "C Call Hierarchy Explorer";
        private const string AppVersion = "1.1.14";
        private const string AppId = "CCallHierarchyExplorer";
        private const string ExeName = "C Call Hierarchy Explorer.exe";

        private readonly Label status;
        private readonly ProgressBar progress;
        private readonly Button installButton;
        private readonly int waitForProcessId;
        internal bool LaunchAfterClose { get; private set; }

        internal InstallerForm(int waitForProcessId)
        {
            this.waitForProcessId = waitForProcessId;
            Text = AppName + " Setup";
            ClientSize = new Size(560, 245);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(246, 249, 251);
            Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location);

            Label title = new Label();
            title.Text = AppName;
            title.Font = new Font("Segoe UI", 20, FontStyle.Regular);
            title.ForeColor = Color.FromArgb(23, 58, 86);
            title.AutoSize = true;
            title.Location = new Point(28, 24);
            Controls.Add(title);

            Label description = new Label();
            description.Text = "C 함수 호출 계층 분석 도구  ·  버전 " + AppVersion;
            description.Font = new Font("Malgun Gothic", 9);
            description.ForeColor = Color.FromArgb(75, 93, 108);
            description.AutoSize = true;
            description.Location = new Point(31, 71);
            Controls.Add(description);

            status = new Label();
            status.Text = "현재 사용자 계정에 설치할 준비가 되었습니다.";
            status.Font = new Font("Malgun Gothic", 9);
            status.ForeColor = Color.FromArgb(45, 62, 75);
            status.AutoSize = false;
            status.Location = new Point(31, 111);
            status.Size = new Size(495, 24);
            Controls.Add(status);

            progress = new ProgressBar();
            progress.Location = new Point(31, 142);
            progress.Size = new Size(495, 18);
            progress.Minimum = 0;
            progress.Maximum = 100;
            Controls.Add(progress);

            installButton = new Button();
            installButton.Text = "설치";
            installButton.Font = new Font("Malgun Gothic", 9);
            installButton.Location = new Point(428, 187);
            installButton.Size = new Size(98, 32);
            installButton.Click += InstallClicked;
            Controls.Add(installButton);

            Button cancelButton = new Button();
            cancelButton.Text = "취소";
            cancelButton.Font = new Font("Malgun Gothic", 9);
            cancelButton.Location = new Point(320, 187);
            cancelButton.Size = new Size(98, 32);
            cancelButton.Click += delegate { Close(); };
            Controls.Add(cancelButton);
        }

        private void InstallClicked(object sender, EventArgs e)
        {
            installButton.Enabled = false;
            UseWaitCursor = true;
            try
            {
                InstallApplication();
                progress.Value = 100;
                status.Text = "설치가 완료되었습니다. 프로그램을 시작합니다.";
                Application.DoEvents();
                MessageBox.Show(this, "설치가 완료되었습니다.", AppName, MessageBoxButtons.OK, MessageBoxIcon.Information);
                LaunchAfterClose = true;
                Close();
            }
            catch (Exception error)
            {
                status.Text = "설치 중 오류가 발생했습니다.";
                MessageBox.Show(this, error.Message, AppName + " Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
                installButton.Enabled = true;
            }
            finally
            {
                UseWaitCursor = false;
            }
        }

        internal void InstallSilently()
        {
            InstallApplication();
        }

        internal void LaunchInstalledApplication()
        {
            Process.Start(new ProcessStartInfo(GetInstalledExe()) { UseShellExecute = true });
        }

        private void InstallApplication()
        {
            string installDir = GetInstallDir();
            string parentDir = Path.GetDirectoryName(installDir);
            Directory.CreateDirectory(parentDir);
            string transactionId = Guid.NewGuid().ToString("N");
            string stagingDir = installDir + ".new-" + transactionId;
            string backupDir = installDir + ".backup-" + transactionId;
            string payloadArchive = Path.Combine(Path.GetTempPath(), AppId + "-" + Guid.NewGuid().ToString("N") + ".zip");
            bool previousMoved = false;
            bool newInstalled = false;
            try
            {
                status.Text = "새 버전 파일을 준비하고 있습니다...";
                Application.DoEvents();
                Directory.CreateDirectory(stagingDir);
                ExtractResource("Payload.zip", payloadArchive, true);
                ZipFile.ExtractToDirectory(payloadArchive, stagingDir);
                ExtractResource("UninstallScript", Path.Combine(stagingDir, "uninstall.ps1"), false);
                ValidateStagedApplication(stagingDir);
                progress.Value = 82;

                WaitForPreviousApplication();
                status.Text = "검증된 새 버전으로 교체하고 있습니다...";
                Application.DoEvents();
                if (Directory.Exists(installDir))
                {
                    Directory.Move(installDir, backupDir);
                    previousMoved = true;
                }
                Directory.Move(stagingDir, installDir);
                newInstalled = true;
                progress.Value = 88;

                status.Text = "바로가기와 제거 정보를 등록하고 있습니다...";
                Application.DoEvents();
                if (!IsTestInstall())
                {
                    string startMenu = Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
                        "Programs", AppName + ".lnk");
                    string desktop = Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                        AppName + ".lnk");
                    CreateShortcut(startMenu);
                    CreateShortcut(desktop);
                    RegisterUninstaller();
                }
                TryDeleteDirectory(backupDir);
                previousMoved = false;
                progress.Value = 96;
            }
            catch (Exception installError)
            {
                Exception rollbackError = null;
                try
                {
                    if (newInstalled && Directory.Exists(installDir)) Directory.Delete(installDir, true);
                    if (previousMoved && Directory.Exists(backupDir)) Directory.Move(backupDir, installDir);
                }
                catch (Exception error)
                {
                    rollbackError = error;
                }
                if (rollbackError != null)
                {
                    throw new IOException(
                        "설치에 실패했고 이전 버전 자동 복원도 완료하지 못했습니다. 백업 폴더를 보존했습니다: "
                        + backupDir + Environment.NewLine + rollbackError.Message,
                        installError);
                }
                throw new IOException(
                    "설치에 실패했지만 이전 버전은 안전하게 복원했습니다. 실행 중인 프로그램을 닫고 설치 버튼을 다시 누르십시오."
                    + Environment.NewLine + installError.Message,
                    installError);
            }
            finally
            {
                if (File.Exists(payloadArchive)) File.Delete(payloadArchive);
                TryDeleteDirectory(stagingDir);
                if (!previousMoved) TryDeleteDirectory(backupDir);
            }
        }

        private void ValidateStagedApplication(string stagingDir)
        {
            string executable = Path.Combine(stagingDir, ExeName);
            string pythonDll = Path.Combine(stagingDir, "_internal", "python312.dll");
            if (!File.Exists(executable)) throw new IOException("새 버전 실행 파일이 설치 패키지에 없습니다.");
            if (!File.Exists(pythonDll)) throw new IOException("새 버전 Python 런타임 DLL이 설치 패키지에 없습니다.");
        }

        private void WaitForPreviousApplication()
        {
            DateTime deadline = DateTime.UtcNow.AddSeconds(IsTestInstall() ? 3 : 30);
            status.Text = "실행 중인 이전 버전이 종료되기를 기다리고 있습니다...";
            Application.DoEvents();
            while (DateTime.UtcNow < deadline)
            {
                bool parentExited = true;
                if (waitForProcessId > 0)
                {
                    try
                    {
                        using (Process process = Process.GetProcessById(waitForProcessId))
                        {
                            parentExited = process.HasExited;
                        }
                    }
                    catch (ArgumentException)
                    {
                        parentExited = true;
                    }
                }
                if (parentExited && CanReplaceInstalledApplication()) return;
                Application.DoEvents();
                Thread.Sleep(250);
            }
            throw new IOException("기존 프로그램이 아직 실행 중입니다. 프로그램을 완전히 닫은 후 설치 버튼을 다시 누르십시오.");
        }

        private bool CanReplaceInstalledApplication()
        {
            string executable = GetInstalledExe();
            if (!File.Exists(executable)) return true;
            try
            {
                using (FileStream stream = new FileStream(executable, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { }
                return true;
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
        }

        private static void TryDeleteDirectory(string path)
        {
            try
            {
                if (Directory.Exists(path)) Directory.Delete(path, true);
            }
            catch
            {
                // A verified installation must not be reported as failed only because backup cleanup was delayed.
            }
        }

        private void ExtractResource(string resourceName, string destination, bool updateProgress)
        {
            using (Stream source = Assembly.GetExecutingAssembly().GetManifestResourceStream(resourceName))
            {
                if (source == null) throw new InvalidOperationException("설치 리소스를 찾을 수 없습니다: " + resourceName);
                using (FileStream output = new FileStream(destination, FileMode.Create, FileAccess.Write, FileShare.None))
                {
                    byte[] buffer = new byte[1024 * 1024];
                    long written = 0;
                    int read;
                    while ((read = source.Read(buffer, 0, buffer.Length)) > 0)
                    {
                        output.Write(buffer, 0, read);
                        written += read;
                        if (updateProgress && source.Length > 0)
                        {
                            progress.Value = Math.Min(80, (int)(written * 80 / source.Length));
                            Application.DoEvents();
                        }
                    }
                }
            }
        }

        private void CreateShortcut(string shortcutPath)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(shortcutPath));
            Type shellType = Type.GetTypeFromProgID("WScript.Shell");
            object shell = Activator.CreateInstance(shellType);
            object shortcut = shellType.InvokeMember(
                "CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { shortcutPath });
            Type shortcutType = shortcut.GetType();
            shortcutType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { GetInstalledExe() });
            shortcutType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { GetInstallDir() });
            shortcutType.InvokeMember("IconLocation", BindingFlags.SetProperty, null, shortcut, new object[] { GetInstalledExe() + ",0" });
            shortcutType.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "Analyze C function call hierarchies" });
            shortcutType.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
            Marshal.FinalReleaseComObject(shortcut);
            Marshal.FinalReleaseComObject(shell);
        }

        private void RegisterUninstaller()
        {
            string keyPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\" + AppId;
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(keyPath))
            {
                string uninstaller = Path.Combine(GetInstallDir(), "uninstall.ps1");
                key.SetValue("DisplayName", AppName);
                key.SetValue("DisplayVersion", AppVersion);
                key.SetValue("Publisher", "Call Hierarchy Tools");
                key.SetValue("DisplayIcon", GetInstalledExe());
                key.SetValue("InstallLocation", GetInstallDir());
                key.SetValue("UninstallString", "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"" + uninstaller + "\"");
                key.SetValue("NoModify", 1, RegistryValueKind.DWord);
                key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                key.SetValue("EstimatedSize", (int)(new FileInfo(GetInstalledExe()).Length / 1024), RegistryValueKind.DWord);
            }
        }

        private static string GetInstallDir()
        {
            string testDirectory = Environment.GetEnvironmentVariable("CCH_INSTALLER_TEST_DIR");
            if (!string.IsNullOrWhiteSpace(testDirectory))
            {
                string fullTarget = Path.GetFullPath(testDirectory);
                string fullTemp = Path.GetFullPath(Path.GetTempPath());
                if (!fullTarget.StartsWith(fullTemp, StringComparison.OrdinalIgnoreCase)
                    || !Path.GetFileName(fullTarget).StartsWith("CCH-InstallerTest-", StringComparison.Ordinal))
                {
                    throw new InvalidOperationException("Unsafe installer test directory.");
                }
                return fullTarget;
            }
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", AppId);
        }

        private static bool IsTestInstall()
        {
            return !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("CCH_INSTALLER_TEST_DIR"));
        }

        private static string GetInstalledExe()
        {
            return Path.Combine(GetInstallDir(), ExeName);
        }
    }
}
