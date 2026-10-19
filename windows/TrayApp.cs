using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace ClaudeCostLens
{
    public static class TrayApp
    {
        public static void Run(string python, string[] pythonArgs, string appPath, string statusPath)
        {
            bool created;
            using (Mutex instance = new Mutex(true, @"Local\ClaudeCostLens.Tray", out created))
            {
                if (!created) { WriteStatus(statusPath, "already_running", null); return; }
                try
                {
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    using (TrayContext context = new TrayContext(python, pythonArgs, appPath, statusPath))
                    {
                        context.StartBackend();
                        Application.Run(context);
                    }
                }
                finally { instance.ReleaseMutex(); }
            }
        }

        internal static void WriteStatus(string path, string state, string message)
        {
            Dictionary<string, object> data = new Dictionary<string, object>();
            data["state"] = state;
            data["message"] = message;
            File.WriteAllText(path, new JavaScriptSerializer().Serialize(data), new UTF8Encoding(false));
        }

        // Follow the Windows command-line quoting rules; no cmd.exe or shell interpolation.
        public static string QuoteArgument(string value)
        {
            StringBuilder result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char character in value)
            {
                if (character == '\\') { slashes++; continue; }
                if (character == '"') result.Append('\\', slashes * 2 + 1);
                else result.Append('\\', slashes);
                result.Append(character);
                slashes = 0;
            }
            result.Append('\\', slashes * 2);
            return result.Append('"').ToString();
        }

        public static string FormatTokens(double count)
        {
            if (count < 1000) return count.ToString("0", CultureInfo.InvariantCulture);
            string[] units = { "K", "M", "B" };
            double divisor = 1000;
            int unit = 0;
            while (unit < 2 && Math.Round(count / divisor, 2, MidpointRounding.AwayFromZero) >= 1000)
            { divisor *= 1000; unit++; }
            return (count / divisor).ToString("F2", CultureInfo.InvariantCulture) + units[unit];
        }
    }

    // Closing this host's handle kills only the Python processes assigned to this job.
    // Windows also closes the handle if the tray host is forcibly terminated.
    internal sealed class BackendJob : IDisposable
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimits
        {
            public long ProcessTime, JobTime;
            public uint Flags;
            public UIntPtr MinWorkingSet, MaxWorkingSet;
            public uint ActiveProcesses;
            public UIntPtr Affinity;
            public uint PriorityClass, SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters
        {
            public ulong ReadOperations, WriteOperations, OtherOperations;
            public ulong ReadBytes, WriteBytes, OtherBytes;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimits
        {
            public BasicLimits Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits info, uint length);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);
        private IntPtr handle;

        public BackendJob()
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            ExtendedLimits limits = new ExtendedLimits();
            limits.Basic.Flags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if (!SetInformationJobObject(handle, 9, ref limits, (uint)Marshal.SizeOf(typeof(ExtendedLimits))))
            { int error = Marshal.GetLastWin32Error(); Dispose(); throw new Win32Exception(error); }
        }
        public void Add(Process process)
        {
            if (!AssignProcessToJobObject(handle, process.Handle)) throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        public void Dispose()
        {
            if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
        }
    }

    internal sealed class TrayContext : ApplicationContext
    {
        private readonly string python, appPath, statusPath;
        private readonly string[] pythonArgs;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue };
        private readonly Form popup = new Form();
        private readonly NotifyIcon tray = new NotifyIcon();
        private readonly ComboBox range = new ComboBox();
        private readonly Label cost = new Label(), tokens = new Label(), breakdown = new Label();
        private readonly Label details = new Label(), models = new Label(), coverage = new Label(), updated = new Label();
        private readonly Button open = new Button(), refresh = new Button();
        private readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        private readonly WebClient client = new WebClient();
        private BackendJob job;
        private Process backend;
        private Uri server;
        private Dictionary<string, object> snapshot;
        private DateTime started, requested, lastRefresh = DateTime.MinValue;
        private bool busy, exiting, reported;
        private string lastError = "正在读取本地用量…";

        public TrayContext(string python, string[] pythonArgs, string appPath, string statusPath)
        {
            this.python = python; this.pythonArgs = pythonArgs ?? new string[0];
            this.appPath = appPath; this.statusPath = statusPath;
            ConfigureWindow();
            // Create a stable UI-thread dispatch handle before background output events start.
            IntPtr dispatchHandle = popup.Handle;
            tray.Icon = MakeIcon();
            tray.Text = "Claude Cost Lens — 正在启动";
            ContextMenuStrip menu = new ContextMenuStrip();
            menu.Items.Add("查看用量", null, delegate { ShowPopup(); });
            menu.Items.Add("打开完整面板", null, delegate { OpenDashboard(); });
            menu.Items.Add("刷新", null, delegate { RefreshData(); });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("退出 Claude Cost Lens", null, delegate { ExitThread(); });
            tray.ContextMenuStrip = menu;
            tray.MouseClick += delegate(object sender, MouseEventArgs e) { if (e.Button == MouseButtons.Left) ShowPopup(); };
            tray.Visible = true;
            client.Encoding = Encoding.UTF8;
            client.Proxy = null; // Keep loopback usage data off system HTTP proxies.
            client.DownloadStringCompleted += Downloaded;
            timer.Interval = 500;
            timer.Tick += Tick;
            timer.Start();
        }

        private void ConfigureWindow()
        {
            popup.Text = "Claude Cost Lens";
            popup.ShowInTaskbar = false;
            popup.TopMost = true;
            popup.FormBorderStyle = FormBorderStyle.FixedToolWindow;
            popup.StartPosition = FormStartPosition.Manual;
            popup.AutoScaleDimensions = new SizeF(96, 96);
            popup.AutoScaleMode = AutoScaleMode.Dpi;
            popup.ClientSize = new Size(408, 640);
            popup.BackColor = Color.FromArgb(18, 23, 36);
            popup.ForeColor = Color.FromArgb(232, 235, 245);
            popup.Font = new Font("Segoe UI", 10);
            popup.Deactivate += delegate { popup.Hide(); };
            popup.FormClosing += delegate(object sender, FormClosingEventArgs e)
            {
                if (e.CloseReason == CloseReason.UserClosing && !exiting) { e.Cancel = true; popup.Hide(); }
            };
            FlowLayoutPanel body = new FlowLayoutPanel();
            body.Dock = DockStyle.Fill; body.FlowDirection = FlowDirection.TopDown;
            body.WrapContents = false; body.AutoScroll = true; body.Padding = new Padding(20, 16, 12, 12);
            popup.Controls.Add(body);
            Label title = NewLabel("Claude Cost Lens", 20, true, 350);
            body.Controls.Add(title);
            body.Controls.Add(NewLabel("本地统计 · 只读 · 含 Advisor", 10, false, 350));
            range.DropDownStyle = ComboBoxStyle.DropDownList;
            range.Items.AddRange(new object[] { "今天", "近 7 天", "近 30 天", "全部本地数据" });
            range.Width = 350; range.Margin = new Padding(0, 12, 0, 14);
            range.SelectedIndex = 0; range.SelectedIndexChanged += delegate { Render(); };
            body.Controls.Add(range);
            ConfigureLabel(cost, 31, true, 350); cost.ForeColor = Color.FromArgb(255, 204, 120);
            ConfigureLabel(tokens, 19, true, 350);
            ConfigureLabel(breakdown, 10, false, 350);
            ConfigureLabel(details, 12, false, 350);
            ConfigureLabel(coverage, 10, false, 350); coverage.ForeColor = Color.FromArgb(237, 184, 90);
            ConfigureLabel(models, 11, false, 350);
            ConfigureLabel(updated, 9, false, 350); updated.ForeColor = Color.FromArgb(151, 166, 194);
            body.Controls.AddRange(new Control[] { cost, tokens, breakdown, details, coverage, models, updated });
            FlowLayoutPanel buttons = new FlowLayoutPanel();
            buttons.Dock = DockStyle.Bottom; buttons.Height = 54; buttons.Padding = new Padding(16, 8, 0, 8);
            popup.Controls.Add(buttons);
            refresh.Text = "刷新"; refresh.AutoSize = true; refresh.ForeColor = Color.Black;
            refresh.Click += delegate { RefreshData(); };
            open.Text = "打开完整面板 ↗"; open.AutoSize = true; open.Enabled = false; open.ForeColor = Color.Black;
            open.Click += delegate { OpenDashboard(); };
            buttons.Controls.Add(refresh); buttons.Controls.Add(open);
            Render();
        }

        private static Label NewLabel(string text, float size, bool bold, int width)
        {
            Label label = new Label(); ConfigureLabel(label, size, bold, width); label.Text = text; return label;
        }
        private static void ConfigureLabel(Label label, float size, bool bold, int width)
        {
            label.AutoSize = true; label.MaximumSize = new Size(width, 0);
            label.Margin = new Padding(0, 0, 0, 12);
            label.Font = new Font("Segoe UI", size, bold ? FontStyle.Bold : FontStyle.Regular);
        }
        [DllImport("user32.dll")]
        private static extern bool DestroyIcon(IntPtr icon);
        private static Icon MakeIcon()
        {
            using (Bitmap bitmap = new Bitmap(32, 32))
            {
                using (Graphics graphics = Graphics.FromImage(bitmap))
                using (Brush brush = new SolidBrush(Color.FromArgb(145, 154, 255)))
                {
                    graphics.Clear(Color.Transparent);
                    graphics.FillRectangle(brush, 3, 16, 6, 12);
                    graphics.FillRectangle(brush, 13, 6, 6, 22);
                    graphics.FillRectangle(brush, 23, 11, 6, 17);
                }
                IntPtr handle = bitmap.GetHicon();
                try { using (Icon original = Icon.FromHandle(handle)) return (Icon)original.Clone(); }
                finally { DestroyIcon(handle); }
            }
        }

        private void Dispatch(Action action)
        {
            if (exiting || popup.IsDisposed) return;
            try { popup.BeginInvoke((MethodInvoker)delegate { if (!exiting) action(); }); }
            catch (InvalidOperationException) { }
        }
        public void StartBackend()
        {
            if (exiting || (backend != null && !backend.HasExited)) return;
            if (backend != null) { backend.Dispose(); backend = null; }
            if (job == null) job = new BackendJob();
            server = null; open.Enabled = false; started = DateTime.UtcNow;
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = python;
            List<string> args = new List<string>(pythonArgs);
            args.AddRange(new string[] { "-u", appPath, "--host", "127.0.0.1", "--port", "0" });
            info.Arguments = String.Join(" ", args.ConvertAll(TrayApp.QuoteArgument).ToArray());
            info.UseShellExecute = false; info.CreateNoWindow = true;
            info.RedirectStandardOutput = true; info.RedirectStandardError = true;
            info.StandardOutputEncoding = Encoding.UTF8; info.StandardErrorEncoding = Encoding.UTF8;
            info.WorkingDirectory = Path.GetDirectoryName(appPath);
            info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            Process child = new Process { StartInfo = info, EnableRaisingEvents = true };
            child.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                const string prefix = "Claude Cost Lens: ";
                Uri uri;
                if (e.Data != null && e.Data.StartsWith(prefix, StringComparison.Ordinal) &&
                    Uri.TryCreate(e.Data.Substring(prefix.Length), UriKind.Absolute, out uri) &&
                    uri.Scheme == "http" && uri.Host == "127.0.0.1" && uri.Port > 0 && uri.AbsolutePath == "/")
                {
                    Uri readyUri = uri;
                    Dispatch(delegate { if (backend == child) { server = readyUri; open.Enabled = true; RefreshData(); } });
                }
            };
            // Drain stderr; never save transcript contents or backend output to disk.
            child.ErrorDataReceived += delegate { };
            child.Exited += delegate { Dispatch(delegate {
                if (backend == child) { server = null; open.Enabled = false; Fail("统计服务已停止，点击刷新可重新启动。"); }
            }); };
            try
            {
                child.Start();
                backend = child;
                job.Add(child);
                child.BeginOutputReadLine(); child.BeginErrorReadLine();
            }
            catch
            {
                try { if (!child.HasExited) child.Kill(); } catch { }
                child.Dispose(); backend = null;
                throw;
            }
        }

        private void Tick(object sender, EventArgs e)
        {
            if (busy && (DateTime.UtcNow - requested).TotalSeconds > 20) client.CancelAsync();
            if (!reported && server == null && (DateTime.UtcNow - started).TotalSeconds > 45) Fail("统计服务启动超时。");
            if (!busy && (DateTime.UtcNow - lastRefresh).TotalSeconds >= 60) RefreshData();
        }
        private void RefreshData()
        {
            if (exiting || busy) return;
            try
            {
                if (backend == null || backend.HasExited) { StartBackend(); return; }
                if (server == null) return;
                busy = true; requested = DateTime.UtcNow; lastRefresh = requested; refresh.Enabled = false;
                client.DownloadStringAsync(new Uri(server, "api/summary"));
            }
            catch (Exception error) { busy = false; refresh.Enabled = true; Fail("刷新失败：" + error.Message); }
        }
        private void Downloaded(object sender, DownloadStringCompletedEventArgs e)
        {
            busy = false; refresh.Enabled = true;
            if (exiting) return;
            if (e.Cancelled || e.Error != null) { Fail("刷新失败，保留上次数据。点击刷新重试。"); return; }
            try
            {
                snapshot = json.Deserialize<Dictionary<string, object>>(e.Result);
                if (snapshot == null || !snapshot.ContainsKey("periods")) throw new InvalidDataException("Invalid summary");
                lastError = null; Render();
                if (!reported) { TrayApp.WriteStatus(statusPath, "ready", null); reported = true; }
            }
            catch (Exception error) { Fail("读取统计失败：" + error.Message); }
        }
        private void Fail(string error)
        {
            lastError = error; Render();
            if (!reported) { TrayApp.WriteStatus(statusPath, "error", error); reported = true; ExitThread(); }
        }

        private static Dictionary<string, object> Map(object value)
        { return value as Dictionary<string, object> ?? new Dictionary<string, object>(); }
        private static object Get(Dictionary<string, object> value, string key)
        { object result; return value.TryGetValue(key, out result) ? result : null; }
        private static double Number(Dictionary<string, object> value, string key)
        { object raw = Get(value, key); return raw == null ? 0 : Convert.ToDouble(raw, CultureInfo.InvariantCulture); }
        private static string Money(double value)
        { return "$" + value.ToString("F2", CultureInfo.InvariantCulture); }
        private static double Total(Dictionary<string, object> value)
        { return Number(value, "inputTokens") + Number(value, "outputTokens") + Number(value, "cacheReadInputTokens") + Number(value, "cacheCreationInputTokens"); }
        private static double Combined(Dictionary<string, object> value)
        { return Get(value, "combined_cost") == null ? Number(value, "cost") : Number(value, "combined_cost"); }

        private void Render()
        {
            if (snapshot == null) { cost.Text = "正在读取…"; coverage.Text = lastError; return; }
            bool allLocal = range.SelectedIndex == 3;
            string key = range.SelectedIndex == 1 ? "week" : range.SelectedIndex == 2 ? "month" : "day";
            Dictionary<string, object> period = allLocal ? Map(Get(snapshot, "totals")) : Map(Get(Map(Get(snapshot, "periods")), key));
            Dictionary<string, object> counts = Map(Get(period, "tokens"));
            int pending = 0, uncertain = 0, estimates = 0, partial = 0;
            DateTime end;
            if (!DateTime.TryParseExact(Convert.ToString(Get(period, "reference")), "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out end)) end = DateTime.UtcNow.AddHours(9).Date;
            string endDay = end.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
            string startDay = end.AddDays(range.SelectedIndex == 1 ? -6 : range.SelectedIndex == 2 ? -29 : 0).ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
            IEnumerable sessions = Get(snapshot, "sessions") as IEnumerable;
            if (sessions != null) foreach (object raw in sessions)
            {
                Dictionary<string, object> session = Map(raw);
                string last = Convert.ToString(Get(session, "last"));
                if (!allLocal)
                {
                    if (last.Length < 10) continue;
                    string day = last.Substring(0, 10);
                    if (String.CompareOrdinal(day, startDay) < 0 || String.CompareOrdinal(day, endDay) > 0) continue;
                }
                string status = Convert.ToString(Get(session, "cost_status"));
                if ((status == "pending" || status == "missing") && Get(session, "estimated_cost") == null) pending++;
                if (status == "unreconciled") uncertain++;
                if (Get(session, "estimated_cost") != null)
                { estimates++; if (!Object.Equals(Get(session, "estimate_complete"), true)) partial++; }
            }
            double amount = Combined(period);
            string amountText = (estimates > 0 ? "≈" : "") + Money(amount);
            if (amount == 0 && estimates == 0 && (pending > 0 || uncertain > 0)) amountText = uncertain > 0 ? "待对账" : "待写入";
            cost.Text = amountText;
            tokens.Text = TrayApp.FormatTokens(Total(counts)) + " Token";
            breakdown.Text = "原生 " + Money(Number(period, "cost")) + "  +  API 估算 " + Money(Number(period, "estimated_cost"));
            details.Text = "输入      " + TrayApp.FormatTokens(Number(counts, "inputTokens")) +
                "\n输出      " + TrayApp.FormatTokens(Number(counts, "outputTokens")) +
                "\n缓存读取  " + TrayApp.FormatTokens(Number(counts, "cacheReadInputTokens")) +
                "\n缓存写入  " + TrayApp.FormatTokens(Number(counts, "cacheCreationInputTokens"));
            coverage.Text = (allLocal ? "全部本地：" : "所选日期：") + estimates + " 个会话使用估算（" + partial + " 个部分估算）\n" +
                pending + " 个原生费用待写入 · " + uncertain + " 个原生基数待对账";
            StringBuilder modelText = new StringBuilder("累计模型用量\n");
            IEnumerable modelRows = Get(snapshot, "models") as IEnumerable;
            int shown = 0;
            if (modelRows != null) foreach (object raw in modelRows)
            {
                Dictionary<string, object> row = Map(raw);
                modelText.Append(Convert.ToString(Get(row, "model"))).Append("   ")
                    .Append(TrayApp.FormatTokens(Total(Map(Get(row, "tokens"))))).AppendLine();
                if (++shown >= 3) break;
            }
            models.Text = modelText.ToString().TrimEnd();
            updated.Text = "累计 " + Number(snapshot, "session_count").ToString("N0") + " 个会话 · " + Number(snapshot, "usage_event_count").ToString("N0") + " 个请求\n" +
                (lastError ?? ("每分钟自动刷新 · " + DateTime.Now.ToString("HH:mm:ss")));
            if (lastError != null) coverage.Text += "\n" + lastError;
            Dictionary<string, object> today = Map(Get(Map(Get(snapshot, "periods")), "day"));
            string tip = "Claude Cost Lens | 今日 " + (Number(today, "estimated_cost") > 0 ? "≈" : "") + Money(Combined(today)) + " | " + TrayApp.FormatTokens(Total(Map(Get(today, "tokens"))));
            tray.Text = tip.Length > 63 ? tip.Substring(0, 63) : tip;
        }
        private void ShowPopup()
        {
            if (popup.Visible) { popup.Hide(); return; }
            Rectangle area = Screen.FromPoint(Cursor.Position).WorkingArea;
            popup.Height = Math.Min(680, Math.Max(240, area.Height - 16));
            popup.Width = Math.Min(popup.Width, Math.Max(240, area.Width - 16));
            popup.Location = new Point(Math.Max(area.Left + 8, Math.Min(Cursor.Position.X - popup.Width, area.Right - popup.Width - 8)),
                Math.Max(area.Top + 8, Math.Min(Cursor.Position.Y - popup.Height, area.Bottom - popup.Height - 8)));
            popup.Show(); popup.Activate();
        }
        private void OpenDashboard()
        {
            if (server == null) return;
            try { Process.Start(new ProcessStartInfo(server.AbsoluteUri) { UseShellExecute = true }); }
            catch (Exception error) { Fail("无法打开浏览器：" + error.Message); }
        }
        protected override void ExitThreadCore()
        {
            exiting = true; base.ExitThreadCore();
        }
        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                exiting = true; timer.Stop(); timer.Dispose();
                client.CancelAsync(); client.Dispose();
                tray.Visible = false;
                if (tray.Icon != null) tray.Icon.Dispose();
                if (tray.ContextMenuStrip != null) tray.ContextMenuStrip.Dispose();
                tray.Dispose();
                if (job != null) { job.Dispose(); job = null; }
                if (backend != null) { backend.Dispose(); backend = null; }
                popup.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
