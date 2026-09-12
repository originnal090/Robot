using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;

namespace HciRobot.Simulator
{
    [Serializable]
    public sealed class AutonomyStatusMessage
    {
        public string type;
        public int schema_version;
        public string source;
        public string session_id;
        public long seq;
        public long sent_at_ms;
        public string mode;
        public bool armed;
        public string control_state;
        public string video_status;
        public int frame_count;
        public AutonomyTargetStatus target;
        public AutonomyOutputStatus output;
        public AutonomyObstacleStatus obstacle;
        public bool estop;
        public string fault;
        public string termination;
        public AutonomyEventStatus @event;

        public bool IsValid(int expectedSchemaVersion)
        {
            return string.Equals(type, AutonomyStatusUdpReceiver.DefaultType, StringComparison.Ordinal) &&
                   schema_version == expectedSchemaVersion &&
                   !string.IsNullOrWhiteSpace(session_id) &&
                   seq >= 1 &&
                   sent_at_ms >= 0 &&
                   !string.IsNullOrWhiteSpace(control_state) &&
                   output != null &&
                   IsFinite(output.v) &&
                   IsFinite(output.steer);
        }

        private static bool IsFinite(float value)
        {
            return !float.IsNaN(value) && !float.IsInfinity(value);
        }
    }

    [Serializable]
    public sealed class AutonomyTargetStatus
    {
        public bool candidate;
        public bool confirmed;
        public bool fresh;
        public int age_ms;
        public float center_x;
        public float center_y;
        public float radius;
        public int frame_width;
        public int frame_height;
    }

    [Serializable]
    public sealed class AutonomyOutputStatus
    {
        public float v;
        public float steer;
        public bool grab;
        public string source;
    }

    [Serializable]
    public sealed class AutonomyObstacleStatus
    {
        public string state;
        public float distance_mm;
        public int avoid_count;
    }

    [Serializable]
    public sealed class AutonomyEventStatus
    {
        public string kind;
        public string message;
    }

    public sealed class SessionSequenceFilter
    {
        private readonly int maximumRetiredSessions;
        private readonly HashSet<string> retiredSessions = new HashSet<string>(StringComparer.Ordinal);
        private readonly Queue<string> retiredOrder = new Queue<string>();

        public SessionSequenceFilter(int maximumRetiredSessions = 32)
        {
            this.maximumRetiredSessions = Math.Max(1, maximumRetiredSessions);
        }

        public string Session { get; private set; }
        public long Sequence { get; private set; } = -1;

        public bool TryAccept(string session, long sequence)
        {
            if (string.IsNullOrWhiteSpace(session) || sequence < 1)
            {
                return false;
            }

            if (string.Equals(Session, session, StringComparison.Ordinal))
            {
                if (sequence <= Sequence)
                {
                    return false;
                }

                Sequence = sequence;
                return true;
            }

            if (retiredSessions.Contains(session))
            {
                return false;
            }

            RetireCurrentSession();
            Session = session;
            Sequence = sequence;
            return true;
        }

        public void Reset()
        {
            Session = null;
            Sequence = -1;
            retiredSessions.Clear();
            retiredOrder.Clear();
        }

        private void RetireCurrentSession()
        {
            if (string.IsNullOrEmpty(Session) || !retiredSessions.Add(Session))
            {
                return;
            }

            retiredOrder.Enqueue(Session);
            while (retiredOrder.Count > maximumRetiredSessions)
            {
                retiredSessions.Remove(retiredOrder.Dequeue());
            }
        }
    }

    public sealed class AutonomyStatusEnvelope
    {
        public AutonomyStatusEnvelope(AutonomyStatusMessage message, long receivedUtcTicks)
        {
            Message = message;
            ReceivedUtcTicks = receivedUtcTicks;
        }

        public AutonomyStatusMessage Message { get; }
        public long ReceivedUtcTicks { get; }
    }

    [DisallowMultipleComponent]
    public sealed class AutonomyStatusUdpReceiver : MonoBehaviour
    {
        public const string DefaultType = "autonomy_status";
        public const int DefaultSchemaVersion = 1;

        [SerializeField] private string listenAddress = "0.0.0.0";
        [SerializeField] private int port = 6102;
        [SerializeField] private int expectedSchemaVersion = DefaultSchemaVersion;
        [SerializeField, Min(0.05f)] private float watchdogSeconds = 1.0f;
        [SerializeField, Min(256)] private int maximumDatagramBytes = 4096;

        private readonly SessionSequenceFilter filter = new SessionSequenceFilter();
        private UdpClient udpClient;
        private Thread receiveThread;
        private CancellationTokenSource cancellation;
        private AutonomyStatusEnvelope pendingLatest;
        private long lastReceiveTicks;
        private bool watchdogRaised;
        private string pendingError;

        public event Action<AutonomyStatusMessage> StatusUpdated;
        public event Action StatusTimedOut;

        public AutonomyStatusMessage Latest { get; private set; }
        public bool IsFresh { get; private set; }

        private void OnEnable()
        {
            StartReceiver();
        }

        private void Update()
        {
            string error = Interlocked.Exchange(ref pendingError, null);
            if (!string.IsNullOrEmpty(error))
            {
                Debug.LogError(error, this);
            }

            AutonomyStatusEnvelope envelope = Interlocked.Exchange(ref pendingLatest, null);
            if (envelope != null)
            {
                Latest = envelope.Message;
                Interlocked.Exchange(ref lastReceiveTicks, envelope.ReceivedUtcTicks);
                IsFresh = true;
                watchdogRaised = false;
                StatusUpdated?.Invoke(Latest);
            }

            long ticks = Interlocked.Read(ref lastReceiveTicks);
            if (ticks <= 0 || watchdogSeconds <= 0f)
            {
                return;
            }

            double age = (DateTime.UtcNow.Ticks - ticks) / (double)TimeSpan.TicksPerSecond;
            if (age > watchdogSeconds && !watchdogRaised)
            {
                watchdogRaised = true;
                IsFresh = false;
                StatusTimedOut?.Invoke();
            }
        }

        private void OnDisable()
        {
            StopReceiver();
        }

        private void OnApplicationQuit()
        {
            StopReceiver();
        }

        private void StartReceiver()
        {
            if (receiveThread != null)
            {
                return;
            }

            filter.Reset();
            Latest = null;
            IsFresh = false;
            watchdogRaised = false;
            Interlocked.Exchange(ref lastReceiveTicks, 0);
            cancellation = new CancellationTokenSource();
            receiveThread = new Thread(ReceiveLoop)
            {
                IsBackground = true,
                Name = "AutonomyStatusUdpReceiver"
            };
            receiveThread.Start(cancellation.Token);
        }

        private void StopReceiver()
        {
            CancellationTokenSource tokenSource = cancellation;
            cancellation = null;
            tokenSource?.Cancel();
            udpClient?.Close();
            udpClient = null;

            Thread thread = receiveThread;
            receiveThread = null;
            if (thread != null && thread != Thread.CurrentThread && thread.IsAlive)
            {
                thread.Join(1500);
            }

            Interlocked.Exchange(ref pendingLatest, null);
            tokenSource?.Dispose();
        }

        private void ReceiveLoop(object state)
        {
            CancellationToken token = (CancellationToken)state;
            try
            {
                var endpoint = new IPEndPoint(ResolveListenAddress(listenAddress), Math.Max(1, Math.Min(65535, port)));
                udpClient = new UdpClient(endpoint);
                udpClient.Client.ReceiveTimeout = 500;
                var remote = new IPEndPoint(IPAddress.Any, 0);

                while (!token.IsCancellationRequested)
                {
                    byte[] datagram;
                    try
                    {
                        datagram = udpClient.Receive(ref remote);
                    }
                    catch (SocketException exception) when (exception.SocketErrorCode == SocketError.TimedOut)
                    {
                        continue;
                    }
                    catch (ObjectDisposedException)
                    {
                        break;
                    }
                    catch (SocketException)
                    {
                        if (token.IsCancellationRequested)
                        {
                            break;
                        }
                        continue;
                    }

                    if (datagram.Length == 0 || datagram.Length > maximumDatagramBytes)
                    {
                        continue;
                    }

                    AutonomyStatusMessage message;
                    try
                    {
                        message = JsonUtility.FromJson<AutonomyStatusMessage>(Encoding.UTF8.GetString(datagram));
                    }
                    catch (ArgumentException)
                    {
                        continue;
                    }

                    if (message == null || !message.IsValid(expectedSchemaVersion) || !filter.TryAccept(message.session_id, message.seq))
                    {
                        continue;
                    }

                    Interlocked.Exchange(ref pendingLatest, new AutonomyStatusEnvelope(message, DateTime.UtcNow.Ticks));
                }
            }
            catch (Exception exception) when (exception is SocketException || exception is ArgumentException)
            {
                Interlocked.Exchange(ref pendingError, $"AutonomyStatusUdpReceiver could not listen on the configured endpoint: {exception.Message}");
            }
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
