using System;
using System.Collections.Concurrent;
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

        private readonly ConcurrentQueue<RobotCommand> pendingCommands = new ConcurrentQueue<RobotCommand>();
        private readonly object clientGate = new object();
        private readonly object writeGate = new object();
        private TcpListener listener;
        private TcpClient client;
        private Thread listenerThread;
        private CancellationTokenSource cancellation;
        private long lastValidCommandTicks;
        private int watchdogStopQueued;
        private string pendingError;

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
                }
            }

            int remaining = Math.Max(1, maximumCommandsPerFrame);
            while (remaining-- > 0 && pendingCommands.TryDequeue(out RobotCommand command))
            {
                if (motionDriver != null)
                {
                    motionDriver.ApplyCommand(command);
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
                    TcpClient previous;
                    lock (clientGate)
                    {
                        previous = client;
                        client = accepted;
                    }
                    previous?.Close();

                    QueueStop();
                    Interlocked.Exchange(ref lastValidCommandTicks, 0);
                    Interlocked.Exchange(ref watchdogStopQueued, 0);
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
                }
                else
                {
                    accepted.Close();
                }
            }
        }

        private void QueueStop()
        {
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
                return;
            }

            pendingCommands.Enqueue(command);
            Interlocked.Exchange(ref lastValidCommandTicks, DateTime.UtcNow.Ticks);
            Interlocked.Exchange(ref watchdogStopQueued, 0);
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
}
