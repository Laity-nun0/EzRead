using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class Launcher
{
    // 遵循 CommandLineToArgvW 的反斜杠和引号规则，不经过命令解释器。
    private static string Quote(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            result.Append(ch);
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    [STAThread]
    private static int Main(string[] args)
    {
        bool verification = Array.IndexOf(args, "--verification-session") >= 0;
        string data = Environment.GetEnvironmentVariable("EZREAD_DATA_DIR");
        try
        {
            string session = null;
            if (args.Length != 0)
            {
                Guid parsed;
                if (args.Length != 2 || args[0] != "--verification-session" ||
                    !Guid.TryParse(args[1], out parsed))
                    throw new ArgumentException("启动参数无效。仅支持 --verification-session <UUID>。");
                session = parsed.ToString("N");
            }
            string root = AppDomain.CurrentDomain.BaseDirectory;
            if (String.IsNullOrWhiteSpace(data))
                data = session == null
                    ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "EzRead", "data")
                    : Path.Combine(Path.GetTempPath(), "EzRead.Verification", session, "data");
            string python = Path.Combine(root, "runtime", "pythonw.exe");
            string launch = Path.Combine(root, "launch.pyw");
            if (!File.Exists(python) || !File.Exists(launch))
                throw new FileNotFoundException("EzRead 安装文件不完整，请重新安装。未找到内置 Python 运行时或启动脚本。");
            ProcessStartInfo start = new ProcessStartInfo(python);
            start.Arguments = "-B " + Quote(launch) + (session == null ? "" : " --verification-session " + Quote(session));
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            start.EnvironmentVariables["EZREAD_DATA_DIR"] = data;
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables.Remove("PYTHONPATH");
            using (Process child = Process.Start(start))
            {
                if (child == null) throw new InvalidOperationException("无法启动 EzRead 内置运行时。");
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            if (verification)
            {
                // 无有效隔离目录时只返回失败，不能回退写入真实文献库。
                if (!String.IsNullOrWhiteSpace(data))
                {
                    try
                    {
                        Directory.CreateDirectory(data);
                        File.AppendAllText(Path.Combine(data, "launcher.log"),
                            DateTime.UtcNow.ToString("o") + " " + error.Message + Environment.NewLine, Encoding.UTF8);
                    }
                    catch (Exception) { /* 诊断日志失败也不能弹出阻塞验证窗口。 */ }
                }
            }
            else MessageBox.Show(error.Message, "EzRead 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
