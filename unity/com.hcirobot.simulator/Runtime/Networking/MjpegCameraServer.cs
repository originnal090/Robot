using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;

namespace HciRobot.Simulator
{
    [DisallowMultipleComponent]
    [RequireComponent(typeof(Camera))]
    public sealed class MjpegCameraServer : MonoBehaviour
    {
        [SerializeField] private Camera sourceCamera;
        [SerializeField] private string listenAddress = "0.0.0.0";
        [SerializeField] private int port = 8080;
        [SerializeField, Min(16)] private int width = 640;
        [SerializeField, Min(16)] private int height = 480;
        [SerializeField, Range(1, 30)] private int framesPerSecond = 10;
        [SerializeField, Range(1, 100)] private int jpegQuality = 75;

        private readonly object frameGate = new object();
        private RenderTexture renderTexture;
        private Texture2D readbackTexture;
        private byte[] latestJpeg;
        private long latestFrameSequence;
        private float nextCaptureTime;
        private TcpListener listener;
        private Thread listenerThread;
        private CancellationTokenSource cancellation;
        private string pendingError;

        private const string Boundary = "hcirobot-frame";

        private void Reset()
        {
            sourceCamera = GetComponent<Camera>();
        }

        private void Awake()
        {
            if (sourceCamera == null)
            {
                sourceCamera = GetComponent<Camera>();
            }
        }

        private void OnEnable()
        {
            CreateCaptureResources();
            nextCaptureTime = Time.unscaledTime;
            StartServer();
        }

        private void Update()
        {
            string error = Interlocked.Exchange(ref pendingError, null);
            if (!string.IsNullOrEmpty(error))
            {
                Debug.LogError(error, this);
            }

            if (Time.unscaledTime < nextCaptureTime)
            {
                return;
            }

            nextCaptureTime = Time.unscaledTime + 1f / Mathf.Max(1, framesPerSecond);
            CaptureFrameOnMainThread();
        }

        private void OnDisable()
        {
            StopServer();
            DestroyCaptureResources();
        }

        private void OnApplicationQuit()
        {
            StopServer();
        }

        public void CaptureFrameOnMainThread()
        {
            if (sourceCamera == null || renderTexture == null || readbackTexture == null)
            {
                return;
            }

            RenderTexture previousActive = RenderTexture.active;
            RenderTexture previousTarget = sourceCamera.targetTexture;
            try
            {
                sourceCamera.targetTexture = renderTexture;
                sourceCamera.Render();
                RenderTexture.active = renderTexture;
                readbackTexture.ReadPixels(new Rect(0, 0, width, height), 0, 0, false);
                readbackTexture.Apply(false, false);
                byte[] encoded = readbackTexture.EncodeToJPG(Mathf.Clamp(jpegQuality, 1, 100));
                lock (frameGate)
                {
                    latestJpeg = encoded;
                    latestFrameSequence++;
                    Monitor.PulseAll(frameGate);
                }
            }
            finally
            {
                sourceCamera.targetTexture = previousTarget;
                RenderTexture.active = previousActive;
            }
        }

        private void CreateCaptureResources()
        {
            width = Mathf.Max(16, width);
            height = Mathf.Max(16, height);
            renderTexture = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32)
            {
                name = "HCIRobot MJPEG RenderTexture"
            };
            renderTexture.Create();
            readbackTexture = new Texture2D(width, height, TextureFormat.RGB24, false, false)
            {
                name = "HCIRobot MJPEG Readback"
            };
        }

        private void DestroyCaptureResources()
        {
            if (sourceCamera != null && sourceCamera.targetTexture == renderTexture)
            {
                sourceCamera.targetTexture = null;
            }
            if (renderTexture != null)
            {
                renderTexture.Release();
                Destroy(renderTexture);
                renderTexture = null;
            }
            if (readbackTexture != null)
            {
                Destroy(readbackTexture);
                readbackTexture = null;
            }
            lock (frameGate)
            {
                latestJpeg = null;
                latestFrameSequence = 0;
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
                Name = "MjpegCameraServer"
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
            lock (frameGate)
            {
                Monitor.PulseAll(frameGate);
            }

            Thread thread = listenerThread;
            listenerThread = null;
            if (thread != null && thread != Thread.CurrentThread && thread.IsAlive)
            {
                thread.Join(1500);
            }
            tokenSource?.Dispose();
        }

        private void ListenLoop(object state)
        {
            CancellationToken token = (CancellationToken)state;
            try
            {
                listener = new TcpListener(ResolveListenAddress(listenAddress), Math.Max(1, Math.Min(65535, port)));
                listener.Server.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
                listener.Start(8);
                while (!token.IsCancellationRequested)
                {
                    TcpClient client;
                    try
                    {
                        client = listener.AcceptTcpClient();
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

                    var worker = new Thread(() => ServeClient(client, token))
                    {
                        IsBackground = true,
                        Name = "MjpegCameraClient"
                    };
                    worker.Start();
                }
            }
            catch (Exception exception) when (exception is SocketException || exception is ArgumentException)
            {
                Interlocked.Exchange(
                    ref pendingError,
                    $"MjpegCameraServer could not listen on the configured endpoint: {exception.Message}");
            }
        }

        private void ServeClient(TcpClient client, CancellationToken token)
        {
            using (client)
            {
                client.NoDelay = true;
                client.ReceiveTimeout = 2000;
                client.SendTimeout = 2000;
                NetworkStream stream;
                try
                {
                    stream = client.GetStream();
                    string request = ReadHttpRequest(stream);
                    if (!IsStreamRequest(request))
                    {
                        WriteSimpleResponse(stream, "404 Not Found", "Use /?action=stream\n");
                        return;
                    }

                    byte[] response = Encoding.ASCII.GetBytes(
                        "HTTP/1.1 200 OK\r\n" +
                        "Cache-Control: no-cache, private\r\n" +
                        "Pragma: no-cache\r\n" +
                        "Connection: close\r\n" +
                        "Content-Type: multipart/x-mixed-replace; boundary=" + Boundary + "\r\n\r\n");
                    stream.Write(response, 0, response.Length);

                    long sentSequence = -1;
                    while (!token.IsCancellationRequested && client.Connected)
                    {
                        byte[] frame = WaitForFrame(ref sentSequence, token);
                        if (frame == null)
                        {
                            continue;
                        }

                        byte[] header = Encoding.ASCII.GetBytes(
                            "--" + Boundary + "\r\n" +
                            "Content-Type: image/jpeg\r\n" +
                            "Content-Length: " + frame.Length + "\r\n\r\n");
                        stream.Write(header, 0, header.Length);
                        stream.Write(frame, 0, frame.Length);
                        stream.WriteByte((byte)'\r');
                        stream.WriteByte((byte)'\n');
                        stream.Flush();
                    }
                }
                catch (Exception exception) when (exception is IOException || exception is ObjectDisposedException || exception is SocketException)
                {
                }
            }
        }

        private byte[] WaitForFrame(ref long sentSequence, CancellationToken token)
        {
            lock (frameGate)
            {
                while (!token.IsCancellationRequested && (latestJpeg == null || latestFrameSequence == sentSequence))
                {
                    Monitor.Wait(frameGate, 500);
                }

                if (token.IsCancellationRequested || latestJpeg == null)
                {
                    return null;
                }

                sentSequence = latestFrameSequence;
                return latestJpeg;
            }
        }

        private static string ReadHttpRequest(NetworkStream stream)
        {
            var bytes = new List<byte>(1024);
            var one = new byte[1];
            while (bytes.Count < 8192)
            {
                int read = stream.Read(one, 0, 1);
                if (read <= 0)
                {
                    break;
                }
                bytes.Add(one[0]);
                int count = bytes.Count;
                if (count >= 4 && bytes[count - 4] == '\r' && bytes[count - 3] == '\n' && bytes[count - 2] == '\r' && bytes[count - 1] == '\n')
                {
                    break;
                }
            }
            return Encoding.ASCII.GetString(bytes.ToArray());
        }

        private static bool IsStreamRequest(string request)
        {
            if (string.IsNullOrEmpty(request))
            {
                return false;
            }

            int firstLineEnd = request.IndexOf("\r\n", StringComparison.Ordinal);
            string firstLine = firstLineEnd >= 0 ? request.Substring(0, firstLineEnd) : request;
            return firstLine.StartsWith("GET /?action=stream ", StringComparison.Ordinal) ||
                   firstLine.StartsWith("GET /?action=stream&", StringComparison.Ordinal) ||
                   firstLine.StartsWith("GET / ", StringComparison.Ordinal);
        }

        private static void WriteSimpleResponse(NetworkStream stream, string status, string body)
        {
            byte[] content = Encoding.UTF8.GetBytes(body);
            byte[] header = Encoding.ASCII.GetBytes(
                "HTTP/1.1 " + status + "\r\n" +
                "Content-Type: text/plain; charset=utf-8\r\n" +
                "Content-Length: " + content.Length + "\r\n" +
                "Connection: close\r\n\r\n");
            stream.Write(header, 0, header.Length);
            stream.Write(content, 0, content.Length);
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
