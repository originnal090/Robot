using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using UnityEngine;

namespace HciRobot.Simulator
{
    [DisallowMultipleComponent]
    public sealed class VirtualRobotTcpServer : MonoBehaviour
    {
        [Header("TCP")]
        [SerializeField] private string listenAddress = "0.0.0.0";
        [SerializeField] private int port = 5075;
        [SerializeField, Min(1024)] private int maximumBufferedBytes = 64 * 1024;
        [SerializeField, Min(1)] private int maximumCommandsPerFrame = 64;

        [Header("TonyPi compatibility")]
        [SerializeField, Range(0f, 1f)] private float deadzone = 0.20f;
        [SerializeField] private bool continuousMotion;
        [SerializeField, Min(0.05f)] private float watchdogSeconds = 0.60f;
        [SerializeField] private TonyPiMotionDriver motionDriver;

        [Header("Diagnostics")]
        [SerializeField] private bool logConnectionEvents = true;
        [SerializeField] private bool logCommandChanges = true;
        [SerializeField] private bool warnOnInvalidInput = true;
        [SerializeField] private string clientStatus = "not connected";
        [SerializeField] private string lastAppliedCommand = "none";
        [SerializeField] private int validCommandsReceived;
        [SerializeField] private int invalidLinesIgnored;

        private readonly ConcurrentQueue<RobotCommand> pendingCommands = new ConcurrentQueue<RobotCommand>();
        private readonly ConcurrentQueue<string> pendingInfoLogs = new ConcurrentQueue<string>();
        private readonly ConcurrentQueue<string> pendingWarningLogs = new ConcurrentQueue<string>();
        private readonly object clientGate = new object();
        private readonly object writeGate = new object();
        private TcpListener listener;
        private TcpClient client;
        private Thread listenerThread;
        private CancellationTokenSource cancellation;
        private long lastValidCommandTicks;
        private int watchdogStopQueued;
        private string pendingError;
        private string pendingClientStatus;
        private string mirroredStepId;
        private string lastLoggedCommandKey;
        private int receivedCommandCount;
        private int rejectedLineCount;
        private int resetCommandDiagnostics;
        private readonly Dictionary<string, string> actionHistory = new Dictionary<string, string>();
        private readonly Dictionary<string, string> actionNames = new Dictionary<string, string>();

        public bool HasClient
        {
            get
            {
                lock (clientGate)
                {
                    return client != null && client.Connected;
                }
            }
        }

        public float Deadzone
        {
            get => deadzone;
            set => deadzone = Mathf.Clamp01(value);
        }

        public bool ContinuousMotion
        {
            get => continuousMotion;
            set => continuousMotion = value;
        }

        public string ClientStatus => clientStatus;
        public string LastAppliedCommand => lastAppliedCommand;
        public int ValidCommandsReceived => validCommandsReceived;
        public int InvalidLinesIgnored => invalidLinesIgnored;

        private void Reset()
        {
            motionDriver = GetComponent<TonyPiMotionDriver>();
        }

        private void OnEnable()
        {
            StartServer();
        }

        private void Update()
        {
            string status = Interlocked.Exchange(ref pendingClientStatus, null);
            if (status != null)
            {
                clientStatus = status;
            }
            validCommandsReceived = Volatile.Read(ref receivedCommandCount);
            invalidLinesIgnored = Volatile.Read(ref rejectedLineCount);
            if (Interlocked.Exchange(ref resetCommandDiagnostics, 0) != 0)
            {
                lastLoggedCommandKey = null;
                lastAppliedCommand = "none";
            }

            while (pendingInfoLogs.TryDequeue(out string info))
            {
                Debug.Log(info, this);
            }
            while (pendingWarningLogs.TryDequeue(out string warning))
            {
                Debug.LogWarning(warning, this);
            }

            string error = Interlocked.Exchange(ref pendingError, null);
            if (!string.IsNullOrEmpty(error))
            {
                Debug.LogError(error, this);
            }

            if (watchdogSeconds > 0f && Interlocked.Read(ref lastValidCommandTicks) > 0)
            {
                double ageSeconds = (DateTime.UtcNow.Ticks - Interlocked.Read(ref lastValidCommandTicks)) /
                                    (double)TimeSpan.TicksPerSecond;
                if (ageSeconds > watchdogSeconds && Interlocked.Exchange(ref watchdogStopQueued, 1) == 0)
                {
                    QueueStop();
                    if (logConnectionEvents)
                    {
                        Debug.LogWarning(
                            $"[HCIRobot TCP] No valid command for {ageSeconds:F2}s; watchdog stopped motion.",
                            this);
                    }
                }
            }

            int remaining = Math.Max(1, maximumCommandsPerFrame);
            while (remaining-- > 0 && pendingCommands.TryDequeue(out RobotCommand command))
            {
                if (motionDriver != null)
                {
                    motionDriver.ApplyCommand(command);
                }
                RecordAppliedCommand(command);
                if (command.IsActionRequest)
                {
                    CompleteAction(command);
                }
            }
        }

        private void OnDisable()
        {
            StopServer();
            if (motionDriver != null)
            {
                motionDriver.StopMotion();
            }
        }

        private void OnApplicationQuit()
        {
            StopServer();
        }

        public void SendDistanceMillimetres(int millimetres)
        {
            SendLine(RobotTelemetryFormatter.DistanceLine(millimetres));
        }

        public void SendLine(byte[] line)
        {
            if (line == null || line.Length == 0)
            {
                return;
            }

            TcpClient target;
            lock (clientGate)
            {
                target = client;
            }

            if (target == null || !target.Connected)
            {
                return;
            }

            try
            {
                lock (writeGate)
                {
                    NetworkStream stream = target.GetStream();
                    stream.Write(line, 0, line.Length);
                    stream.Flush();
                }
            }
            catch (Exception exception) when (exception is IOException ||
                                               exception is ObjectDisposedException ||
                                               exception is SocketException)
            {
                if (CloseClientIfCurrent(target))
                {
                    QueueStop();
                }
            }
        }

        private void StartServer()
        {
            if (listenerThread != null)
            {
                return;
            }

            cancellation = new CancellationTokenSource();
            Interlocked.Exchange(ref pendingClientStatus, "starting");
            listenerThread = new Thread(ListenLoop)
            {
                IsBackground = true,
                Name = "VirtualRobotTcpServer"
            };
            listenerThread.Start(cancellation.Token);
        }

        private void StopServer()
        {
            CancellationTokenSource tokenSource = cancellation;
            cancellation = null;
            tokenSource?.Cancel();

            try
            {
                listener?.Stop();
            }
            catch (SocketException)
            {
            }
            listener = null;

            TcpClient active;
            lock (clientGate)
            {
                active = client;
                client = null;
            }
            active?.Close();

            Thread thread = listenerThread;
            listenerThread = null;
            if (thread != null && thread != Thread.CurrentThread && thread.IsAlive)
            {
                thread.Join(1500);
            }

            while (pendingCommands.TryDequeue(out _))
            {
            }
            Interlocked.Exchange(ref pendingClientStatus, "not connected");
            Interlocked.Exchange(ref lastValidCommandTicks, 0);
            Interlocked.Exchange(ref watchdogStopQueued, 0);
            tokenSource?.Dispose();
        }

        private void ListenLoop(object state)
        {
            CancellationToken token = (CancellationToken)state;
            try
            {
                IPAddress address = ResolveListenAddress(listenAddress);
                listener = new TcpListener(address, Math.Max(1, Math.Min(65535, port)));
                listener.Server.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
                listener.Start(1);
                if (logConnectionEvents)
                {
                    pendingInfoLogs.Enqueue($"[HCIRobot TCP] Listening on {address}:{port}.");
                }

                while (!token.IsCancellationRequested)
                {
                    TcpClient accepted;
                    try
                    {
                        accepted = listener.AcceptTcpClient();
                    }
                    catch (SocketException)
                    {
                        if (token.IsCancellationRequested)
                        {
                            break;
                        }
                        continue;
                    }
                    catch (ObjectDisposedException)
                    {
                        break;
                    }

                    ConfigureClient(accepted);
                    string endpoint = accepted.Client.RemoteEndPoint?.ToString() ?? "unknown endpoint";
                    TcpClient previous;
                    lock (clientGate)
                    {
                        previous = client;
                        client = accepted;
                        actionHistory.Clear();
                        actionNames.Clear();
                    }
                    previous?.Close();

                    Interlocked.Exchange(ref receivedCommandCount, 0);
                    Interlocked.Exchange(ref rejectedLineCount, 0);
                    Interlocked.Exchange(ref resetCommandDiagnostics, 1);
                    Interlocked.Exchange(ref pendingClientStatus, endpoint);
                    if (logConnectionEvents)
                    {
                        pendingInfoLogs.Enqueue($"[HCIRobot TCP] Client connected: {endpoint}.");
                    }

                    QueueStop();
                    Interlocked.Exchange(ref lastValidCommandTicks, 0);
                    Interlocked.Exchange(ref watchdogStopQueued, 0);
                    SendLine(RobotTelemetryFormatter.CapabilitiesLine());
                    var worker = new Thread(() => ReadClient(accepted, token))
                    {
                        IsBackground = true,
                        Name = "VirtualRobotTcpClient"
                    };
                    worker.Start();
                }
            }
            catch (Exception exception) when (exception is SocketException || exception is ArgumentException)
            {
                Interlocked.Exchange(
                    ref pendingError,
                    $"VirtualRobotTcpServer could not listen on the configured endpoint: {exception.Message}");
            }
        }

        private void ReadClient(TcpClient accepted, CancellationToken token)
        {
            var accumulator = new JsonLineAccumulator(maximumBufferedBytes);
            var readBuffer = new byte[4096];
            string endpoint = accepted.Client.RemoteEndPoint?.ToString() ?? "unknown endpoint";
            try
            {
                NetworkStream stream = accepted.GetStream();
                while (!token.IsCancellationRequested && accepted.Connected)
                {
                    int read = stream.Read(readBuffer, 0, readBuffer.Length);
                    if (read <= 0)
                    {
                        break;
                    }

                    accumulator.Append(readBuffer, read, line => HandleLine(accepted, line));
                }
            }
            catch (Exception exception) when (exception is IOException ||
                                               exception is ObjectDisposedException ||
                                               exception is SocketException)
            {
            }
            finally
            {
                if (CloseClientIfCurrent(accepted))
                {
                    QueueStop();
                    Interlocked.Exchange(ref pendingClientStatus, "not connected");
                    if (logConnectionEvents)
                    {
                        pendingInfoLogs.Enqueue($"[HCIRobot TCP] Client disconnected: {endpoint}; motion stopped.");
                    }
                }
                else
                {
                    accepted.Close();
                }
            }
        }

        private void QueueStop()
        {
            lock (clientGate)
            {
                mirroredStepId = null;
            }
            while (pendingCommands.TryDequeue(out _))
            {
            }
            pendingCommands.Enqueue(RobotCommand.Stop);
        }

        private void HandleLine(TcpClient source, string line)
        {
            lock (clientGate)
            {
                if (!ReferenceEquals(client, source))
                {
                    return;
                }
            }

            if (!RobotProtocolParser.TryParseLine(line, deadzone, continuousMotion, out RobotCommand command))
            {
                int rejected = Interlocked.Increment(ref rejectedLineCount);
                if (warnOnInvalidInput && rejected == 1)
                {
                    pendingWarningLogs.Enqueue(
                        "[HCIRobot TCP] Ignored invalid input. Further invalid lines are counted in "
                        + "Invalid Lines Ignored without Console spam.");
                }
                return;
            }
            Interlocked.Increment(ref receivedCommandCount);

            lock (clientGate)
            {
                // A previous reader can finish parsing after a new connection
                // has replaced it. Do not enqueue its late command.
                if (!ReferenceEquals(client, source)) return;
                bool refreshWatchdog = true;
                byte[] actionReply = null;
                bool enqueue = true;
                if (command.IsActionRequest)
                {
                    if (actionHistory.TryGetValue(command.ActionId, out string previousStatus))
                    {
                        actionReply = RobotTelemetryFormatter.ActionStatusLine(
                            command.ActionId, actionNames[command.ActionId], previousStatus);
                        enqueue = false;
                    }
                    else if (actionHistory.Count >= 1024)
                    {
                        actionReply = RobotTelemetryFormatter.ActionStatusLine(
                            command.ActionId, command.Action, "error", "session_action_limit");
                        enqueue = false;
                    }
                    else
                    {
                        actionHistory.Add(command.ActionId, "accepted");
                        actionNames.Add(command.ActionId, command.Action);
                        actionReply = RobotTelemetryFormatter.ActionStatusLine(
                            command.ActionId, command.Action, "accepted");
                    }
                }
                else if (command.IsMirroredStep)
                {
                    bool matches = mirroredStepId == command.MirroredStepId;
                    if (command.MirroredStepPhase == "heartbeat" && !matches) return;
                    if (command.MirroredStepPhase == "start")
                        mirroredStepId = command.MirroredStepId;
                    else
                    {
                        refreshWatchdog = matches;
                        if (matches && command.MirroredStepPhase != "heartbeat") mirroredStepId = null;
                    }
                }
                else if (!command.IsMirroredAction) mirroredStepId = null;
                if (enqueue) pendingCommands.Enqueue(command);
                if (refreshWatchdog)
                {
                    Interlocked.Exchange(ref lastValidCommandTicks, DateTime.UtcNow.Ticks);
                    Interlocked.Exchange(ref watchdogStopQueued, 0);
                }
                if (actionReply != null) SendLine(actionReply);
            }
        }

        private void CompleteAction(RobotCommand command)
        {
            byte[] reply = null;
            lock (clientGate)
            {
                if (client != null && actionHistory.TryGetValue(command.ActionId, out string status)
                    && status == "accepted")
                {
                    actionHistory[command.ActionId] = "done";
                    reply = RobotTelemetryFormatter.ActionStatusLine(
                        command.ActionId, command.Action, "done");
                }
            }
            if (reply != null)
            {
                SendLine(reply);
                if (logCommandChanges)
                {
                    Debug.Log($"[HCIRobot TCP] Action done: {command.Action} id={command.ActionId}.", this);
                }
            }
        }

        private void RecordAppliedCommand(RobotCommand command)
        {
            string summary = RobotCommandDiagnostics.Describe(command);
            lastAppliedCommand = summary;
            if (!logCommandChanges ||
                (command.IsMirroredStep && command.MirroredStepPhase == "heartbeat"))
            {
                return;
            }

            string key = RobotCommandDiagnostics.ChangeKey(command);
            if (key == lastLoggedCommandKey)
            {
                return;
            }
            lastLoggedCommandKey = key;
            Debug.Log($"[HCIRobot TCP] Applied {summary}.", this);
        }

        private bool CloseClientIfCurrent(TcpClient target)
        {
            bool wasCurrent = false;
            lock (clientGate)
            {
                if (ReferenceEquals(client, target))
                {
                    client = null;
                    wasCurrent = true;
                }
            }

            try
            {
                target?.Close();
            }
            catch (SocketException)
            {
            }
            return wasCurrent;
        }

        private static void ConfigureClient(TcpClient target)
        {
            target.NoDelay = true;
            target.ReceiveTimeout = 0;
            target.SendTimeout = 1000;
        }

        private static IPAddress ResolveListenAddress(string value)
        {
            if (string.IsNullOrWhiteSpace(value) || value == "0.0.0.0" || value == "*")
            {
                return IPAddress.Any;
            }

            if (value == "::")
            {
                return IPAddress.IPv6Any;
            }

            return IPAddress.Parse(value);
        }
    }

    /// <summary>
    /// Compact, stable command descriptions for diagnostics. ChangeKey excludes
    /// continuously varying magnitudes so joystick/autonomy traffic does not flood
    /// the Console while the effective direction remains unchanged.
    /// </summary>
    public static class RobotCommandDiagnostics
    {
        public static string ChangeKey(RobotCommand command)
        {
            if (command.IsMirroredStep)
            {
                return $"mirror-step:{command.MirroredStepId}:{command.MirroredStepPhase}";
            }
            if (command.IsAction)
            {
                return $"action:{command.ActionId}:{command.Action}:{command.ActionStatus}";
            }
            return "motion:" + MotionLabel(command);
        }

        public static string Describe(RobotCommand command)
        {
            if (command.IsMirroredStep)
            {
                return $"mirror step {command.MirroredStepPhase} id={command.MirroredStepId}";
            }
            if (command.IsAction)
            {
                string receipt = string.IsNullOrEmpty(command.ActionStatus)
                    ? string.Empty
                    : $" status={command.ActionStatus}";
                string ident = string.IsNullOrEmpty(command.ActionId)
                    ? string.Empty
                    : $" id={command.ActionId}";
                return $"action={command.Action}{ident}{receipt}";
            }
            return $"motion={MotionLabel(command)} v={command.Velocity:+0.00;-0.00;0.00} "
                + $"steer={command.Steer:+0.00;-0.00;0.00} "
                + $"lateral={command.Lateral:+0.00;-0.00;0.00}";
        }

        private static string MotionLabel(RobotCommand command)
        {
            const float epsilon = 0.0001f;
            if (Mathf.Abs(command.Lateral) > epsilon)
            {
                return command.Lateral < 0f ? "lateral-left" : "lateral-right";
            }

            string longitudinal = command.Velocity > epsilon ? "forward"
                : command.Velocity < -epsilon ? "backward"
                : string.Empty;
            string turn = command.Steer > epsilon ? "right"
                : command.Steer < -epsilon ? "left"
                : string.Empty;
            if (!string.IsNullOrEmpty(longitudinal) && !string.IsNullOrEmpty(turn))
            {
                return longitudinal + "+" + turn;
            }
            if (!string.IsNullOrEmpty(longitudinal))
            {
                return longitudinal;
            }
            if (!string.IsNullOrEmpty(turn))
            {
                return "turn-" + turn;
            }
            return "stand";
        }
    }
}
