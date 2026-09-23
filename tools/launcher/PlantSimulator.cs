// Plant Simulator.exe - the double-click launcher for the portable build.
//
// What it does (and nothing more):
//   1. Finds the bundled Python (runtime\python.exe next to this exe; falls
//      back to a source checkout's .venv, or hands over to the .bat which
//      knows how to set one up).
//   2. Runs launch.py with it, HIDDEN - no black console window. launch.py
//      picks a free port, starts Streamlit and opens the browser.
//   3. Sits in the system tray with "Open in browser / Show log / Stop".
//      Everything the app prints goes to plant_simulator.log next to the exe.
//   4. A second double-click re-opens the browser instead of starting a
//      second server.
//
// Built by tools\build_launcher.ps1 with the C# compiler that ships in every
// Windows 10/11 (.NET Framework 4) - no SDK needed.

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("Dezignatek Plant Simulator")]
[assembly: AssemblyProduct("Plant Simulator")]
[assembly: AssemblyCompany("Dezignatek")]
[assembly: AssemblyDescription("Thermoform + Cut & Clash plant simulator")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

static class Program
{
    const string AppName = "Plant Simulator";

    [STAThread]
    static int Main()
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string urlFile = Path.Combine(dir, "plant_simulator.url");

        // One instance per folder: a second double-click just opens the browser.
        bool first;
        var mutex = new Mutex(true, "DezignatekPlantSimulator-" + dir.ToLowerInvariant().GetHashCode(), out first);
        if (!first)
        {
            string existing = File.Exists(urlFile) ? File.ReadAllText(urlFile).Trim() : "";
            if (existing.StartsWith("http")) OpenUrl(existing);
            else MessageBox.Show("The Plant Simulator is already starting up - look for its icon in the system tray " +
                                 "(bottom right, near the clock).", AppName, MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 0;
        }

        string python = FindPython(dir);
        if (python == null)
        {
            // Source checkout with nothing set up yet: the .bat knows how to
            // create the private environment (it needs a visible window for that).
            string bat = Path.Combine(dir, "Plant Simulator.bat");
            if (!File.Exists(bat)) bat = Path.Combine(dir, "resources", "Plant Simulator.bat");
            if (File.Exists(bat))
            {
                Process.Start(new ProcessStartInfo("cmd.exe", "/c \"\"" + bat + "\"\"") { WorkingDirectory = dir, UseShellExecute = true });
                return 0;
            }
            MessageBox.Show("No Python runtime was found next to " + AppName + ".exe.\n\nThis exe belongs inside the " +
                            "portable PlantSimulator folder (with its 'runtime' folder) - unzip the whole ZIP, then run it from there.",
                            AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        using (var ctx = new TrayContext(dir, python, urlFile))
        {
            Application.Run(ctx);
        }
        GC.KeepAlive(mutex);
        return 0;
    }

    static string FindPython(string dir)
    {
        string[] candidates = {
            Path.Combine(dir, "runtime", "python.exe"),           // portable build
            Path.Combine(dir, ".venv", "Scripts", "python.exe"),  // source checkout after first .bat run
        };
        foreach (var c in candidates) if (File.Exists(c)) return c;
        return null;
    }

    public static void OpenUrl(string url)
    {
        try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); } catch { }
    }
}

/// A Windows job object with "kill on close": every process put in it dies
/// when this launcher exits - however it exits, including Task Manager - so
/// a hidden server can never be left running behind.
static class KillOnCloseJob
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attrs, string name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit; public uint LimitFlags; public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct IO_COUNTERS { public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount; }
    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation; public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }
    const int JobObjectExtendedLimitInformation = 9;
    const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

    static IntPtr job = IntPtr.Zero;

    public static void Add(Process p)
    {
        try
        {
            if (job == IntPtr.Zero)
            {
                job = CreateJobObject(IntPtr.Zero, null);
                var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                int size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
                IntPtr buf = Marshal.AllocHGlobal(size);
                try
                {
                    Marshal.StructureToPtr(info, buf, false);
                    SetInformationJobObject(job, JobObjectExtendedLimitInformation, buf, (uint)size);
                }
                finally { Marshal.FreeHGlobal(buf); }
            }
            AssignProcessToJobObject(job, p.Handle);
        }
        catch { /* best effort - the Stop menu still kills the child explicitly */ }
    }
}

class TrayContext : ApplicationContext
{
    readonly string dir, urlFile, logFile;
    readonly NotifyIcon tray;
    readonly Process child;
    readonly StreamWriter log;
    readonly object logLock = new object();
    readonly Control ui = new Control();   // to hop back onto the UI thread from process events
    string url;
    bool stopping;

    public TrayContext(string dir, string python, string urlFile)
    {
        this.dir = dir;
        this.urlFile = urlFile;
        logFile = Path.Combine(dir, "plant_simulator.log");
        try { File.Delete(urlFile); } catch { }
        log = new StreamWriter(logFile, false) { AutoFlush = true };
        log.WriteLine("[" + DateTime.Now + "] Plant Simulator launcher starting: " + python);
        ui.CreateControl();

        var menu = new ContextMenuStrip();
        menu.Items.Add("Open in browser", null, (s, e) => { if (url != null) Program.OpenUrl(url); });
        menu.Items.Add("Show log", null, (s, e) => Program.OpenUrl(logFile));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Stop Plant Simulator", null, (s, e) => Stop());

        tray = new NotifyIcon
        {
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath),
            Text = "Plant Simulator - starting...",
            ContextMenuStrip = menu,
            Visible = true,
        };
        tray.DoubleClick += (s, e) => { if (url != null) Program.OpenUrl(url); };

        var psi = new ProcessStartInfo(python, "\"" + Path.Combine(dir, "launch.py") + "\"")
        {
            WorkingDirectory = dir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        child = new Process { StartInfo = psi, EnableRaisingEvents = true };
        child.OutputDataReceived += (s, e) => OnLine(e.Data);
        child.ErrorDataReceived += (s, e) => OnLine(e.Data);
        child.Exited += (s, e) => ui.BeginInvoke(new Action(OnExited));
        try
        {
            child.Start();
            KillOnCloseJob.Add(child);
            child.BeginOutputReadLine();
            child.BeginErrorReadLine();
        }
        catch (Exception ex)
        {
            Fail("Could not start the bundled Python:\n" + ex.Message);
        }
        tray.ShowBalloonTip(4000, "Plant Simulator", "Starting - your browser will open in a moment. " +
                            "Right-click this icon to stop it.", ToolTipIcon.Info);
    }

    void OnLine(string line)
    {
        if (line == null) return;
        lock (logLock) { try { log.WriteLine(line); } catch { } }
        if (url == null)
        {
            var m = Regex.Match(line, @"http://localhost:\d+");
            if (m.Success)
            {
                url = m.Value;
                try { File.WriteAllText(urlFile, url); } catch { }
                ui.BeginInvoke(new Action(() => tray.Text = "Plant Simulator - running at " + url));
            }
        }
    }

    void OnExited()
    {
        if (stopping) return;
        int code = 0;
        try { code = child.ExitCode; } catch { }
        if (code != 0)
            Fail("The simulator stopped unexpectedly (exit code " + code + ").\n\nThe log next to the exe says why:\n" + logFile);
        else
            Quit();
    }

    void Fail(string message)
    {
        stopping = true;
        try { tray.Visible = false; } catch { }
        MessageBox.Show(message, "Plant Simulator", MessageBoxButtons.OK, MessageBoxIcon.Error);
        Quit();
    }

    void Stop()
    {
        stopping = true;
        try { if (!child.HasExited) child.Kill(); } catch { }
        Quit();
    }

    void Quit()
    {
        try { tray.Visible = false; } catch { }
        try { File.Delete(urlFile); } catch { }
        lock (logLock) { try { log.WriteLine("[" + DateTime.Now + "] stopped"); log.Close(); } catch { } }
        ExitThread();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            try { if (!child.HasExited) child.Kill(); } catch { }
            tray.Dispose();
        }
        base.Dispose(disposing);
    }
}
