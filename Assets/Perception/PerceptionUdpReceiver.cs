using System;
using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;
using UnityEngine.Events;

public sealed class PerceptionUdpReceiver : MonoBehaviour
{
    [Header("Orange Pi 感知遥测（仅接收）")]
    [SerializeField] private int listenPort = 6101;
    [SerializeField, Min(0.1f)] private float watchdogSeconds = 0.5f;
    [SerializeField] private bool logPackets;

    [Header("事件")]
    public PerceptionTelemetryEvent onTelemetry = new PerceptionTelemetryEvent();
    public UnityEvent<bool> onConnectionChanged = new UnityEvent<bool>();
    public UnityEvent<bool> onDetectionChanged = new UnityEvent<bool>();

    public bool IsConnected { get; private set; }
    public bool IsDetected { get; private set; }
    public PerceptionTelemetryMessage LatestMessage { get; private set; }
    public float LastPacketAgeSeconds =>
        IsConnected ? Mathf.Max(0f, Time.realtimeSinceStartup - _lastPacketTime) : float.PositiveInfinity;

    private readonly ConcurrentQueue<string> _incoming = new ConcurrentQueue<string>();
    private UdpClient _client;
    private Thread _receiverThread;
    private volatile bool _running;
    private string _threadError;
    private string _activeSessionId;
    private long _lastSequence;
    private float _lastPacketTime;

    private void OnEnable()
    {
        StartReceiver();
    }

    private void Update()
    {
        if (!string.IsNullOrEmpty(_threadError))
        {
            string error = _threadError;
            _threadError = null;
            Debug.LogWarning("[Perception UDP] " + error);
        }

        string latestJson = null;
        while (_incoming.TryDequeue(out string json))
            latestJson = json;

        if (!string.IsNullOrEmpty(latestJson))
            ProcessPacket(latestJson);

        if (IsConnected && Time.realtimeSinceStartup - _lastPacketTime > watchdogSeconds)
        {
            SetConnected(false);
            SetDetected(false);
        }
    }

    private void StartReceiver()
    {
        if (_running)
            return;
        if (listenPort < 1 || listenPort > 65535)
        {
            Debug.LogError("[Perception UDP] Invalid listen port: " + listenPort);
            return;
        }

        _running = true;
        _receiverThread = new Thread(ReceiveLoop)
        {
            IsBackground = true,
            Name = "Perception-UDP-Receiver"
        };
        _receiverThread.Start();
    }

    private void ReceiveLoop()
    {
        try
        {
            _client = new UdpClient(new IPEndPoint(IPAddress.Any, listenPort));
            _client.Client.ReceiveTimeout = 500;
            IPEndPoint remote = new IPEndPoint(IPAddress.Any, 0);

            while (_running)
            {
                try
                {
                    byte[] data = _client.Receive(ref remote);
                    if (data.Length == 0 || data.Length > 4096)
                        continue;
                    _incoming.Enqueue(Encoding.UTF8.GetString(data));
                    while (_incoming.Count > 32 && _incoming.TryDequeue(out _)) { }
                }
                catch (SocketException exception)
                    when (exception.SocketErrorCode == SocketError.TimedOut)
                {
                }
                catch (ObjectDisposedException)
                {
                    break;
                }
            }
        }
        catch (Exception exception)
        {
            if (_running)
                _threadError = exception.GetType().Name + ": " + exception.Message;
        }
    }

    private void ProcessPacket(string json)
    {
        try
        {
            PerceptionTelemetryMessage message =
                JsonUtility.FromJson<PerceptionTelemetryMessage>(json);
            if (message == null || !message.IsSupported())
                return;

            if (!string.Equals(_activeSessionId, message.session_id, StringComparison.Ordinal))
            {
                _activeSessionId = message.session_id;
                _lastSequence = 0;
            }
            if (message.seq <= _lastSequence)
                return;

            _lastSequence = message.seq;
            _lastPacketTime = Time.realtimeSinceStartup;
            LatestMessage = message;
            SetConnected(true);
            SetDetected(message.video_ok && message.detected);
            onTelemetry?.Invoke(message);

            if (logPackets)
            {
                Debug.Log(
                    $"[Perception UDP] seq={message.seq} state={message.state} " +
                    $"detected={message.detected} center=({message.center_x:F1},{message.center_y:F1})");
            }
        }
        catch (Exception exception)
        {
            Debug.LogWarning("[Perception UDP] Invalid packet: " + exception.Message);
        }
    }

    private void SetConnected(bool value)
    {
        if (IsConnected == value)
            return;
        IsConnected = value;
        onConnectionChanged?.Invoke(value);
    }

    private void SetDetected(bool value)
    {
        if (IsDetected == value)
            return;
        IsDetected = value;
        onDetectionChanged?.Invoke(value);
    }

    private void OnDisable()
    {
        StopReceiver();
    }

    private void OnDestroy()
    {
        StopReceiver();
    }

    private void StopReceiver()
    {
        _running = false;
        try { _client?.Close(); } catch { }
        if (_receiverThread != null && _receiverThread.IsAlive)
            _receiverThread.Join(1000);
        _receiverThread = null;
        _client = null;
        while (_incoming.TryDequeue(out _)) { }
        SetConnected(false);
        SetDetected(false);
    }
}
