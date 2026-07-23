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
[assembly: AssemblyVersion("1.1.12.0")]
[assembly: AssemblyFileVersion("1.1.12.0")]

namespace CCallHierarchyExplorerSetup
{
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            if (Array.Exists(args, delegate(string value) { return string.Equals(value, "/S", StringComparison.OrdinalIgnoreCase); }))
            {
                try
                {
                    using (InstallerForm installer = new InstallerForm()) installer.InstallSilently();
                    Environment.ExitCode = 0;
                }
                catch
                {
                    Environment.ExitCode = 1;
                }
                return;
            }
            using (InstallerForm installer = new InstallerForm())
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
    }

    internal sealed class InstallerForm : Form
    {
        private const string AppName = "C Call Hierarchy Explorer";
        private const string AppVersion = "1.1.12";
        private const string AppId = "CCallHierarchyExplorer";
        private const string ExeName = "C Call Hierarchy Explorer.exe";

        private readonly Label status;
        private readonly ProgressBar progress;
        private readonly Button installButton;
        internal bool LaunchAfterClose { get; private set; }

        internal InstallerForm()
        {
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
            if (Directory.Exists(installDir))
            {
                Directory.Delete(installDir, true);
            }
            Directory.CreateDirectory(installDir);
            status.Text = "프로그램 파일을 설치하고 있습니다...";
            Application.DoEvents();
            string payloadArchive = Path.Combine(Path.GetTempPath(), AppId + "-" + Guid.NewGuid().ToString("N") + ".zip");
            try
            {
                ExtractResource("Payload.zip", payloadArchive, true);
                ZipFile.ExtractToDirectory(payloadArchive, installDir);
            }
            finally
            {
                if (File.Exists(payloadArchive)) File.Delete(payloadArchive);
            }
            progress.Value = 82;
            ExtractResource("UninstallScript", Path.Combine(installDir, "uninstall.ps1"), false);
            progress.Value = 88;

            status.Text = "바로가기와 제거 정보를 등록하고 있습니다...";
            Application.DoEvents();
            string startMenu = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
                "Programs", AppName + ".lnk");
            string desktop = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                AppName + ".lnk");
            CreateShortcut(startMenu);
            CreateShortcut(desktop);
            RegisterUninstaller();
            progress.Value = 96;
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
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", AppId);
        }

        private static string GetInstalledExe()
        {
            return Path.Combine(GetInstallDir(), ExeName);
        }
    }
}
